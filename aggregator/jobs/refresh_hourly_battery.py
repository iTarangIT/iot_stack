"""
refresh_hourly_battery — RDS replacement for the Timescale continuous aggregate.

On the old Hostinger TimescaleDB, `hourly_battery_per_vehicle` was a continuous
aggregate refreshed automatically by a background policy
(schema.sql: add_continuous_aggregate_policy, start_offset 2 days, every 15 min).

AWS RDS for plain PostgreSQL has no continuous aggregates, so schema_rds.sql
recreates `hourly_battery_per_vehicle` as a plain table plus an incremental
UPSERT function `refresh_hourly_battery(p_lookback INTERVAL DEFAULT '2 days')`.
This job calls that function on the IoT DB (the `iot` connection — post-migration
that DSN points at RDS) on the same 15-minute cadence as the old policy.

The source table `telemetry_battery` is populated by poll.py's daily backfill, so
this rollup is non-empty once backfill has run.
"""
from __future__ import annotations

JOB_NAME = "refresh_hourly_battery"
INTERVAL_ENV = "INTERVAL_REFRESH_HOURLY_BATTERY"
DEFAULT_INTERVAL_SECONDS = 900  # 15 min — matches the old continuous-agg policy

# Lookback window passed to the refresh function — mirrors the old policy's
# start_offset of 2 days.
LOOKBACK = "2 days"


def run(iot, _rds, log) -> int:
    with iot.cursor() as cur:
        cur.execute("SELECT refresh_hourly_battery(%s::interval)", (LOOKBACK,))
        # Best-effort rows-touched signal for aggregator_runs — see the sibling
        # refresh_daily_distance job for why this is a window count, not an
        # exact affected-row count.
        cur.execute(
            "SELECT count(*) FROM hourly_battery_per_vehicle "
            "WHERE hour >= now() - %s::interval",
            (LOOKBACK,),
        )
        rows = cur.fetchone()[0]
    iot.commit()
    log.info("refreshed hourly_battery_per_vehicle (lookback=%s, window rows=%d)",
             LOOKBACK, rows)
    return int(rows)
