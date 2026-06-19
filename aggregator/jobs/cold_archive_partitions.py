"""
cold_archive_partitions — export soon-to-be-dropped partitions to S3.

WHY THIS JOB EXISTS (MIGRATION_NOTES.md §3, "NEW REQUIRED JOB"):
On the old Hostinger TimescaleDB, old chunks were *compressed* in place and kept,
so retention dropping a chunk still left a compressed copy. AWS RDS for plain
PostgreSQL has no Timescale compression — schema_rds.sql relies on pg_partman
retention which **DROPS** partitions permanently (retention_keep_table = false).
The pg_partman background worker (pg_partman_bgw) enforces that hourly.

So before a partition is dropped, its data must be exported to S3, otherwise it
is lost forever. This job is the long-term-cost control that *replaces* Timescale
compression. It runs daily, finds partitions that are within `lead` days of being
dropped, streams each to gzipped CSV in S3, and records it in a ledger so it is
never exported twice. It NEVER drops partitions itself — pg_partman owns that.

Ordering guarantee: archive must happen before the drop. Keep
IOT_ARCHIVE_LEAD_DAYS larger than the pg_partman_bgw.interval (≈ hourly) so a
partition is always archived at least a day before it becomes eligible to drop.

Config (env-driven; no secrets committed — see .env.example):
  IOT_ARCHIVE_S3_BUCKET      required; unset => job logs a warning and no-ops
                             (the rest of the aggregator keeps running).
  IOT_ARCHIVE_S3_PREFIX      key prefix under the bucket (default "partitions").
  IOT_ARCHIVE_LEAD_DAYS      days before the retention drop to archive (default 3).
  IOT_ARCHIVE_RETENTION_JSON optional per-table override of the retention window,
                             e.g. {"telemetry_gps":"90 days"}. Default empty =>
                             the authoritative partman.part_config value is used.
  AWS creds/region           via the standard boto3 chain — the Fargate task IAM
                             role in prod, or AWS_* env vars locally.

Idempotency ledger `iot_archive_log` is self-created on first run so the job is
robust regardless of schema-load order.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import tempfile

JOB_NAME = "cold_archive_partitions"
INTERVAL_ENV = "INTERVAL_COLD_ARCHIVE"
DEFAULT_INTERVAL_SECONDS = 86400  # daily

S3_BUCKET = os.environ.get("IOT_ARCHIVE_S3_BUCKET", "").strip()
S3_PREFIX = os.environ.get("IOT_ARCHIVE_S3_PREFIX", "partitions").strip().strip("/")
LEAD_DAYS = int(os.environ.get("IOT_ARCHIVE_LEAD_DAYS", "3"))


def _retention_overrides() -> dict:
    raw = os.environ.get("IOT_ARCHIVE_RETENTION_JSON", "").strip()
    if not raw:
        return {}
    try:
        return dict(json.loads(raw))
    except (ValueError, TypeError):
        return {}


# Capture the upper bound of a RANGE partition from pg_get_expr(relpartbound),
# e.g.  FOR VALUES FROM ('2026-01-01 00:00:00+00') TO ('2026-01-02 00:00:00+00')
_BOUND_RE = re.compile(r"TO\s*\(\s*'([^']+)'\s*\)", re.IGNORECASE)

# Managed parents + their retention window. partman.part_config is the source of
# truth; an env override (IOT_ARCHIVE_RETENTION_JSON) can shorten it per table.
MANAGED_SQL = """
SELECT parent_table, retention
FROM partman.part_config
WHERE retention IS NOT NULL
"""

# All child partitions of a parent, with their declared range bound text.
PARTITIONS_SQL = """
SELECT n.nspname AS schemaname,
       c.relname AS partition_name,
       pg_get_expr(c.relpartbound, c.oid) AS bound
FROM pg_inherits i
JOIN pg_class      c ON c.oid = i.inhrelid
JOIN pg_namespace  n ON n.oid = c.relnamespace
JOIN pg_class      p ON p.oid = i.inhparent
JOIN pg_namespace  pn ON pn.oid = p.relnamespace
WHERE pn.nspname || '.' || p.relname = %s
  AND c.relkind IN ('r', 'p')
