<?php
// The Stats word tracker's counts (action=trend), shared by the full site's api.php and
// the public Fish stats site's api.php (fish stats repo), so both count the same way.
// It only ever returns numbers, episode ids and times, never transcript text: that is
// what makes it safe on the public site. Needs fts_phrase() (api.php defines it).
// Used as:  if ($action === 'trend') trend_action($db);   (it echoes the JSON and exits)

// Trend: how often a word or phrase is said, by year, show and speaker
// (the Stats word tracker). Rows: [period, podcast, speaker, times said, lines].
// Where in the episode it's said, per podcast and speaker:
//   pos:  [podcast, speaker, [times said in each tenth of the episode]] (a share
//         of its length, so 30- and 90-minute episodes compare);
//   mins: [podcast, speaker, [times said in each 5 minutes; the last, 115+]].
//   top:  [episode id, speaker, times said] for each show's 40 episodes with the most;
//   fl:   [podcast, speaker, first episode id, its second, last episode id, its second]
//         (when each person first and last said it; the page picks the earliest and
//         latest of the people and shows it keeps).
// Optional: q may hold alternatives, "bigfoot | sasquatch", counted as one word;
//   scale=year (default) | month ("2019-04") | episode (the episode id, and the show
//   column is null) sets the period;
//   spk= one speaker ("?" = no name), yf= / yt= a year range, kind= an episode kind.
// action=trend&list=1 instead returns every episode once: {shows, ep: [[id, show index,
// name, length in seconds, kind]]}, for the page's per-hour sums and episode names.
// An episode's length is its last line's start (lines are stored in order, so
// that is the highest id: one index step per episode, ~7 ms for them all).
// One query, grouped in SQL so PHP only folds the few thousand totals; said()
// counts the occurrences on each line (at least 1: FTS also matches word forms,
// "ghosts" for "ghost"). The inner LIMIT is only a safety cap ("the" is ~260k lines).
// Episode kinds mirror episodes.kind() (scripts/episodes.py FISH_KIND): the same regexes,
// first match wins, on the name without its date. They
// run as an SQL function so the episode filter stays in the query; keep the two in step.
const TREND_KINDS = [
    'No Such Thing As A Fish' => [['Little Fish', '/^(\d+\.\s+)?little fish\b/i'], ['Drop Us A Line', '/^(bonus\s+)?drop us a line\b/i'],
        ['Compilation', '/^(bonus\s+)?(compilation|best of)\b/i'], ['Club Fish', '/^club fish\b/i'], ['Bonus', '/^bonus\b/i'],
        ['Special', '/^(?!\d)(?=.*\b(factball|christmas|fishmas|live|special)\b)/i'], ['Main', '//']],
];
function trend_kind($podcast, $name) {
    $t = preg_replace('/^\d{4}-\d\d-\d\d\s*-?\s*/', '', $name);
    foreach (TREND_KINDS[$podcast] ?? [] as [$k, $re]) if (preg_match($re, $t)) return $k;
    return 'Issue';
}
function trend_action($db) {
    $db->createFunction('kind', 'trend_kind', 2, SQLITE3_DETERMINISTIC);
    $len = "(SELECT start_seconds FROM segments WHERE episode_id = episodes.id ORDER BY id DESC LIMIT 1)";
    if (isset($_GET['list'])) {
        $shows = []; $ep = [];
        $res = $db->query("SELECT id, podcast, episode, $len AS l, kind(podcast, episode) AS k FROM episodes
                           WHERE media_type = 'podcast' ORDER BY episode, id");
        while ($r = $res->fetchArray(SQLITE3_ASSOC)) {
            $si = array_search($r['podcast'], $shows, true);
            if ($si === false) { $si = count($shows); $shows[] = $r['podcast']; }
            $ep[] = [$r['id'], $si, $r['episode'], (int) round($r['l'] ?? 0), $r['k']];
        }
        echo json_encode(['shows' => $shows, 'ep' => $ep]);
        exit;
    }
    $q = trim($_GET['q'] ?? '');
    $empty = ['q' => $q, 'rows' => [], 'pos' => [], 'mins' => [], 'top' => [], 'fl' => []];
    $alts = array_values(array_unique(array_filter(array_map(function ($a) { return trim(preg_replace('/\s+/', ' ', $a)); }, explode('|', $q)),
        function ($a) { return mb_strlen($a) >= 2; })));
    if (!$alts || count($alts) > 8 || mb_strlen($q) > 250 || max(array_map('mb_strlen', $alts)) > 60) { echo json_encode($empty); exit; }
    $pats = array_map(function ($a) { return implode('[\s-]+', preg_split('/\s+/', preg_quote(mb_strtolower($a), '/'))); }, $alts);
    $re = '/(?<![\w\'])(?:' . implode('|', $pats) . ')(?![\w\'])/iu';
    $db->createFunction('said', function ($t) use ($re) { return max(1, (int) preg_match_all($re, $t)); }, 1, SQLITE3_DETERMINISTIC);
    $scale = $_GET['scale'] ?? 'year';
    $period = $scale === 'month' ? "substr(len.episode,1,7)" : ($scale === 'episode' ? "len.id" : "substr(len.episode,1,4)");
    // Filters: the episodes (years, kind) narrow the len list the lines are joined to.
    $epWhere = ''; $bind = [];
    foreach (['yf' => '>=', 'yt' => '<='] as $k => $op) {
        if (preg_match('/^\d{4}$/', $_GET[$k] ?? '')) { $epWhere .= " AND substr(episode,1,4) $op :$k"; $bind[":$k"] = $_GET[$k]; }
    }
    $kind = $_GET['kind'] ?? '';
    if ($kind === 'main') $epWhere .= " AND kind(podcast, episode) IN ('Main', 'Guest episode', 'Issue')";
    elseif ($kind !== '') { $epWhere .= " AND kind(podcast, episode) = :kind"; $bind[':kind'] = $kind; }
    $spkWhere = '';
    if (isset($_GET['spk']) && $_GET['spk'] !== '') { $spkWhere = " AND s.speaker = :spk"; $bind[':spk'] = $_GET['spk'] === '?' ? '' : $_GET['spk']; }
    // m: one row per matching line; g: those summed per episode, speaker, tenth and 5-minute slot.
    // Then four groupings of g in one statement (g says which): 0 by period, 1 tenths, 2 five-minute
    // slots, 3 per episode and speaker (top + firsts). The empty -1 row comes first because PHP's
    // execute() runs a statement to its first row and then starts it again: without it the whole
    // count would run twice (that alone made "the" take 0.8 s instead of 0.6 s).
    $stmt = $db->prepare("WITH len AS MATERIALIZED (
                              SELECT id, podcast, episode, $len AS l FROM episodes WHERE media_type = 'podcast'$epWhere),
                          m AS (
                              SELECT len.id AS eid, $period AS p, substr(len.episode,1,10) AS d, len.podcast, s.speaker AS spk,
                                     said(s.text) AS n, s.start_seconds AS t,
                                     CASE WHEN len.l > 0 THEN MIN(9, s.start_seconds * 10 / len.l) ELSE 0 END AS b,
                                     MIN(23, s.start_seconds / 300) AS mb
                              FROM segments_fts JOIN segments s ON s.id = segments_fts.rowid JOIN len ON len.id = s.episode_id
                              WHERE segments_fts MATCH :m$spkWhere LIMIT 1000000),
                          g AS MATERIALIZED (
                              SELECT eid, p, d, podcast, spk, b, mb, SUM(n) AS n, COUNT(*) AS c, MIN(t) AS t0, MAX(t) AS t1 FROM m GROUP BY eid, spk, b, mb)
                          SELECT -1 AS g, 0 AS k, '' AS podcast, '' AS spk, 0 AS n, 0 AS c, '' AS d, 0 AS t0, 0 AS t1
                          UNION ALL SELECT 0, p, podcast, spk, SUM(n), SUM(c), '', 0, 0 FROM g GROUP BY p, podcast, spk
                          UNION ALL SELECT 1, b, podcast, spk, SUM(n), 0, '', 0, 0 FROM g GROUP BY podcast, spk, b
                          UNION ALL SELECT 2, mb, podcast, spk, SUM(n), 0, '', 0, 0 FROM g GROUP BY podcast, spk, mb
                          UNION ALL SELECT 3, eid, MAX(podcast), spk, SUM(n), 0, MAX(d), MIN(t0), MAX(t1) FROM g GROUP BY eid, spk");
    $stmt->bindValue(':m', implode(' OR ', array_map(function ($a) { return fts_phrase($a, false); }, $alts)), SQLITE3_TEXT);
    foreach ($bind as $k => $v) $stmt->bindValue($k, $v, SQLITE3_TEXT);
    $res = $stmt->execute();
    $rows = $pos = $mins = $per = $fl = $epTot = [];
    while ($res && $r = $res->fetchArray(SQLITE3_ASSOC)) {
        if ($r['g'] < 0) continue;
        $spk = $r['spk'] ?? ''; $ps = $r['podcast'] . "\t" . $spk;
        // By episode the show is left out (null): the page knows it from the episode list.
        if ($r['g'] === 0) { $rows[] = $scale === 'episode' ? [$r['k'], null, $spk, $r['n'], $r['c']] : [(string) $r['k'], $r['podcast'], $spk, $r['n'], $r['c']]; continue; }
        if ($r['g'] !== 3) {
            if (!isset($pos[$ps])) { $pos[$ps] = [$r['podcast'], $spk, array_fill(0, 10, 0)]; $mins[$ps] = [$r['podcast'], $spk, array_fill(0, 24, 0)]; }
            if ($r['g'] === 1) $pos[$ps][2][(int) $r['k']] += $r['n']; else $mins[$ps][2][(int) $r['k']] += $r['n'];
            continue;
        }
        // Per episode and speaker: the episode's total (for the top list) and each person's first and last.
        $e = $r['k']; $per[] = [$e, $spk, $r['n']];
        $epTot[$r['podcast']][$e] = ($epTot[$r['podcast']][$e] ?? 0) + $r['n'];
        $when = $r['d'] . sprintf('%08d', $e);   // date order; same-day episodes by id
        if (!isset($fl[$ps])) $fl[$ps] = [$r['podcast'], $spk, $e, $r['t0'], $e, $r['t1'], $when, $when];
        if ($when < $fl[$ps][6]) { $fl[$ps][2] = $e; $fl[$ps][3] = $r['t0']; $fl[$ps][6] = $when; }
        if ($when > $fl[$ps][7]) { $fl[$ps][4] = $e; $fl[$ps][5] = $r['t1']; $fl[$ps][7] = $when; }
    }
    $keep = [];
    foreach ($epTot as $tot) { arsort($tot); $keep += array_slice($tot, 0, 40, true); }
    $top = array_values(array_filter($per, function ($x) use ($keep) { return isset($keep[$x[0]]); }));
    $fl = array_map(function ($x) { return [$x[0], $x[1], $x[2], (int) $x[3], $x[4], (int) $x[5]]; }, array_values($fl));
    echo json_encode(['q' => $q, 'rows' => $rows, 'pos' => array_values($pos), 'mins' => array_values($mins), 'top' => $top, 'fl' => $fl]);
    exit;
}
