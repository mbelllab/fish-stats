"""Build the data behind the "Who and what they mention" stats page (statistics/mentions.html).

Counts two curated lists in every transcript line: cryptids (Bigfoot, Nessie...) and famous
people (Darwin, Stephen Fry...). Both lists, with the spellings Whisper uses, live in
curated/mentions_words.json. Cryptids match in any case; people match
case-sensitively (Whisper capitalises names, which keeps "bond" and "fry" out).

Output: per name, mentions per show per year, and who said them per show per year (every named
speaker, unlabelled lines as "Unknown", by index into one speaker table "spk", so the page can
break any bar down by speaker for whichever show and year it shows); per show, who said them all
(hosts by name); and hours of audio and of each host's talking, so the page can show "per hour".

    python stats_mentions.py        (writes statistics/mentions.json)
"""
import collections, json, os, re, time

import build_stats as B

LIST = os.path.join(B.CURATED, "mentions_words.json")


def mentions_config():
    return json.load(open(LIST, encoding="utf-8"))


def name_rx(words, case):
    """One pattern for a whole list, longest first, so "loch ness monster" is one hit, not two.
    A possessive ("Darwin's") counts; a longer word ("Darwinian") does not."""
    words = sorted(set(words), key=len, reverse=True)
    body = "|".join(re.escape(w).replace(r"\ ", r"[\s-]+") for w in words)
    return re.compile(r"(?<![\w'])(" + body + r")(?:'s|s'|’s)?(?![\w'’])", 0 if case else re.I)


def search_q(words):
    """The main search for a name: its spellings as an either-or query. The search matches the
    start of a word, so "bigfoot" also finds "bigfoots" and those are left out."""
    ws = list(dict.fromkeys(w.lower() for w in words))
    keep = [w for w in ws if not any(w != k and w.startswith(k) for k in ws)]
    return " | ".join(f'"{w}"' if " " in w or "-" in w else w for w in keep[:5])


def count(eps, items, nots, case, hosts, spk_ix):
    """Per item: {show: {year: n}} and {show: {year: [speaker, n, speaker, n...]}} (speakers as
    indexes into spk_ix, most first); per show: {speaker: {year: n}} (non-hosts pooled as "Others")."""
    key = {}
    for name, words in items:
        for w in words:
            key[re.sub(r"[\s-]+", " ", w if case else w.lower())] = name
    r = name_rx(key, case)
    blank = name_rx(nots, case) if nots else None
    per = {name: collections.defaultdict(collections.Counter) for name, _ in items}
    by = {name: collections.defaultdict(lambda: collections.defaultdict(collections.Counter)) for name, _ in items}
    who = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    for e in eps:
        p, y, hs = e["podcast"], e["date"][:4], hosts[e["podcast"]]
        for t, text, spk in e["segs"]:
            if blank:
                text = blank.sub(" ", text)
            for m in r.finditer(text):
                k = re.sub(r"[\s-]+", " ", m[1] if case else m[1].lower())
                per[key[k]][p][y] += 1
                by[key[k]][p][y][spk if B.is_person(spk) else "Unknown"] += 1
                who[p][spk if spk in hs else "Others"][y] += 1
    out = []
    for name, words in items:
        c = {p: dict(sorted(v.items())) for p, v in per[name].items()}
        if c:
            w = {p: {y: [x for s, n in sorted(ss.items(), key=lambda z: -z[1])
                         for x in (spk_ix.setdefault(s, len(spk_ix)), n)] for y, ss in sorted(v.items())}
                 for p, v in by[name].items()}
            out.append({"n": name, "q": search_q(words), "c": c, "w": w})
    out.sort(key=lambda x: -sum(sum(v.values()) for v in x["c"].values()))
    return out, {p: {s: dict(sorted(v.items())) for s, v in sp.items()} for p, sp in who.items()}


def hours(eps, hosts):
    """Hours of audio per show per year, and hours each host talked per show per year."""
    audio = collections.defaultdict(collections.Counter)
    talk = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    for e in eps:
        p, y = e["podcast"], e["date"][:4]
        durs = B.durations(e["segs"])
        audio[p][y] += e["segs"][-1][0] + durs[-1]
        for (t, text, spk), d in zip(e["segs"], durs):
            if spk in hosts[p]:
                talk[p][spk][y] += d
    h = lambda s: round(s / 3600, 2)
    return ({p: {y: h(s) for y, s in sorted(v.items())} for p, v in audio.items()},
            {p: {s: {y: h(x) for y, x in sorted(v.items())} for s, v in sp.items()} for p, sp in talk.items()})


def build(eps, cfg=None):
    m = mentions_config()
    hosts = B.HOSTS
    spk_ix = {}
    cryptids, cwho = count(eps, m["cryptids"], m.get("cryptids_not", []), False, hosts, spk_ix)
    people, pwho = count(eps, m["people"], m.get("people_not", []), True, hosts, spk_ix)
    audio, talk = hours(eps, hosts)
    return {"hosts": hosts, "spk": list(spk_ix), "hours": audio, "talk": talk, "cryptids": cryptids, "cryptid_who": cwho,
            "people": people, "people_who": pwho}


if __name__ == "__main__":
    t0 = time.time()
    eps = B.load()
    d = build(eps, B.words_config())
    d["built"] = time.strftime("%Y-%m-%d %H:%M")
    out = os.path.join(B.OUT, "mentions.json")
    json.dump(d, open(out, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"Mentions: {len(d['cryptids'])} cryptids, {len(d['people'])} people in {time.time() - t0:.0f}s "
          f"-> statistics/mentions.json ({os.path.getsize(out) // 1024}K)")
