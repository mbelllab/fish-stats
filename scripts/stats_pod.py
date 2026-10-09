"""Friends and enemies of the pod: everyone and everything the Fish hosts have declared a
"friend of the podcast" or an "enemy of the podcast" (statistics/pod.html).

Two halves:
  - this script finds every mention in the Fish transcripts on each build (a regex over the
    lines, so new episodes turn up by themselves);
  - curated/pod_friends.json is the read: for each mention, who or what was meant,
    the verdict and a few words on why, all taken from what was said on air.

A mention is matched to its read by episode, the phrase and the nearest time within MATCH_S
seconds, so re-timed transcripts keep their reads. Mentions with no read, or read
as "unclear", go in the page's "Not yet read" list until someone reads them.

    python stats_pod.py          (writes statistics/pod.json)
"""
import collections, json, os, re, time

import build_stats as B

READ = os.path.join(B.CURATED, "pod_friends.json")
MATCH_S = 20          # a read still fits its mention if the line has moved this many seconds
CLIP = 260            # characters of the line kept for the quote
CONTEXT = 2           # lines either side kept for mentions nobody has read yet

# "friend of the podcast", "enemies of the show", "foe of the pod", "acquaintance of the podcast",
# "mutual buddy of the podcast", "a friend of Fish", "sworn enemy...", "pod friend", "honorary friend".
PHRASE = re.compile(
    r"\b(?:(?:arch|sworn)[\s-]*)?(friends?|enem(?:y|ies)|foes?|nemes[ie]s|acquaintances?|bud+(?:y|ies)|all(?:y|ies))"
    r"\s+(?:of|to)\s+(?:the\s+)?(pod(?:cast)?s?|show|fish)\b"
    r"|\bpod[\s-]+(friends?|enem(?:y|ies))\b|\bhonorary\s+(friends?|enem(?:y|ies))\b", re.I)
HINT = re.compile(r"friend|enem|foe|nemes|acquaint|budd|all[yi]", re.I)
VERDICTS = ("friend", "enemy", "acquaintance", "not", "unclear")


def phrase_key(s):
    return re.sub(r"[\s-]+", " ", s.lower()).strip()


def find(eps):
    """Every mention in the Fish episodes: (episode, line index, matched phrase). A phrase split
    across two lines ("a friend of" / "the podcast") counts for the line it starts on."""
    out = []
    for e in eps:
        if e["podcast"] != B.FISH:
            continue
        segs = e["segs"]
        for i, (t, text, spk) in enumerate(segs):
            joined = text + " " + (segs[i + 1][1] if i + 1 < len(segs) else "")
            if not HINT.search(joined):        # a cheap test first: the full regex is slow over every line
                continue
            seen = set()
            for m in PHRASE.finditer(joined):
                key = phrase_key(m[0])
                if m.start() < len(text) and key not in seen:
                    seen.add(key)
                    out.append((e, i, key, m.end() > len(text)))
    return out


def load_read():
    path = B.POD_READ or READ            # build_stats --pod-read: another site's copy (fish stats renames some episodes)
    if not os.path.exists(path):
        return {}, []
    d = json.load(open(path, encoding="utf-8"))
    return d.get("names", {}), d.get("mentions", [])


def match(found, reads):
    """Pair each found mention with its read: same episode and phrase, within MATCH_S seconds.
    Lines keep their order when a transcript is re-timed, so the pairing keeps the order too:
    per episode and phrase, the most pairs, then the least total time shift (a small alignment)."""
    groups = collections.defaultdict(lambda: ([], []))
    for k, (e, i, key, _) in enumerate(found):
        groups[(e["episode"], key)][0].append((e["segs"][i][0], k))
    for j, r in enumerate(reads):
        groups[(r["e"], phrase_key(r.get("p", "")))][1].append((r["t"], j))
    got = {}
    for fs, rs in groups.values():
        if not fs or not rs:
            continue
        fs.sort(); rs.sort()
        # best[a][b] = (pairs, -shift) using the first a mentions and first b reads
        best = [[(0, 0)] * (len(rs) + 1) for _ in range(len(fs) + 1)]
        for a in range(1, len(fs) + 1):
            for b in range(1, len(rs) + 1):
                opts = [best[a - 1][b], best[a][b - 1]]
                dt = abs(fs[a - 1][0] - rs[b - 1][0])
                if dt <= MATCH_S:
                    n, sh = best[a - 1][b - 1]
                    opts.append((n + 1, sh - dt))
                best[a][b] = max(opts)
        a, b = len(fs), len(rs)
        while a and b:
            if best[a][b] == best[a - 1][b]:
                a -= 1
            elif best[a][b] == best[a][b - 1]:
                b -= 1
            else:
                got[fs[a - 1][1]] = rs[b - 1][1]
                a -= 1; b -= 1
    return got


def build(eps, cfg=None):
    names, reads = load_read()
    found = find(eps)
    got = match(found, reads)
    ep_ix, ep_list = {}, []
    mentions, people = [], {}
    for k, (e, i, key, spans) in enumerate(found):
        if e["episode"] not in ep_ix:
            ep_ix[e["episode"]] = len(ep_list)
            ref = B.ep_ref(e)
            del ref["p"]                      # Fish only
            ep_list.append(ref)
        segs = e["segs"]
        t, text, spk = segs[i]
        quote = text + (" " + segs[i + 1][1] if spans else "")
        m = {"i": ep_ix[e["episode"]], "t": t, "s": spk, "q": quote[:CLIP]}
        r = reads[got[k]] if k in got else None
        v = r["v"] if r and r.get("v") in VERDICTS else "unread"
        m["v"] = v
        if r and r.get("why"):
            m["w"] = r["why"]
        if v in ("friend", "enemy", "acquaintance"):
            m["n"] = r["who"]
            for n in r["who"]:
                p = people.setdefault(n, {"n": n, "f": [], "x": [], "a": []})
                p["f" if v == "friend" else "x" if v == "enemy" else "a"].append(len(mentions))
        elif v in ("unread", "unclear"):
            m["c"] = [[x[0], x[2], x[1][:CLIP]] for j, x in enumerate(segs[max(0, i - CONTEXT):i + CONTEXT + 1], max(0, i - CONTEXT)) if j != i]
        mentions.append(m)
    # Each name's "why": the curated one if there is one, else the why of its first declaration.
    out = []
    for p in people.values():
        ids = sorted(p["f"] + p["x"] + p["a"], key=lambda j: (ep_list[mentions[j]["i"]]["d"], mentions[j]["t"]))
        p["why"] = names.get(p["n"], {}).get("why") or mentions[ids[0]].get("w", "")
        for key in ("f", "x", "a"):
            if not p[key]:
                del p[key]
        out.append(p)
    out.sort(key=lambda p: p["n"].lower())
    stale = len(reads) - len(got)
    return {"eps": ep_list, "mentions": mentions, "people": out, "stale": stale}


if __name__ == "__main__":
    t0 = time.time()
    d = build(B.load())
    d["built"] = time.strftime("%Y-%m-%d %H:%M")
    path = os.path.join(B.OUT, "pod.json")
    json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    c = collections.Counter(m["v"] for m in d["mentions"])
    print(f"Pod: {len(d['mentions'])} mentions ({dict(c)}), {len(d['people'])} names, {d['stale']} reads unmatched, "
          f"{os.path.getsize(path) // 1024}K in {time.time() - t0:.0f}s")
