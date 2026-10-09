"""Build the data behind the Stats pages (stats.html, stats/*.html).

Reads search_index.db (every transcript line with its speaker and time), facts.json
and writes one JSON file per stats page into statistics/.

Word lists (penis words, swears, catchphrases) live in curated/stats_words.json
so they can be changed without touching code.

Some pages each have their own module (stats_flow, stats_style, stats_mentions, stats_pod);
main() runs them on the same loaded episodes and writes their JSON with the rest.

The Fact stats page reads the site's own facts.json (nothing is copied into
statistics/); this script only reads it for summary.json.

    python build_stats.py

Another site can build its stats from its own database with the same code (fish stats
does, for the public Fish-only site). Each option changes one thing; none given = as above:
    python build_stats.py --db PATH --out DIR --root DIR --shows fish
      --db     the database to read (default search_index.db)
      --out    where the JSON goes (default statistics/)
      --root   the folder with that site's facts.json (default the web root)
      --shows  show keys from shows.json, comma-separated (default all of them)
      --skip   pages not to build, comma-separated
      --all-kinds  the hub's headline numbers count every episode, bonus ones too
      --pod-read   the Friends and enemies reads to use (default curated/pod_friends.json)
"""
import collections, json, os, re, sqlite3, statistics, sys, time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB = os.path.join(ROOT, "search_index.db")
OUT = os.path.join(ROOT, "statistics")
from paths import CURATED, PODCASTS, HOSTS, FISH   # the shows: curated/shows.json
import episodes                                                      # date / number / title / kind of an episode name
WORDS = os.path.join(CURATED, "stats_words.json")
NOT_GUESTS = set()
NOT_PEOPLE = re.compile(r"^(guest|unknown)\b", re.I)
GAP_CAP = 20          # a line "lasts" until the next line, but never more than this many seconds
SLOW = 1.5            # ...nor longer than its words take at a slow pace (words a second): silences and music don't count
ALL_KINDS = False     # --all-kinds: the headline numbers count every episode, not only the main ones
POD_READ = None       # --pod-read: the Friends and enemies reads to use instead of curated/pod_friends.json

def words_config():
    """The tracked word lists. No built-in fallback: a missing file should fail loudly."""
    return json.load(open(WORDS, encoding="utf-8"))


def rx(words):
    words = sorted(set(w.lower() for w in words), key=len, reverse=True)
    return re.compile(r"(?<![\w'])(" + "|".join(re.escape(w).replace(r"\ ", r"[\s-]+") for w in words) + r")(?![\w'])", re.I) if words else None


def load():
    con = sqlite3.connect(DB)
    eps = {}
    for eid, podcast, ep in con.execute("SELECT id, podcast, episode FROM episodes WHERE media_type='podcast'"):
        if podcast in PODCASTS:
            eps[eid] = {"id": eid, "podcast": podcast, "episode": ep, "date": episodes.date(ep), "num": episodes.number(ep),
                        "title": episodes.title(ep), "kind": episodes.kind(podcast, ep), "segs": []}
    for eid, t, text, spk in con.execute("SELECT episode_id, start_seconds, text, coalesce(speaker,'') FROM segments ORDER BY episode_id, start_seconds, id"):
        if eid in eps:
            eps[eid]["segs"].append((t, text, spk))
    return [e for e in eps.values() if e["segs"] and e["date"]]


def durations(segs):
    """Seconds of talk per line: until the next line starts, at most GAP_CAP (5 for the last), and
    never longer than the line's words take at SLOW words a second (the rest is a pause).
    Lines that start in the same second (a line split on the site, where someone else cuts in,
    or short lines the transcriber put in one second) share that time by how much each says."""
    out = []
    i = 0
    while i < len(segs):
        t = segs[i][0]
        j = i
        while j < len(segs) and segs[j][0] == t:
            j += 1
        nxt = segs[j][0] if j < len(segs) else t + 5
        group = segs[i:j]
        say = sum(len(text.split()) for _, text, _ in group) / SLOW
        total = max(len(group), min(GAP_CAP, nxt - t, say))
        chars = [max(1, len(text)) for _, text, _ in group]
        out += [total * c / sum(chars) for c in chars]
        i = j
    return out


