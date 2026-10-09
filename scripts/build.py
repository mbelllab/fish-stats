"""Build the site from your transcripts, into site/ (a minute or two).

  1. data/search_index.db   indexer.py: every data/<show>/vtts/*.vtt, with speaker names
  2. descriptions           data/descriptions.json, if you have one ({episode: text});
                            used to guess guests' names and credit their facts
  3. speaker names          name_speakers.py, if you have voiceprints (data/work/voiceprints.npz),
                            then the index again so the names go in
  4. site/facts.json        build_facts.py: every headline fact
  5. site/statistics/*.json build_stats.py: the numbers for every stats page
  6. the pages              pages/ copied into site/, plus the episode list

    python scripts/build.py
"""
import glob, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, time

import paths
import episodes

PY = sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(paths.ROOT, "search_index.db")
SITE, PAGES = paths.SITE, paths.PAGES
KEYS = ",".join(s["key"] for s in paths.SHOWS)


def run(*args):
    print("  " + " ".join(os.path.basename(a) if i < 2 else a for i, a in enumerate([PY] + list(args))), flush=True)
    subprocess.run([PY] + list(args), check=True, cwd=HERE)


def descriptions():
    """data/descriptions.json ({"<episode file name, no .vtt>": "description"}) into the database."""
    src = os.path.join(paths.ROOT, "descriptions.json")
    if not os.path.exists(src):
        return
    d = json.load(open(src, encoding="utf-8"))
    con = sqlite3.connect(DB)
    known = {e: p for p, e in con.execute("SELECT podcast, episode FROM episodes WHERE media_type='podcast'")}
    con.executemany("INSERT OR REPLACE INTO episode_meta (podcast, episode, description, pub_date) VALUES (?, ?, ?, ?)",
                    [(known[e], e, text, episodes.date(e)) for e, text in d.items() if e in known])
    con.commit()
    print(f"  descriptions: {sum(e in known for e in d)} of {len(d)} match an episode", flush=True)


def build_data():
    print("Database, speaker names, facts and stats:", flush=True)
    run("indexer.py")
    descriptions()
    if os.path.exists(paths.PROFILES_PATH):
        run("name_speakers.py")
        run("indexer.py")
    os.makedirs(os.path.join(SITE, "statistics"), exist_ok=True)
    run("build_facts.py", "--db", DB, "--out", os.path.join(SITE, "facts.json"))
    run("build_stats.py", "--db", DB, "--out", os.path.join(SITE, "statistics"), "--root", SITE,
        "--shows", KEYS, "--all-kinds")


def build_episodes():
    """statistics/episodes.json (the episode list) and episode-links.js (where an episode's
    name links to: data/episode_links.json, {"<episode>": "https://..."}, if you have one)."""
    links_src = os.path.join(paths.ROOT, "episode_links.json")
    links = json.load(open(links_src, encoding="utf-8")) if os.path.exists(links_src) else {}
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = []
    for podcast, name, secs in con.execute(
            "SELECT e.podcast, e.episode, max(s.start_seconds) FROM episodes e JOIN segments s ON s.episode_id = e.id "
            "WHERE e.media_type = 'podcast' GROUP BY e.id ORDER BY e.episode"):
        rows.append({"d": episodes.date(name), "n": episodes.number(name), "t": episodes.title(name),
                     "u": links.get(name, ""), "s": secs or 0, "k": episodes.kind(podcast, name)})
    out = {"episodes": rows, "waiting": 0, "built": time.strftime("%Y-%m-%d %H:%M")}
    json.dump(out, open(os.path.join(SITE, "statistics", "episodes.json"), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(SITE, "episode-links.js"), "w", encoding="utf-8") as f:
        f.write("window.EPISODE_LINKS = " + json.dumps(links, ensure_ascii=False, separators=(",", ":")) + ";\n")
    print(f"Episode list: {len(rows)} episodes", flush=True)


def build_pages():
    for src in glob.glob(os.path.join(PAGES, "**", "*"), recursive=True):
        if os.path.isfile(src):
            dst = os.path.join(SITE, os.path.relpath(src, PAGES))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)


def stamp():
    """A version tag on every local .css/.js link (?v=<hash>), so browsers fetch a changed
    file instead of an old cached copy."""
    ref = re.compile(r'''((?:href|src)=")([^"#?:]+\.(?:css|js))(?:\?v=[0-9a-f]+)?(")''')
    for page in glob.glob(os.path.join(SITE, "**", "*.html"), recursive=True):
        base = os.path.dirname(page)
        html = open(page, encoding="utf-8").read()

        def tag(m):
            path = os.path.normpath(os.path.join(base, m[2]))
            if not os.path.isfile(path):
                return m[0]
            return f"{m[1]}{m[2]}?v={hashlib.sha1(open(path, 'rb').read()).hexdigest()[:8]}{m[3]}"
        open(page, "w", encoding="utf-8").write(ref.sub(tag, html))


def main():
    t0 = time.time()
    build_data()
    build_episodes()
    build_pages()
    stamp()
    print(f"Built {SITE} in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
