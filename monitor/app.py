"""Monitor service — JSON + HTML status page for the IoT stack.

Mirrors `scripts/status.sh`: container state, health pings (postgres /
redis / risk-sandbox), and postgres data freshness. Behind HTTP basic
auth; bound to 127.0.0.1 by compose, fronted by Caddy on the host.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import docker
import httpx
import psycopg
import redis
from docker.errors import NotFound as DockerNotFound
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

PG_DSN = os.environ["PG_DSN"]
REDIS_URL = os.environ["REDIS_URL"]
RISK_SANDBOX_URL = os.environ.get("RISK_SANDBOX_URL", "http://risk-sandbox:8000")
MONITOR_USER = os.environ["MONITOR_USER"]
MONITOR_PASS = os.environ["MONITOR_PASS"]

# Compose container names we care about, in display order.
SERVICES = [
    "itarang_postgres",
    "itarang_redis",
    "itarang_poller",
    "itarang_aggregator",
    "itarang_risk_sandbox",
]

# Same SELECT block as scripts/status.sh:26-32. Kept identical so terminal
# and HTML views can't silently disagree on what "fresh" means.
FRESHNESS_SQL = """
SELECT 'vehicle_state'                AS name, COUNT(*)::bigint AS rows, MAX(updated_at)::text   AS last FROM vehicle_state UNION ALL
SELECT 'telemetry_gps'                       , COUNT(*)::bigint              , MAX(time)::text             FROM telemetry_gps UNION ALL
SELECT 'telemetry_battery'                   , COUNT(*)::bigint              , MAX(time)::text             FROM telemetry_battery UNION ALL
SELECT 'alerts (open)'                       , COUNT(*) FILTER (WHERE resolved_at IS NULL)::bigint, MAX(time)::text FROM alerts UNION ALL
SELECT 'dashboard_nbfc_loans_with_iot'       , COUNT(*)::bigint              , MAX(refreshed_at)::text     FROM dashboard_nbfc_loans_with_iot
"""

app = FastAPI(title="itarang-iot-monitor", docs_url=None, redoc_url=None)
security = HTTPBasic()
docker_client = docker.from_env()
INDEX_HTML = (Path(__file__).parent / "index.html").read_text()


def require_auth(creds: HTTPBasicCredentials = Depends(security)) -> None:
    user_ok = secrets.compare_digest(creds.username, MONITOR_USER)
    pass_ok = secrets.compare_digest(creds.password, MONITOR_PASS)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bad credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def _container_info(name: str) -> dict[str, Any]:
    try:
        c = docker_client.containers.get(name)
    except DockerNotFound:
        return {"name": name, "state": "missing", "status": "not found", "started_at": None, "restart_count": 0, "logs": ""}
    attrs = c.attrs
    state = attrs.get("State", {})
    try:
        logs = c.logs(tail=10, timestamps=False).decode("utf-8", errors="replace")
    except Exception as e:
        logs = f"<failed to read logs: {e}>"
    return {
        "name": name,
        "state": state.get("Status", "unknown"),
        "status": "healthy" if state.get("Health", {}).get("Status") == "healthy"
                  else state.get("Health", {}).get("Status") or state.get("Status", "unknown"),
        "started_at": state.get("StartedAt"),
        "restart_count": attrs.get("RestartCount", 0),
        "logs": logs,
    }


def _check_postgres() -> dict[str, Any]:
    try:
        with psycopg.connect(PG_DSN, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return {"ok": True, "detail": "pg_isready"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def _check_redis() -> dict[str, Any]:
    try:
        r = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
        pong = r.ping()
        keys = r.dbsize()
        info = r.info(section="memory")
        return {"ok": bool(pong), "detail": f"keys={keys} mem={info.get('used_memory_human', '?')}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def _check_risk_sandbox() -> dict[str, Any]:
    try:
        r = httpx.get(f"{RISK_SANDBOX_URL}/health", timeout=2.0)
        return {"ok": r.status_code == 200, "detail": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def _freshness() -> list[dict[str, Any]]:
    try:
        with psycopg.connect(PG_DSN, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(FRESHNESS_SQL)
                return [
                    {"name": row[0], "rows": int(row[1]), "last": row[2]}
                    for row in cur.fetchall()
                ]
    except Exception as e:
        return [{"name": "<query failed>", "rows": 0, "last": str(e)}]


def _host() -> dict[str, Any]:
    # Disk for /, mem from /proc/meminfo. Load from /proc/loadavg.
    du = shutil.disk_usage("/")
    try:
        meminfo = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            meminfo[k.strip()] = v.strip().split()[0]
        mem_total_kb = int(meminfo.get("MemTotal", "0"))
        mem_avail_kb = int(meminfo.get("MemAvailable", "0"))
        mem_used_kb = max(mem_total_kb - mem_avail_kb, 0)
    except Exception:
        mem_total_kb = mem_used_kb = 0
    try:
        load = Path("/proc/loadavg").read_text().split()[:3]
    except Exception:
        load = ["?", "?", "?"]
    return {
        "hostname": socket.gethostname(),
        "disk_used_gb": round(du.used / 1e9, 1),
        "disk_total_gb": round(du.total / 1e9, 1),
        "disk_pct": round(du.used / du.total * 100, 1) if du.total else 0,
        "mem_used_mb": round(mem_used_kb / 1024, 0),
        "mem_total_mb": round(mem_total_kb / 1024, 0),
        "load": load,
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"ok": "yes"}


@app.get("/", response_class=HTMLResponse)
def index(_: None = Depends(require_auth)) -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.get("/api/status")
def api_status(_: None = Depends(require_auth)) -> JSONResponse:
    return JSONResponse({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "containers": [_container_info(name) for name in SERVICES],
        "health": {
            "postgres": _check_postgres(),
            "redis": _check_redis(),
            "risk_sandbox": _check_risk_sandbox(),
        },
        "freshness": _freshness(),
        "host": _host(),
    })