ORDER BY c.relname
"""

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS iot_archive_log (
    parent_table   TEXT        NOT NULL,
    partition_name TEXT        NOT NULL,
    s3_key         TEXT        NOT NULL,
    row_count      BIGINT      NOT NULL,
    bytes          BIGINT      NOT NULL,
    archived_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (parent_table, partition_name)
)
"""


def _ensure_ledger(iot) -> None:
    with iot.cursor() as cur:
        cur.execute(LEDGER_DDL)
    iot.commit()


def _already_archived(iot) -> set[tuple[str, str]]:
    with iot.cursor() as cur:
        cur.execute("SELECT parent_table, partition_name FROM iot_archive_log")
        return {(r[0], r[1]) for r in cur.fetchall()}


def _due(iot, bound_ts: str, retention: str, lead_days: int) -> bool:
    """Is this partition (upper bound = bound_ts) within `lead_days` of its
    retention drop? Evaluated against the DB clock for correctness."""
    with iot.cursor() as cur:
        cur.execute(
            "SELECT %s::timestamptz < now() - (%s::interval - make_interval(days => %s))",
            (bound_ts, retention, lead_days),
        )
        return bool(cur.fetchone()[0])


def _archive_one(iot, s3, schemaname: str, partition: str, parent: str, log) -> tuple[int, int]:
    """COPY the partition to a temp gzip CSV and upload to S3. Returns
    (row_count, bytes_uploaded)."""
    qualified = f'"{schemaname}"."{partition}"'
    key = f"{S3_PREFIX}/{parent}/{partition}.csv.gz" if S3_PREFIX else f"{parent}/{partition}.csv.gz"

    # Stream COPY → temp gzip file (memory-safe for tens-of-millions of rows).
    with tempfile.NamedTemporaryFile(suffix=".csv.gz") as tmp:
        row_count = 0
        with gzip.open(tmp, "wb") as gz:
            with iot.cursor() as cur:
                with cur.copy(
                    f"COPY (SELECT * FROM {qualified}) TO STDOUT "
                    f"WITH (FORMAT csv, HEADER true)"
                ) as copy:
                    for data in copy:
                        gz.write(data)
                row_count = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        tmp.flush()
        size = os.fstat(tmp.fileno()).st_size
        tmp.seek(0)
        s3.upload_fileobj(tmp, S3_BUCKET, key)

    with iot.cursor() as cur:
        cur.execute(
            """INSERT INTO iot_archive_log
                 (parent_table, partition_name, s3_key, row_count, bytes)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (parent_table, partition_name) DO NOTHING""",
            (parent, partition, key, row_count, size),
        )
    iot.commit()
    log.info("archived %s → s3://%s/%s (rows≈%d, %d bytes)",
             partition, S3_BUCKET, key, row_count, size)
    return row_count, size


def run(iot, _rds, log) -> int:
    if not S3_BUCKET:
        log.warning("IOT_ARCHIVE_S3_BUCKET not set — skipping cold archive "
                    "(NOTE: pg_partman will still DROP partitions; data will be "
                    "lost unless this is configured before retention kicks in)")
        return 0

    # boto3 imported lazily so the module loads even if the dep is missing in a
    # non-archive environment; if it's genuinely needed and absent, fail loudly.
    import boto3

    _ensure_ledger(iot)
    done = _already_archived(iot)
    overrides = _retention_overrides()
    s3 = boto3.client("s3")

    with iot.cursor() as cur:
        cur.execute(MANAGED_SQL)
        managed = cur.fetchall()

    archived = 0
    for parent, retention in managed:
        retention = overrides.get(parent, retention)
        with iot.cursor() as cur:
            cur.execute(PARTITIONS_SQL, (parent,))
            parts = cur.fetchall()

        for schemaname, partition, bound in parts:
            if (parent, partition) in done:
                continue
            if not bound:
                continue
            m = _BOUND_RE.search(bound)
            if not m:
                # default/unbounded partition or unexpected shape — skip safely
                log.debug("skip %s: no parseable upper bound (%s)", partition, bound)
                continue
            if not _due(iot, m.group(1), retention, LEAD_DAYS):
                continue
            try:
                _archive_one(iot, s3, schemaname, partition, parent, log)
                archived += 1
            except Exception:  # noqa: BLE001 — capture per-partition, keep going
                log.exception("failed to archive %s (will retry next run)", partition)

    log.info("cold archive done: %d partition(s) exported to s3://%s/%s",
             archived, S3_BUCKET, S3_PREFIX)
    return archived
