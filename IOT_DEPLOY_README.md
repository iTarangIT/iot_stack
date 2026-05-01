# IoT Stack — `iot_stack` repo (github.com/iTarangIT/iot_stack)

Companion to `.github/workflows/deploy-iot-stack.yml`. Read once, set up once, then forget.

## Architecture

```
                Intellicar API
                      │
                      ▼
                 ┌─────────┐
                 │ poller  │  Python · ingest every 30s
                 └────┬────┘
                      ▼
        ┌──────────────────────────────┐
        │ postgres (TimescaleDB)       │
        │  ├── raw IoT tables          │
        │  ├── continuous aggregates   │ ← Timescale auto-refresh
        │  └── dashboard_* tables      │ ← aggregator writes here
        │ + redis (poller cache)       │
        └────────┬─────────────────▲───┘
                 │                 │ writes joined results
                 ▼                 │
        ┌────────────────┐  ┌──────┴────────┐
        │ risk-sandbox   │  │ aggregator    │
        │ FastAPI        │  │ Python cron   │ ◄─── AWS RDS (NBFC loans)
        │ on-demand ML   │  └───────────────┘
        └────────┬───────┘         ▲
                 │                 │
                 └─────────┬───────┘
                           ▼
                  ┌─────────────────────┐
                  │ Next.js CRM         │  reads dashboard_* via IoT bridge,
                  │ (owns IoT views)    │  calls risk-sandbox for on-demand runs
                  └─────────────────────┘
```

The PHP "dashboard" container that used to ship with this stack is **retired**. The IoT dashboard now lives in the Next.js CRM and reads pre-built `dashboard_*` tables.

## Containers

| Service       | Port (host)       | Build source       | What it does                                                  |
| ------------- | ----------------- | ------------------ | ------------------------------------------------------------- |
| `postgres`    | `0.0.0.0:5433`    | timescale/timescaledb:latest-pg16 | TimescaleDB — raw IoT + dashboard_* tables.    |
| `redis`       | `127.0.0.1:6380`  | redis:7-alpine     | Poller hot-state cache.                                       |
| `poller`      | (no port)         | `./poller`         | Polls Intellicar API → writes Timescale + Redis.              |
| `aggregator`  | (no port)         | `./aggregator`     | APScheduler-driven Python jobs → cross-source joins → dashboard_* tables. |
| `risk-sandbox`| `127.0.0.1:8091`  | `./risk-sandbox`   | FastAPI sandbox for LangGraph hypothesis runs from /nbfc/risk. |
| `monitor`     | `127.0.0.1:8092`  | `./monitor`        | HTML/JSON status page (containers, health, freshness). Behind Caddy + basic auth. |

`postgres` is bound to `0.0.0.0:5433` because the Next.js CRM (sandbox + production) connects from outside the VPS. Lock this down with UFW IP allowlist or Tailscale before this is anything other than dev data.

## Required GitHub Secrets

Repo Settings → Secrets and variables → Actions:

| Secret                | Value                                                                |
| --------------------- | -------------------------------------------------------------------- |
| `IOT_VPS_HOST`        | `72.61.246.37` (or whatever IP you SSH to)                          |
| `IOT_VPS_USER`        | `root`                                                               |
| `IOT_VPS_PORT`        | SSH port                                                             |
| `IOT_VPS_SSH_KEY`     | Private key matching `/root/.ssh/authorized_keys`                    |
| `IOT_ENV_FILE_B64`    | `base64 -w0 .env` of the file shown below                            |
| `SLACK_WEBHOOK_URL`   | (optional) incoming-webhook URL                                      |

### `.env` shape

```env
PG_PASSWORD=<long-random>
INTELLICAR_USERNAME=<…>
INTELLICAR_PASSWORD=<…>
# DSN for the read-only NBFC user on AWS RDS — aggregator reads this.
NBFC_RDS_DSN_RO=postgresql://nbfc_ro:<pwd>@<rds-endpoint>:5432/<db>?sslmode=require
# Basic-auth credentials for the HTML status page at iot-status.itarang.com
MONITOR_USER=admin
MONITOR_PASS=<long-random>
```

To turn into a secret value:
```sh
base64 -w0 .env | pbcopy        # macOS
base64 -w0 .env | xclip         # Linux
```

## VPS bootstrap (once, then never again)

The current VPS already has the running stack at `/opt/intellicar/storage/`. We're swapping that working directory to `/opt/intellicar/` (root of repo). One-time migration:

```sh
# As root on the VPS
cd /opt
mv intellicar intellicar.OLD-$(date +%Y%m%d)            # keep as safety net
git clone https://github.com/iTarangIT/iot_stack.git intellicar
cd intellicar

# Recreate .env from your saved copy
cp /opt/intellicar.OLD-*/storage/.env .env
chmod 600 .env
# … edit .env to add NBFC_RDS_DSN_RO if missing

# First boot
docker compose up -d
docker compose ps     # all services should be healthy in <60s

# Once you've confirmed health, the OLD dir can be removed:
#   rm -rf /opt/intellicar.OLD-*
```

