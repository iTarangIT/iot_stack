-- ─────────────────────────────────────────────────────────────────────────────
-- Itarang IoT Stack — dashboard_* tables and aggregator metadata.
--
-- Loaded by docker-compose at FIRST postgres init only (volume already
-- existed for our deploy, so we apply this manually via psql).
-- Re-applied via psql for additive changes — see IOT_DEPLOY_README.md
-- → "Migration policy".
--
-- WHAT'S IN HERE:
--   1. aggregator_runs — written by scheduler.py on every job execution
--      (success or failure). Useful for monitoring data freshness.
--   2. dashboard_nbfc_loans_with_iot — written by jobs/nbfc_loan_iot_join.py.
--      The CRM /nbfc/risk page reads this directly.
--
-- WHAT'S NOT IN HERE (intentionally):
--   - dashboard_fleet_summary — retired. The CRM should query vehicle_state
--     (maintained by poll.py) directly, optionally JOINed with `vehicles`.
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

-- ─── jobs/nbfc_loan_iot_join.py — cross-source join for /nbfc/risk page ────
-- Schema mirrors RDS nbfc_loans (loan_application_id, tenant_id, vehicleno,
-- emi_amount, emi_due_date_dom, current_dpd, outstanding_amount, is_active)
-- plus the IoT slice we need for the dashboard from local vehicle_state
-- (last_seen, soc_pct, online, lat, lon, open_alert_count).
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
