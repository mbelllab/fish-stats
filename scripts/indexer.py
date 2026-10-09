"""
The search database: FTS5 indexer.

Builds the full-text-search database from podcast VTT transcripts:

  episodes      one row per transcript, with web-root-relative paths
  segments      transcript text merged into sentence-sized chunks
  segments_fts  FTS5 index over segments.text (porter stemming)

Paths are stored with forward slashes (e.g. "No Such Thing As A Fish/mp3s/....mp3")
so they work as URLs. Unchanged files are skipped on re-run and removed files are pruned.
Each segment keeps its [Speaker N] label (a segment never spans two speakers) plus the
name from name_speakers.py (data/work/speakers/<Podcast>/<episode>.json), if any.

If edits.db (downloaded from the server) sits in the web root, its text and speaker
corrections are baked in so they become searchable. The automatic names stay in
auto_speakers (one row per named voice), which the bake never touches, so "Reset to
automatic" still works afterwards.

Usage:
    python indexer.py [output_db]
    # Default output is search_index.db (the live DB). Pass a different filename
    # to build a separate copy without touching the live DB.
    python indexer.py --edits=PATH ...
    # Bake a different edits.db (test_api.py uses a throwaway one).
    python indexer.py --vacuum
    # Also VACUUM (compact) the file. Done automatically only when episodes
    # were pruned: a VACUUM lays the whole file out afresh, so rsync's delta
    # transfer then has to send nearly all of it (~130 MB) again.
"""

import os
import re
import sys
import shutil
import time
import json
import sqlite3


# Run relative to the web root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
from name_fixes import fix_name
os.chdir(paths.ROOT)

PODCASTS = paths.PODCASTS     # the shows (curated/shows.json)

# Flush a segment at a sentence boundary once it is at least MIN_CHARS long,
# or force a flush at MAX_CHARS.
MIN_CHARS = 60
MAX_CHARS = 320

CUE_RE = re.compile(
    r'(\d{2}:\d{2}(?::\d{2})?\.\d{3})\s*-->\s*(\d{2}:\d{2}(?::\d{2})?\.\d{3})'
    r'[^\n]*\n(.*?)(?=\n\s*\n|\n\d{2}:\d{2}|\Z)',
    re.DOTALL,
)
BRACKET_RE = re.compile(r'\[[^\]]*\]')          # [Speaker 1], [music], etc.
LABEL_RE = re.compile(r'^\s*\[(Speaker \d+)\]')
LABEL_SUB_RE = re.compile(r'\[Speaker \d+\]')
SPEAKERS_DIR = paths.SPEAKERS_DIR
EDITS_DB = "edits.db"
WS_RE = re.compile(r'\s+')
SENT_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')
SENT_END_RE = re.compile(r'[.!?]["\')\]]?$')


def ts_to_seconds(t):
    parts = [float(p) for p in t.split(':')]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]


def clean_text(text):
    text = BRACKET_RE.sub('', text)
    return WS_RE.sub(' ', text).strip()


def merge_cues(cues):
    """Merge tiny VTT cues into sentence-sized segments, never across a
    change of speaker.

    cues: iterable of (start_seconds, end_seconds, raw_text)
    yields: (start_seconds, end_seconds, text, label)   label e.g. "Speaker 3" or ""
    """
    buf = ""
    seg_start = None
    seg_end = None
    seg_label = ""
    for start, end, raw in cues:
        m = LABEL_RE.match(raw)
        label = m.group(1) if m else ""
        text = clean_text(raw)
        if not text:
            continue
        if buf and label != seg_label:
            yield (seg_start, seg_end, buf, seg_label)
            buf = ""
            seg_start = None
        if seg_start is None:
            seg_start = start
            seg_label = label
        seg_end = end
        buf = (buf + " " + text).strip() if buf else text
        if (len(buf) >= MIN_CHARS and SENT_END_RE.search(buf)) or len(buf) >= MAX_CHARS:
            yield (seg_start, seg_end, buf, seg_label)
            buf = ""
            seg_start = None
            seg_end = None
    if buf:
        yield (seg_start, seg_end, buf, seg_label)


def speakers_file(podcast, base):
    return os.path.join(SPEAKERS_DIR, podcast, base + ".json")


