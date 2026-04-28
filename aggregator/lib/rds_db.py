"""Connection helper for AWS RDS (NBFC source-of-truth DB).

Use a READ-ONLY user. The aggregator should never write to RDS — joined
results are written back to the LOCAL Timescale (dashboard_* tables).
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg


def _dsn() -> str:
    dsn = os.environ.get("RDS_DSN")
    if not dsn:
        # Allow the aggregator to start without RDS configured — jobs that
        # don't need RDS still run. Jobs that DO need RDS will fail loudly
        # and be captured in aggregator_runs.
        raise RuntimeError("RDS_DSN env var is not set")
    return dsn


@contextmanager
def rds_connection():
    """Same per-call pattern as iot_db — RDS round-trip is the dominant cost,
    not connect time. SSL is RDS default; psycopg negotiates automatically."""
    conn = psycopg.connect(
        _dsn(),
        autocommit=True,        # this connection is read-only; no tx needed
        connect_timeout=15,     # cross-region/cross-VPC; allow more slack
        application_name="itarang-aggregator",
    )
    try:
        yield conn
    finally:
        conn.close()