def ep_ref(e):
    return {"p": e["podcast"], "e": e["episode"], "t": e["title"], "d": e["date"], "n": e["num"], "k": e["kind"]}


def is_person(name):
    return bool(name) and not NOT_PEOPLE.search(name)


def build_talk(eps):
    rows = []
    for e in eps:
        durs = durations(e["segs"])
        per = collections.defaultdict(lambda: {"sec": 0, "words": 0, "turns": 0})
        last = None
        for (t, text, spk), d in zip(e["segs"], durs):
            if not is_person(spk):
                last = None
                continue
            p = per[spk]
            p["sec"] += d
            p["words"] += len(text.split())
            if spk != last:
                p["turns"] += 1
            last = spk
        rows.append({**ep_ref(e), "len": round(e["segs"][-1][0] + durs[-1]),
                     "s": {k: [round(v["sec"]), v["words"], v["turns"]] for k, v in per.items() if v["sec"] >= 30}})
    return {"hosts": HOSTS, "episodes": rows}


def build_ttp(eps, cfg):
    levels = [("strict", rx(cfg["penis_strict"])), ("euph", rx(cfg["penis_euphemisms"])), ("ambig", rx(cfg["penis_ambiguous"]))]
    nots = rx(cfg.get("penis_not", []))
    rows = []
    for e in eps:
        if e["podcast"] != FISH:
            continue
        hit = {}
        for t, text, spk in e["segs"]:
            clean = nots.sub(" ", text) if nots else text
            for name, r in levels:
                if name in hit or not r:
                    continue
                m = r.search(clean)
                if m:
                    hit[name] = {"t": t, "w": m[1].lower(), "who": spk, "line": text[:220]}
            if len(hit) == len(levels):
                break
        count = {name: sum(len(r.findall(nots.sub(" ", x[1]) if nots else x[1])) for x in e["segs"]) for name, r in levels if r}
        rows.append({**ep_ref(e), "len": e["segs"][-1][0], "hit": hit, "count": count})
    return {"words": {k: cfg[k] for k in ("penis_strict", "penis_euphemisms", "penis_ambiguous", "penis_not")}, "episodes": rows}


def build_counts(eps, words, key):
    """Per episode, per speaker: how many times each word/phrase in the list was said."""
    r = rx(words)
    rows = []
    for e in eps:
        per = collections.defaultdict(collections.Counter)
        for t, text, spk in e["segs"]:
            for m in r.finditer(text):
                per[spk or "?"][re.sub(r"[\s-]+", " ", m[1].lower())] += 1
        if per:
            ref = ep_ref(e)
            del ref["k"]            # the Catchphrases and Swear jar pages don't use the episode type
            rows.append({**ref, key: {s: dict(c) for s, c in per.items()}})
    return rows


ALIASES = {"Rhys": "Rhys Darby"}


def build_guests(talk):
    g = collections.defaultdict(lambda: {"eps": []})
    for row in talk["episodes"]:
        hosts = set(HOSTS[row["p"]])
        for name, (sec, words, turns) in row["s"].items():
            if name in hosts or name in NOT_GUESTS or not is_person(name) or sec < 60:
                continue
            g[ALIASES.get(name, name)]["eps"].append({"p": row["p"], "e": row["e"], "t": row["t"], "d": row["d"], "n": row["n"], "sec": sec})
    out = []
    for name, v in g.items():
        eps = sorted(v["eps"], key=lambda x: x["d"])
        out.append({"name": name, "count": len(eps), "shows": sorted({x["p"] for x in eps}), "first": eps[0]["d"], "last": eps[-1]["d"],
                    "talk": sum(x["sec"] for x in eps), "eps": eps})
    out.sort(key=lambda x: (-x["count"], x["name"]))
    return {"guests": out}