# Voices named by hand (curated/speaker_names.json): {podcast: {episode: {label: name}}}.
# They win over name_speakers.py's guess, and are the "automatic" name that Reset goes back to.
MANUAL_PATH = os.path.join(paths.CURATED, "speaker_names.json")
MANUAL = {}
if os.path.exists(MANUAL_PATH):
    with open(MANUAL_PATH, encoding="utf-8") as _f:
        MANUAL = {k: v for k, v in json.load(_f).items() if not k.startswith("_")}



def load_speaker_names(podcast, base):
    """{"Speaker 3": ("Dan", "voiceprint"), ...} from name_speakers.py output,
    with any hand-made names on top."""
    path = speakers_file(podcast, base)
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
    # fix_name: guest names guessed from a description keep the feed's spelling; the
    # transcript name fixes correct them here too ("Ella Al Shamahi" -> "Ella Al-Shamahi").
    names = {lab: (fix_name(i.get("name") or ""), i.get("source") or "")
             for lab, i in data.items() if i.get("name")}
    for lab, name in MANUAL.get(podcast, {}).get(base, {}).items():
        names[lab] = (name, "manual")
    return names


# Sentences moved to the voice they really sound like (check_sentences.py):
# {"<cue start, ms>": "Speaker N"} per episode, applied before sentences are joined into lines.
MOVES_DIR = os.path.join(paths.DATA, "sentence_voices")


def load_moves(podcast, base):
    path = os.path.join(MOVES_DIR, podcast, base + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), os.path.getmtime(path)
    except (OSError, ValueError):
        return {}, 0


def parse_vtt(path, moves=None):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()
    def cue(s, e, txt):
        to = moves.get(str(int(round(ts_to_seconds(s) * 1000)))) if moves else None
        if to:
            txt = LABEL_SUB_RE.sub(f"[{to}]", txt, count=1)
        return ts_to_seconds(s), ts_to_seconds(e), txt
    return list(merge_cues(cue(s, e, txt) for s, e, txt in CUE_RE.findall(content)))




