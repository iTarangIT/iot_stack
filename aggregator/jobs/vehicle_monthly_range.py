"""
vehicle_monthly_range — coulomb-counting range estimate per vehicle per month.

Per (vehicleno, year_month) computes:
    monthly_ah = Σ |pack_current| × Δt_hours
                 over telemetry_battery samples in the month where the pack
                 is NOT charging (charging IS FALSE OR NULL), with no
                 integration across gaps > 5 minutes between samples.

    monthly_km = Σ distance_km from distance_rollup (bucket_size='day')
                 for the month.

    rated_ah   = vehicles.capacity_kwh × 1000 / nominal_voltage
                 nominal_voltage = median pack_voltage of recent non-charging
                 samples for the vehicle (fallback 60.0 V).

    range_km   = (monthly_km / monthly_ah) × (rated_ah × DoD)

    NULL when monthly_km < 50 OR monthly_ah < 5  (insufficient signal).

DoD comes from env DEFAULT_DOD (default 0.8) — applied uniformly. e-rickshaw
packs are typically not discharged below 20% so 80% usable is a reasonable
fleet-wide default. Override per-deploy via the env var, or carry per-vehicle
DoD into vehicles.info JSONB and read it here when available.

Job behaviour:
  - Default interval: 86400 s (daily).
  - Each run upserts the *current* calendar month for every active vehicle.
    Idempotent — safe to re-run.
  - One-shot backfill: set BACKFILL_MONTHS=N before container start. The job
    will additionally recompute the previous N months on its first run, then
    fall back to current-month-only on subsequent runs (it self-clears the
    flag by writing a marker row).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

JOB_NAME = "vehicle_monthly_range"
INTERVAL_ENV = "INTERVAL_VEHICLE_MONTHLY_RANGE"
DEFAULT_INTERVAL_SECONDS = 86400  # daily

DEFAULT_DOD = float(os.environ.get("DEFAULT_DOD", "0.8"))
GAP_CLAMP_SECONDS = 300  # don't integrate across telemetry gaps > 5 min
NOMINAL_VOLTAGE_FALLBACK = 60.0  # e-rickshaw default if no recent samples


# ─────────────────────────────────────────────────────────────────────────────
# The recompute query.
#
# Window: months we want to (re)compute. Parameter %(months_back)s = how many
# completed months *before* the current one to include (0 = current only).
#
# Strategy:
#   - month_window: list of (year_month) DATEs we'll write rows for.
#   - sample_dt:    one row per telemetry_battery sample with Δt vs the
#                   previous sample for the same vehicle, clamped at the
#                   gap threshold; only kept where charging is false/null.
#   - ah:           Σ |pack_current| × Δt_hours per (vehicleno, month).
#   - km:           Σ distance_km from distance_rollup per (vehicleno, month).
#   - cap:          capacity_kwh × 1000 / nominal_voltage per vehicleno,
#                   where nominal_voltage = median pack_voltage from the last
#                   30 days of non-charging samples (>= 30 V, < 100 V to drop
#                   obviously-bad readings), fallback 60 V.
#
# We CROSS JOIN month_window × vehicles so that vehicles with zero telemetry
# in a month still get a row (with NULL range_km).
# ─────────────────────────────────────────────────────────────────────────────
RECOMPUTE_SQL = """
WITH RECURSIVE month_window AS (
    SELECT date_trunc('month', now() AT TIME ZONE 'UTC')::date AS year_month
    UNION ALL
    SELECT (year_month - interval '1 month')::date
    FROM month_window
    WHERE year_month > (date_trunc('month', now() AT TIME ZONE 'UTC')
                        - make_interval(months => %(months_back)s))::date
),
sample_dt AS (
    SELECT
        vehicleno,
        date_trunc('month', time)::date AS year_month,
        ABS(pack_current) AS abs_current,
        EXTRACT(EPOCH FROM (
            time - LAG(time) OVER (PARTITION BY vehicleno ORDER BY time)
        )) AS dt_seconds
    FROM telemetry_battery
    WHERE pack_current IS NOT NULL
      AND (charging IS NULL OR charging = FALSE)
      AND time >= (SELECT MIN(year_month) FROM month_window)
      AND time <  (SELECT MAX(year_month) FROM month_window) + interval '1 month'
),
ah AS (
    SELECT
        vehicleno,
        year_month,
        SUM(abs_current * LEAST(dt_seconds, %(gap_clamp)s) / 3600.0) AS monthly_ah,
        COUNT(*) AS sample_count
    FROM sample_dt
    WHERE dt_seconds IS NOT NULL AND dt_seconds > 0
    GROUP BY vehicleno, year_month
),
km AS (
    SELECT
        vehicleno,
        date_trunc('month', time)::date AS year_month,
        SUM(distance_km) AS monthly_km
    FROM distance_rollup
    WHERE bucket_size = 'day'
      AND time >= (SELECT MIN(year_month) FROM month_window)
      AND time <  (SELECT MAX(year_month) FROM month_window) + interval '1 month'
    GROUP BY vehicleno, date_trunc('month', time)
),
nominal AS (
    SELECT
        vehicleno,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY pack_voltage) AS nominal_voltage
    FROM telemetry_battery
    WHERE pack_voltage IS NOT NULL
      AND pack_voltage BETWEEN 30 AND 100
      AND (charging IS NULL OR charging = FALSE)
      AND time > now() - interval '30 days'
    GROUP BY vehicleno
),
cap AS (
    SELECT
        v.vehicleno,
        (v.capacity_kwh * 1000.0
            / COALESCE(n.nominal_voltage, %(nominal_fallback)s))::real AS rated_ah
    FROM vehicles v
    LEFT JOIN nominal n USING (vehicleno)
    WHERE v.capacity_kwh IS NOT NULL AND v.capacity_kwh > 0
)
SELECT
    v.vehicleno,
    mw.year_month,
    km.monthly_km::real                                              AS monthly_km,
    ah.monthly_ah::real                                              AS monthly_ah,
    CASE
        WHEN km.monthly_km IS NULL OR ah.monthly_ah IS NULL          THEN NULL
        WHEN km.monthly_km < 50  OR ah.monthly_ah < 5                THEN NULL
        WHEN cap.rated_ah IS NULL                                    THEN NULL
        ELSE ((km.monthly_km / ah.monthly_ah)
              * cap.rated_ah * %(dod)s)::real
    END                                                              AS range_km,
    COALESCE(ah.sample_count, 0)::int                                AS sample_count
