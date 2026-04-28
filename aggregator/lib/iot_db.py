"""Connection helper for the local Timescale (IoT) database."""
from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg


def _dsn() -> str:
    dsn = os.environ.get("IOT_PG_DSN")
    if not dsn:
        raise RuntimeError("IOT_PG_DSN env var is not set")
    return dsn


@contextmanager
def iot_connection():
    """
    Per-job connection — opened, used, closed. We don't pool because:
      - jobs run on minute+ intervals; opening a connection is microseconds.
      - APScheduler is single-process; pooling adds complexity for ~zero win.
      - A leaked connection from a misbehaving job dies with the connection,
        not a long-lived pool entry.
    """
    conn = psycopg.connect(_dsn(), autocommit=False, connect_timeout=10)
    try:
        yield conn
    finally:
        conn.close()