def ensure_schema(conn):
    """Create the tables. Returns True when auto_speakers was just added to a
    database that already had episodes (filled in by fill_auto_speakers())."""
    # An old segments layout (with `seq` or `end_seconds`) is dropped and rebuilt;
    # that forces one full reindex.
    existing = [r[1] for r in conn.execute("PRAGMA table_info(segments)")]
    if existing and ('seq' in existing or 'end_seconds' in existing
                     or 'label' not in existing):
        conn.executescript(
            "DROP TABLE IF EXISTS segments_fts;"
            "DROP TABLE IF EXISTS segments;"
            "DROP TABLE IF EXISTS episodes;"
            "DROP TABLE IF EXISTS auto_speakers;"
        )
    had_auto = conn.execute("SELECT 1 FROM sqlite_master WHERE name='auto_speakers'").fetchone()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS episodes (
            id           INTEGER PRIMARY KEY,
            media_type   TEXT NOT NULL,           -- 'podcast'
            podcast      TEXT NOT NULL,           -- podcast name or book title
            episode      TEXT NOT NULL,           -- episode base name or book title
            vtt_path     TEXT NOT NULL DEFAULT '',-- web-relative .vtt (podcasts)
            audio_path   TEXT NOT NULL DEFAULT '',-- web-relative .mp3
            source_mtime REAL NOT NULL,
            vtt_mtime    REAL NOT NULL DEFAULT 0, -- transcript file only: edits older than it are stale
            indexed_at   REAL NOT NULL,
            UNIQUE(media_type, podcast, episode)
        );
        -- start_seconds is whole seconds, plenty for audio seeking.
        CREATE TABLE IF NOT EXISTS segments (
            id            INTEGER PRIMARY KEY,
            episode_id    INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            start_seconds INTEGER NOT NULL,
            text          TEXT NOT NULL,
            locator       TEXT NOT NULL DEFAULT '',
            label         TEXT NOT NULL DEFAULT '', -- raw diarization label, "Speaker 3"
            speaker       TEXT NOT NULL DEFAULT '', -- display name ("Dan"), '' if unknown
            speaker_src   TEXT NOT NULL DEFAULT ''  -- voiceprint | sure | guess | manual | ident | edit | edit-line
            -- (old rows may say 'manual': hand-set names from an early tool;
            --  treated like any other name)
        );
        CREATE INDEX IF NOT EXISTS idx_segments_episode ON segments(episode_id);
        -- The automatic name of each named voice (from name_speakers.py), kept apart
        -- from segments.speaker because baking website edits overwrites that.
        -- api.php and apply_edits() reset a voice or line to this; a voice with no
        -- row here has no automatic name ('').
        CREATE TABLE IF NOT EXISTS auto_speakers (
            episode_id  INTEGER NOT NULL,
            label       TEXT NOT NULL,            -- "Speaker 3"
            speaker     TEXT NOT NULL,            -- "Dan"
            speaker_src TEXT NOT NULL,            -- voiceprint | guess | sure ...
            PRIMARY KEY (episode_id, label)
        ) WITHOUT ROWID;
        CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts
            USING fts5(text, content='segments', content_rowid='id',
                       tokenize='porter unicode61 remove_diacritics 2');
        -- Episode descriptions from the RSS feeds (filled by fetch_metadata.py);
        -- created empty here so the API's LEFT JOIN works on a fresh DB.
        CREATE TABLE IF NOT EXISTS episode_meta (
            podcast     TEXT NOT NULL,
            episode     TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            pub_date    TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (podcast, episode)
        );
    """)
    if 'vtt_mtime' not in [r[1] for r in conn.execute("PRAGMA table_info(episodes)")]:
        conn.execute("ALTER TABLE episodes ADD COLUMN vtt_mtime REAL NOT NULL DEFAULT 0")
    return not had_auto and conn.execute("SELECT 1 FROM episodes LIMIT 1").fetchone() is not None


def fill_auto_speakers(conn):
    """One-off for a database made before auto_speakers existed: its unchanged
    episodes are skipped, so fill their automatic names from the same speaker
    files upsert_episode() reads (an episode is re-indexed whenever its file
    changes, so the files still match what was indexed)."""
    rows = []
    for eid, pod, ep in conn.execute("SELECT id, podcast, episode FROM episodes "
                                     "WHERE media_type='podcast'").fetchall():
        rows += [(eid, lab, n, s) for lab, (n, s) in load_speaker_names(pod, ep).items()]
    conn.executemany("INSERT OR REPLACE INTO auto_speakers VALUES (?,?,?,?)", rows)
    print(f"  added automatic names for {len(rows)} voices (auto_speakers)")


def get_existing_episode(conn, media_type, podcast, episode):
    row = conn.execute(
        "SELECT id, source_mtime FROM episodes WHERE media_type=? AND podcast=? AND episode=?",
        (media_type, podcast, episode),
    ).fetchone()
    return row


def delete_episode(conn, episode_id):
    conn.execute("DELETE FROM segments WHERE episode_id=?", (episode_id,))
    conn.execute("DELETE FROM auto_speakers WHERE episode_id=?", (episode_id,))
    conn.execute("DELETE FROM episodes WHERE id=?", (episode_id,))


def upsert_episode(conn, media_type, podcast, episode, vtt_path, audio_path,
                   mtime, segments, names=None, vtt_mtime=0):
    """Replace an episode's row + segments. `segments` is a list of
    (start, end, text, label). `names` maps label -> (name, source)."""
    existing = get_existing_episode(conn, media_type, podcast, episode)
    if existing:
        delete_episode(conn, existing[0])
    cur = conn.execute(
        "INSERT INTO episodes (media_type, podcast, episode, vtt_path, audio_path, "
        "source_mtime, vtt_mtime, indexed_at) VALUES (?,?,?,?,?,?,?,?)",
        (media_type, podcast, episode, vtt_path, audio_path, mtime, vtt_mtime, time.time()),
    )
    episode_id = cur.lastrowid
    rows = []
    names = dict(names or {})
    for s, _e, t, extra in segments:
        name, src = names.get(extra, ('', ''))
        rows.append((episode_id, int(round(s)), t, '', extra, name, src))
    conn.executemany(
        "INSERT INTO segments (episode_id, start_seconds, text, locator, label, "
        "speaker, speaker_src) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    if media_type == 'podcast' and names:
        conn.executemany("INSERT INTO auto_speakers VALUES (?,?,?,?)",
                         [(episode_id, lab, n, s) for lab, (n, s) in names.items()])
    return len(segments)


def split_head(line, tail):
    """A split's first part (api.php split_head): the line without its end `tail`, or None
    when tail isn't the line's end or would leave nothing in front of it."""
    if not tail or len(tail) >= len(line) or not line.endswith(tail):
        return None
    return line[:-len(tail)].rstrip() or None


WORDS_RE = re.compile(r"[^\w\s]+")


def words_only(s):
    """Words only (api.php words_only), for finding a line's words whatever the punctuation."""
    return " ".join(WORDS_RE.sub("", s.lower()).replace("_", "").split())


