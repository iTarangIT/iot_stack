<?php
/**
 * Itarang dashboard backend (standalone-dashboard build)
 * ------------------------------------------------------
 * Same routes as the CRM-embedded version plus two additions:
 *   ?op=fleet_state                      → structured vehicle_state rows
 *                                          (preferred for the standalone HTML)
 *   ?op=vehicle&vno=...                  → now also returns can_24h with SOC,
 *                                          voltage, current, temperature
 *                                          extracted from telemetry_can JSONB
 *                                          (so SOC charts work TODAY before the
 *                                          nightly poll_daily backfill runs)
 *
 * All other routes are unchanged from the CRM build.
 */

declare(strict_types=1);
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

// === CONFIG (read from environment or hardcode after testing) ===
$REDIS_HOST       = getenv('REDIS_HOST') ?: '127.0.0.1';
$REDIS_PORT       = (int)(getenv('REDIS_PORT') ?: 6379);
$PG_DSN           = getenv('PG_DSN_HOST') ?: 'pgsql:host=127.0.0.1;port=5432;dbname=intellicar';
$PG_USER          = getenv('PG_USER') ?: 'dashboard_ro';
$PG_PASS          = getenv('PG_PASS') ?: 'CHANGEME_dashboard_ro';
$INTELLICAR_BASE  = getenv('INTELLICAR_BASE') ?: 'https://apiplatform.intellicar.in/api/standard';
$TOKEN_REDIS_KEY  = 'intellicar:token';

// === HELPERS ===
function fail(int $code, string $msg): void {
    http_response_code($code);
    echo json_encode(['error' => $msg]);
    exit;
}

function pdo(): PDO {
    global $PG_DSN, $PG_USER, $PG_PASS;
    static $pdo = null;
    if ($pdo === null) {
        try {
            $pdo = new PDO($PG_DSN, $PG_USER, $PG_PASS, [
                PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                PDO::ATTR_PERSISTENT => true,
            ]);
        } catch (Throwable $e) {
            fail(500, 'pg connect: ' . $e->getMessage());
        }
    }
    return $pdo;
}

function redis_conn(): Redis {
    global $REDIS_HOST, $REDIS_PORT;
    static $r = null;
    if ($r === null) {
        $r = new Redis();
        if (!$r->pconnect($REDIS_HOST, $REDIS_PORT, 1.0)) {
            fail(500, 'redis connect failed');
        }
    }
    return $r;
}

function intellicar_token(): string {
    global $TOKEN_REDIS_KEY;
    $tok = redis_conn()->get($TOKEN_REDIS_KEY);
    if (!$tok) {
        fail(503, 'Intellicar token not in Redis yet — poller may be down or just starting');
    }
    return (string)$tok;
}

function intellicar_post(string $path, array $payload, int $timeout = 12): array {
    global $INTELLICAR_BASE;
    $url = rtrim($INTELLICAR_BASE, '/') . '/' . ltrim($path, '/');
    $body = json_encode(['token' => intellicar_token()] + $payload, JSON_UNESCAPED_SLASHES);
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_POSTFIELDS => $body,
        CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_CONNECTTIMEOUT => 5,
        CURLOPT_TIMEOUT => $timeout,
        CURLOPT_SSL_VERIFYPEER => true,
    ]);
    $raw = curl_exec($ch);
    $err = curl_error($ch);
    $code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    if ($raw === false)   fail(502, "intellicar curl: $err");
    if ($code >= 400)     fail($code, "intellicar $path returned HTTP $code");
    $decoded = json_decode($raw, true);
    if (!is_array($decoded)) fail(502, "intellicar $path returned non-JSON");
    return $decoded;
}

