-- ─────────────────────────────────────────────────────────────────────────────
-- Itarang IoT Stack — dashboard_* tables and Timescale continuous aggregates.
--
-- Loaded by docker-compose at FIRST postgres init (before any user data
-- exists). Re-applied manually via psql after that — see IOT_DEPLOY_README.md
-- → "Migration policy".
--
-- Two kinds of objects in here:
--   1. dashboard_* tables — written by the Python aggregator (jobs/*.py)
--      via UPSERT on a primary key. The CRM IoT views read these directly.
--   2. continuous aggregates — Timescale-managed materialized views over
--      the raw hypertables. Auto-refresh on a schedule, no Python required.
--      Use these for time-bucket rollups (per-5min, per-hour, per-day).
--
-- Convention: table name dashboard_<thing>, refreshed_at timestamp on every
-- aggregator-managed table so the CRM can show "as of N seconds ago".
-- ─────────────────────────────────────────────────────────────────────────────

-- ─── Run log: every job execution lands here ────────────────────────────────
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

-- ─── jobs/fleet_summary.py — current state per vehicle ──────────────────────
CREATE TABLE IF NOT EXISTS dashboard_fleet_summary (
    vehicleno     TEXT        PRIMARY KEY,
    last_seen     TIMESTAMPTZ NULL,
    current_soc   NUMERIC     NULL,
    last_speed    NUMERIC     NULL,
    last_lat      NUMERIC     NULL,
    last_lng      NUMERIC     NULL,
    ignition      BOOLEAN     NULL,
    gps_fix       BOOLEAN     NULL,
    open_alerts   INTEGER     NOT NULL DEFAULT 0,
    status        TEXT        NOT NULL,        -- live | stale | offline | never_seen
    refreshed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dashboard_fleet_summary_status_idx
    ON dashboard_fleet_summary (status);
CREATE INDEX IF NOT EXISTS dashboard_fleet_summary_last_seen_idx
    ON dashboard_fleet_summary (last_seen DESC);

-- ─── jobs/nbfc_loan_iot_join.py — cross-source join for /nbfc/risk page ────
CREATE TABLE IF NOT EXISTS dashboard_nbfc_loans_with_iot (
    loan_id                TEXT        PRIMARY KEY,
    borrower_id            TEXT        NULL,
    vehicleno              TEXT        NULL,
    tenant_id              TEXT        NULL,
    dpd_days               INTEGER     NULL,
    principal_outstanding  NUMERIC     NULL,
    status                 TEXT        NULL,
    last_seen              TIMESTAMPTZ NULL,
    soc_avg_24h            NUMERIC     NULL,
    soc_min_24h            NUMERIC     NULL,
    distance_24h_km        NUMERIC     NULL,
    samples_24h            INTEGER     NULL,
    refreshed_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dashboard_nbfc_loans_with_iot_tenant_idx
    ON dashboard_nbfc_loans_with_iot (tenant_id, dpd_days DESC);
CREATE INDEX IF NOT EXISTS dashboard_nbfc_loans_with_iot_vehicleno_idx
    ON dashboard_nbfc_loans_with_iot (vehicleno);

-- ─── Continuous aggregates — Timescale handles the rest in-database ────────
-- These run inside Postgres on a schedule via add_continuous_aggregate_policy.
-- Add columns to the SELECT to surface in the dashboard view; refresh window
-- governs how often they update.
--
-- Requires the source tables (state_history, alerts) to be Timescale
-- hypertables. They already are if poll.py uses create_hypertable() — verify
-- with: SELECT * FROM timescaledb_information.hypertables;
--
-- TODO: enable these AFTER you've confirmed the column shape of
-- state_history matches. Until then they're commented out; flip on when ready.
--
-- CREATE MATERIALIZED VIEW IF NOT EXISTS dashboard_fleet_state_5min
-- WITH (timescaledb.continuous) AS
-- SELECT
--     time_bucket('5 minutes', ts) AS bucket,
--     vehicleno,
--     AVG(soc)::numeric(5,2) AS avg_soc,
--     MAX(speed)             AS max_speed,
--     COUNT(*)               AS samples
-- FROM state_history
-- GROUP BY bucket, vehicleno;
--
-- SELECT add_continuous_aggregate_policy(
--     'dashboard_fleet_state_5min',
--     start_offset => INTERVAL '2 hours',
--     end_offset   => INTERVAL '5 minutes',
--     schedule_interval => INTERVAL '5 minutes'
-- );
