"""
Itarang IoT-stack aggregator — scheduled batch jobs.

Each job in jobs/*.py exposes:
    INTERVAL_ENV: name of the env var holding seconds between runs
    DEFAULT_INTERVAL_SECONDS: fallback if env var not set
    JOB_NAME: short identifier logged into aggregator_runs
    def run(iot_conn, rds_conn, logger) -> int  # returns rows written

The scheduler:
  1. Discovers jobs by importing every module from jobs/ (excluding __init__).
  2. Schedules each via APScheduler at its configured interval.
  3. Wraps every run in try/except, persists run metadata to aggregator_runs.
  4. Logs to stdout (docker logs picks it up).

Why APScheduler vs plain cron:
  - cron-in-container is awkward (signal handling, log capture, env loading).
  - APScheduler keeps everything in one Python process — one failure mode,
    one log stream, no shell glue.
"""
from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import signal
import sys
import time
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from lib.iot_db import iot_connection
from lib.rds_db import rds_connection

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("aggregator")

# ─────────────────────────────────────────────────────────────────────────────
# Job discovery
# ─────────────────────────────────────────────────────────────────────────────
import jobs  # noqa: E402  (imported for pkgutil.iter_modules)


def discover_jobs():
    """Yield (name, module) for every importable submodule of `jobs`."""
    for _, modname, _ in pkgutil.iter_modules(jobs.__path__):
        if modname.startswith("_"):
            continue
        mod = importlib.import_module(f"jobs.{modname}")
        if not hasattr(mod, "run"):
            log.warning("jobs.%s has no run() — skipping", modname)
            continue
        yield modname, mod


# ─────────────────────────────────────────────────────────────────────────────
# Job runner — wraps each call with connection mgmt, error capture, run log
# ─────────────────────────────────────────────────────────────────────────────
def make_runner(name: str, mod):
    job_name = getattr(mod, "JOB_NAME", name)

    def runner():
        started = datetime.now(timezone.utc)
        rows = 0
        err = None
        try:
            with iot_connection() as iot, rds_connection() as rds:
                rows = mod.run(iot, rds, logging.getLogger(f"job.{job_name}")) or 0
        except Exception as e:  # noqa: BLE001 — we want all failures captured
            err = repr(e)
            log.exception("job %s failed", job_name)
        finally:
            finished = datetime.now(timezone.utc)
            duration_ms = int((finished - started).total_seconds() * 1000)
            try:
                with iot_connection() as iot, iot.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO aggregator_runs
                            (job_name, started_at, finished_at, duration_ms, rows_written, error)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (job_name, started, finished, duration_ms, rows, err),
                    )
                    iot.commit()
            except Exception:
                # Don't let run-log persistence failure mask the original problem.
                log.exception("failed to persist run log for %s", job_name)
        log.info(
            "job %s done rows=%d duration_ms=%d err=%s",
            job_name, rows, duration_ms, err,
        )

    return runner


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    sched = BlockingScheduler(
        timezone="UTC",
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
    )

    registered = 0
    for name, mod in discover_jobs():
        interval_env = getattr(mod, "INTERVAL_ENV", None)
        default = int(getattr(mod, "DEFAULT_INTERVAL_SECONDS", 300))
        interval = int(os.environ.get(interval_env, default)) if interval_env else default
        job_name = getattr(mod, "JOB_NAME", name)
        sched.add_job(
            make_runner(name, mod),
            "interval",
            seconds=interval,
            id=job_name,
            name=job_name,
            next_run_time=datetime.now(timezone.utc),  # run once immediately on boot
        )
        log.info("scheduled job=%s interval=%ds", job_name, interval)
        registered += 1

    if registered == 0:
        log.error("no jobs registered — exiting")
        sys.exit(1)

    # Graceful shutdown on SIGTERM (docker stop)
    def _shutdown(_sig, _frm):
        log.info("shutdown signal — stopping scheduler")
        sched.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("scheduler started with %d job(s)", registered)
    sched.start()


if __name__ == "__main__":
    main()
