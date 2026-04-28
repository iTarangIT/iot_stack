"""
nbfc_loan_iot_join — the cross-source join that justifies this whole container.

Pulls the active NBFC loan list (with vehicleno) from AWS RDS, pulls
24h IoT activity per vehicle from local Timescale, joins in pandas, and
upserts into dashboard_nbfc_loans_with_iot for the CRM /nbfc/risk page.

Why pandas instead of dblink/FDW:
  - RDS lives in a different VPC. Postgres FDW would need network rules
    we don't want to maintain.
  - The join is small (active loans count, not millions of rows). Pulling
    both sides into memory and merging is simpler and faster than tunneling
    one side through the other.
"""
from __future__ import annotations

import pandas as pd

JOB_NAME = "nbfc_loan_iot_join"
INTERVAL_ENV = "INTERVAL_NBFC_LOAN_IOT"
DEFAULT_INTERVAL_SECONDS = 300


# ─── source A: NBFC loans on AWS RDS (read-only) ─────────────────────────────
# TODO: confirm exact column names against drizzle/0033_nbfc_dashboard.sql.
# Likely candidates from the schema we set up: nbfc_loans (loan_id, borrower_id,
# vehicle_no, dpd_days, principal_outstanding, status, tenant_id, …).
RDS_SQL = """
SELECT
    loan_id,
    borrower_id,
    vehicle_no       AS vehicleno,
    tenant_id,
    dpd_days,
    principal_outstanding,
    status
FROM nbfc_loans
WHERE status IN ('active', 'overdue')
  AND vehicle_no IS NOT NULL
"""

# ─── source B: IoT 24h activity per vehicle on local Timescale ───────────────
IOT_SQL = """
SELECT
    vehicleno,
    MAX(ts)                         AS last_seen,
    AVG(soc)                        AS soc_avg_24h,
    MIN(soc)                        AS soc_min_24h,
    SUM(distance_delta_km)          AS distance_24h_km,
    BOOL_OR(ignition)               AS ignition_24h,
    COUNT(*)                        AS samples_24h
FROM state_history
WHERE ts > now() - interval '24 hours'
GROUP BY vehicleno
"""

UPSERT_SQL = """
INSERT INTO dashboard_nbfc_loans_with_iot
    (loan_id, borrower_id, vehicleno, tenant_id, dpd_days,
     principal_outstanding, status,
     last_seen, soc_avg_24h, soc_min_24h, distance_24h_km, samples_24h,
     refreshed_at)
VALUES
    (%(loan_id)s, %(borrower_id)s, %(vehicleno)s, %(tenant_id)s, %(dpd_days)s,
     %(principal_outstanding)s, %(status)s,
     %(last_seen)s, %(soc_avg_24h)s, %(soc_min_24h)s, %(distance_24h_km)s,
     %(samples_24h)s, now())
ON CONFLICT (loan_id) DO UPDATE SET
    borrower_id           = EXCLUDED.borrower_id,
    vehicleno             = EXCLUDED.vehicleno,
    tenant_id             = EXCLUDED.tenant_id,
    dpd_days              = EXCLUDED.dpd_days,
    principal_outstanding = EXCLUDED.principal_outstanding,
    status                = EXCLUDED.status,
    last_seen             = EXCLUDED.last_seen,
    soc_avg_24h           = EXCLUDED.soc_avg_24h,
    soc_min_24h           = EXCLUDED.soc_min_24h,
    distance_24h_km       = EXCLUDED.distance_24h_km,
    samples_24h           = EXCLUDED.samples_24h,
    refreshed_at          = EXCLUDED.refreshed_at;
"""


def run(iot, rds, log) -> int:
    # Pull both sides
    loans = pd.read_sql(RDS_SQL, rds)
    if loans.empty:
        log.info("no active loans with vehicleno on RDS — nothing to join")
        return 0

    iot_24h = pd.read_sql(IOT_SQL, iot)
    log.info("pulled %d loans, %d vehicles with 24h activity", len(loans), len(iot_24h))

    # Left-join: every active loan gets a row even if no IoT samples in 24h.
    merged = loans.merge(iot_24h, on="vehicleno", how="left")

    # Coerce nan→None so psycopg writes SQL NULL, not the string 'nan'
    merged = merged.where(pd.notnull(merged), None)

    rows = merged.to_dict("records")
    with iot.cursor() as cur:
        cur.executemany(UPSERT_SQL, rows)
    iot.commit()
    log.info("upserted %d loan↔iot rows", len(rows))
    return len(rows)
