# Battery 30-second sidecar

Writes **30-second** battery telemetry into the production RDS (`itarang`) that
the CEO / dealer dashboards read, so battery health is live instead of frozen at
the once-a-day backfill.

## Why this exists

The dashboards read the AWS RDS `itarang` over the box's autossh tunnel on
`127.0.0.1:5544`. That RDS is fed by an **off-box** poller that writes live
gps/can plus a **once-a-day** battery backfill — so `telemetry_battery` there was
~24 h stale. Intellicar's `getbatterymetricshistory` actually holds 30-second
battery data; this sidecar pulls it on a short rolling window and writes it
straight into that RDS.

**Battery-only by design:** it calls only `getbatterymetricshistory` and writes
only `telemetry_battery`. It never touches gps / can / vehicle_state, so it
cannot conflict with the off-box poller that owns those. Each vehicle's window
starts strictly after that vehicle's own high-water mark, so overlapping runs
never duplicate. `BATTERY_LOOKBACK_H` caps the catch-up so it self-heals after an
outage without an unbounded pull.

## Deploy

Runs on the iot_stack host (`72.61.246.37`, `/opt/intellicar`). It reuses the
already-built `intellicar-poller` image (aiohttp + asyncpg) — no separate build —
and mounts this script.

```bash
mkdir -p /opt/intellicar/battery-sidecar
cp battery-sidecar/battery_backfill.py /opt/intellicar/battery-sidecar/

# env: Intellicar creds from the poller .env + the RDS DSN (write-capable user)
SIDE=/opt/intellicar/battery-sidecar/sidecar.env
: > "$SIDE"; chmod 600 "$SIDE"
for k in INTELLICAR_USERNAME INTELLICAR_PASSWORD INTELLICAR_BASE; do
  v=$(grep -E "^$k=" /opt/intellicar/.env | head -1 | cut -d= -f2- | tr -d '"')
  [ -n "$v" ] && printf '%s=%s\n' "$k" "$v" >> "$SIDE"
done
RDS_DSN=$(grep -E '^IOT_DATABASE_URL=' /home/itarang-crm/htdocs/crm.itarang.com/current/.env | cut -d= -f2- | tr -d '"')
printf 'PG_DSN=%s\n' "$RDS_DSN" >> "$SIDE"

docker rm -f itarang_battery_sidecar 2>/dev/null
docker run -d --name itarang_battery_sidecar --restart unless-stopped --network host \
  --env-file "$SIDE" \
  -v /opt/intellicar/battery-sidecar/battery_backfill.py:/battery_backfill.py:ro \
  --entrypoint python intellicar-poller -u /battery_backfill.py
```

`--network host` is required so the container can reach the RDS tunnel on
`127.0.0.1:5544`. `--restart unless-stopped` keeps it alive across reboots.

## Environment

| var | default | meaning |
|-----|---------|---------|
| `PG_DSN` | *(required)* | target DB — the RDS, via the `5544` tunnel |
| `INTELLICAR_USERNAME` / `INTELLICAR_PASSWORD` | *(required)* | Intellicar API creds |
| `INTELLICAR_BASE` | `https://apiplatform.intellicar.in/api/standard` | API base |
| `BATTERY_POLL_SEC` | `300` | cycle interval (seconds) |
| `BATTERY_LOOKBACK_H` | `48` | catch-up / outage self-heal cap (hours) |
| `CONCURRENCY` | `20` | parallel Intellicar API calls |

## Verify

```bash
docker logs --tail 5 itarang_battery_sidecar
# [battery-sidecar] up — poll=300s lookback<=48h target=127.0.0.1:5544/itarang
# [battery-sidecar] +N battery rows / 316 vehicles (0 errs) in X.Xs
```

From the DB side (pgAdmin on `itarang`): `SELECT max(time) FROM telemetry_battery`
should be within a cycle of `now()`, and the per-vehicle median gap over the last
2 h should be ~30 s.

## Remove

```bash
docker rm -f itarang_battery_sidecar
```

Fully reversible; removing it leaves no trace and telemetry_battery simply
reverts to the off-box poller's daily backfill.

## Long-term

This is a bridge. The clean fix is to run the `poll_recent` battery job (already
in `poller/poll.py`) on the **off-box poller that feeds this RDS**, then retire
this sidecar. This sidecar targets the RDS directly only because that poller is
not reachable from this host.
