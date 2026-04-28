# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo purpose

This directory is **CI/CD scaffolding only** — it holds two files that are checked in to a *different* repo, `itarang-iot-stack`:

- `deploy-iot-stack.yml` → `itarang-iot-stack/.github/workflows/`
- `IOT_DEPLOY_README.md` → `itarang-iot-stack/` (root)

There is no application source here. The workflow assumes the target repo contains `docker-compose.yml`, `schema.sql`, `poller/`, `risk-sandbox/`, and `dashboard/`. To work on those services, open the `itarang-iot-stack` repo, not this directory.

## The deployed stack

The workflow deploys five containers via `docker compose` to a single VPS:

| Service | Host port | Image | Role |
| --- | --- | --- | --- |
| `postgres` | `127.0.0.1:5432` | `timescale/timescaledb:latest-pg16` | TimescaleDB bridge DB |
| `redis` | `127.0.0.1:6379` | `redis:7-alpine` | Polling-state cache |
| `poller` | — | built from `./poller` | Intellicar API → Postgres + Redis loop |
| `risk-sandbox` | `127.0.0.1:8091` | built from `./risk-sandbox` | FastAPI sandbox running LangGraph hypothesis tests |
| `dashboard` | `127.0.0.1:8090` | built from `./dashboard` | PHP dashboard reading the bridge DB |

All ports bind `127.0.0.1` only. Caddy/Nginx in front of CloudPanel does TLS and public exposure — the workflow's health checks hit loopback only.

## Deploy workflow architecture

`deploy-iot-stack.yml`:

- **Triggers:** push to `main` or `workflow_dispatch`. Concurrency group `deploy-iot-stack`, `cancel-in-progress: false`.
- **Steps:** Slack-start → checkout (depth 50) → schema-change guard → SSH deploy → health checks → `docker image prune` → rollback (failure-only) → Slack-result.
- **Health checks** (each polls in 1s steps until pass or timeout): `pg_isready` 60s · `redis-cli ping` 30s · `risk-sandbox GET /health` == 200 over 60s · `dashboard GET /` ∈ {200,301,302} over 60s · poller container `running` + log line matching `polling|fetched|state cycle|roster` over 60s. The poller log check **warns but does not fail** the deploy (credential failures are surfaced separately).
- **Rollback:** before pulling, the deploy step writes `PREV_SHA` to `/tmp/itarang-iot-prev-sha`. The rollback step runs only if `ssh_deploy.outcome == 'failure'`, resets `git` to `PREV_SHA`, and rebuilds.
- **VPS layout:** app dir `/home/itarang-iot/htdocs/iot.itarang.com`; deploy user `itarang-iot` (member of `docker` group); `.env` is rewritten on every deploy from the `IOT_ENV_FILE_B64` secret.
- **Required GitHub secrets:** `IOT_VPS_HOST`, `IOT_VPS_USER`, `IOT_VPS_PORT`, `IOT_VPS_SSH_KEY`, `IOT_ENV_FILE_B64`. `SLACK_WEBHOOK_URL` is optional.

## Non-obvious rules (read before editing the workflow)

- **Schema migrations are manual.** `schema.sql` is read only on the first postgres-volume init (via `/docker-entrypoint-initdb.d/`). The "Detect schema.sql changes" step **fails the deploy** if `schema.sql` or any `migrations/*.sql` changed since the previous commit. Workflow to ship a schema change:
  1. Apply SQL by hand on the VPS: `docker compose exec -T postgres psql -U intellicar -d intellicar < migration.sql`
  2. Update `schema.sql` in the repo to match (so fresh installs are correct).
  3. Trigger via **`workflow_dispatch`** (not push-to-main) to consciously assert the migration is done.
- **`script_stop: false` is intentional, do not "fix" it to `true`.** drone-ssh's `script_stop: true` kills the script on any non-zero exit — including `[ -f X ]` tests inside `if` blocks. Real error handling is `set -eo pipefail` inside the script body.
- **`.env` is delivered as base64.** `IOT_ENV_FILE_B64` decodes to the entire `.env`. The workflow strips CR (`tr -d '\r'`) so CRLF-encoded secrets still work. Required keys: `PG_PASSWORD`, `INTELLICAR_USERNAME`, `INTELLICAR_PASSWORD` (plus anything `phase4_dashboard.sh` / `phase6_risk_sandbox.sh` need). Regenerate with `base64 -w0 .env` and paste as the secret value.
- **Slack steps no-op without the secret.** Every Slack curl ends in `|| true`; a missing `SLACK_WEBHOOK_URL` is fine.
- **Not handled by this workflow:** Postgres volume backups, TLS / public domain, secret-rotation reminders, real data-freshness checks on the poller (the log-line check is "container alive", not "rows are landing in `state_history`" — use a separate Grafana/Uptime-Kuma alert on `MAX(ts)` lag).