FROM vehicles v
CROSS JOIN month_window mw
LEFT JOIN ah  ON ah.vehicleno = v.vehicleno  AND ah.year_month = mw.year_month
LEFT JOIN km  ON km.vehicleno = v.vehicleno  AND km.year_month = mw.year_month
LEFT JOIN cap ON cap.vehicleno = v.vehicleno
"""

UPSERT_SQL = """
INSERT INTO dashboard_vehicle_monthly_range
    (vehicleno, year_month, monthly_km, monthly_ah, range_km,
     sample_count, refreshed_at)
VALUES (%s, %s, %s, %s, %s, %s, now())
ON CONFLICT (vehicleno, year_month) DO UPDATE SET
    monthly_km   = EXCLUDED.monthly_km,
    monthly_ah   = EXCLUDED.monthly_ah,
    range_km     = EXCLUDED.range_km,
    sample_count = EXCLUDED.sample_count,
    refreshed_at = EXCLUDED.refreshed_at;
"""


def _months_back() -> int:
    """How many months prior to the current to recompute on this run.

    On first run after deploy with BACKFILL_MONTHS set, returns that value
    and clears the flag (file marker in /tmp). Subsequent runs return 0.
    """
    raw = os.environ.get("BACKFILL_MONTHS")
    if not raw:
        return 0
    try:
        n = int(raw)
    except ValueError:
        return 0
    if n <= 0:
        return 0
    marker = "/tmp/.vehicle_monthly_range_backfill_done"
    if os.path.exists(marker):
        return 0
    # Mark as done on first read; even if this run fails we don't want to
    # repeat a 12-month sweep every interval (~daily) — operator can rm
    # the marker to retrigger.
    try:
        with open(marker, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass
    return n


def run(iot, _rds, log) -> int:
    months_back = _months_back()
    params = {
        "months_back": months_back,
        "gap_clamp": GAP_CLAMP_SECONDS,
        "dod": DEFAULT_DOD,
        "nominal_fallback": NOMINAL_VOLTAGE_FALLBACK,
    }

    log.info(
        "recomputing range: months_back=%d dod=%.2f gap_clamp=%ds",
        months_back, DEFAULT_DOD, GAP_CLAMP_SECONDS,
    )

    with iot.cursor() as cur:
        cur.execute(RECOMPUTE_SQL, params)
        rows = cur.fetchall()

    if not rows:
        log.info("no rows produced — vehicles table empty?")
        return 0

    payload = [
        (vehicleno, year_month, monthly_km, monthly_ah, range_km, sample_count)
        for (vehicleno, year_month, monthly_km, monthly_ah, range_km, sample_count) in rows
    ]
    with iot.cursor() as cur:
        cur.executemany(UPSERT_SQL, payload)
    iot.commit()

    non_null = sum(1 for r in payload if r[4] is not None)
    log.info(
        "upserted %d (vehicle, month) rows; %d have a non-null range_km",
        len(payload), non_null,
    )
    return len(payload)
