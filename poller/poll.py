"""
Itarang · Intellicar telemetry poller  (v3 — corrected API surface)
====================================================================

Schedules
---------
  poll_state       every 30 s   getlastgpsstatus + getlatestcan per vehicle
                                  → vehicle_state (live map) + telemetry_gps
                                    (live GPS snapshot) + telemetry_can
  poll_recent      every  5 m   getbatterymetricshistory over (last_seen, now]
                                  per vehicle → telemetry_battery at true
                                  30-second resolution. (GPS is NOT here:
                                  getgpshistory returns empty on this account.)
  refresh_roster   every  1 h   listvehicles + listusers + listvehicledevicemapping
  poll_daily       cron 01:05 UTC   for each vehicle, fetch yesterday's:
                                       · getfuelhistory            → telemetry_fuel
                                       · getdistancetravelled      → distance_rollup
                                       then refresh getvehicleinfo
                                  (battery + gps now handled by poll_recent)

Why poll_recent
---------------
  getlatestcan / getlastgpsstatus return only the newest cached frame, so polling
  them every 30 s cannot be denser than the device push cadence and — with no
  unique (time, vehicleno) constraint — each identical re-read became a duplicate
  row (measured ~84% dup in telemetry_can, ~63% in telemetry_gps). The *history*
  endpoints return every device frame exactly once at 30-second resolution
  (verified 30s median, ~0% dup in telemetry_battery); running them on a short
  rolling window makes that data continuous instead of once-a-day.

Stores
------
  Redis     : state:<vno> = JSON blob ; intellicar:token = cached JWT (12d TTL)
  Postgres  : telemetry_gps, telemetry_can         (live snapshot, poll_state)
              telemetry_battery                    (rolling 30s history, poll_recent)
              telemetry_fuel                       (daily backfill)
              distance_rollup                      (daily, bucket='day')
              vehicle_state                        (mirrors latest values)
              vehicles (info JSONB), alerts        (with hysteresis)

Sharding: vehicles are bucketed by md5(vehicleno) % WORKER_COUNT == WORKER_ID.

API note
--------
All POSTs include {"token": <cached_token>, ...}. History endpoints take
"starttime"/"endtime" as **epoch milliseconds**. Live endpoints take just
{"vehicleno"}.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import aiohttp
import asyncpg
import redis.asyncio as aioredis
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------- config ----------
INTELLICAR_BASE     = os.environ.get("INTELLICAR_BASE", "https://apiplatform.intellicar.in/api/standard")
USERNAME            = os.environ["INTELLICAR_USERNAME"]
PASSWORD            = os.environ["INTELLICAR_PASSWORD"]
PG_DSN              = os.environ["PG_DSN"]
REDIS_URL           = os.environ.get("REDIS_URL", "redis://redis:6379/0")
WORKER_ID           = int(os.environ.get("WORKER_ID", "0"))
WORKER_COUNT        = int(os.environ.get("WORKER_COUNT", "1"))
CONCURRENCY         = int(os.environ.get("CONCURRENCY", "50"))
POLL_STATE_SEC      = int(os.environ.get("POLL_STATE_SEC", "30"))
ROSTER_REFRESH_SEC  = int(os.environ.get("ROSTER_REFRESH_SEC", "3600"))
# poll_recent pulls full-resolution *history* on a short interval so the
# time-series tables carry true 30-second data continuously, instead of only via
# the daily backfill. The "latest snapshot" endpoints (getlatestcan /
# getlastgpsstatus) can never be denser than the device push cadence and, with no
# unique (time, vehicleno) constraint, each identical re-read became a duplicate
# row. The history endpoints return every device frame exactly once (verified: 30s
# median, ~0% dupes in telemetry_battery).
POLL_RECENT_SEC     = int(os.environ.get("POLL_RECENT_SEC", "300"))    # 5 min
RECENT_LOOKBACK_H   = int(os.environ.get("RECENT_LOOKBACK_H", "48"))   # catch-up / outage self-heal cap
DAILY_REFRESH_HOUR  = int(os.environ.get("DAILY_REFRESH_HOUR", "1"))   # UTC hour
DAILY_REFRESH_MIN   = int(os.environ.get("DAILY_REFRESH_MIN",  "5"))
LOG_LEVEL           = os.environ.get("LOG_LEVEL", "INFO")

TOKEN_LIFETIME_DAYS = 12
TOKEN_REDIS_KEY     = "intellicar:token"

# Alert thresholds (offline only — battery/temp alerts evaluated against
# yesterday's backfill in poll_daily, since live battery isn't available).
# 60 min matches typical EV depot/charge cycles — 30 min was too aggressive
# and flapped vehicles offline between trips.
OFFLINE_SEC  = 60 * 60
OFFLINE_INTERVAL_SQL = "60 minutes"  # keep in sync with OFFLINE_SEC


def shard_owns(v: str) -> bool:
    return int(hashlib.md5(v.encode()).hexdigest(), 16) % WORKER_COUNT == WORKER_ID


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ts_to_dt(ms, default=None):
    """Intellicar timestamps come back in ms; some endpoints use seconds.
    Handle both."""
    if ms is None:
        return default
    try:
        ms = int(ms)
    except (ValueError, TypeError):
        return default
    # > year 2200 in seconds means it's already ms
    if ms > 7_258_118_400:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return datetime.fromtimestamp(ms, tz=timezone.utc)


def to_bool(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "on", "yes")
    return None


# =========================================================================
# Poller
# =========================================================================
class Poller:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self.pg: asyncpg.Pool | None = None
        self.redis: aioredis.Redis | None = None
        self.token: str | None = None
        self.token_expires: datetime = utc_now()
        self.vehicles: list[str] = []
        self.sem = asyncio.Semaphore(CONCURRENCY)
        self.metrics = {"state_polls": 0, "ok": 0, "err": 0}

    # ----- lifecycle -----
    async def setup(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
            headers={"User-Agent": "itarang-poller/3.0"},
        )
        self.pg = await asyncpg.create_pool(PG_DSN, min_size=10, max_size=50)
        self.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        await self.refresh_token()
        await self.refresh_roster()
        logging.info("Poller ready: worker %d/%d, %d vehicles, concurrency %d",
                     WORKER_ID, WORKER_COUNT, len(self.vehicles), CONCURRENCY)

    async def shutdown(self) -> None:
        if self.session: await self.session.close()
        if self.redis:   await self.redis.close()
        if self.pg:      await self.pg.close()

    # ----- auth -----
    async def refresh_token(self) -> None:
        async with self.session.post(
            f"{INTELLICAR_BASE}/gettoken",
            json={"username": USERNAME, "password": PASSWORD},
        ) as r:
            body = await r.json()
        if body.get("status") != "SUCCESS":
            raise RuntimeError(f"Auth failed: {body}")
        self.token = body["data"]["token"]
        self.token_expires = utc_now() + timedelta(days=TOKEN_LIFETIME_DAYS)
        # Share with PHP via Redis
        await self.redis.set(TOKEN_REDIS_KEY, self.token,
                             ex=int(timedelta(days=TOKEN_LIFETIME_DAYS).total_seconds()))
        logging.info("Token refreshed (expires ~%s)", self.token_expires.isoformat())

    async def post(self, path: str, payload: dict, _retried: bool = False) -> dict:
        if utc_now() >= self.token_expires:
            await self.refresh_token()
        body = {"token": self.token, **payload}
        async with self.sem:
            try:
                async with self.session.post(f"{INTELLICAR_BASE}/{path}", json=body) as r:
                    data = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                return {"status": "FAILURE", "err": f"transport: {e!r}"}
        if not _retried and data.get("status") == "FAILURE" \
                and "token" in (data.get("err") or "").lower():
            await self.refresh_token()
            return await self.post(path, payload, _retried=True)
        return data

    # ----- roster -----
    async def refresh_roster(self) -> None:
        body = await self.post("listvehicles", {})
        rows = body.get("data") or []
        self.vehicles = [v["vehicleno"] for v in rows
                         if v.get("vehicleno") and shard_owns(v["vehicleno"])]
        if rows:
            async with self.pg.acquire() as conn:
                await conn.executemany(
                    """INSERT INTO vehicles (vehicleno, makemodel, deviceid, owner, updated_at)
                       VALUES ($1, $2, $3, $4, NOW())
                       ON CONFLICT (vehicleno) DO UPDATE
                       SET makemodel = EXCLUDED.makemodel,
                           deviceid  = COALESCE(EXCLUDED.deviceid, vehicles.deviceid),
                           owner     = COALESCE(EXCLUDED.owner, vehicles.owner),
                           updated_at = NOW()""",
                    [(v["vehicleno"], v.get("makemodel"),
                      v.get("deviceid"), v.get("owner")) for v in rows],
                )
        # Users
        users = await self.post("listusers", {})
        for u in users.get("data") or []:
            if u.get("username"):
                async with self.pg.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO users (username, role) VALUES ($1, $2) "
                        "ON CONFLICT (username) DO UPDATE SET role = EXCLUDED.role",
                        u["username"], u.get("role"))
        # Device mapping
        mapping = await self.post("listvehicledevicemapping", {})
        for m in mapping.get("data") or []:
            if m.get("vehicleno") and m.get("deviceid"):
                async with self.pg.acquire() as conn:
                    await conn.execute(
                        "UPDATE vehicles SET deviceid = $2, updated_at = NOW() "
                        "WHERE vehicleno = $1",
                        m["vehicleno"], m["deviceid"])
        logging.info("Roster refreshed: %d vehicles total, %d for this worker",
                     len(rows), len(self.vehicles))

    # =========================================================================
    # poll_state — every 30 s — GPS + latest CAN frame per vehicle
    # =========================================================================
    async def poll_state(self) -> None:
        if not self.vehicles:
            return
        start = time.monotonic()
        results = await asyncio.gather(
            *(self._poll_state_one(v) for v in self.vehicles),
            return_exceptions=True,
        )
        ok = sum(1 for r in results if r is True)
        err = len(results) - ok
        self.metrics["state_polls"] += 1
        self.metrics["ok"] += ok
        self.metrics["err"] += err
        elapsed = time.monotonic() - start
        logging.info("poll_state #%d: %d ok / %d err in %.1fs",
                     self.metrics["state_polls"], ok, err, elapsed)
        if elapsed > POLL_STATE_SEC * 0.8:
            logging.warning("poll cycle used %.0f%% of budget — consider sharding",
                            100 * elapsed / POLL_STATE_SEC)

    async def _poll_state_one(self, vno: str) -> bool:
        try:
            gps_resp, can_resp = await asyncio.gather(
                self.post("getlastgpsstatus", {"vehicleno": vno}),
                self.post("getlatestcan",     {"vehicleno": vno}),
            )
            # Intellicar occasionally returns {status:SUCCESS, data:null} under
            # concurrent load even when a subsequent isolated call succeeds.
            # One short retry recovers the majority of these.
            if gps_resp.get("status") == "SUCCESS" and gps_resp.get("data") is None:
                await asyncio.sleep(0.25)
                gps_resp = await self.post("getlastgpsstatus", {"vehicleno": vno})
            if can_resp.get("status") == "SUCCESS" and can_resp.get("data") is None:
                await asyncio.sleep(0.25)
                can_resp = await self.post("getlatestcan", {"vehicleno": vno})

            gps = gps_resp.get("data") if gps_resp.get("status") == "SUCCESS" else None
            can = can_resp.get("data") if can_resp.get("status") == "SUCCESS" else None

            now = utc_now()
            # Intellicar GPS: `commtime` is the device-reported timestamp in epoch ms.
            # CRITICAL: gps_t must be None when gps is absent — otherwise it silently
            # inherits `now` (via ts_to_dt's default), which makes `online` True and
            # writes a bogus last_gps_at.
            gps_t = ts_to_dt(gps.get("commtime")) if gps else None
            # Intellicar CAN: each field is {value, timestamp}. Use the max
            # per-field timestamp as the frame time so we capture the newest signal.
            # Like gps_t, stays None when can is absent.
            can_t = None
            if can:
                try:
                    can_t = ts_to_dt(max(
                        (v.get("timestamp") for v in can.values()
                         if isinstance(v, dict) and v.get("timestamp")),
                        default=None,
                    ), now)
                except Exception:
                    can_t = now

            # Pull live BMS fields out of the CAN dict.
            def cv(key):
                v = can.get(key) if can else None
                return v.get("value") if isinstance(v, dict) else None

            soc       = cv("soc")
            soh       = cv("soh")
            pack_v    = cv("battery_voltage")
            pack_i    = cv("current")
            pack_temp = cv("battery_temp")
            allow_chg = cv("allow_charging")
            charging  = bool(allow_chg) if allow_chg is not None else None

            # ---- Redis: last-known state (merged GPS + CAN) ----
            await self.redis.set(
                f"state:{vno}",
                json.dumps({
                    "vehicleno": vno,
                    "updated_at": now.isoformat(),
                    "gps": gps,
                    "can": can,
                }),
                ex=3600,
            )

            # ---- Postgres ----
            async with self.pg.acquire() as conn:
                async with conn.transaction():
                    # GPS is sourced from the live snapshot here, NOT from
                    # getgpshistory: that endpoint returns an empty data[] on this
                    # account (verified against a vehicle whose getbatterymetricshistory
                    # was full), so it cannot feed the GPS time-series. getlastgpsstatus
                    # is the only working GPS feed. NOTE: telemetry_gps has no unique
                    # (time, vehicleno) constraint, so re-inserting the same latest
                    # frame duplicates rows — pre-existing behaviour, to be cleaned up
                    # by the one-time dedupe + UNIQUE index follow-up.
                    if gps:
                        await conn.execute(
                            """INSERT INTO telemetry_gps
                               (time, vehicleno, lat, lon, speed_kph, heading,
                                ignition, gps_fix, ext_voltage)
                               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                               ON CONFLICT DO NOTHING""",
                            gps_t, vno,
                            gps.get("lat"), gps.get("lng"),
                            gps.get("speed"), gps.get("heading"),
                            to_bool(gps.get("ignstatus")),
                            ((gps.get("fixquality") or 0) > 0),
                            gps.get("vehbattery"),
                        )
                    if can:
                        await conn.execute(
                            "INSERT INTO telemetry_can (time, vehicleno, payload) "
                            "VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                            can_t, vno, json.dumps(can))

                    # Initial online for the INSERT path (first time we see this
                    # vehicle): only this-cycle's gps_t is available. The UPDATE
                    # path overrides this from the persisted last_gps_at — see
                    # the SQL below — so transient nulls don't flap us offline.
                    initial_online = (
                        (now - gps_t).total_seconds() < OFFLINE_SEC if gps_t else False
                    )

                    new_online = await conn.fetchval(
                        f"""INSERT INTO vehicle_state
                           (vehicleno, last_seen, last_gps_at, last_battery_at,
                            lat, lon, speed_kph, heading, ignition, gps_fix,
                            soc_pct, soh_pct, pack_voltage, pack_current, pack_temp_c,
                            charging, online, updated_at)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,NOW())
                           ON CONFLICT (vehicleno) DO UPDATE SET
                             last_seen       = EXCLUDED.last_seen,
                             last_gps_at     = COALESCE(EXCLUDED.last_gps_at,     vehicle_state.last_gps_at),
                             last_battery_at = COALESCE(EXCLUDED.last_battery_at, vehicle_state.last_battery_at),
                             lat             = COALESCE(EXCLUDED.lat,             vehicle_state.lat),
                             lon             = COALESCE(EXCLUDED.lon,             vehicle_state.lon),
                             speed_kph       = COALESCE(EXCLUDED.speed_kph,       vehicle_state.speed_kph),
                             heading         = COALESCE(EXCLUDED.heading,         vehicle_state.heading),
                             ignition        = COALESCE(EXCLUDED.ignition,        vehicle_state.ignition),
                             gps_fix         = COALESCE(EXCLUDED.gps_fix,         vehicle_state.gps_fix),
                             soc_pct         = COALESCE(EXCLUDED.soc_pct,         vehicle_state.soc_pct),
                             soh_pct         = COALESCE(EXCLUDED.soh_pct,         vehicle_state.soh_pct),
                             pack_voltage    = COALESCE(EXCLUDED.pack_voltage,    vehicle_state.pack_voltage),
                             pack_current    = COALESCE(EXCLUDED.pack_current,    vehicle_state.pack_current),
                             pack_temp_c     = COALESCE(EXCLUDED.pack_temp_c,     vehicle_state.pack_temp_c),
                             charging        = COALESCE(EXCLUDED.charging,        vehicle_state.charging),
                             -- online is derived from the persisted last_gps_at
                             -- (after COALESCE), so a transient Intellicar
                             -- data:null doesn't flap a vehicle offline.
                             online = (
                               COALESCE(EXCLUDED.last_gps_at, vehicle_state.last_gps_at)
                               > NOW() - INTERVAL '{OFFLINE_INTERVAL_SQL}'
                             ),
                             updated_at      = NOW()
                           RETURNING online""",
                        vno, now, gps_t, (can_t if can else None),
                        gps.get("lat") if gps else None,
                        gps.get("lng") if gps else None,
                        gps.get("speed")   if gps else None,
                        gps.get("heading") if gps else None,
                        to_bool(gps.get("ignstatus")) if gps else None,
                        ((gps.get("fixquality") or 0) > 0) if gps else None,
                        soc, soh, pack_v, pack_i, pack_temp, charging, initial_online,
                    )

                    # Live alerts: only "offline" can be evaluated from GPS alone.
                    # Use the value SQL just computed so alerts and the online
                    # flag stay in sync.
                    await self._evaluate_offline(conn, vno, bool(new_online), now)
            return True
        except Exception as e:
            logging.warning("poll_state(%s) failed: %s", vno, e)
            return False

    async def _evaluate_offline(self, conn, vno, online, now):
        if not online:
            existing = await conn.fetchval(
                "SELECT 1 FROM alerts WHERE vehicleno=$1 AND alert_type='offline' "
                "AND resolved_at IS NULL LIMIT 1", vno)
            if not existing:
                # Message reflects the current OFFLINE_SEC threshold so it
                # never goes stale when we tune the constant.
                msg = f"No GPS for >{OFFLINE_SEC // 60} min"
                await conn.execute(
                    "INSERT INTO alerts (time,vehicleno,alert_type,severity,message) "
                    "VALUES ($1,$2,'offline','warn',$3)",
                    now, vno, msg)
        else:
            await conn.execute(
                "UPDATE alerts SET resolved_at=$1 WHERE vehicleno=$2 "
                "AND alert_type='offline' AND resolved_at IS NULL",
                now, vno)

    # =========================================================================
    # poll_recent — every POLL_RECENT_SEC — rolling full-resolution BATTERY history
    # Pulls getbatterymetricshistory over (last_seen, now] per vehicle so
    # telemetry_battery carries true 30-second data within minutes, not once a day.
    # Each window starts strictly after that vehicle's high-water mark, so
    # overlapping runs never duplicate (the hypertable has no unique
    # (time, vehicleno) constraint). RECENT_LOOKBACK_H caps the catch-up so a
    # first run / post-outage gap self-heals without an unbounded pull.
    # GPS is deliberately NOT pulled here: getgpshistory returns an empty data[]
    # on this account, so GPS stays on the poll_state live snapshot.
    # =========================================================================
    async def _high_water_marks(self, table: str) -> dict:
        """Latest ingested `time` per vehicle, for building non-overlapping
        history windows. Bounded to a recent span so the scan stays cheap."""
        async with self.pg.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT vehicleno, max(time) AS hwm
                    FROM {table}
                    WHERE time >= now() - make_interval(hours => $1)
                    GROUP BY vehicleno""",
                RECENT_LOOKBACK_H + 24,
            )
        return {r["vehicleno"]: r["hwm"] for r in rows}

    async def poll_recent(self) -> None:
        if not self.vehicles:
            return
        now   = utc_now()
        floor = now - timedelta(hours=RECENT_LOOKBACK_H)
        end_epoch = int(now.timestamp() * 1000)
        hwm_batt = await self._high_water_marks("telemetry_battery")

        def window_start(hwm):
            start = hwm or floor
            if start < floor:
                start = floor
            # +1 ms: strictly after the last stored row, so we never re-pull and
            # duplicate the boundary sample.
            return int(start.timestamp() * 1000) + 1

        async def one(vno: str):
            bs = window_start(hwm_batt.get(vno))
            if bs >= end_epoch:
                return 0
            try:
                resp = await self.post("getbatterymetricshistory", {
                    "vehicleno": vno, "starttime": bs, "endtime": end_epoch})
                if resp.get("status") == "SUCCESS":
                    return await self._insert_battery_rows(vno, resp.get("data") or [])
            except Exception as e:
                logging.warning("poll_recent battery(%s): %s", vno, e)
            return 0

        start = time.monotonic()
        results = await asyncio.gather(
            *(one(v) for v in self.vehicles), return_exceptions=True)
        count = sum(r for r in results if isinstance(r, int))
        logging.info("poll_recent: +%d battery rows in %.1fs (lookback<=%dh)",
                     count, time.monotonic() - start, RECENT_LOOKBACK_H)

    # =========================================================================
    # poll_daily — once per day at 01:05 UTC — backfill yesterday's history
    # =========================================================================
    async def poll_daily(self) -> None:
        if not self.vehicles:
            return
        end   = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=1)
        # Intellicar history endpoints expect epoch MILLISECONDS
        start_epoch = int(start.timestamp() * 1000)
        end_epoch   = int(end.timestamp() * 1000)

        logging.info("poll_daily start: backfilling %s → %s for %d vehicles",
                     start.isoformat(), end.isoformat(), len(self.vehicles))

        counts = {"fuel": 0, "distance": 0, "info": 0}

        for vno in self.vehicles:
            # Battery + GPS history are now pulled continuously by poll_recent
            # (every POLL_RECENT_SEC over a rolling window), so they are
            # intentionally NOT backfilled here — re-pulling the same window would
            # duplicate rows, since these hypertables have no unique
            # (time, vehicleno) constraint. poll_recent's wide lookback cap covers
            # the same catch-up this daily pass used to provide.

            # 3. Fuel history
            try:
                resp = await self.post("getfuelhistory", {
                    "vehicleno": vno,
                    "starttime": start_epoch,
                    "endtime":   end_epoch,
                })
                if resp.get("status") == "SUCCESS":
                    counts["fuel"] += await self._insert_fuel_rows(vno, resp.get("data") or [])
            except Exception as e:
                logging.warning("getfuelhistory(%s): %s", vno, e)

            # 4. Distance travelled (single number for the day)
            try:
                resp = await self.post("getdistancetravelled", {
                    "vehicleno": vno,
                    "starttime": start_epoch,
                    "endtime":   end_epoch,
                })
                if resp.get("status") == "SUCCESS":
                    d = resp.get("data") or {}
                    if isinstance(d, list) and d:
                        d = d[0]
                    dist = d.get("distance") or d.get("distancekm") or d.get("totaldistance")
                    if dist is not None:
                        async with self.pg.acquire() as conn:
                            await conn.execute(
                                """INSERT INTO distance_rollup
                                   (time, vehicleno, bucket_size, distance_km)
                                   VALUES ($1,$2,'day',$3)
                                   ON CONFLICT (time, vehicleno, bucket_size) DO UPDATE
                                   SET distance_km = EXCLUDED.distance_km""",
                                start, vno, float(dist))
                        counts["distance"] += 1
            except Exception as e:
                logging.warning("getdistancetravelled(%s): %s", vno, e)

        # 5. Refresh static metadata (getvehicleinfo only — getarbidparammap removed)
        for vno in self.vehicles:
            try:
                info = await self.post("getvehicleinfo", {"vehicleno": vno})
                if info.get("status") == "SUCCESS":
                    async with self.pg.acquire() as conn:
                        await conn.execute(
                            "UPDATE vehicles SET info = $2, info_updated = NOW() "
                            "WHERE vehicleno = $1",
                            vno, json.dumps(info.get("data") or {}))
                    counts["info"] += 1
            except Exception as e:
                logging.warning("getvehicleinfo(%s): %s", vno, e)

        logging.info("poll_daily done: fuel=%d rows, distance=%d vehicles, "
                     "info=%d vehicles (battery+gps now via poll_recent)",
                     counts["fuel"], counts["distance"], counts["info"])

    async def _insert_battery_rows(self, vno: str, rows: list) -> int:
        if not rows:
            return 0
        latest_t = None; latest = None
        records = []
        for r in rows:
            t = ts_to_dt(r.get("timestamp") or r.get("time"))
            if t is None:
                continue
            records.append((
                t, vno,
                r.get("soc"), r.get("soh"),
                r.get("voltage") or r.get("packvoltage"),
                r.get("current") or r.get("packcurrent"),
                r.get("temperature") or r.get("packtemp"),
                None, None,
                to_bool(r.get("charging")),
            ))
            if latest_t is None or t > latest_t:
                latest_t, latest = t, r
        async with self.pg.acquire() as conn:
            await conn.executemany(
                """INSERT INTO telemetry_battery
                   (time, vehicleno, soc_pct, soh_pct, pack_voltage, pack_current,
                    pack_temp_c, cell_min_mv, cell_max_mv, charging)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                   ON CONFLICT DO NOTHING""", records)
            # Update vehicle_state with the most recent sample
            if latest:
                await conn.execute(
                    """UPDATE vehicle_state SET
                         last_battery_at = $2,
                         soc_pct       = $3,
                         soh_pct       = $4,
                         pack_voltage  = $5,
                         pack_current  = $6,
                         pack_temp_c   = $7,
                         charging      = $8,
                         updated_at    = NOW()
                       WHERE vehicleno = $1""",
                    vno, latest_t,
                    latest.get("soc"), latest.get("soh"),
                    latest.get("voltage") or latest.get("packvoltage"),
                    latest.get("current") or latest.get("packcurrent"),
                    latest.get("temperature") or latest.get("packtemp"),
                    to_bool(latest.get("charging")))
        return len(records)

    async def _insert_gps_rows(self, vno: str, rows: list) -> int:
        if not rows:
            return 0
        records = []
        for r in rows:
            t = ts_to_dt(r.get("gpstime") or r.get("timestamp"))
            if t is None:
                continue
            records.append((
                t, vno,
                r.get("latitude"), r.get("longitude"),
                r.get("speed"), r.get("heading"),
                to_bool(r.get("ignition")), to_bool(r.get("gpsfix")),
                r.get("extvoltage"),
            ))
        async with self.pg.acquire() as conn:
            await conn.executemany(
                """INSERT INTO telemetry_gps
                   (time, vehicleno, lat, lon, speed_kph, heading,
                    ignition, gps_fix, ext_voltage)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                   ON CONFLICT DO NOTHING""", records)
        return len(records)

    async def _insert_fuel_rows(self, vno: str, rows: list) -> int:
        if not rows:
            return 0
        latest_t = None; latest = None
        records = []
        for r in rows:
            t = ts_to_dt(r.get("timestamp") or r.get("time"))
            if t is None:
                continue
            records.append((
                t, vno,
                r.get("fuelpct") or r.get("fuel"),
                r.get("range")   or r.get("rangekm"),
                r.get("fuelvolts"),
                r.get("consumption"),
            ))
            if latest_t is None or t > latest_t:
                latest_t, latest = t, r
        async with self.pg.acquire() as conn:
            await conn.executemany(
                """INSERT INTO telemetry_fuel
                   (time, vehicleno, fuel_pct, range_km, fuel_volts, consumption)
                   VALUES ($1,$2,$3,$4,$5,$6)
                   ON CONFLICT DO NOTHING""", records)
            if latest:
                await conn.execute(
                    """UPDATE vehicle_state SET
                         last_fuel_at = $2,
                         fuel_pct     = $3,
                         range_km     = $4,
                         updated_at   = NOW()
                       WHERE vehicleno = $1""",
                    vno, latest_t,
                    latest.get("fuelpct") or latest.get("fuel"),
                    latest.get("range")   or latest.get("rangekm"))
        return len(records)


# =========================================================================
# Entrypoint
# =========================================================================
async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    poller = Poller()
    await poller.setup()

    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(poller.poll_state,     "interval", seconds=POLL_STATE_SEC,
                  id="poll_state", max_instances=1, coalesce=True,
                  next_run_time=datetime.now())
    sched.add_job(poller.poll_recent,    "interval", seconds=POLL_RECENT_SEC,
                  id="poll_recent", max_instances=1, coalesce=True,
                  next_run_time=datetime.now())
    sched.add_job(poller.refresh_roster, "interval", seconds=ROSTER_REFRESH_SEC,
                  id="refresh_roster")
    sched.add_job(poller.poll_daily,     "cron",
                  hour=DAILY_REFRESH_HOUR, minute=DAILY_REFRESH_MIN,
                  id="poll_daily")
    sched.start()

    logging.info("Scheduler started (state=%ds, recent=%ds, roster=1h, daily=01:05 UTC).",
                 POLL_STATE_SEC, POLL_RECENT_SEC)
    try:
        await asyncio.Event().wait()
    finally:
        await poller.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
