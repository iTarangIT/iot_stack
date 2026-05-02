#!/usr/bin/env python3
"""Headed browser smoke test for https://iot-status.itarang.com/.

Logs in over HTTP basic auth, waits for the page's JS to fetch
/api/status, then validates:

  - every container is `running` with low restart count
  - postgres / redis / risk-sandbox health probes return ok
  - postgres data freshness timestamps are within thresholds
  - the architecture diagram has no `bad` nodes

Prints a colored pass/fail report and exits 0 on all green, 1 otherwise.

Setup (once):
    python3 -m venv .venv && source .venv/bin/activate
    pip install "playwright==1.49.1"
    playwright install chromium

Run:
    python scripts/check_status_page.py
    # or with overrides:
    python scripts/check_status_page.py \\
        --url https://iot-status.itarang.com/ \\
        --user admin --password 'password' \\
        --headed   # default; pass --no-headed for CI
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Freshness staleness thresholds — keep loose; we only flag genuinely broken
# pipelines, not normal jitter. Poller runs every 30s, aggregator every 60-300s.
FRESHNESS_THRESHOLDS_SEC = {
    "vehicle_state": 5 * 60,
    "telemetry_gps": 5 * 60,
    "telemetry_battery": 10 * 60,
    "alerts (open)": 24 * 3600,           # alerts are sparse; only flag if a full day silent
    "dashboard_nbfc_loans_with_iot": 30 * 60,
}

GREEN = "\033[32m"; RED = "\033[31m"; YEL = "\033[33m"; DIM = "\033[2m"; BOLD = "\033[1m"; NC = "\033[0m"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        mark = f"{GREEN}✔{NC}" if self.ok else f"{RED}✘{NC}"
        return f"  {mark}  {self.name:<48s}  {DIM}{self.detail}{NC}"


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Postgres MAX(time)::text and the API both produce naive or +00 strings.
        s = s.replace(" ", "T")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def evaluate(status: dict[str, Any], diagram_states: dict[str, str]) -> list[CheckResult]:
    results: list[CheckResult] = []

    # 1. Containers
    containers = status.get("containers") or []
    if not containers:
        results.append(CheckResult("containers list non-empty", False, "API returned no containers"))
    for c in containers:
        name = c.get("name", "?")
        state = c.get("state")
        restarts = c.get("restart_count", 0)
        ok = state == "running" and restarts <= 5
        detail = f"state={state} restarts={restarts}"
        results.append(CheckResult(f"container {name}", ok, detail))

    # 2. Health probes
    for svc in ("postgres", "redis", "risk_sandbox"):
        h = (status.get("health") or {}).get(svc) or {}
        results.append(CheckResult(
            f"health probe {svc}",
            bool(h.get("ok")),
            (h.get("detail") or "")[:80],
        ))

    # 3. Freshness
    now = datetime.now(timezone.utc)
    for row in status.get("freshness") or []:
        name = row.get("name", "?")
        last_iso = row.get("last")
        last = _parse_iso(last_iso)
        threshold = FRESHNESS_THRESHOLDS_SEC.get(name, 60 * 60)
        if not last:
            results.append(CheckResult(f"freshness {name}", False, f"unparseable timestamp: {last_iso!r}"))
            continue
        age_sec = (now - last).total_seconds()
        ok = age_sec <= threshold
        results.append(CheckResult(
            f"freshness {name}",
            ok,
            f"last={last_iso} age={int(age_sec)}s threshold={threshold}s",
        ))

    # 4. Diagram
    bad_nodes = [n for n, s in diagram_states.items() if s == "bad"]
    results.append(CheckResult(
        "architecture diagram",
        not bad_nodes,
        f"{len(diagram_states)} nodes scanned" if not bad_nodes else f"red nodes: {', '.join(bad_nodes)}",
    ))

    # 5. Intellicar endpoints — every endpoint must have a last_run_utc and
    # not be older than its declared threshold. Prefer the server-provided
    # stale_threshold_sec since some endpoints (e.g. getlatestcan) are
    # bursty by design and need a looser bound than their 30s cadence.
    SCHEDULE_THRESHOLDS_SEC = {
        "30s":   5 * 60,
        "1h":    2 * 3600,
        "daily": 26 * 3600,
    }
    for ep in status.get("intellicar") or []:
        name = ep.get("endpoint", "?")
        if name == "_db_error":
            results.append(CheckResult("intellicar endpoints (db)", False, ep.get("error") or "db error"))
            continue
        sched = ep.get("schedule") or ""
        threshold = ep.get("stale_threshold_sec")
        if threshold is None:
            threshold = next((s for k, s in SCHEDULE_THRESHOLDS_SEC.items() if k in sched), 24 * 3600)
        age = ep.get("age_sec")
        if ep.get("error"):
            results.append(CheckResult(f"endpoint {name}", False, f"error: {ep['error'][:80]}"))
        elif age is None:
            results.append(CheckResult(f"endpoint {name}", False, "never run"))
        else:
            ok = age <= threshold
            results.append(CheckResult(
                f"endpoint {name}",
                ok,
                f"last={ep.get('last_run_ist')} age={age}s threshold={threshold}s",
            ))

    return results


def main() -> int:
    p = argparse.ArgumentParser(description="Headed browser check for iot-status.itarang.com")
    p.add_argument("--url",      default="https://iot-status.itarang.com/")
    p.add_argument("--user",     default="admin")
    p.add_argument("--password", default="password")
    p.add_argument("--headed",     dest="headed", action="store_true",  default=True)
    p.add_argument("--no-headed",  dest="headed", action="store_false")
    p.add_argument("--timeout-ms", type=int, default=30_000)
    args = p.parse_args()

    api_url = args.url.rstrip("/") + "/api/status"

    print(f"{BOLD}Itarang IoT status-page check{NC}  {DIM}{datetime.now().isoformat(timespec='seconds')}{NC}")
    print(f"  url:  {args.url}")
    print(f"  user: {args.user}")
    print()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            http_credentials={"username": args.user, "password": args.password},
            ignore_https_errors=False,
        )
        page = context.new_page()

        # Surface page-side errors so we don't silently miss them.
        console_errors: list[str] = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))

        try:
            page.goto(args.url, timeout=args.timeout_ms, wait_until="domcontentloaded")
        except PWTimeout:
            print(f"{RED}✘ page navigation timed out after {args.timeout_ms}ms{NC}")
            browser.close()
            return 1
        except Exception as e:
            print(f"{RED}✘ page navigation failed: {e}{NC}")
            browser.close()
            return 1

        # Wait until the JS poll has populated the "updated …" header text.
        try:
            page.wait_for_function(
                """() => {
                    const el = document.getElementById('updated');
                    return el && el.textContent && el.textContent.startsWith('updated ');
                }""",
                timeout=args.timeout_ms,
            )
        except PWTimeout:
            err = console_errors[-1] if console_errors else "no console errors captured"
            print(f"{RED}✘ status page never finished its first poll within {args.timeout_ms}ms{NC}")
            print(f"  last page error: {DIM}{err}{NC}")
            browser.close()
            return 1

        # Pull the JSON straight from the API using the same authenticated context.
        # Single source of truth instead of scraping the DOM we just rendered.
        api_resp = context.request.get(api_url)
        if not api_resp.ok:
            print(f"{RED}✘ /api/status returned HTTP {api_resp.status}{NC}")
            browser.close()
            return 1
        try:
            status = api_resp.json()
        except Exception as e:
            print(f"{RED}✘ /api/status response was not JSON: {e}{NC}")
            browser.close()
            return 1

        # Pull the actual classes the diagram nodes ended up with — verifies the
        # rendering layer agrees with the API payload.
        diagram_states: dict[str, str] = page.evaluate(
            """() => {
                const out = {};
                document.querySelectorAll('.arch .node').forEach(n => {
                    const id = n.id || '?';
                    const cls = ['ok','bad','warn'].find(c => n.classList.contains(c)) || 'idle';
                    out[id] = cls;
                });
                return out;
            }"""
        )

        results = evaluate(status, diagram_states)
        any_failed = any(not r.ok for r in results)

        # Optional: capture a screenshot for the report when something is wrong.
        if any_failed:
            shot = "iot-status-failure.png"
            page.screenshot(path=shot, full_page=True)
            print(f"  {DIM}screenshot saved → {shot}{NC}")

        browser.close()

    # ── report ──────────────────────────────────────────────────────────
    print(f"{BOLD}Checks{NC}")
    for r in results:
        print(r.line())

    if console_errors:
        print()
        print(f"{YEL}Page console errors ({len(console_errors)}):{NC}")
        for line in console_errors[-5:]:
            print(f"  {DIM}{line[:200]}{NC}")

    print()
    if any_failed:
        n_fail = sum(1 for r in results if not r.ok)
        print(f"{RED}{BOLD}✘ NOT GOOD{NC} — {n_fail}/{len(results)} checks failed.")
        return 1

    summary = {
        "containers": len([r for r in results if r.name.startswith("container ")]),
        "freshness":  len([r for r in results if r.name.startswith("freshness ")]),
        "host":       (status.get("host") or {}).get("hostname"),
    }
    print(f"{GREEN}{BOLD}✔ ALL GOOD{NC} — {len(results)} checks passed.  "
          f"{DIM}{json.dumps(summary)}{NC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
