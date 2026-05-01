"""
fleet_summary — RETIRED.

The work this job was going to do (per-vehicle current state for the dashboard)
is already done upstream by poll.py, which maintains the `vehicle_state` table
on every poll cycle. The CRM should query `vehicle_state` (optionally joined
with `vehicles` for makemodel/owner) directly — no aggregator round-trip needed.

This file is left as a no-op stub so the scheduler picks it up cleanly until
you `git rm aggregator/jobs/fleet_summary.py` and redeploy.
"""
from __future__ import annotations

JOB_NAME = "fleet_summary_noop"
INTERVAL_ENV = "INTERVAL_FLEET_SUMMARY"
DEFAULT_INTERVAL_SECONDS = 3600  # essentially never


def run(_iot, _rds, log) -> int:
    log.debug("fleet_summary is retired — vehicle_state is the source of truth")
    return 0
