#!/usr/bin/env bash
# Itarang IoT Stack — live terminal status page.
#
# Usage:
#   watch -n 5 -c ./scripts/status.sh    # auto-refresh every 5s (recommended)
#   ./scripts/status.sh                  # one-shot
#
# Press Ctrl+C to exit watch.
#
# Run from /opt/intellicar (or anywhere — script chdir's to repo root).

set -u
cd "$(dirname "$(readlink -f "$0")")/.."

# ANSI colors
B='\033[1m'; G='\033[32m'; Y='\033[33m'; R='\033[31m'; D='\033[2m'; N='\033[0m'

printf "${B}═══ Itarang IoT Stack ═══${N}  %s\n\n" "$(date '+%Y-%m-%d %H:%M:%S %Z')"

# ─── 1. Containers ──────────────────────────────────────────────────────────
printf "${B}▸ Containers${N}\n"
docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null \
  | sed -E "s/(Up [^ ]+(\\([^)]+\\))?)/${G}\\1${N}/" \
  | sed -E "s/(unhealthy)/${Y}\\1${N}/" \
  | sed -E "s/(Restarting|Exited|Created|Dead)/${R}\\1${N}/" \
  | sed 's/^/  /'
echo

# ─── 2. Postgres data freshness ─────────────────────────────────────────────
printf "${B}▸ Postgres data freshness${N}\n"
docker compose exec -T postgres psql -U intellicar -d intellicar -t -A -F $'\t' -c "
SELECT 'vehicle_state',                 COUNT(*), COALESCE(MAX(updated_at)::text,    'never') FROM vehicle_state UNION ALL
SELECT 'telemetry_gps',                 COUNT(*), COALESCE(MAX(time)::text,          'never') FROM telemetry_gps UNION ALL
SELECT 'telemetry_battery',             COUNT(*), COALESCE(MAX(time)::text,          'never') FROM telemetry_battery UNION ALL
SELECT 'alerts (open)',                 COUNT(*) FILTER (WHERE resolved_at IS NULL), COALESCE(MAX(time)::text, 'never') FROM alerts UNION ALL
SELECT 'dashboard_nbfc_loans_with_iot', COUNT(*), COALESCE(MAX(refreshed_at)::text,  'never') FROM dashboard_nbfc_loans_with_iot
" 2>/dev/null | awk -F'\t' '{ printf "  %-32s rows=%-10s last=%s\n", $1, $2, $3 }'
echo

# ─── 3. Redis ───────────────────────────────────────────────────────────────
printf "${B}▸ Redis${N}\n"
PONG=$(docker compose exec -T redis redis-cli ping 2>/dev/null | tr -d '\r\n')
KEYS=$(docker compose exec -T redis redis-cli dbsize 2>/dev/null | tr -d '\r\n')
MEM=$(docker compose exec -T redis redis-cli info memory 2>/dev/null | awk -F: '/used_memory_human/{gsub(/\r/,""); print $2}')
printf "  ping=%s  keys=%s  mem=%s\n\n" "${PONG:-?}" "${KEYS:-?}" "${MEM:-?}"

# ─── 4. Poller — last activity from logs ────────────────────────────────────
printf "${B}▸ Poller (last 3 cycles)${N}\n"
docker compose logs --tail=30 poller 2>/dev/null \
  | grep -E 'poll_state #|Roster refreshed' \
  | tail -3 \
  | sed -E 's/^itarang_poller +\| +//' \
  | sed 's/^/  /'
echo

# ─── 5. Aggregator — last 5 runs (color OK / FAIL) ──────────────────────────
printf "${B}▸ Aggregator (last 5 runs)${N}\n"
docker compose exec -T postgres psql -U intellicar -d intellicar -t -A -F $'\t' -c "
SELECT job_name,
       to_char(started_at, 'HH24:MI:SS'),
       duration_ms,
       rows_written,
       CASE WHEN error IS NULL THEN 'OK' ELSE 'FAIL' END
FROM aggregator_runs
ORDER BY started_at DESC LIMIT 5
" 2>/dev/null | awk -F'\t' -v G="$G" -v R="$R" -v N="$N" '{
    color = ($5=="OK") ? G : R
    printf "  %-22s %s  %5dms  %4d rows  %s%s%s\n", $1, $2, $3, $4, color, $5, N
}'
echo

# ─── 6. Host ────────────────────────────────────────────────────────────────
printf "${B}▸ Host${N}\n"
printf "  disk=%s  mem=%s  load=%s\n" \
    "$(df -h / | awk 'NR==2 {print $3"/"$2" ("$5")"}')" \
    "$(free -h | awk '/^Mem:/ {print $3"/"$2}')" \
    "$(uptime | sed -E 's/.*load average: //')"
