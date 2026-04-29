"""
nbfc_loan_iot_join — the cross-source join that justifies this whole container.

Pulls the active NBFC loan list (with vehicleno) from AWS RDS, pulls the
current per-vehicle state from local Timescale (vehicle_state, maintained
by poll.py), joins on vehicleno in pandas, and upserts into
dashboard_nbfc_loans_with_iot for the CRM /nbfc/risk page.

Why pandas over Postgres FDW / dblink:
  - RDS lives in a different VPC. FDW would require network rules we don't
    want to maintain.
  - Active-loan count stays small (thousands, not millions). Pulling both
    sides into memory and merging is simpler and faster than tunneling one
    through the other.
"""
from __future__ import annotations

import pandas as pd

JOB_NAME = "nbfc_loan_iot_join"
INTERVAL_ENV = "INTERVAL_NBFC_LOAN_IOT"
DEFAULT_INTERVAL_SECONDS = 300


# ─── source A: NBFC loans on AWS RDS (read-only) ─────────────────────────────
# Schema confirmed against drizzle/0033_nbfc_dashboard.sql + RDS introspection.
RDS_SQL = """
SELECT
    loan_application_id,
    tenant_id::text       AS tenant_id,
    vehicleno,
    emi_amount,
    emi_due_date_dom,
    current_dpd,
    outstanding_amount,
    is_active
FROM nbfc_loans
WHERE is_active = TRUE
  AND vehicleno IS NOT NULL
"""

# ─── source B: current IoT state per vehicle on local Timescale ─────────────
# vehicle_state is upserted by poll.py on every poll cycle. We grab whatever's
# there now — staleness is signalled by `last_seen` and `online`.
IOT_SQL = """
SELECT
    vehicleno,
    last_seen,
    soc_pct,
    online,
    lat,
    lon,
    open_alert_count
FROM vehicle_state
"""

UPSERT_SQL = """
INSERT INTO dashboard_nbfc_loans_with_iot
    (loan_application_id, tenant_id, vehicleno,
     emi_amount, emi_due_date_dom, current_dpd, outstanding_amount, is_active,
     last_seen, soc_pct, online, lat, lon, open_alert_count,
     refreshed_at)
VALUES
    (%(loan_application_id)s, %(tenant_id)s, %(vehicleno)s,
     %(emi_amount)s, %(emi_due_date_dom)s, %(current_dpd)s,
     %(outstanding_amount)s, %(is_active)s,
     %(last_seen)s, %(soc_pct)s, %(online)s, %(lat)s, %(lon)s,
     %(open_alert_count)s,
     now())
ON CONFLICT (loan_application_id) DO UPDATE SET
    tenant_id          = EXCLUDED.tenant_id,
    vehicleno          = EXCLUDED.vehicleno,
    emi_amount         = EXCLUDED.emi_amount,
    emi_due_date_dom   = EXCLUDED.emi_due_date_dom,
    current_dpd        = EXCLUDED.current_dpd,
    outstanding_amount = EXCLUDED.outstanding_amount,
    is_active          = EXCLUDED.is_active,
    last_seen          = EXCLUDED.last_seen,
    soc_pct            = EXCLUDED.soc_pct,
    online             = EXCLUDED.online,
    lat                = EXCLUDED.lat,
    lon                = EXCLUDED.lon,
    open_alert_count   = EXCLUDED.open_alert_count,
    refreshed_at       = EXCLUDED.refreshed_at;
"""


def run(iot, rds, log) -> int:
    loans = pd.read_sql(RDS_SQL, rds)
    if loans.empty:
        log.info("no active loans with vehicleno on RDS — nothing to join")
        return 0

    iot_state = pd.read_sql(IOT_SQL, iot)
    log.info(
        "pulled %d active loans, %d vehicles in vehicle_state",
        len(loans), len(iot_state),
    )

    # Left join — every active loan keeps a row even if no IoT state exists
    # yet (new vehicle not polled, or device offline since deploy).
    merged = loans.merge(iot_state, on="vehicleno", how="left")

    # nan → None so psycopg writes SQL NULL, not 'NaN'
    merged = merged.where(pd.notnull(merged), None)

    rows = merged.to_dict("records")
    with iot.cursor() as cur:
        cur.executemany(UPSERT_SQL, rows)
    iot.commit()
    log.info("upserted %d loan↔iot rows", len(rows))
    return len(rows)
