<?php
// Fish Stats API: only what the word tracker needs, and only numbers.
//   action=speakers        the names heard most (the tracker's person picker)
//   action=trend[&list=1]  word counts by year / speaker / place in the episode (trend.php,
//                          the same code as the full site's, copied in by build.py)
// Nothing here returns transcript text. The database sits outside the website folder
// (DATA below), so it can't be downloaded either.
//
// Answers are cached on disk until the database changes (a new upload), and one visitor
// (IP) gets at most LIMIT uncached answers a minute, so a popular word can't overload
// the server. The cache and the counter live next to the database.
header('Content-Type: application/json');

const LIMIT = 30;
// On the server: ~/stats-data/ (upload_site.sh puts the database there). For a local
// test (php -S), FISHSTATS_DATA can point somewhere else.
$DATA = (PHP_SAPI === 'cli-server' ? getenv('FISHSTATS_DATA') : false) ?: dirname(__DIR__) . '/stats-data';
$DBFILE = "$DATA/search_index.db";

function fail($code, $msg) { http_response_code($code); echo json_encode(['error' => $msg]); exit; }

$action = $_GET['action'] ?? '';
if ($action !== 'speakers' && $action !== 'trend') fail(400, 'Unknown action');
if (!is_file($DBFILE)) fail(503, 'The stats database is being updated. Try again in a minute.');

// The cache key: the whole question, plus the database's age, so an upload starts a fresh cache.
$params = $_GET; ksort($params);
$key = sha1(filemtime($DBFILE) . '|' . http_build_query($params));
$cacheDir = "$DATA/cache";
$cacheFile = "$cacheDir/$key.json";
header('Cache-Control: public, max-age=300');
if (is_file($cacheFile)) { readfile($cacheFile); exit; }

// Rate limit on uncached answers: a counter per IP per minute.
try {
    $lim = new SQLite3("$DATA/limits.db");
    $lim->busyTimeout(2000);
    $lim->exec("CREATE TABLE IF NOT EXISTS hits (ip TEXT, minute INTEGER, n INTEGER, PRIMARY KEY (ip, minute))");
    $ip = $_SERVER['HTTP_CF_CONNECTING_IP'] ?? $_SERVER['REMOTE_ADDR'] ?? '';
    $minute = intdiv(time(), 60);
    // Three plain statements (no UPSERT/RETURNING), so an older SQLite on the host is fine.
    foreach (["INSERT OR IGNORE INTO hits VALUES (:ip, :m, 0)", "UPDATE hits SET n = n + 1 WHERE ip = :ip AND minute = :m",
              "SELECT n FROM hits WHERE ip = :ip AND minute = :m"] as $sql) {
        $st = $lim->prepare($sql);
        $st->bindValue(':ip', $ip, SQLITE3_TEXT); $st->bindValue(':m', $minute, SQLITE3_INTEGER);
        $r = $st->execute();
    }
    $n = $r->fetchArray(SQLITE3_NUM)[0] ?? 0;
    if (mt_rand(1, 50) === 1) $lim->exec("DELETE FROM hits WHERE minute < " . ($minute - 5));
    if ($n > LIMIT) { header('Retry-After: 60'); fail(429, 'Too many searches at once. Wait a minute and try again.'); }
} catch (Exception $e) {
    // The counter isn't essential: answer anyway.
}

try {
    $db = new SQLite3($DBFILE, SQLITE3_OPEN_READONLY);
} catch (Exception $e) {
    fail(500, 'Could not open the database');
}
$db->busyTimeout(3000);

// Safe FTS5 phrase (as the full site's api.php): doubled quotes escape them.
function fts_phrase($term, $prefix) {
    return '"' . str_replace('"', '""', $term) . '"' . ($prefix ? '*' : '');
}

// Both actions echo their JSON and exit; catch it to keep a copy in the cache.
ob_start(function ($out) use ($cacheDir, $cacheFile) {
    if (http_response_code() === 200 && $out !== '') {
        if (!is_dir($cacheDir)) @mkdir($cacheDir, 0755, true);
        @file_put_contents("$cacheFile.tmp", $out) && @rename("$cacheFile.tmp", $cacheFile);
    }
    return $out;
});

if ($action === 'speakers') {
    $out = [];
    $res = $db->query("SELECT speaker, COUNT(*) AS c FROM segments
                       WHERE speaker != '' AND speaker_src != 'guess'
                       GROUP BY speaker HAVING c >= 20 ORDER BY c DESC LIMIT 40");
    while ($row = $res->fetchArray(SQLITE3_ASSOC)) $out[] = $row['speaker'];
    echo json_encode($out);
    exit;
}

require __DIR__ . '/trend.php';
trend_action($db);
