-- Itarang · Intellicar telemetry schema
-- PostgreSQL 16 + TimescaleDB
-- Run order: this file is mounted into /docker-entrypoint-initdb.d/
-- so it executes once when the postgres container first starts.

-- 1. Extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- =========================================================================
-- 2. STATIC / SLOW-CHANGING TABLES (regular Postgres, not hypertables)
-- =========================================================================

CREATE TABLE IF NOT EXISTS vehicles (
    vehicleno      TEXT PRIMARY KEY,
    makemodel      TEXT,
    deviceid       TEXT,
    owner          TEXT,
    geofence_id    TEXT,
    capacity_kwh   NUMERIC(6,2),
    -- Refreshed by poll_daily (daily)
    info           JSONB,        -- raw getvehicleinfo response
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

-- Mirror of "current state per vehicle".
-- GPS + CAN columns: updated every 30 s by the live poller.
-- Battery + fuel columns: backfilled once per day from getbatterymetricshistory
-- and getfuelhistory (the most recent sample for each vehicle yesterday).
-- Same data also lives in Redis for sub-ms reads; this table is the source
-- of truth and is what Redis is rehydrated from on cold start.
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
    -- Fuel-status fields (for EVs these mirror battery; for ICE they differ)
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
-- 3. TIME-SERIES TABLES (TimescaleDB hypertables)
-- =========================================================================

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
);
SELECT create_hypertable('telemetry_gps', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_gps_vehicle_time ON telemetry_gps(vehicleno, time DESC);

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
);
SELECT create_hypertable('telemetry_battery', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_batt_vehicle_time ON telemetry_battery(vehicleno, time DESC);

CREATE TABLE IF NOT EXISTS telemetry_fuel (
    time          TIMESTAMPTZ NOT NULL,
    vehicleno     TEXT NOT NULL,
    fuel_pct      REAL,           -- 0-100
    range_km      REAL,           -- estimated remaining range
    fuel_volts    REAL,           -- raw analog if present
    consumption   REAL             -- kWh/100km or l/100km
);
SELECT create_hypertable('telemetry_fuel', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_fuel_vehicle_time ON telemetry_fuel(vehicleno, time DESC);

-- Daily distance rollup (NOT raw telemetry)
-- One row per (vehicle, day). Source: getdistancetravelled, called once per
-- night for the prior calendar day. bucket_size column is kept TEXT for
-- forward-compatibility if hourly rollups ever return.
CREATE TABLE IF NOT EXISTS distance_rollup (
    time          TIMESTAMPTZ NOT NULL,         -- start of bucket
    vehicleno     TEXT NOT NULL,
    bucket_size   TEXT NOT NULL,                -- 'hour' | 'day'
    distance_km   REAL,
    energy_kwh    REAL,
    moving_seconds INT,
    PRIMARY KEY (time, vehicleno, bucket_size)
);
SELECT create_hypertable('distance_rollup', 'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_distrollup_v ON distance_rollup(vehicleno, time DESC);

-- Latest CAN snapshot stored as JSON because the param set varies per vehicle
CREATE TABLE IF NOT EXISTS telemetry_can (
    time         TIMESTAMPTZ NOT NULL,
    vehicleno    TEXT NOT NULL,
    payload      JSONB NOT NULL                -- decoded params keyed by name
);
SELECT create_hypertable('telemetry_can', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_can_vehicle_time ON telemetry_can(vehicleno, time DESC);

CREATE TABLE IF NOT EXISTS alerts (
    time         TIMESTAMPTZ NOT NULL,
    vehicleno    TEXT NOT NULL,
    alert_type   TEXT NOT NULL,        -- low_soc / over_temp / offline / ignition_offline / ...
    severity     TEXT NOT NULL,        -- info / warn / critical
    message      TEXT,
    value        REAL,
    threshold    REAL,
    resolved_at  TIMESTAMPTZ
);
SELECT create_hypertable('alerts', 'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_alerts_open    ON alerts(vehicleno, time DESC) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_alerts_type    ON alerts(alert_type, time DESC);

CREATE TABLE IF NOT EXISTS trips (
    time          TIMESTAMPTZ NOT NULL,    -- start time, used as Timescale dimension
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
);
SELECT create_hypertable('trips', 'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_trips_vehicle  ON trips(vehicleno, time DESC);

-- =========================================================================
-- 4. COMPRESSION POLICIES (Timescale auto-compresses old chunks)
-- =========================================================================
-- Compress chunks older than 7 days, segmented by vehicleno for fast reads.
ALTER TABLE telemetry_gps     SET (timescaledb.compress, timescaledb.compress_segmentby = 'vehicleno');
ALTER TABLE telemetry_battery SET (timescaledb.compress, timescaledb.compress_segmentby = 'vehicleno');
ALTER TABLE telemetry_fuel    SET (timescaledb.compress, timescaledb.compress_segmentby = 'vehicleno');
ALTER TABLE telemetry_can     SET (timescaledb.compress, timescaledb.compress_segmentby = 'vehicleno');
ALTER TABLE distance_rollup   SET (timescaledb.compress, timescaledb.compress_segmentby = 'vehicleno');
ALTER TABLE alerts            SET (timescaledb.compress, timescaledb.compress_segmentby = 'vehicleno');
ALTER TABLE trips             SET (timescaledb.compress, timescaledb.compress_segmentby = 'vehicleno');

SELECT add_compression_policy('telemetry_gps',     INTERVAL '7 days',  if_not_exists => TRUE);
SELECT add_compression_policy('telemetry_battery', INTERVAL '7 days',  if_not_exists => TRUE);
SELECT add_compression_policy('telemetry_fuel',    INTERVAL '7 days',  if_not_exists => TRUE);
SELECT add_compression_policy('telemetry_can',     INTERVAL '7 days',  if_not_exists => TRUE);
SELECT add_compression_policy('distance_rollup',   INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('alerts',            INTERVAL '14 days', if_not_exists => TRUE);
SELECT add_compression_policy('trips',             INTERVAL '14 days', if_not_exists => TRUE);

-- =========================================================================
-- 5. RETENTION POLICIES (drop chunks after the warm window)
-- =========================================================================
-- Keep 90 days warm. Cold archiver must run BEFORE chunks are dropped.
SELECT add_retention_policy('telemetry_gps',     INTERVAL '90 days',  if_not_exists => TRUE);
SELECT add_retention_policy('telemetry_battery', INTERVAL '90 days',  if_not_exists => TRUE);
SELECT add_retention_policy('telemetry_fuel',    INTERVAL '90 days',  if_not_exists => TRUE);
SELECT add_retention_policy('telemetry_can',     INTERVAL '30 days',  if_not_exists => TRUE);
SELECT add_retention_policy('distance_rollup',   INTERVAL '730 days', if_not_exists => TRUE);
SELECT add_retention_policy('alerts',            INTERVAL '365 days', if_not_exists => TRUE);
SELECT add_retention_policy('trips',             INTERVAL '365 days', if_not_exists => TRUE);

-- =========================================================================
-- 6. CONTINUOUS AGGREGATES (precomputed rollups for the dashboard)
-- =========================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS daily_distance_per_vehicle
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 day', time) AS day,
    vehicleno,
    SUM(distance_km) AS km,
    SUM(energy_kwh)  AS kwh,
    COUNT(*)         AS trips
FROM trips
GROUP BY day, vehicleno
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS hourly_battery_per_vehicle
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 hour', time) AS hour,
    vehicleno,
    AVG(soc_pct)      AS avg_soc,
    MIN(soc_pct)      AS min_soc,
    MAX(pack_temp_c)  AS max_temp,
    AVG(pack_voltage) AS avg_voltage
FROM telemetry_battery
GROUP BY hour, vehicleno
WITH NO DATA;

SELECT add_continuous_aggregate_policy('daily_distance_per_vehicle',
    start_offset => INTERVAL '7 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '30 minutes',
    if_not_exists => TRUE);

SELECT add_continuous_aggregate_policy('hourly_battery_per_vehicle',
    start_offset => INTERVAL '2 days',
    end_offset   => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '15 minutes',
    if_not_exists => TRUE);

-- =========================================================================
-- 7. APP ROLE FOR THE DASHBOARD (read-only)
-- =========================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_ro') THEN
        CREATE ROLE dashboard_ro LOGIN PASSWORD 'FyztuUFcC5O1uRIbOGsWQEco';
    END IF;
END $$;

GRANT CONNECT ON DATABASE intellicar TO dashboard_ro;
GRANT USAGE ON SCHEMA public TO dashboard_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dashboard_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO dashboard_ro;
