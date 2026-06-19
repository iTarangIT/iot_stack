"""
refresh_daily_distance — RDS replacement for the Timescale continuous aggregate.

On the old Hostinger TimescaleDB, `daily_distance_per_vehicle` was a continuous
aggregate refreshed automatically by a background policy
(schema.sql: add_continuous_aggregate_policy, start_offset 7 days, every 30 min).

AWS RDS for plain PostgreSQL has no continuous aggregates, so schema_rds.sql
recreates `daily_distance_per_vehicle` as a plain table plus an incremental
UPSERT function `refresh_daily_distance(p_lookback INTERVAL DEFAULT '7 days')`.
This job calls that function on the IoT DB (the `iot` connection — post-migration
that DSN points at RDS) on the same 30-minute cadence as the old policy.

NOTE (pre-existing, not introduced by the migration): the refresh function reads
from `trips`, which poll.py does not currently populate — so this rollup stays
empty until trips are ingested. Behaviour matches the old continuous aggregate.
See MIGRATION_TODO.md.
"""
from __future__ import annotations

JOB_NAME = "refresh_daily_distance"
INTERVAL_ENV = "INTERVAL_REFRESH_DAILY_DISTANCE"
DEFAULT_INTERVAL_SECONDS = 1800  # 30 min — matches the old continuous-agg policy

# Lookback window passed to the refresh function — mirrors the old policy's
# start_offset of 7 days.
LOOKBACK = "7 days"


def run(iot, _rds, log) -> int:
    with iot.cursor() as cur:
        cur.execute("SELECT refresh_daily_distance(%s::interval)", (LOOKBACK,))
        # Best-effort rows-touched signal for aggregator_runs: count the rows
        # in the window the function just upserted. The function returns void,
        # so we can't get an exact affected-row count without changing its
        # signature (see MIGRATION_TODO.md, optional enhancement).
        cur.execute(
            "SELECT count(*) FROM daily_distance_per_vehicle "
            "WHERE day >= (now() - %s::interval)::date",
            (LOOKBACK,),
        )
        rows = cur.fetchone()[0]
    iot.commit()
    log.info("refreshed daily_distance_per_vehicle (lookback=%s, window rows=%d)",
             LOOKBACK, rows)
    return int(rows)
