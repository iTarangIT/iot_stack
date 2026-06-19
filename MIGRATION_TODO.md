# IoT Stack → AWS RDS — Migration TODO & Gap Analysis

Companion to `aws migration/files (3)/schema_rds.sql` and
`aws migration/files (3)/MIGRATION_NOTES.md`.

> **Note on file locations:** the brief said `schema_rds.sql` and
> `MIGRATION_NOTES.md` were placed in the repo root. They are actually under
> `aws migration/files (3)/`. Paths below reflect the real location.

This document has two halves:
1. **Gap analysis** — every place the application code or the new schema assumes
   TimescaleDB / continuous aggregates, and what each must change to.
2. **Manual checklist** — the steps you must run yourself against live AWS
   (param group + reboot, schema load, data migration, verification), pulled from
   `MIGRATION_NOTES.md` §1, §4, §5. None of these were executed — this is prep
   only.

---

## 1. Gap analysis

### 1a. Continuous aggregates → scheduled aggregator jobs  ✅ done in code
- **Old:** TimescaleDB auto-refreshed two continuous aggregates via background
  policies — `schema.sql:243-253`
  (`daily_distance_per_vehicle` every 30 min / start_offset 7 days;
  `hourly_battery_per_vehicle` every 15 min / start_offset 2 days). **No
  aggregator job did this** — Timescale's scheduler did.
- **New:** `schema_rds.sql:239-289` recreates them as plain rollup tables plus
  `refresh_daily_distance('7 days')` and `refresh_hourly_battery('2 days')`. RDS
  has no continuous-aggregate scheduler, so two new aggregator jobs now call
  those functions on the same cadence:
  - `aggregator/jobs/refresh_daily_distance.py` (30 min).
  - `aggregator/jobs/refresh_hourly_battery.py` (15 min).
- They run on the **`iot`** connection (`IOT_PG_DSN`), which post-migration is
  the IoT RDS where these functions live.

### 1b. Compression gone → S3 cold-archive job is now mandatory  ✅ done in code
- **Old:** `schema.sql:186-200` compressed old chunks in place; retention dropped
  only already-compressed copies.
- **New:** `schema_rds.sql:224-230` sets `retention_keep_table = false`, so
  `pg_partman_bgw` **permanently DROPS** partitions (90/30/730/365-day windows).
  Without an archive, that data is lost forever (`MIGRATION_NOTES.md` §3).
- **Added:** `aggregator/jobs/cold_archive_partitions.py` — daily job that finds
  partitions within `IOT_ARCHIVE_LEAD_DAYS` of their drop (reading retention from
  `partman.part_config`), streams each to gzipped CSV, uploads to
  `s3://$IOT_ARCHIVE_S3_BUCKET/$IOT_ARCHIVE_S3_PREFIX/<parent>/<partition>.csv.gz`,
  and records it in a self-created `iot_archive_log` ledger (idempotent). It never
  drops partitions — pg_partman owns that.
  - ⚠️ **Operational:** keep `IOT_ARCHIVE_LEAD_DAYS` (default 3) larger than
    `pg_partman_bgw.interval` (≈ hourly) so the export always precedes the drop.
  - ⚠️ **Fargate sizing:** the job COPYs each partition to a temp gzip file on
    local disk before upload. A day of GPS for ~5000 vehicles can be multiple GB
    compressed — size the Fargate task's ephemeral storage (default 20 GB,
    raisable to 200 GB) accordingly, or switch to a streaming multipart upload.
  - boto3 creds come from the Fargate task IAM role in prod (needs
    `s3:PutObject` on the archive bucket). Do not put keys in env.

### 1c. `schema_rds.sql` is missing 3 tables the aggregator REQUIRES  ⚠️ ACTION NEEDED
The aggregator writes to tables that exist today in `schema/dashboard_aggregates.sql`
but are **absent from `schema_rds.sql`**. On a fresh RDS load with `schema_rds.sql`
alone, the aggregator crashes:
| Table | Written by | Defined in |
|-------|-----------|------------|
| `aggregator_runs` | `aggregator/scheduler.py:89` (every job run) | `schema/dashboard_aggregates.sql:21` |
| `dashboard_nbfc_loans_with_iot` | `aggregator/jobs/nbfc_loan_iot_join.py:58` | `schema/dashboard_aggregates.sql:38` |
| `dashboard_vehicle_monthly_range` | `aggregator/jobs/vehicle_monthly_range.py:155` | `schema/dashboard_aggregates.sql:66` |