function range_or_default(): array {
    $now_ms = (int)(microtime(true) * 1000);
    $to_in   = $_GET['to']   ?? (string)$now_ms;
    $from_in = $_GET['from'] ?? (string)($now_ms - 86400 * 1000);
    $parse_to_ms = function ($v) {
        $v = (string)$v;
        if (ctype_digit($v)) {
            $n = (int)$v;
            return ($n >= 1_000_000_000_000) ? $n : $n * 1000;
        }
        $sec = strtotime($v . ' UTC');
        return ($sec === false) ? null : $sec * 1000;
    };
    $from_ms = $parse_to_ms($from_in);
    $to_ms   = $parse_to_ms($to_in);
    if ($from_ms === null || $to_ms === null || $from_ms <= 0 || $to_ms <= 0)
        fail(400, 'from/to must be "YYYY-MM-DD HH:MM:SS" UTC, epoch s, or epoch ms');
    if ($to_ms <= $from_ms) fail(400, 'to must be after from');
    if (($to_ms - $from_ms) > 90 * 86400 * 1000) fail(400, 'range capped at 90 days');
    return ['from' => $from_ms, 'to' => $to_ms];
}

// === ROUTES ===
$op = $_GET['op'] ?? 'fleet_state';

switch ($op) {

case 'fleet_state':
    // Structured vehicle_state rows joined with vehicles for makemodel.
    // This is what the standalone dashboard reads — schema is stable and
    // already includes computed `online`, no client-side guessing.
    $rows = pdo()->query("
        SELECT
            vs.vehicleno,
            v.makemodel,
            v.owner,
            vs.last_seen,
            vs.last_gps_at,
            vs.last_battery_at,
            vs.lat, vs.lon,
            vs.speed_kph,
            vs.heading,
            vs.ignition,
            vs.gps_fix,
            vs.soc_pct,
            vs.soh_pct,
            vs.pack_voltage,
            vs.pack_current,
            vs.pack_temp_c,
            vs.charging,
            vs.online,
            vs.updated_at,
            EXTRACT(EPOCH FROM (NOW() - vs.last_gps_at))::int AS sec_since_gps
        FROM vehicle_state vs
        LEFT JOIN vehicles v USING (vehicleno)
        ORDER BY vs.online DESC, vs.last_gps_at DESC NULLS LAST
    ")->fetchAll();

    // Open alert count for the badge
    $open_alerts = (int)pdo()->query(
        "SELECT COUNT(*) FROM alerts WHERE resolved_at IS NULL"
    )->fetchColumn();

    echo json_encode([
        'generated_at' => gmdate('c'),
        'count'        => count($rows),
        'open_alerts'  => $open_alerts,
        'vehicles'     => $rows,
    ]);
    break;

case 'fleet':
    // Original Redis-first endpoint — kept for compatibility.
    $r = redis_conn();
    $keys = $r->keys('state:*');
    if (!$keys) {
        $rows = pdo()->query("SELECT * FROM vehicle_state")->fetchAll();
        echo json_encode([
            'source' => 'postgres',
            'generated_at' => gmdate('c'),
            'vehicles' => $rows,
        ]);
        break;
    }
    $values = $r->mget($keys);
    $out = [];
    foreach ($values as $v) {
        $decoded = json_decode((string)$v, true);
        if ($decoded) $out[] = $decoded;
    }
    echo json_encode([
        'source' => 'redis',
        'generated_at' => gmdate('c'),
        'count' => count($out),
        'vehicles' => $out,
    ]);
    break;

case 'vehicle':
    $vno = $_GET['vno'] ?? '';
    if ($vno === '') fail(400, 'vno required');

    // Live state row (one row, structured)
    $stmt = pdo()->prepare("
        SELECT vs.*, v.makemodel, v.owner,
               EXTRACT(EPOCH FROM (NOW() - vs.last_gps_at))::int AS sec_since_gps
        FROM vehicle_state vs
        LEFT JOIN vehicles v USING (vehicleno)
        WHERE vs.vehicleno = :vno
    ");
    $stmt->execute([':vno' => $vno]);
    $state = $stmt->fetch();

    $live = redis_conn()->get("state:$vno");

    // 24h GPS breadcrumb
    $stmt = pdo()->prepare("
        SELECT time, lat, lon, speed_kph, heading, ignition
        FROM telemetry_gps
        WHERE vehicleno = :vno AND time > NOW() - INTERVAL '24 hours'
        ORDER BY time
    ");
    $stmt->execute([':vno' => $vno]);
    $gps = $stmt->fetchAll();

    // 24h battery (from poll_daily backfill — empty until tonight's run)
    $stmt = pdo()->prepare("
        SELECT time, soc_pct, pack_voltage, pack_current, pack_temp_c
        FROM telemetry_battery
        WHERE vehicleno = :vno AND time > NOW() - INTERVAL '24 hours'
        ORDER BY time
    ");
    $stmt->execute([':vno' => $vno]);
    $batt = $stmt->fetchAll();

    // 24h CAN (live every-30s frames — extract SOC, V, I, temp from JSONB).
    // This fills the chart gap until the daily backfill runs.
    $stmt = pdo()->prepare("
        SELECT time,
               (payload->'soc'->>'value')::float             AS soc_pct,
               (payload->'battery_voltage'->>'value')::float AS pack_voltage,
               (payload->'current'->>'value')::float         AS pack_current,
               (payload->'battery_temp'->>'value')::float    AS pack_temp_c
        FROM telemetry_can
        WHERE vehicleno = :vno AND time > NOW() - INTERVAL '24 hours'
        ORDER BY time
    ");
    $stmt->execute([':vno' => $vno]);
    $can = $stmt->fetchAll();

    echo json_encode([
        'vehicleno'   => $vno,
        'state'       => $state,
        'live'        => $live ? json_decode($live, true) : null,
        'gps_24h'     => $gps,
        'battery_24h' => $batt,
        'can_24h'     => $can,
    ]);
    break;

case 'alerts':
    $open = isset($_GET['open']) ? (int)$_GET['open'] : 1;
    $sql = "SELECT time, vehicleno, alert_type, severity, message, value, threshold, resolved_at
            FROM alerts " . ($open ? "WHERE resolved_at IS NULL " : "") .
           "ORDER BY time DESC LIMIT 500";
    echo json_encode(['alerts' => pdo()->query($sql)->fetchAll()]);
    break;

case 'trips':
    $since = $_GET['since'] ?? gmdate('Y-m-d', time() - 7*86400);
    $stmt = pdo()->prepare("
        SELECT time, end_time, vehicleno, trip_id, distance_km, duration_s, energy_kwh
        FROM trips
        WHERE time >= :since
        ORDER BY time DESC LIMIT 1000
    ");
    $stmt->execute([':since' => $since]);
    echo json_encode(['trips' => $stmt->fetchAll()]);
    break;

case 'daily_km':
    $days = max(1, min(365, (int)($_GET['days'] ?? 30)));
    $stmt = pdo()->prepare("
        SELECT day, vehicleno, km, kwh, trips
        FROM daily_distance_per_vehicle
        WHERE day >= NOW() - (:days || ' days')::interval
        ORDER BY day, vehicleno
    ");
    $stmt->execute([':days' => $days]);
    echo json_encode(['daily_km' => $stmt->fetchAll()]);
    break;

// On-demand history (Intellicar proxy)
case 'gps_history':
case 'battery_history':
case 'distance_history':
case 'fuel_history':
    $vno = $_GET['vno'] ?? '';
    if ($vno === '') fail(400, 'vno required');
    $r = range_or_default();
    $endpoint_map = [
        'gps_history'      => 'getgpshistory',
        'battery_history'  => 'getbatterymetricshistory',
        'distance_history' => 'getdistancetravelled',
        'fuel_history'     => 'getfuelhistory',
    ];
    $path = $endpoint_map[$op];
    echo json_encode([
        'source'    => 'intellicar',
        'endpoint'  => $path,
        'vehicleno' => $vno,
        'starttime' => $r['from'],
        'endtime'   => $r['to'],
        'data' => intellicar_post($path, [
            'vehicleno' => $vno,
            'starttime' => $r['from'],
            'endtime'   => $r['to'],
        ]),
    ]);
    break;

default:
    fail(400, "unknown op: $op");
}