def episode_cues(vtt_rel):
    """The transcript file's cues as [(start, end, text)], labels taken off."""
    try:
        with open(vtt_rel, encoding="utf-8", errors="replace") as f:
            content = f.read().replace("\r\n", "\n")
    except OSError:
        return []
    return [(ts_to_seconds(a), ts_to_seconds(b), clean_text(t)) for a, b, t in CUE_RE.findall(content)]


def tail_start(cues, start, head, tail, nxt):
    """When the split-off part of a line starts (api.php tail_start): its first words found in
    the cues after the line's start (past the first part's words), the time inside that cue by
    position. Whole seconds, at least the line's start and no later than the next line; the
    line's start when the words can't be found (an edited line)."""
    joined, at = "", []
    for i, (a, b, text) in enumerate(cues):
        if a < start - 1:
            continue
        if a > start + 90:
            break
        w = words_only(text)
        if not w:
            continue
        at.append((len(joined) + (1 if joined else 0), i))
        joined += (" " if joined else "") + w
    key = " ".join(words_only(tail).split()[:4])
    if not key or not at:
        return start
    h = words_only(head)
    frm = joined.find(h[:40])
    frm = 0 if frm < 0 else frm + max(0, len(h) - 10)
    pos = joined.find(key, min(frm, len(joined)))
    if pos < 0:
        pos = joined.find(key)
    if pos < 0:
        return start
    k = max(n for n, (off, _) in enumerate(at) if off <= pos) if any(off <= pos for off, _ in at) else 0
    a, b, text = cues[at[k][1]]
    ln = len(words_only(text))
    t = a + ((pos - at[k][0]) / ln if ln else 0) * max(0, b - a)
    return int(max(start, min(int(t // 1), nxt)))


EDIT_REACH = 30   # seconds: how far a line edit looks for its line when the time moved (api.php)


def find_line(conn, eid, start, text):
    """The segment an edit is about (api.php find_line): that start second and text, or else
    the line with that text nearest in time, within EDIT_REACH seconds. (id, start) or None."""
    return conn.execute("SELECT id, start_seconds FROM segments WHERE episode_id=? AND text=? "
                        "AND start_seconds BETWEEN ? AND ? ORDER BY abs(start_seconds - ?), id LIMIT 1",
                        (eid, text, start - EDIT_REACH, start + EDIT_REACH, start)).fetchone()


def apply_edits(conn, edits_path):
    """Bake edits.db (downloaded from the website) into the segments.

    Same rules as api.php's live overlay: edits run oldest-first, and a line
    edit targets the line found by find_line() whose CURRENT text equals the
    edit's `orig` - so re-applying edits that are already baked in is a
    harmless no-op. Edits older than the episode's transcript file (vtt_mtime)
    were made on a previous transcript and are skipped. Returns the number of
    edits that changed something."""
    if not os.path.exists(edits_path):
        return 0
    ed = sqlite3.connect(edits_path)
    try:
        rows = ed.execute(
            "SELECT podcast, episode, kind, seg_start, orig, label, value, ts "
            "FROM edits ORDER BY id").fetchall()
    except sqlite3.Error:
        return 0
    finally:
        ed.close()
    def auto(eid, label):  # the automatic (name, source) of a voice
        r = conn.execute("SELECT speaker, speaker_src FROM auto_speakers WHERE episode_id=? AND label=?",
                         (eid, label)).fetchone()
        return r or ('', '')
    ids, cues = {}, {}
    n = 0
    for pod, ep, kind, start, orig, label, value, ts in rows:
        key = (pod, ep)
        if key not in ids:
            r = conn.execute("SELECT id, vtt_mtime, vtt_path FROM episodes WHERE podcast=? AND episode=? "
                             "AND media_type='podcast'", key).fetchone()
            ids[key] = r if r else (None, 0, '')
        eid, since, vtt_rel = ids[key]
        if eid is None or int(ts) < int(since):  # missing, or made on an older transcript
            continue
        if kind == 'reviewed':   # the "fully reviewed" mark: about the episode, not its lines
            continue
        if kind == 'rename':
            # Every line of that label except lines reassigned one by one;
            # an empty value means back to the automatic name.
            name, src = (value, 'edit') if value else auto(eid, label)
            cur = conn.execute("UPDATE segments SET speaker=?, speaker_src=? WHERE episode_id=? "
                               "AND label=? AND speaker_src!='edit-line'",
                               (name, src, eid, label))
            n += cur.rowcount > 0
            continue
        line = find_line(conn, eid, start, orig)
        if kind == 'restore':
            # a deleted line back where it was (it is gone from the segments once a delete is baked)
            if line:
                continue
            name, src = auto(eid, label)
            conn.execute("INSERT INTO segments (episode_id, start_seconds, text, locator, label, speaker, speaker_src) "
                         "VALUES (?,?,?,'',?,?,?)", (eid, start, orig, label, name, src))
            n += 1
            continue
        if not line:
            continue
        sid, sstart = line
        if kind == 'text':
            conn.execute("UPDATE segments SET text=? WHERE id=?", (value, sid))
        elif kind == 'line_speaker':
            if value:
                name, src = value, 'edit-line'
            else:  # back to the automatic name for this line's voice
                lab = conn.execute("SELECT label FROM segments WHERE id=?", (sid,)).fetchone()
                name, src = auto(eid, lab[0] if lab else '')
            conn.execute("UPDATE segments SET speaker=?, speaker_src=? WHERE id=?", (name, src, sid))
        elif kind == 'delete':
            conn.execute("DELETE FROM segments WHERE id=?", (sid,))
        elif kind == 'split':
            # value = the end of the line, which becomes a line of its own (same voice), starting
            # when those words are said in the transcript file
            head = split_head(orig, value)
            if head is None:
                continue
            if vtt_rel not in cues:
                cues[vtt_rel] = episode_cues(vtt_rel)
            nxt = conn.execute("SELECT min(start_seconds) FROM segments WHERE episode_id=? AND start_seconds>?",
                               (eid, sstart)).fetchone()[0]
            t = tail_start(cues[vtt_rel], sstart, head, value, nxt if nxt is not None else sstart + 3600)
            lab, spk, src = conn.execute("SELECT label, speaker, speaker_src FROM segments WHERE id=?", (sid,)).fetchone()
            conn.execute("UPDATE segments SET text=? WHERE id=?", (head, sid))
            conn.execute("INSERT INTO segments (episode_id, start_seconds, text, locator, label, speaker, speaker_src) "
                         "VALUES (?,?,?,'',?,?,?)", (eid, t, value, lab, spk, src))
        elif kind == 'join':
            # orig = the first part, value = the line split off it (the next line with that text)
            b = conn.execute("SELECT id FROM segments WHERE episode_id=? AND text=? AND start_seconds BETWEEN ? AND ? "
                             "AND id != ? ORDER BY start_seconds, id LIMIT 1",
                             (eid, value, sstart, sstart + EDIT_REACH, sid)).fetchone()
            if not b:
                continue
            conn.execute("DELETE FROM segments WHERE id=?", (b[0],))
            conn.execute("UPDATE segments SET text=? WHERE id=?", (orig + " " + value, sid))
        else:
            continue
        n += 1
    return n


def build_index(db_path, vacuum=False, edits=EDITS_DB):
    # Work on a copy and swap it in at the end, so a crash mid-build (or an
    # upload during it) never sees a half-written live database. The fast,
    # crash-unsafe pragmas in _build are fine on the throwaway copy.
    # upload.sh skips *.tmp.
    tmp_path = db_path + ".tmp"
    if os.path.exists(db_path):
        shutil.copyfile(db_path, tmp_path)
    elif os.path.exists(tmp_path):
        os.remove(tmp_path)
    try:
        _build(tmp_path, db_path, vacuum, edits)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    os.replace(tmp_path, db_path)


def _build(db_path, live_path, vacuum=False, edits=EDITS_DB):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.execute("PRAGMA synchronous=OFF")
    if ensure_schema(conn):
        fill_auto_speakers(conn)

    seen = set()  # (media_type, podcast, episode) present on disk this run
    stats = {"episodes": 0, "segments": 0, "skipped": 0}

    # 1. Podcasts
    for podcast in PODCASTS:
        vtt_dir = os.path.join(podcast, "vtts")
        if not os.path.isdir(vtt_dir):
            print(f"  (no vtts folder for {podcast}, skipping)")
            continue
        for filename in sorted(os.listdir(vtt_dir)):
            if not filename.endswith(".vtt"):
                continue
            base = filename[:-4]
            key = ("podcast", podcast, base)
            seen.add(key)

            vtt_fs_path = os.path.join(vtt_dir, filename)
            mtime = vtt_mtime = os.path.getmtime(vtt_fs_path)
            spk_path = speakers_file(podcast, base)
            if os.path.exists(spk_path):  # re-index when speaker names change too
                mtime = max(mtime, os.path.getmtime(spk_path))
            if base in MANUAL.get(podcast, {}):  # or when a hand-made name is added
                mtime = max(mtime, os.path.getmtime(MANUAL_PATH))
            moves, moved_at = load_moves(podcast, base)   # or sentences are moved
            mtime = max(mtime, moved_at)

            existing = get_existing_episode(conn, *key)
            if existing and abs(existing[1] - mtime) < 1e-6:
                # fills vtt_mtime on rows indexed before that column existed
                conn.execute("UPDATE episodes SET vtt_mtime=? WHERE id=? AND vtt_mtime!=?",
                             (vtt_mtime, existing[0], vtt_mtime))
                stats["skipped"] += 1
                continue

            vtt_rel = f"{podcast}/vtts/{base}.vtt"
            mp3_rel = f"{podcast}/mp3s/{base}.mp3"

            segments = parse_vtt(vtt_fs_path, moves)
            if not segments:
                continue
            n = upsert_episode(conn, "podcast", podcast, base, vtt_rel, mp3_rel,
                               mtime, segments, load_speaker_names(podcast, base), vtt_mtime)
            stats["episodes"] += 1
            stats["segments"] += n
        print(f"  indexed podcast: {podcast}")


    # 3. Prune episodes whose source files are gone
    all_rows = conn.execute(
        "SELECT id, media_type, podcast, episode FROM episodes"
    ).fetchall()
    pruned = 0
    for eid, mt, pod, ep in all_rows:
        if (mt, pod, ep) not in seen:
            delete_episode(conn, eid)
            pruned += 1
    if pruned:
        print(f"  pruned {pruned} removed episodes")

    # 4. Bake in corrections made on the website (edits.db)
    applied = apply_edits(conn, edits)
    if applied:
        print(f"  applied {applied} website edits from {edits}")

    # 5. Rebuild the FTS index from the segments content table
    conn.execute("INSERT INTO segments_fts(segments_fts) VALUES('rebuild')")
    conn.commit()

    total_ep = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    total_seg = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    if pruned or vacuum:   # see --vacuum in the docstring
        conn.execute("VACUUM")
    conn.close()

    print(f"\nDone. {stats['episodes']} episodes (re)indexed, "
          f"{stats['skipped']} unchanged/skipped.")
    print(f"Database now holds {total_ep} episodes / {total_seg} segments "
          f"-> {live_path}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--vacuum" and not a.startswith("--edits=")]
    edits = next((a[8:] for a in sys.argv[1:] if a.startswith("--edits=")), EDITS_DB)
    out = args[0] if args else "search_index.db"
    build_index(out, vacuum="--vacuum" in sys.argv[1:], edits=edits)
