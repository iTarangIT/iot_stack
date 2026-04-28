"""
Intellicar Standard API - bulk sample data fetcher
"""
from __future__ import annotations
import json, os, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

BASE = "https://apiplatform.intellicar.in/api/standard"
OUT = Path(__file__).resolve().parent.parent / "sample-data"
OUT.mkdir(parents=True, exist_ok=True)


def load_env():
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    user = os.environ.get("INTELLICAR_USERNAME")
    pw = os.environ.get("INTELLICAR_PASSWORD")
    if not user or not pw:
        sys.exit("ERROR: set INTELLICAR_USERNAME and INTELLICAR_PASSWORD env vars or put them in .env")
    return user, pw


def post(path, payload, timeout=30):
    url = f"{BASE}/{path}"
    r = requests.post(url, json=payload, timeout=timeout)
    try:
        return r.json()
    except ValueError:
        return {"_raw_text": r.text, "_http_status": r.status_code}


def save(name, body):
    p = OUT / f"{name}.json"
    p.write_text(json.dumps(body, indent=2, ensure_ascii=False))
    status = body.get("status") if isinstance(body, dict) else "?"
    count = None
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        count = len(body["data"])
    tag = f"[{status}]" + (f" n={count}" if count is not None else "")
    print(f"  saved {name}.json {tag}")


def ms(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def main():
    user, pw = load_env()
    print(f"\nAuthenticating as {user} ...")
    tok_resp = post("gettoken", {"username": user, "password": pw})
    save("01_gettoken", tok_resp)
    if tok_resp.get("status") != "SUCCESS":
        sys.exit(f"Auth failed: {tok_resp}")
    token = tok_resp["data"]["token"]
    print(f"Token acquired (len={len(token)})\n")

    print("Listing vehicles ...")
    lv = post("listvehicles", {"token": token})
    save("02_listvehicles", lv)
    vehicles = [v["vehicleno"] for v in (lv.get("data") or []) if v.get("vehicleno")]
    if not vehicles:
        print("No vehicles accessible - continuing with admin-only endpoints")
        sample_vehicle = None
    else:
        sample_vehicle = vehicles[0]
        print(f"Found {len(vehicles)} vehicles - using {sample_vehicle} for per-vehicle samples\n")

    save("03_listusers", post("listusers", {"token": token}))
    save("04_listvehicledevicemapping", post("listvehicledevicemapping", {"token": token}))

    if not sample_vehicle:
        print("\nDone (admin endpoints only).")
        return

    vno = sample_vehicle
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=1)
    week_start = now - timedelta(days=7)

    save("05_getvehicleinfo", post("getvehicleinfo", {"token": token, "vehicleno": vno}))
    save("06_getdeviceforvehicle", post("getdeviceforvehicle", {"token": token, "vehicleno": vno}))
    save("07_getlastgpsstatus", post("getlastgpsstatus", {"token": token, "vehicleno": vno}))
    save("08_getgpshistory", post("getgpshistory", {"token": token, "vehicleno": vno, "starttime": ms(start), "endtime": ms(now)}))
    save("09_getdistancetravelled", post("getdistancetravelled", {"token": token, "vehicleno": vno, "starttime": ms(start), "endtime": ms(now)}))
    save("10_getlastbatterymetrics", post("getlastbatterymetrics", {"token": token, "vehicleno": vno}))
    save("11_getbatterymetricshistory", post("getbatterymetricshistory", {"token": token, "vehicleno": vno, "starttime": ms(week_start), "endtime": ms(now)}))
    save("12_getlatestcan", post("getlatestcan", {"token": token, "vehicleno": vno}))
    save("13_getarbidparammap", post("getarbidparammap", {"token": token, "vehicleno": vno}))
    save("14_getfuelstatus", post("getfuelstatus", {"token": token, "vehicleno": vno}))
    save("15_getfuelhistory", post("getfuelhistory", {"token": token, "vehicleno": vno, "inlitres": False, "starttime": ms(start), "endtime": ms(now)}))

    print("\nBuilding fleet snapshot (last GPS + last battery for every vehicle) ...")
    snapshot = []
    for i, v in enumerate(vehicles, 1):
        gps = post("getlastgpsstatus", {"token": token, "vehicleno": v})
        batt = post("getlastbatterymetrics", {"token": token, "vehicleno": v})
        snapshot.append({"vehicleno": v, "gps": gps.get("data"), "battery": batt.get("data")})
        if i % 50 == 0:
            print(f"  {i}/{len(vehicles)} ...")
        time.sleep(0.05)

    (OUT / "fleet_snapshot.json").write_text(json.dumps({"generated_at": now.isoformat(), "vehicles": snapshot}, indent=2))
    print(f"  fleet_snapshot.json  n={len(snapshot)}")
    print("\nAll sample data saved to:", OUT)


if __name__ == "__main__":
    main()