After this, every push to `main` (or manual `workflow_dispatch`) triggers `.github/workflows/deploy-iot-stack.yml` which pulls, rebuilds changed images, and recreates only affected containers.

## Migration policy

`schema.sql` and `schema/dashboard_aggregates.sql` are loaded by Postgres **only on first volume init** (via `/docker-entrypoint-initdb.d/`). After the volume exists, those files are ignored.

Additive changes (new columns, tables, indexes) must be applied manually before the code that needs them lands on `main`:

```sh
# Compose your migration
nano /tmp/migration.sql

# Apply inside the running container
docker exec -i itarang_postgres psql -U intellicar -d intellicar < /tmp/migration.sql
```

The "Detect schema changes" step refuses to deploy if `schema.sql`, `schema/*.sql`, or `migrations/*.sql` changed since the last deploy. Process when this trips:

1. Apply the SQL by hand on the VPS (above).
2. Update the repo file to match (so future fresh installs are correct).
3. Trigger a manual `workflow_dispatch` to confirm you've done step 1.

(Mirrors the Drizzle policy in the CRM's `deploy-production.yml`.)

## Adding aggregator jobs

Drop a new `.py` file in `aggregator/jobs/`:

```python
# aggregator/jobs/your_job.py
JOB_NAME = "your_job"
INTERVAL_ENV = "INTERVAL_YOUR_JOB"     # also add to docker-compose.yml env block
DEFAULT_INTERVAL_SECONDS = 600

def run(iot, rds, log) -> int:
    # iot: psycopg connection to local Timescale (write here)
    # rds: psycopg connection to AWS RDS (read-only)
    # log: logger pre-namespaced to your job
    # return: number of rows written (logged into aggregator_runs)
    ...
```

The scheduler auto-discovers it on next deploy.

## Monitoring page

A live HTML status page (containers, health pings, postgres data freshness, host stats) ships as the `monitor` service. It reads the docker socket read-only, queries postgres/redis/risk-sandbox over the internal compose network, and serves both a single-page HTML at `/` and JSON at `/api/status`. Auto-refreshes every 5s.

The container binds `127.0.0.1:8092` only — expose it through Caddy.

**Caddy site (host-side, e.g. `/etc/caddy/Caddyfile.d/iot-status.conf`):**

```
iot-status.itarang.com {
    reverse_proxy 127.0.0.1:8092
}
```

Reload Caddy (`systemctl reload caddy`) and the page is live at `https://iot-status.itarang.com/`. Basic auth is enforced inside the FastAPI app using `MONITOR_USER` / `MONITOR_PASS` from `.env`, so Caddy stays a plain reverse proxy.

If you ever want to point Uptime Kuma at it, hit `/api/status` with the same basic-auth credentials and alert on `health.postgres.ok=false` etc.

## Rollback

Triggered automatically on health-check failure — restores `PREV_SHA`, rebuilds. Manual rollback:

```sh
ssh root@<vps>
cd /opt/intellicar
git log --oneline -10
git reset --hard <good-sha>
docker compose build --pull
docker compose up -d
```

## What this does NOT do

- **Postgres backups.** Set up `pg_dump` + cron + offsite copy separately. The compose volume is `pgdata`; on disaster you need that backup.
- **TLS / public domain.** CloudPanel/Caddy/Nginx in front does that.
- **Secret rotation.** When you rotate `INTELLICAR_*` or `NBFC_RDS_DSN_RO`, regenerate `IOT_ENV_FILE_B64` and update the GitHub secret.
- **Aggregator data-freshness alerts.** Add a Grafana / Uptime-Kuma alert on `MAX(refreshed_at)` for each `dashboard_*` table — that catches "aggregator is up but jobs are silently failing".
- **Lock down postgres :5433.** Currently public. Tighten with UFW or Tailscale ASAP.

## Repo layout

```
iot_stack/
├── .github/workflows/
│   └── deploy-iot-stack.yml
├── docker-compose.yml
├── schema.sql                   ← from VPS (first-init bootstrap)
├── schema/
│   └── dashboard_aggregates.sql ← dashboard_* tables + agg DDL
├── poller/                      ← from VPS
│   ├── Dockerfile
│   ├── poll.py
│   └── requirements.txt
├── aggregator/                  ← NEW (this repo)
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── scheduler.py
│   ├── jobs/
│   │   ├── fleet_summary.py
│   │   └── nbfc_loan_iot_join.py
│   └── lib/
│       ├── iot_db.py
│       └── rds_db.py
├── risk-sandbox/                ← from VPS
│   ├── Dockerfile
│   └── executor.py
├── monitor/                     ← live HTML status page
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app.py
│   └── index.html
├── scripts/                     ← from VPS
│   └── fetch_intellicar.py
├── legacy/                      ← retired; reference only
│   ├── dashboard/               ← old PHP dashboard
│   └── phase-deploy-scripts/    ← phase3/4/5/6 base64 deploy scripts
├── CLAUDE.md
└── IOT_DEPLOY_README.md
```
