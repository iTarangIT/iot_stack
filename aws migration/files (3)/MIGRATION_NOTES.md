# iTarang IoT — RDS Schema Migration Notes (Option A: plain Postgres, no Timescale)

Companion to `schema_rds.sql`. Read this before running the SQL.

## 0. Why this rewrite exists
The original `schema.sql` depends on TimescaleDB (hypertables, compression,
retention policies, continuous aggregates). AWS RDS for PostgreSQL does not
support the full TimescaleDB extension, and iTarang requires data to stay in
the AWS account (NBFC/compliance). So the time-series tables are rebuilt on
native declarative partitioning + pg_partman, sized for 2,000–5,000 vehicles.

## 1. ONE-TIME prerequisite — custom DB parameter group (REQUIRES REBOOT)
`pg_partman` needs a background worker preloaded. The `default.postgres16`
parameter group is read-only, so:

1. RDS Console → Parameter groups → Create parameter group
   - Family: postgres16, Type: DB Parameter Group, name: `itarang-iot-pg16`
2. Edit it, set:
   - `shared_preload_libraries = pg_partman_bgw,pg_stat_statements`
   - `pg_partman_bgw.interval  = 3600`
   - `pg_partman_bgw.role      = itarang_admin`
   - `pg_partman_bgw.dbname    = itarang`
3. RDS → Databases → itarang-iot-db → Modify → set DB parameter group to
   `itarang-iot-pg16` → Apply. Then REBOOT the instance (param affects
   shared_preload_libraries, which only loads at boot).
4. After reboot, connect and run `schema_rds.sql` as `itarang_admin`.

## 2. What changed vs the original (table by table)
| Original (Timescale)              | Now (RDS)                                  |
|-----------------------------------|--------------------------------------------|
| create_hypertable(... 1 day)      | PARTITION BY RANGE + pg_partman, 1-day      |
| create_hypertable(... 7 days)     | PARTITION BY RANGE + pg_partman, 7-day      |
| compression policies              | (none) — bounded by retention + S3 archive  |
| add_retention_policy              | partman.part_config retention, bgw enforces |
| continuous aggregates (matview)   | rollup tables + incremental UPSERT funcs    |
| dashboard_ro plaintext password   | placeholder → set via Secrets Manager       |
| GRANT ... DATABASE intellicar     | corrected to DATABASE itarang               |

## 3. Application code changes (poller + aggregator)
These are the code edits your developer must make; the schema alone is not enough.

### Poller (poller/poll.py)
- INSERTs into telemetry_* tables: NO CHANGE needed. Writing to the partitioned
  parent table routes rows to the correct partition automatically.
- Remove any TimescaleDB-specific SQL if present (none expected in the hot path).

### Aggregator (aggregator/*)
- The two continuous aggregates are gone. Replace whatever refreshed them with
  scheduled calls to the new functions:
    SELECT refresh_daily_distance('7 days');   -- every 30 min
    SELECT refresh_hourly_battery('2 days');   -- every 15 min
- NEW REQUIRED JOB — cold archive before retention drop:
  Before pg_partman drops partitions (90/30/730/365-day windows), export the
  about-to-expire partition to S3 (Parquet/CSV → s3://itarang-iot-archive/).
  Run daily. This is what replaces Timescale compression as the long-term-cost
  control. Without it, data is permanently lost when partitions drop.

## 4. Migration of EXISTING data from Hostinger
1. On Hostinger, dump data only (schema is being recreated here):
     pg_dump --data-only --no-owner --table='vehicles' --table='users' \
             --table='vehicle_state' --table='telemetry_*' --table='trips' \
             --table='alerts' --table='distance_rollup' \
             -d <olddb> -Fc -f itarang_data.dump
   (If the old DB has compressed Timescale chunks, decompress or use a plain
    COPY export per table instead — Timescale dumps need care. Simplest robust
    path: COPY each table to CSV, then \copy into RDS.)
2. Load order matters (FKs): vehicles → users → vehicle_state → telemetry_* /
   trips / alerts / distance_rollup.
3. After load, run the rollup refresh functions once to backfill dashboards.

## 5. Verification checklist (do BEFORE cutover)
- [ ] Row counts per table match Hostinger (SELECT count(*) each).
- [ ] New daily partitions auto-created overnight (check pg_partman ran).
- [ ] refresh_daily_distance / refresh_hourly_battery populate the rollups.
- [ ] dashboard_ro can SELECT but not INSERT.
- [ ] A test retention drop on a throwaway old partition works (and the S3
      archive captured it first).
