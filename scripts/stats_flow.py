"""Conversation flow for the "Who talks most" page (statistics/talk.html): who hands over to whom,
and each person's longest uninterrupted run. Writes statistics/flow.json.

A turn is a run of consecutive lines by one named speaker. A line labelled with no one (or a
non-person label) ends the turn and is never bridged, so a handover is only counted when one
named person's line is followed straight away by another's.

    python stats_flow.py
"""
import collections, json, os, re, time

import build_stats as B

START, END = 180, 60    # runs starting in the first 3 minutes (cold open, theme, the host's welcome) or the last minute are skipped
SLOW = 1.5              # words a second: a line may last past build_stats.GAP_CAP only if it has the words to fill it
MIN_RUN = 30            # seconds; shorter runs can't make a leaderboard
TOP = 12                # runs kept per person per episode group (plenty for the leaderboards and each person's best)
READING = re.compile(r"audiobook", re.I)            # book readings: someone reading aloud isn't holding the floor
DASH = re.compile(r"^-\s|[.?!]\s+-\s")             # the transcriber's "- " marks a new voice inside a line


def group(e):
    """Same split as the page's Episodes control: "m" main episodes, "o" everything else."""
    return "m" if e["kind"] in ("Main", "Guest episode", "Issue") else "o"


def line_secs(segs):
    """How long each line lasts: until the next line, but no more than GAP_CAP seconds unless the line
    has enough words to fill the time at a slow pace. Pauses, music and ad breaks are cut down; a long
    line of real talk is not."""
    out = []
    for i, (t, text, spk) in enumerate(segs):
        nxt = segs[i + 1][0] if i + 1 < len(segs) else t + 5
        out.append(max(1, min(nxt - t, max(B.GAP_CAP, len(text.split()) / SLOW))))
    return out


def turns(segs):
    """(first line, line after the last, speaker) for each turn."""
    i, n = 0, len(segs)
    while i < n:
        j = i + 1
        while j < n and segs[j][2] == segs[i][2]:
            j += 1
        yield i, j, segs[i][2]
        i = j


def build(eps, cfg=None):
    grid = {p: collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
            for p in B.PODCASTS}                                      # grid[show][group][from][to] = handovers
    runs = {p: collections.defaultdict(list) for p in B.PODCASTS}     # runs[show][person] = [run]
    for e in eps:
        p, segs = e["podcast"], e["segs"]
        who = lambda s: s if s in B.HOSTS[p] else "Guest"
        g = group(e)
        durs, end, reading = line_secs(segs), segs[-1][0] - END, READING.search(e["title"])
        # a run only counts once someone else has spoken and while someone else is still to come:
        # that drops a host's solo intro, outro and ad reads, and solo episodes entirely
        firsts, lasts = {}, {}
        for k, (_, _, s) in enumerate(segs):
            if B.is_person(s):
                firsts.setdefault(s, k); lasts[s] = k
        for i, j, spk in turns(segs):
            if not B.is_person(spk):
                continue
            if j < len(segs) and B.is_person(segs[j][2]):
                grid[p][g][who(spk)][who(segs[j][2])] += 1
            if reading or not (any(k < i for o, k in firsts.items() if o != spk) and any(k >= j for o, k in lasts.items() if o != spk)):
                continue
            # a line where the transcriber marked a second voice ends the run there
            pieces, a = [], i
            for k in range(i, j):
                if DASH.search(segs[k][1]):
                    pieces.append((a, k)); a = k + 1
            pieces.append((a, j))
            for a, b in pieces:
                sec = sum(durs[a:b])
                if sec >= MIN_RUN and START <= segs[a][0] <= end:
                    words = " ".join(x[1] for x in segs[a:b]).split()
                    runs[p][spk].append({**B.ep_ref(e), "w": spk, "at": round(segs[a][0]), "s": round(sec),
                                         "x": " ".join(words[:14]) + (" …" if len(words) > 14 else "")})
    out = {"hosts": B.HOSTS, "grid": {}, "runs": {}}
    for p in B.PODCASTS:
        out["grid"][p] = {g: {a: dict(c) for a, c in gs.items()} for g, gs in grid[p].items()}
        keep, hosts = [], set(B.HOSTS[p])
        for spk, lst in runs[p].items():
            lst.sort(key=lambda r: -r["s"])
            for gr in "mo":
                keep += [r for r in lst if group({"kind": r["k"]}) == gr][:TOP]
        # guests: only the longest runs matter, so the long tail of one-off guests is cut
        guests = sorted((r for r in keep if r["w"] not in hosts), key=lambda r: -r["s"])[:60]
        out["runs"][p] = sorted([r for r in keep if r["w"] in hosts] + guests, key=lambda r: -r["s"])
    return out


if __name__ == "__main__":
    t0 = time.time()
    d = build(B.load())
    d["built"] = time.strftime("%Y-%m-%d %H:%M")
    path = os.path.join(B.OUT, "flow.json")
    json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"Flow: {time.time() - t0:.0f}s -> statistics/flow.json ({os.path.getsize(path) // 1024}K)")