def build_lineup(talk):
    fish = [r for r in talk["episodes"] if r["p"] == FISH and r["k"] == "Main"]
    hosts = HOSTS[FISH]
    rows = []
    for r in fish:
        present = [h for h in hosts if r["s"].get(h, [0])[0] >= 120]
        guests = [n for n, v in r["s"].items() if n not in hosts and is_person(n) and v[0] >= 120]
        rows.append({"e": r["e"], "t": r["t"], "d": r["d"], "n": r["n"], "hosts": present, "guests": guests})
    return {"hosts": hosts, "episodes": rows}


def build_summary(d):
    """Headline numbers for the cards on stats.html."""
    out = {}
    main = [e for e in d["ttp"]["episodes"] if ALL_KINDS or e["k"] == "Main"]
    hits = sorted((e for e in main if "strict" in e["hit"]), key=lambda e: e["hit"]["strict"]["t"])
    if hits:
        f = hits[0]
        out["ttp"] = {"with": len(hits), "of": len(main), "median": statistics.median(e["hit"]["strict"]["t"] for e in hits),
                      "fastest": {"t": f["hit"]["strict"]["t"], "who": f["hit"]["strict"]["who"], "n": f["n"], "e": f["e"]}}
    fish = [r for r in d["talk"]["episodes"] if r["p"] == FISH and (ALL_KINDS or r["k"] == "Main")]
    shares = collections.defaultdict(list)
    for r in fish:
        tot = sum(v[0] for k, v in r["s"].items() if k in HOSTS[FISH]) or 1
        for h in HOSTS[FISH]:
            if h in r["s"]:
                shares[h].append(r["s"][h][0] / tot)
    out["talk"] = sorted(([h, round(100 * statistics.mean(v), 1)] for h, v in shares.items() if v), key=lambda x: -x[1])
    out["guests"] = [[g["name"], g["count"]] for g in d["guests"]["guests"][:3]]
    sw = collections.Counter(); sec = collections.Counter()
    for r in d["talk"]["episodes"]:
        for k, v in r["s"].items():
            sec[k] += v[0]
    for r in d["swears"]["episodes"]:
        for k, c in r["c"].items():
            sw[k] += sum(c.values())
    rate = sorted(((k, 3600 * sw[k] / sec[k]) for k in sw if sec[k] > 20 * 3600), key=lambda x: -x[1])
    out["swears"] = [[k, round(v, 1)] for k, v in rate[:3]]
    ph = collections.Counter()
    for r in d["phrases"]["episodes"]:
        for c in r["c"].values():
            ph.update(c)
    out["phrases"] = ph.most_common(3)
    who = collections.Counter(f["who"] for e in d["facts"]["fish"] for f in e["facts"] if f.get("who"))
    out["facts"] = {"total": sum(len(e["facts"]) for e in d["facts"]["fish"]), "top": who.most_common(4)}
    lu = d["lineup"]["episodes"]
    out["lineup"] = {"all4": sum(1 for r in lu if len(r["hosts"]) == 4), "of": len(lu)}
    out["episodes"] = len(d["talk"]["episodes"])
    out["hours"] = round(sum(r["len"] for r in d["talk"]["episodes"]) / 3600)
    # hours of audio per show per year, for the word tracker's "per hour" view
    hy = collections.defaultdict(lambda: collections.defaultdict(float))
    for r in d["talk"]["episodes"]:
        hy[r["p"]][r["d"][:4]] += r["len"] / 3600
    out["hours_by_year"] = {p: {y: round(h, 2) for y, h in sorted(v.items())} for p, v in hy.items()}
    out["hosts"] = HOSTS        # the word tracker's "Hosts only" filter
    hosts = set(HOSTS[FISH])
    best = max((r for r in d["flow"]["runs"].get(FISH, []) if r["w"] in hosts and (ALL_KINDS or r["k"] == "Main")), key=lambda r: r["s"], default=None)
    if best:
        out["flow"] = {"who": best["w"], "s": best["s"]}
    if d["style"].get("vocab"):
        out["style"] = d["style"]["vocab"][0][:2]                       # [name, different words per 1,000]
    m = d["mentions"]
    out["mentions"] = {p: round(sum(sum(c["c"].get(p, {}).values()) for c in m["cryptids"]) / (sum(m["hours"].get(p, {}).values()) or 1), 1)
                       for p in PODCASTS}                               # cryptid mentions per hour, per show
    out["pod"] = {"friends": sum(1 for x in d["pod"]["people"] if "f" in x), "enemies": sum(1 for x in d["pod"]["people"] if "x" in x)}
    return out


