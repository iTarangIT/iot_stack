-- =============================================================================
-- iTarang · Intellicar telemetry schema — RDS PostgreSQL 16 (NO TimescaleDB)
-- Target: AWS RDS for PostgreSQL 16.x, instance itarang-iot-db
-- Partitioning: native declarative range partitioning + pg_partman
-- Sized for: 2,000–5,000 vehicles @ 30s polling
-- =============================================================================
--
-- PREREQUISITE (one-time, before running this file):
--   1. In the RDS custom DB parameter group, set:
--        shared_preload_libraries = pg_partman_bgw, pg_stat_statements
--      (then REBOOT the instance — this needs a custom parameter group; the
--       default.postgres16 group is not editable. Create one, attach, reboot.)
--   2. pg_partman_bgw background worker settings (same param group):
--        pg_partman_bgw.interval  = 3600
--        pg_partman_bgw.role      = itarang_admin
--        pg_partman_bgw.dbname    = itarang
--
-- Run this file as the master user (itarang_admin) against database "itarang".
-- =============================================================================

-- 1. EXTENSIONS -------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_partman;            -- partition automation
CREATE SCHEMA IF NOT EXISTS partman;                  -- partman keeps its objects here
-- (pg_stat_statements is loaded via shared_preload_libraries; no CREATE needed for it to work,
--  but create it if you want the view:)
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- =========================================================================
-- 2. STATIC / SLOW-CHANGING TABLES  (unchanged from original)
-- =========================================================================
CREATE TABLE IF NOT EXISTS vehicles (
    vehicleno      TEXT PRIMARY KEY,
    makemodel      TEXT,
    deviceid       TEXT,
    owner          TEXT,
    geofence_id    TEXT,
    capacity_kwh   NUMERIC(6,2),
    info           JSONB,
    info_updated   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_vehicles_owner ON vehicles(owner);

CREATE TABLE IF NOT EXISTS users (
    username       TEXT PRIMARY KEY,
    role           TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS vehicle_state (
    vehicleno         TEXT PRIMARY KEY REFERENCES vehicles(vehicleno) ON DELETE CASCADE,
    last_seen         TIMESTAMPTZ,
    last_gps_at       TIMESTAMPTZ,
    last_battery_at   TIMESTAMPTZ,
    lat               DOUBLE PRECISION,
    lon               DOUBLE PRECISION,
    speed_kph         REAL,
    heading           REAL,
    ignition          BOOLEAN,
    gps_fix           BOOLEAN,
    soc_pct           REAL,
    soh_pct           REAL,
    pack_voltage      REAL,
    pack_current      REAL,
    pack_temp_c       REAL,
    charging          BOOLEAN,
    fuel_pct          REAL,
    range_km          REAL,
    last_fuel_at      TIMESTAMPTZ,
    online            BOOLEAN,
    open_alert_count  INT NOT NULL DEFAULT 0,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_state_online ON vehicle_state(online);
CREATE INDEX IF NOT EXISTS idx_state_soc    ON vehicle_state(soc_pct);

-- =========================================================================
-- 3. TIME-SERIES TABLES  (native PARTITION BY RANGE on "time")
-- =========================================================================
-- NOTE: a partitioned table's PRIMARY KEY / UNIQUE constraints must include
-- the partition key (time). Where the original had no PK we omit it; where it
-- had one (distance_rollup) we keep time in it (already present).

-- ---- telemetry_gps : DAILY partitions -----------------------------------
CREATE TABLE IF NOT EXISTS telemetry_gps (
    time         TIMESTAMPTZ NOT NULL,
    vehicleno    TEXT NOT NULL,
    lat          DOUBLE PRECISION,
    lon          DOUBLE PRECISION,
    speed_kph    REAL,
    heading      REAL,
    ignition     BOOLEAN,
    gps_fix      BOOLEAN,
    ext_voltage  REAL
) PARTITION BY RANGE (time);
CREATE INDEX IF NOT EXISTS idx_gps_vehicle_time ON telemetry_gps(vehicleno, time DESC);

-- ---- telemetry_battery : DAILY ------------------------------------------
CREATE TABLE IF NOT EXISTS telemetry_battery (
    time          TIMESTAMPTZ NOT NULL,
    vehicleno     TEXT NOT NULL,
    soc_pct       REAL,
    soh_pct       REAL,
    pack_voltage  REAL,
    pack_current  REAL,
    pack_temp_c   REAL,
    cell_min_mv   REAL,
    cell_max_mv   REAL,
    charging      BOOLEAN
) PARTITION BY RANGE (time);
CREATE INDEX IF NOT EXISTS idx_batt_vehicle_time ON telemetry_battery(vehicleno, time DESC);

-- ---- telemetry_fuel : DAILY ---------------------------------------------
CREATE TABLE IF NOT EXISTS telemetry_fuel (
    time          TIMESTAMPTZ NOT NULL,
    vehicleno     TEXT NOT NULL,
    fuel_pct      REAL,
    range_km      REAL,
    fuel_volts    REAL,
    consumption   REAL
) PARTITION BY RANGE (time);
CREATE INDEX IF NOT EXISTS idx_fuel_vehicle_time ON telemetry_fuel(vehicleno, time DESC);

-- ---- telemetry_can : DAILY ----------------------------------------------
CREATE TABLE IF NOT EXISTS telemetry_can (
    time         TIMESTAMPTZ NOT NULL,
    vehicleno    TEXT NOT NULL,
    payload      JSONB NOT NULL
) PARTITION BY RANGE (time);
CREATE INDEX IF NOT EXISTS idx_can_vehicle_time ON telemetry_can(vehicleno, time DESC);

-- ---- distance_rollup : WEEKLY (had composite PK incl. time) -------------
CREATE TABLE IF NOT EXISTS distance_rollup (
    time           TIMESTAMPTZ NOT NULL,
    vehicleno      TEXT NOT NULL,
    bucket_size    TEXT NOT NULL,
    distance_km    REAL,
    energy_kwh     REAL,
    moving_seconds INT,
    PRIMARY KEY (time, vehicleno, bucket_size)
) PARTITION BY RANGE (time);
CREATE INDEX IF NOT EXISTS idx_distrollup_v ON distance_rollup(vehicleno, time DESC);

-- ---- alerts : WEEKLY ----------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    time         TIMESTAMPTZ NOT NULL,
    vehicleno    TEXT NOT NULL,
    alert_type   TEXT NOT NULL,
    severity     TEXT NOT NULL,
    message      TEXT,
    value        REAL,
    threshold    REAL,
    resolved_at  TIMESTAMPTZ
) PARTITION BY RANGE (time);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(vehicleno, time DESC) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(alert_type, time DESC);

-- ---- trips : WEEKLY -----------------------------------------------------
CREATE TABLE IF NOT EXISTS trips (
    time          TIMESTAMPTZ NOT NULL,
    vehicleno     TEXT NOT NULL,
    trip_id       TEXT NOT NULL,
    end_time      TIMESTAMPTZ,
    start_lat     DOUBLE PRECISION,
    start_lon     DOUBLE PRECISION,
    end_lat       DOUBLE PRECISION,
    end_lon       DOUBLE PRECISION,
    distance_km   REAL,
    duration_s    INT,
    energy_kwh    REAL,
    avg_speed_kph REAL
) PARTITION BY RANGE (time);
CREATE INDEX IF NOT EXISTS idx_trips_vehicle ON trips(vehicleno, time DESC);

-- =========================================================================
-- 4. pg_partman CONFIG  (replaces create_hypertable + retention policies)
-- =========================================================================
-- p_interval sets partition width. p_premake = how many future partitions to
-- keep ready. p_default_table=false avoids a catch-all default partition
-- (rows outside a partition would otherwise pile into it).

-- DAILY tables (1 day partitions, 4 days pre-created)
SELECT partman.create_parent(
  p_parent_table := 'public.telemetry_gps',
  p_control := 'time', p_type := 'range', p_interval := '1 day',
  p_premake := 4, p_default_table := false);

SELECT partman.create_parent(
  p_parent_table := 'public.telemetry_battery',
  p_control := 'time', p_type := 'range', p_interval := '1 day',
  p_premake := 4, p_default_table := false);

SELECT partman.create_parent(
  p_parent_table := 'public.telemetry_fuel',
  p_control := 'time', p_type := 'range', p_interval := '1 day',
  p_premake := 4, p_default_table := false);

SELECT partman.create_parent(
  p_parent_table := 'public.telemetry_can',
  p_control := 'time', p_type := 'range', p_interval := '1 day',
  p_premake := 4, p_default_table := false);

-- WEEKLY tables (7 day partitions, 4 weeks pre-created)
SELECT partman.create_parent(
  p_parent_table := 'public.distance_rollup',
  p_control := 'time', p_type := 'range', p_interval := '7 days',
  p_premake := 4, p_default_table := false);

SELECT partman.create_parent(
  p_parent_table := 'public.alerts',
  p_control := 'time', p_type := 'range', p_interval := '7 days',
  p_premake := 4, p_default_table := false);

SELECT partman.create_parent(
  p_parent_table := 'public.trips',
  p_control := 'time', p_type := 'range', p_interval := '7 days',
  p_premake := 4, p_default_table := false);

-- ---- RETENTION (replaces add_retention_policy) --------------------------
-- partman drops partitions older than p_retention. The pg_partman_bgw worker
-- enforces this hourly. retention_keep_table=false => partitions are DROPPED.
-- IMPORTANT: the S3 cold-archive job (aggregator) MUST run before the drop.
UPDATE partman.part_config SET retention = '90 days',  retention_keep_table = false WHERE parent_table = 'public.telemetry_gps';
UPDATE partman.part_config SET retention = '90 days',  retention_keep_table = false WHERE parent_table = 'public.telemetry_battery';
UPDATE partman.part_config SET retention = '90 days',  retention_keep_table = false WHERE parent_table = 'public.telemetry_fuel';
UPDATE partman.part_config SET retention = '30 days',  retention_keep_table = false WHERE parent_table = 'public.telemetry_can';
UPDATE partman.part_config SET retention = '730 days', retention_keep_table = false WHERE parent_table = 'public.distance_rollup';
UPDATE partman.part_config SET retention = '365 days', retention_keep_table = false WHERE parent_table = 'public.alerts';
UPDATE partman.part_config SET retention = '365 days', retention_keep_table = false WHERE parent_table = 'public.trips';

-- =========================================================================
-- 5. ROLLUP TABLES  (replace Timescale continuous aggregates)
-- =========================================================================
-- Strategy: plain tables refreshed INCREMENTALLY by the aggregator (UPSERT the
-- recent window), instead of full MATERIALIZED VIEW refreshes. This scales to
-- 5000 vehicles where a full refresh would be expensive.

CREATE TABLE IF NOT EXISTS daily_distance_per_vehicle (
    day        DATE NOT NULL,
    vehicleno  TEXT NOT NULL,
    km         REAL,
    kwh        REAL,
    trips      INT,
    PRIMARY KEY (day, vehicleno)
);

CREATE TABLE IF NOT EXISTS hourly_battery_per_vehicle (
    hour         TIMESTAMPTZ NOT NULL,
    vehicleno    TEXT NOT NULL,
    avg_soc      REAL,
    min_soc      REAL,
    max_temp     REAL,
    avg_voltage  REAL,
    PRIMARY KEY (hour, vehicleno)
);

-- Refresh functions — call these from the aggregator on a schedule
-- (daily_distance: every 30 min; hourly_battery: every 15 min), passing the
-- lookback window that mirrors the old continuous-agg start_offset.

CREATE OR REPLACE FUNCTION refresh_daily_distance(p_lookback INTERVAL DEFAULT '7 days')
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO daily_distance_per_vehicle (day, vehicleno, km, kwh, trips)
    SELECT date_trunc('day', time)::date AS day,
           vehicleno,
           SUM(distance_km), SUM(energy_kwh), COUNT(*)
    FROM trips
    WHERE time >= NOW() - p_lookback
    GROUP BY 1, 2
    ON CONFLICT (day, vehicleno) DO UPDATE
      SET km = EXCLUDED.km, kwh = EXCLUDED.kwh, trips = EXCLUDED.trips;
END $$;

CREATE OR REPLACE FUNCTION refresh_hourly_battery(p_lookback INTERVAL DEFAULT '2 days')
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO hourly_battery_per_vehicle (hour, vehicleno, avg_soc, min_soc, max_temp, avg_voltage)
    SELECT date_trunc('hour', time) AS hour,
           vehicleno,
           AVG(soc_pct), MIN(soc_pct), MAX(pack_temp_c), AVG(pack_voltage)
    FROM telemetry_battery
    WHERE time >= NOW() - p_lookback
    GROUP BY 1, 2
    ON CONFLICT (hour, vehicleno) DO UPDATE
      SET avg_soc = EXCLUDED.avg_soc, min_soc = EXCLUDED.min_soc,
          max_temp = EXCLUDED.max_temp, avg_voltage = EXCLUDED.avg_voltage;
END $$;

-- =========================================================================
-- 6. AGGREGATOR / DASHBOARD METADATA TABLES
-- =========================================================================
-- Plain Postgres (no Timescale) — ported verbatim from
-- schema/dashboard_aggregates.sql so this file is self-sufficient. The
-- aggregator container writes these; without them it crashes on a fresh load.
-- No foreign keys, so order between them is free. They are created BEFORE the
-- dashboard_ro role section below, so the blanket
-- "GRANT SELECT ON ALL TABLES IN SCHEMA public" there covers them automatically.

-- ---- aggregator_runs : run log, written by aggregator/scheduler.py ----------
-- One row per job execution (success or failure). Useful for data-freshness
-- monitoring.
CREATE TABLE IF NOT EXISTS aggregator_runs (
    id            BIGSERIAL PRIMARY KEY,
    job_name      TEXT        NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL,
    finished_at   TIMESTAMPTZ NOT NULL,
    duration_ms   INTEGER     NOT NULL,
    rows_written  INTEGER     NOT NULL DEFAULT 0,
    error         TEXT        NULL
);
CREATE INDEX IF NOT EXISTS aggregator_runs_job_started_idx
    ON aggregator_runs (job_name, started_at DESC);

-- ---- dashboard_nbfc_loans_with_iot : written by jobs/nbfc_loan_iot_join.py --
-- Cross-source join for the CRM /nbfc/risk page: NBFC loans (from the NBFC RDS)
-- joined with the IoT slice from vehicle_state.
CREATE TABLE IF NOT EXISTS dashboard_nbfc_loans_with_iot (
    loan_application_id    TEXT             PRIMARY KEY,
    tenant_id              TEXT             NULL,
    vehicleno              TEXT             NULL,
    emi_amount             NUMERIC          NULL,
    emi_due_date_dom       INTEGER          NULL,
    current_dpd            INTEGER          NULL,
    outstanding_amount     NUMERIC          NULL,
    is_active              BOOLEAN          NULL,
    last_seen              TIMESTAMPTZ      NULL,
    soc_pct                REAL             NULL,
    online                 BOOLEAN          NULL,
    lat                    DOUBLE PRECISION NULL,
    lon                    DOUBLE PRECISION NULL,
    open_alert_count       INTEGER          NULL,
    refreshed_at           TIMESTAMPTZ      NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dashboard_nbfc_loans_with_iot_tenant_idx
    ON dashboard_nbfc_loans_with_iot (tenant_id, current_dpd DESC);
CREATE INDEX IF NOT EXISTS dashboard_nbfc_loans_with_iot_vehicleno_idx
    ON dashboard_nbfc_loans_with_iot (vehicleno);

-- ---- dashboard_vehicle_monthly_range : written by jobs/vehicle_monthly_range.py
-- One row per (vehicleno, first-of-month). range_km is NULL when monthly_km < 50
-- or monthly_ah < 5 (insufficient signal — see the job for the formula).
CREATE TABLE IF NOT EXISTS dashboard_vehicle_monthly_range (
    vehicleno     TEXT        NOT NULL,
    year_month    DATE        NOT NULL,         -- first of month, UTC
    monthly_km    REAL        NULL,
    monthly_ah    REAL        NULL,
    range_km      REAL        NULL,
    sample_count  INTEGER     NOT NULL DEFAULT 0,
    refreshed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (vehicleno, year_month)
);
CREATE INDEX IF NOT EXISTS dashboard_vehicle_monthly_range_month_idx
    ON dashboard_vehicle_monthly_range (year_month);

-- =========================================================================
-- 7. READ-ONLY DASHBOARD ROLE
-- =========================================================================
-- SECURITY: password removed. Set it from Secrets Manager AFTER creation, e.g.
--   ALTER ROLE dashboard_ro PASSWORD '<value-from-secrets-manager>';
-- Or better: manage this role's secret in Secrets Manager like the master user.
-- NOTE: original GRANT referenced database "intellicar"; corrected to "itarang".
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_ro') THEN
        CREATE ROLE dashboard_ro LOGIN PASSWORD 'CHANGE_ME_VIA_SECRETS_MANAGER';
    END IF;
END $$;

GRANT CONNECT ON DATABASE itarang TO dashboard_ro;
GRANT USAGE ON SCHEMA public TO dashboard_ro;
-- Covers every table created above, including the Section 6 aggregator/dashboard
-- metadata tables — no per-table grant needed.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dashboard_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO dashboard_ro;
