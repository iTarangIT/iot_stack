#!/usr/bin/env bash
# ==============================================================================
# Itarang Phase 5 — open VPS Postgres to the CRM (Vercel) over the internet.
# Run as root on the Hostinger VPS.
#
# What this does:
#   1. Generates a fresh strong password for dashboard_ro
#   2. Re-binds the Postgres container port from 127.0.0.1:5433 to 0.0.0.0:5433
#   3. Configures pg_hba.conf to require hostssl + scram-sha-256 from any IP
#      for the dashboard_ro role (still localhost-trust for everyone else)
#   4. Generates a self-signed SSL cert + enables ssl=on inside Postgres
#   5. Locks dashboard_ro to SELECT only on telemetry tables (revoke everything
#      else)
#   6. Opens 5433 on ufw if installed
#   7. Prints the IOT_DATABASE_URL string for the CRM .env.local
#
# Idempotent — safe to re-run; rotates the password each time.
#
# SECURITY NOTE: This exposes Postgres to the public internet. Mitigations:
#   - TLS-only (sslmode=require enforced via hostssl in pg_hba)
#   - scram-sha-256 + 24-char random password
#   - dashboard_ro has SELECT only on a 7-table allowlist
#   - rate-limit concurrent connections via Postgres max_connections-per-role
# Stronger paths for later: Cloudflare Tunnel, Tailscale ACL, or a small
# bastion that proxies authenticated requests.
# ==============================================================================
set -euo pipefail

STORAGE_DIR=/opt/intellicar/storage
COMPOSE="$STORAGE_DIR/docker-compose.yml"

cd "$STORAGE_DIR"

VPS_IP=$(curl -s --max-time 5 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo "==> [0/7] VPS public IP detected: $VPS_IP"

echo "==> [1/7] Rotating dashboard_ro password"
DASH_PW=$(openssl rand -base64 32 | tr -d '/+=' | head -c 24)
docker compose exec -T postgres psql -U intellicar intellicar \
    -c "ALTER ROLE dashboard_ro WITH PASSWORD '$DASH_PW';" >/dev/null
echo "    password rotated (24 chars, no /+= chars)"

echo "==> [2/7] Re-binding Postgres container port (127.0.0.1:5433 → 0.0.0.0:5433)"
cp "$COMPOSE" "$COMPOSE.bak.phase5.$(date +%s)"
# Match either current binding shape and replace
sed -i 's|"127\.0\.0\.1:5433:5432"|"0.0.0.0:5433:5432"|' "$COMPOSE" || true
sed -i 's|"127\.0\.0\.1:5432:5432"|"0.0.0.0:5433:5432"|' "$COMPOSE" || true
grep -E "5433|5432" "$COMPOSE" | head -3

echo "==> [3/7] Generating self-signed SSL cert in Postgres data volume"
docker compose exec -T postgres bash -lc '
    cd /var/lib/postgresql/data
    if [ ! -f server.crt ]; then
        openssl req -new -x509 -days 825 -nodes -text \
            -out server.crt -keyout server.key \
            -subj "/CN=itarang-iot-pg" >/dev/null 2>&1
        chmod 600 server.key
        chown postgres:postgres server.key server.crt
        echo "    cert generated"
    else
        echo "    cert already exists"
    fi
'

echo "==> [4/7] Enabling ssl=on + adding pg_hba rule for dashboard_ro from anywhere"
docker compose exec -T postgres bash -lc '
    PGCONF=/var/lib/postgresql/data/postgresql.conf
    PGHBA=/var/lib/postgresql/data/pg_hba.conf

    # ssl on
    if grep -qE "^[[:space:]]*ssl[[:space:]]*=" $PGCONF; then
        sed -i "s/^[[:space:]]*ssl[[:space:]]*=.*/ssl = on/" $PGCONF
    else
        echo "ssl = on" >> $PGCONF
    fi
    grep -qE "^ssl_cert_file" $PGCONF || echo "ssl_cert_file = \"server.crt\"" >> $PGCONF
    grep -qE "^ssl_key_file"  $PGCONF || echo "ssl_key_file = \"server.key\"" >> $PGCONF

    # listen on 0.0.0.0 (already set by image but make sure)
    grep -qE "^listen_addresses" $PGCONF || echo "listen_addresses = \"*\"" >> $PGCONF

    # pg_hba: only dashboard_ro from any IP, hostssl-only
    if ! grep -q "dashboard_ro_external" $PGHBA; then
        cat >> $PGHBA <<EOF

# --- Itarang phase5_pg_external: dashboard_ro from anywhere over TLS ---
# tag: dashboard_ro_external
hostssl  intellicar  dashboard_ro  0.0.0.0/0  scram-sha-256
hostssl  intellicar  dashboard_ro  ::/0       scram-sha-256
EOF
    fi
    echo "    postgresql.conf + pg_hba.conf updated"
'

echo "==> [5/7] Locking dashboard_ro to SELECT on telemetry tables only"
docker compose exec -T postgres psql -U intellicar intellicar <<'SQL_EOF'
-- Revoke any over-privileged grants from earlier installs
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM dashboard_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM dashboard_ro;
REVOKE USAGE ON SCHEMA public FROM dashboard_ro;

GRANT USAGE ON SCHEMA public TO dashboard_ro;
GRANT SELECT ON
    vehicle_state,
    telemetry_gps,
    telemetry_battery,
    telemetry_can,
    alerts,
    vehicles,
    daily_distance_per_vehicle
TO dashboard_ro;

-- Future-proof: any new tables added later are NOT auto-granted to dashboard_ro
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM dashboard_ro;

-- Cap concurrent connections from this role
ALTER ROLE dashboard_ro CONNECTION LIMIT 10;

-- Verify
\du dashboard_ro
SQL_EOF

echo "==> [6/7] Recreating Postgres container (pg_hba changes need restart) + opening firewall"
docker compose up -d --force-recreate postgres
sleep 4
docker compose ps postgres

if command -v ufw >/dev/null 2>&1; then
    if ufw status | grep -q "Status: active"; then
        ufw allow 5433/tcp
        echo "    ufw: 5433/tcp opened"
    else
        echo "    ufw installed but inactive — you may still need to open 5433 in your hosting panel firewall"
    fi
else
    echo "    ufw not installed — open 5433 in CloudPanel/Hostinger firewall manually"
fi

echo ""
echo "==> [7/7] Smoke test: connect from outside the container (still on VPS, but via TLS)"
docker compose exec -T postgres psql "host=$VPS_IP port=5432 user=dashboard_ro password=$DASH_PW dbname=intellicar sslmode=require" \
    -c "SELECT 1 AS ok, current_user AS as_user;" 2>&1 | head -5 || \
    echo "    in-container loopback test skipped (it's fine if it fails — the firewall test from your laptop is what matters)"

echo ""
echo "================================================================="
echo " Phase 5 complete. Add this line to your CRM .env.local:"
echo ""
echo "   IOT_DATABASE_URL=postgres://dashboard_ro:$DASH_PW@$VPS_IP:5433/intellicar?sslmode=require"
echo ""
echo " Also add it to the Vercel environment for your CRM project."
echo ""
echo " Test from your laptop (psql client required):"
echo "   psql \"postgres://dashboard_ro:$DASH_PW@$VPS_IP:5433/intellicar?sslmode=require\" -c \"SELECT COUNT(*) FROM vehicle_state;\""
echo ""
echo " To rotate the password later, just re-run this script."
echo "================================================================="