def configure(argv):
    """The command-line options above. They replace the module's settings, which the
    stats_* modules read at build time (B.DB, B.PODCASTS, B.HOSTS), so they follow too.
    Returns the pages to skip."""
    global DB, OUT, ROOT, PODCASTS, HOSTS, ALL_KINDS, POD_READ
    import argparse, paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--db"); ap.add_argument("--out"); ap.add_argument("--root")
    ap.add_argument("--shows"); ap.add_argument("--skip", default="")
    ap.add_argument("--all-kinds", action="store_true"); ap.add_argument("--pod-read")
    a = ap.parse_args(argv)
    ALL_KINDS = a.all_kinds
    POD_READ = os.path.abspath(a.pod_read) if a.pod_read else None
    DB = os.path.abspath(a.db) if a.db else DB
    OUT = os.path.abspath(a.out) if a.out else OUT
    ROOT = os.path.abspath(a.root) if a.root else ROOT
    if a.shows:
        names = {s["key"]: s["name"] for s in paths.SHOWS}
        PODCASTS = [names[k] for k in a.shows.split(",")]
        HOSTS = {p: HOSTS[p] for p in PODCASTS}
    return set(filter(None, a.skip.split(",")))


def load_json(name):
    p = os.path.join(ROOT, name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}


def main(argv=None):
    skip = configure(argv)
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    cfg = words_config()
    eps = load()
    talk = build_talk(eps)
    data = {
        "talk": talk,
        "ttp": build_ttp(eps, cfg),
        "guests": build_guests(talk),
        "swears": {"words": cfg["swears"], "episodes": build_counts(eps, cfg["swears"], "c")},
        "phrases": {"words": cfg["catchphrases"], "episodes": build_counts(eps, cfg["catchphrases"], "c")},
        "lineup": build_lineup(talk),
        "facts": {"fish": load_json("facts.json").get("fish", [])},
    }
    # Imported here, not at the top: each module imports this one for load(), HOSTS and friends.
    import stats_flow, stats_style, stats_mentions, stats_pod
    for name, mod in (("flow", stats_flow), ("style", stats_style), ("mentions", stats_mentions), ("pod", stats_pod)):
        if name not in skip:
            data[name] = mod.build(eps, cfg)
    data["summary"] = build_summary(data)
    del data["facts"]     # only needed for summary.json; the page reads the site's own copy
    for name, d in data.items():
        d["built"] = time.strftime("%Y-%m-%d %H:%M")
        json.dump(d, open(os.path.join(OUT, name + ".json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    sizes = ", ".join(f"{n} {os.path.getsize(os.path.join(OUT, n + '.json')) // 1024}K" for n in data)
    print(f"Stats: {len(eps)} episodes in {time.time() - t0:.0f}s -> {os.path.relpath(OUT, ROOT) if OUT.startswith(ROOT) else OUT} ({sizes})")


if __name__ == "__main__":
    # The stats_* modules "import build_stats": point that at this run, so they see the options
    # configure() set (B.POD_READ, B.HOSTS...) and not a second, unconfigured copy.
    sys.modules["build_stats"] = sys.modules[__name__]
    main()