`schema/dashboard_aggregates.sql` is **plain Postgres (no Timescale) and
RDS-safe**. Pick one (see checklist step 3):
- **Recommended:** apply `schema/dashboard_aggregates.sql` to RDS right after
  `schema_rds.sql`, OR
- merge those three tables into `schema_rds.sql` — **needs your confirmation**, I
  did not edit `schema_rds.sql`.

`iot_archive_log` (the cold-archive ledger) is **not** in this list — the archive
job self-creates it, so no schema action is needed for it.

### 1d. Poller — verified clean, no code change  ✅
- `grep` over `poller/` found **no TimescaleDB SQL** (only doc-comment mentions of
  "Timescale"). No `create_hypertable` / `time_bucket` / retention / compression.
- `telemetry_*` INSERTs (`poller/poll.py:304-321, 520-525, 563-568, 590-594`) use
  `ON CONFLICT DO NOTHING` with **no conflict target** → valid on partitioned
  parents; rows route to the correct partition automatically.
- `distance_rollup` upsert (`poller/poll.py:470-475`) targets
  `(time, vehicleno, bucket_size)`, which matches its partition-key-inclusive
  PRIMARY KEY in `schema_rds.sql:141`. ✅
- `alerts` insert/update by `vehicleno`/`resolved_at`
  (`poller/poll.py:385-399`) — plain SQL, correct across partitions. ✅
- ⚠️ **Deployment, not code:** the poller's `PG_DSN` (and the aggregator's
  `IOT_PG_DSN`) must include `?sslmode=require` once they point at RDS. The
  poller uses `asyncpg`; confirm it negotiates TLS against RDS during cutover.

### 1e. Schema ↔ code consistency — no mismatches found  ✅
- `refresh_daily_distance(p_lookback INTERVAL DEFAULT '7 days')` and
  `refresh_hourly_battery(p_lookback INTERVAL DEFAULT '2 days')`
  (`schema_rds.sql:262, 276`) — signatures match the new jobs' calls.
- Every column the jobs/poller read or write exists in `schema_rds.sql`
  (telemetry_gps/battery/fuel/can, distance_rollup, alerts, vehicles,
  vehicle_state, the two rollup tables). No renamed/missing columns.

### 1f. Pre-existing observations (NOT introduced by this migration)
- `refresh_daily_distance` reads from `trips`, but `poll.py` never INSERTs into
  `trips` → `daily_distance_per_vehicle` stays empty. This matches the old
  continuous aggregate's behaviour. Flagged, not fixed — wiring trip ingestion is
  out of scope.
- `aggregator/scheduler.py:77` opens an `rds_connection()` for **every** job, and
  `rds_db.py:20` raises if `RDS_DSN` is unset. So the aggregator needs `RDS_DSN`
  configured even though the new refresh/archive jobs only use `iot`. Existing
  coupling — just make sure `RDS_DSN` (the NBFC read-only DSN) is set on Fargate.
- `daily_distance_per_vehicle` / `hourly_battery_per_vehicle` were continuous-agg
  views before and are plain tables now; the CRM/dashboard reads of them are
  unaffected (same names/columns), but confirm no reader expects view-only
  behaviour.

### 1g. `dashboard_ro` password  ⚠️ ACTION NEEDED
- `schema_rds.sql:301` creates `dashboard_ro` with the placeholder
  `'CHANGE_ME_VIA_SECRETS_MANAGER'`. After running the schema, set the real
  password from Secrets Manager:
  `ALTER ROLE dashboard_ro PASSWORD '<value-from-secrets-manager>';`
