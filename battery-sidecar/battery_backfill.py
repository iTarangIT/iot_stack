"""
Itarang · battery 30s backfill sidecar
======================================
Fills today's 30-second battery history into the PRODUCTION telemetry DB
(itarang @ 127.0.0.1:5544, the RDS the CEO/dealer dashboards read) so battery
health shows live instead of stalling at the once-a-day backfill.

Battery-only by design: it calls ONLY getbatterymetricshistory and writes ONLY
telemetry_battery. It never touches gps/can/vehicle_state, so it cannot conflict
with the existing (off-box) poller that owns those. Each vehicle's window starts
strictly after its own high-water mark, so overlapping runs don't duplicate.

Env:
  PG_DSN                target DB (the RDS, via the 5544 tunnel)
  INTELLICAR_USERNAME / INTELLICAR_PASSWORD / INTELLICAR_BASE
  BATTERY_POLL_SEC      cycle interval  (default 300)
  BATTERY_LOOKBACK_H    catch-up cap    (default 48)
  CONCURRENCY           parallel API calls (default 20)
"""
import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

import aiohttp
import asyncpg

INTELLICAR_BASE = os.environ.get("INTELLICAR_BASE", "https://apiplatform.intellicar.in/api/standard")
USERNAME        = os.environ["INTELLICAR_USERNAME"]
PASSWORD        = os.environ["INTELLICAR_PASSWORD"]
PG_DSN          = os.environ["PG_DSN"]
POLL_SEC        = int(os.environ.get("BATTERY_POLL_SEC", "300"))
LOOKBACK_H      = int(os.environ.get("BATTERY_LOOKBACK_H", "48"))
CONCURRENCY     = int(os.environ.get("CONCURRENCY", "20"))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ts_to_dt(ms):
    if ms is None:
        return None
    try:
        ms = int(ms)
    except (ValueError, TypeError):
        return None
    # values above ~year 2200 in seconds are already milliseconds
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


class Sidecar:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self.pg: asyncpg.Pool | None = None
        self.token: str | None = None
        self.token_exp: datetime = utc_now()
        self.sem = asyncio.Semaphore(CONCURRENCY)

    async def setup(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
            headers={"User-Agent": "itarang-battery-sidecar/1.0"},
        )
        self.pg = await asyncpg.create_pool(PG_DSN, min_size=2, max_size=10)
        await self.refresh_token()

    async def teardown(self) -> None:
        try:
            if self.session:
                await self.session.close()
        except Exception:
            pass
        try:
            if self.pg:
                await self.pg.close()
        except Exception:
            pass
        self.session = None
        self.pg = None

    async def refresh_token(self) -> None:
        async with self.session.post(
            f"{INTELLICAR_BASE}/gettoken",
            json={"username": USERNAME, "password": PASSWORD},
        ) as r:
            body = await r.json()
        if body.get("status") != "SUCCESS":
            raise RuntimeError(f"Auth failed: {body}")
        self.token = body["data"]["token"]
        self.token_exp = utc_now() + timedelta(days=10)

    async def post(self, path: str, payload: dict, _retried: bool = False) -> dict:
        if utc_now() >= self.token_exp:
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

    async def vehicles(self) -> list[str]:
        # roster comes from the DB the other poller already maintains
        async with self.pg.acquire() as conn:
            rows = await conn.fetch(
                "SELECT vehicleno FROM vehicle_state WHERE vehicleno IS NOT NULL")
        return [r["vehicleno"] for r in rows]

    async def high_water_marks(self) -> dict:
        async with self.pg.acquire() as conn:
            rows = await conn.fetch(
                "SELECT vehicleno, max(time) AS hwm FROM telemetry_battery "
                "WHERE time >= now() - make_interval(hours => $1) GROUP BY vehicleno",
                LOOKBACK_H + 24,
            )
        return {r["vehicleno"]: r["hwm"] for r in rows}

    async def insert_battery(self, vno: str, rows: list) -> int:
        records = []
        for r in rows:
            t = ts_to_dt(r.get("timestamp") or r.get("time"))
            if t is None:
                continue
            records.append((
                t, vno,
                r.get("soc"), r.get("soh"),
                r.get("battery_voltage") or r.get("voltage") or r.get("packvoltage"),
                r.get("current") or r.get("packcurrent"),
                r.get("battery_temp") or r.get("temperature") or r.get("packtemp"),
                None, None,
                to_bool(r.get("charging")),
            ))
        if not records:
            return 0
        async with self.pg.acquire() as conn:
            await conn.executemany(
                """INSERT INTO telemetry_battery
                   (time, vehicleno, soc_pct, soh_pct, pack_voltage, pack_current,
                    pack_temp_c, cell_min_mv, cell_max_mv, charging)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                   ON CONFLICT DO NOTHING""",
                records,
            )
        return len(records)

    async def cycle(self) -> None:
        now = utc_now()
        floor = now - timedelta(hours=LOOKBACK_H)
        end_epoch = int(now.timestamp() * 1000)
        vlist = await self.vehicles()
        if not vlist:
            print("[battery-sidecar] no vehicles in vehicle_state — skipping", flush=True)
            return
        hwm = await self.high_water_marks()

        def start_ms(v):
            s = hwm.get(v) or floor
            if s < floor:
                s = floor
            # +1 ms: strictly after the last stored row, so we never re-pull it
            return int(s.timestamp() * 1000) + 1

        async def one(v: str):
            bs = start_ms(v)
            if bs >= end_epoch:
                return 0
            resp = await self.post("getbatterymetricshistory",
                                   {"vehicleno": v, "starttime": bs, "endtime": end_epoch})
            if resp.get("status") == "SUCCESS":
                try:
                    return await self.insert_battery(v, resp.get("data") or [])
                except Exception as e:
                    print(f"[battery-sidecar] insert {v}: {e}", flush=True)
                    return 0
            return 0

        t0 = time.monotonic()
        results = await asyncio.gather(*(one(v) for v in vlist), return_exceptions=True)
        n = sum(x for x in results if isinstance(x, int))
        errs = sum(1 for x in results if isinstance(x, Exception))
        print(f"[battery-sidecar] +{n} battery rows / {len(vlist)} vehicles "
              f"({errs} errs) in {time.monotonic() - t0:.1f}s", flush=True)

    async def run(self) -> None:
        while True:
            try:
                await self.setup()
                break
            except Exception as e:
                print(f"[battery-sidecar] setup failed ({e}); retry in 15s", flush=True)
                await self.teardown()
                await asyncio.sleep(15)
        target = PG_DSN.split("@")[-1]
        print(f"[battery-sidecar] up — poll={POLL_SEC}s lookback<={LOOKBACK_H}h target={target}", flush=True)
        while True:
            try:
                await self.cycle()
            except Exception as e:
                print(f"[battery-sidecar] cycle error: {e}", flush=True)
            await asyncio.sleep(POLL_SEC)


if __name__ == "__main__":
    asyncio.run(Sidecar().run())
