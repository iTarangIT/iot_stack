"""
fleet_summary — pre-compute the fleet-overview row per vehicle.

Reads from the local Timescale tables that poll.py writes (vehicles, state_history,
alerts) and upserts one row per vehicleno into dashboard_fleet_summary.

The CRM IoT views read this table directly — no joins, no aggregations at
request time. Page load is O(1) per vehicle.
"""
from __future__ import annotations

JOB_NAME = "fleet_summary"
INTERVAL_ENV = "INTERVAL_FLEET_SUMMARY"
DEFAULT_INTERVAL_SECONDS = 60


# Built once. Tuned to:
#   - Use a single CTE-driven UPSERT so we avoid round-tripping per vehicle.
#   - Fall back gracefully (NULLs) when a vehicle has no recent telemetry.
#   - Always overwrite (no historic dashboard_fleet_summary rows kept — this
#     table is "current state per vehicle", history lives in raw tables).
#
# TODO: column names below assume a `state_history` hypertable + a `vehicles`
# roster table. Adjust to match what poll.py actually writes (open the Postgres
# schema with `\d state_history` after deploy and align).
SQL = """
WITH latest AS (
    SELECT DISTINCT ON (vehicleno)
        vehicleno,
        ts            AS last_seen,
        soc           AS current_soc,
        speed         AS last_speed,
        latitude      AS last_lat,
        longitude     AS last_lng,
        ignition,
        gps_fix
    FROM state_history
    WHERE ts > now() - interval '24 hours'
    ORDER BY vehicleno, ts DESC
),
alert_counts AS (
    SELECT vehicleno, COUNT(*)::int AS open_alerts
    FROM alerts
    WHERE resolved_at IS NULL
    GROUP BY vehicleno
)
INSERT INTO dashboard_fleet_summary
    (vehicleno, last_seen, current_soc, last_speed, last_lat, last_lng,
     ignition, gps_fix, open_alerts, status, refreshed_at)
SELECT
    v.vehicleno,
    l.last_seen,
    l.current_soc,
    l.last_speed,
    l.last_lat,
    l.last_lng,
    l.ignition,
    l.gps_fix,
    COALESCE(a.open_alerts, 0),
    CASE
        WHEN l.last_seen IS NULL                                 THEN 'never_seen'
        WHEN l.last_seen < now() - interval '1 hour'             THEN 'offline'
        WHEN l.last_seen < now() - interval '5 minutes'          THEN 'stale'
        ELSE                                                          'live'
    END AS status,
    now() AS refreshed_at
FROM vehicles v
LEFT JOIN latest       l ON l.vehicleno = v.vehicleno
LEFT JOIN alert_counts a ON a.vehicleno = v.vehicleno
ON CONFLICT (vehicleno) DO UPDATE SET
    last_seen    = EXCLUDED.last_seen,
    current_soc  = EXCLUDED.current_soc,
    last_speed   = EXCLUDED.last_speed,
    last_lat     = EXCLUDED.last_lat,
    last_lng     = EXCLUDED.last_lng,
    ignition     = EXCLUDED.ignition,
    gps_fix      = EXCLUDED.gps_fix,
    open_alerts  = EXCLUDED.open_alerts,
    status       = EXCLUDED.status,
    refreshed_at = EXCLUDED.refreshed_at;
"""


def run(iot, _rds, log) -> int:
    """iot: psycopg connection to local Timescale. rds unused for this job."""
    with iot.cursor() as cur:
        cur.execute(SQL)
        rows = cur.rowcount
    iot.commit()
    log.info("upserted %d fleet rows", rows)
    return rows