- The old `schema.sql:261` had a **plaintext password committed to git history**
  (`FyztuUFcC5O1uRIbOGsWQEco`). **Rotate it on the live Hostinger system** before
  decommissioning, since it's exposed in history. (Handling unchanged in code per
  scope — this is a manual security task.)

---

## 2. Files changed in this prep pass

| File | Change |
|------|--------|
| `aggregator/jobs/refresh_daily_distance.py` | **new** — 30-min job calling `refresh_daily_distance('7 days')` |
| `aggregator/jobs/refresh_hourly_battery.py` | **new** — 15-min job calling `refresh_hourly_battery('2 days')` |
| `aggregator/jobs/cold_archive_partitions.py` | **new** — daily partition → S3 archive (boto3) |
| `aggregator/requirements.txt` | **mod** — add `boto3` |
| `.env.example` | **mod** — new interval + `IOT_ARCHIVE_*` vars, RDS DSN/sslmode notes |
| `docker-compose.yml` | **mod** — aggregator env parity for the new vars |
| `MIGRATION_TODO.md` | **new** — this file |

Not touched (by scope): `schema_rds.sql`, `poller/poll.py`, anything in AWS.

---

## 3. Manual checklist (run these yourself — nothing here was executed)

### Pre-flight (`MIGRATION_NOTES.md` §1) — REQUIRES A REBOOT
1. RDS Console → **Create a custom DB parameter group** (family `postgres16`),
   name e.g. `itarang-iot-pg16`. Set:
   - `shared_preload_libraries = pg_partman_bgw,pg_stat_statements`
   - `pg_partman_bgw.interval = 3600`
   - `pg_partman_bgw.role = itarang_admin`
   - `pg_partman_bgw.dbname = itarang`
2. Attach it to `itarang-iot-db` → **Modify** → **Reboot** (shared_preload_libraries
   only loads at boot). Fine to do now — nothing is live yet.

### Schema load
3. Connect as `itarang_admin` to database `itarang` and run, in order:
   1. `aws migration/files (3)/schema_rds.sql`
   2. **`schema/dashboard_aggregates.sql`** ← required, or the aggregator crashes
      (see gap 1c). Skip only if you instead merge those 3 tables into
      `schema_rds.sql` (tell me to do that edit).
4. Set the `dashboard_ro` password from Secrets Manager (gap 1g):
   `ALTER ROLE dashboard_ro PASSWORD '<from-secrets-manager>';`

### Data migration from Hostinger (`MIGRATION_NOTES.md` §4)
5. Dump **data only** from Hostinger (schema is recreated, not dumped). Timescale
   chunks need care — simplest robust path is a per-table `COPY ... TO CSV` then
   `\copy` into RDS.
6. Load in **FK order**: `vehicles → users → vehicle_state → telemetry_* / trips /
   alerts / distance_rollup`.
7. After load, run the rollups once to backfill dashboards:
   `SELECT refresh_daily_distance('7 days'); SELECT refresh_hourly_battery('2 days');`
   (or just let the new aggregator jobs run on boot).

### Verification before cutover (`MIGRATION_NOTES.md` §5 + new jobs)
- [ ] Per-table `count(*)` matches Hostinger.
- [ ] New daily partitions auto-created overnight (pg_partman_bgw ran).
- [ ] `refresh_daily_distance` / `refresh_hourly_battery` populate the rollups
      (and the aggregator's `aggregator_runs` shows them succeeding).
- [ ] `dashboard_ro` can `SELECT` but not `INSERT`.
- [ ] Cold archive: on a throwaway old partition, confirm
      `cold_archive_partitions` writes the `.csv.gz` to S3 and inserts an
      `iot_archive_log` row **before** any retention drop. Confirm the Fargate
      task role has `s3:PutObject` and enough ephemeral disk (gap 1b).
- [ ] Poller `PG_DSN` / aggregator `IOT_PG_DSN` include `?sslmode=require` and
      connect to RDS over TLS (gap 1d).
- [ ] `RDS_DSN` (NBFC read-only) is set so the aggregator starts at all (gap 1f).

### Security
- [ ] Rotate the leaked `dashboard_ro` password on the live Hostinger box
      (gap 1g) before decommissioning.
