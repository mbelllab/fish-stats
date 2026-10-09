"""Build facts.json for the Facts page (facts.html).

Fish: every main episode's headline facts ("My fact this week is that ..."),
with who gave it, the date, and the time in the episode. Read from
search_index.db (so speaker names are the ones the site shows).

Extra or corrected entries can go in curated/facts_extra.json
({"<podcast>|<episode>": [{"n":1,"who":"James","t":75,"fact":"..."}], ...});
an episode listed there replaces what the script found for it (an empty list
means "not a facts episode", unless the script found 3+ clear "My fact" lines).


    python build_facts.py            # writes facts.json at the site root
    python build_facts.py --db PATH --out PATH   # another site's database (fish stats)
"""
import json, os, re, sqlite3

import episodes, paths

ROOT = paths.ROOT
DB = os.path.join(ROOT, "search_index.db")
OUT = os.path.join(ROOT, "facts.json")
EXTRA = os.path.join(paths.CURATED, "facts_extra.json")
FISH = paths.FISH
HOSTS = tuple(paths.HOSTS[FISH])

MY_FACT_RE = re.compile(
    r"\bmy fact (?:this week |today |for (?:this|the) week |for you )?is[,:]? (?:that |about )?", re.I)
ANNOUNCE_RE = re.compile(
    r"\b(?:fact number (?:one|two|three|four|five|1|2|3|4|5)|first fact|next fact|final fact|last fact|"
    r"starting with you)\b", re.I)
HANDOVER_RE = re.compile(r"\b(?:next up|up next|over to|your turn|turn,|time for|let's go (?:to|with)|"
                         r"moving on to|on to (?:our|the)|which (?:comes|is) from|that's from|it is time|"
                         r"[A-Z][a-z]+'s (?:fact|turn|up)|fact (?:one|two|three|four)|starting with)\b", re.I)
# Episodes that are not a normal "four facts" show.
SKIP_RE = re.compile(r"bonus|little fish|compilation|drop us a line|club fish|meet the|"
                     r"factball|trailer|introducing|audiobook|wasted material|newsbite|"
                     r"fishmas|book of the year|christmas special|q ?& ?a", re.I)


# End of a sentence, but not after "St." / "Mr." / "Dr." / "e.g." / "U.S." / a single initial.
ABBR_SAFE_END = re.compile(r"^(.*?(?<!\bSt)(?<!\bMr)(?<!\bMrs)(?<!\bMs)(?<!\bDr)(?<!\bvs)(?<!\be\.g)(?<!\bi\.e)"
                           r"(?<!\b[A-Z])(?<!\bNo)(?<!\.\.)[.?!](?!\.))(?:\s|$)")


def tidy(text):
    text = re.sub(r"\s+", " ", text).strip(" ,-–—")
    text = re.sub(r"^(?:that|about)\s+", "", text, flags=re.I)
    if text:
        text = text[0].upper() + text[1:]
    if text and text[-1] not in ".?!":
        text += "."
    return text


def sentence_from(segs, i, start_at):
    """Text from segs[i] (from char start_at) on, same speaker, to the first sentence end."""
    who = segs[i][2]
    out = segs[i][1][start_at:]
    j = i + 1
    while not re.search(ABBR_SAFE_END, out) and j < len(segs) and len(out) < 350:
        if segs[j][2] != who and segs[j][2]:
            break
        out += " " + segs[j][1]
        j += 1
    m = re.search(ABBR_SAFE_END, out)
    sent = m[1] if m else out
    return sent[:400]


# How the hosts get named when a fact is handed over (incl. Dan's "Chizinski" for Anna).
ALIASES = {"Anna": r"Anna|Ptaszynski|Ch?[aeiouyz]{0,2}[sz]+[iy]nsk[iy]|Tashinski",
           "Andy": r"Andy|Andrew", "James": r"James|Harkin", "Dan": r"Dan|Daniel|Schreiber"}


def named_in(text, names):
    """The last person named in text (by position)."""
    best, pos = "", -1
    for n in names:
        pat = ALIASES.get(n) or "|".join(map(re.escape, {n, n.split()[0]}))   # guests: "Mary" = Mary Roach
        for m in re.finditer(rf"\b(?:{pat})\b", text):
            if m.start() > pos:
                best, pos = n, m.start()
    return best


stats = {"announced": 0, "speaker": 0, "agree": 0, "disagree": 0}


def turns(segs):
    """Join consecutive lines by the same speaker (old transcripts split sentences
    across many short lines). A turn is cut after 40 s so times stay useful."""
    out = []
    for t, text, who in segs:
        if out and out[-1][2] == who and t - out[-1][0] < 40 and not re.search(r"[.?!]$", out[-1][1]):
            out[-1] = (out[-1][0], out[-1][1] + " " + text, who)
        else:
            out.append((t, text, who))
    return out


def who_announced(line, announcer, names):
    """Name in a hand-over line: "fact number two, and that is Anna" / "...that is my fact"."""
    if re.search(r"\b(?:is|that's|and it's) (?:my fact|mine|me)\b", line, re.I) and announcer in names:
        return announcer
    return named_in(line, names)


# "fact number one, and that is Russell": a name the hand-over gives for someone whose voice has no name.
HANDED_TO_RE = re.compile(r"\b(?:that is|that's|and it's|it's|it is|over to|with)\s+([A-Z][a-z]+)\b")
NOT_A_NAME = {"My", "Me", "Mine", "The", "Our", "Fact", "Number", "Time", "It", "That", "This", "Okay", "Yes", "So"}


def handed_to(line, description):
    """A guest named in a hand-over who isn't among the labelled voices: their full name from the
    episode description when it has one ("Russell T Davies joins Dan, ..."), else the first name."""
    m = HANDED_TO_RE.search(line)
    if not m or m[1] in NOT_A_NAME or named_in(m[1], HOSTS):
        return ""
    full = re.search(rf"\b{m[1]}(?:\s+(?:[A-Z][\w'.-]*|de|van|von|al))*\b", description or "")
    if full and " " in full[0]:
        return full[0]
    return ELVES.get(m[1], m[1])


# The QI elves who stood in for a host in the early years; the descriptions give only a first name.
ELVES = {"Alex": "Alex Bell", "Anne": "Anne Miller", "Miller": "Anne Miller"}


def fish_facts(segs, guests, description=""):
    names = HOSTS + tuple(guests)
    segs = turns(segs)
    slots = []      # (t, presenter, how, fact)
    used = set()    # turns already used as a fact
    # 1. Every hand-over: "fact number one, and that is James" / "final fact ... Andy".
    for i, (t, text, who) in enumerate(segs):
        m = ANNOUNCE_RE.search(text)
        if not m:
            continue
        line = text[m.start():] + " " + " ".join(s[1] for s in segs[i + 1:i + 2])[:60]
        presenter = who_announced(line[:120], who, names)
        # Only used if the fact's own voice has no name (see the end): a hand-over can mangle a name.
        named = "" if presenter else handed_to(line[:120], description)
        fact, ft, src = "", t, ""
        # The fact: "My fact (this week) is that ..." within the next minute ...
        for j in range(i, len(segs)):
            if segs[j][0] - t > 60:
                break
            body = segs[j][1] if j > i else text[m.end():]
            mm = MY_FACT_RE.search(body)
            if mm:
                fact, ft = tidy(sentence_from([(segs[j][0], body, segs[j][2])] + segs[j + 1:], 0, mm.end())), segs[j][0]
                src = "my"
                used.add(j)
                break
        # ... or else the first proper sentence after a numbered hand-over that names someone.
        strong = re.search(r"fact number|first fact|final fact|starting with", text, re.I) and (presenter or named)
        if not fact and strong:
            for j in range(i + 1, len(segs)):
                if segs[j][0] - t > 30:
                    break
                cand = tidy(sentence_from(segs, j, 0))
                if len(cand) >= 25 and not re.match(r"(?:okay|thank|hello|yes|yeah|no|right|so)\b", cand, re.I):
                    fact, ft, src = cand, segs[j][0], "guess"
                    used.add(j)
                    break
        if fact and len(fact) >= 15:
            spk = next((x[2] for x in segs if x[0] == ft), "")
            if presenter == who and spk in names and spk != who and spk not in HOSTS:
                presenter = spk          # a guest who shares the host's first name
            slots.append((ft, presenter, "announced" if presenter else "speaker", fact, who, src, named if strong else ""))
    # 2. "My fact this week is ..." with no hand-over found before it.
    for i, (t, text, who) in enumerate(segs):
        if i in used:
            continue
        m = MY_FACT_RE.search(text)
        if m and not any(abs(t - s[0]) < 90 for s in slots):
            fact = tidy(sentence_from(segs, i, m.end()))
            if len(fact) >= 15:
                slots.append((t, "", "speaker", fact, who, "my", ""))
    slots.sort()
    facts = []
    for t, presenter, how, fact, who, src, named in slots:
        if facts and t - facts[-1]["t"] < 90:       # the same fact twice
            continue
        if not presenter:
            # fall back to the speaker label of the fact line
            spk = next((s[2] for s in segs if s[0] == t), "")
            presenter = spk if spk in names else "" if spk else named
        stats[how] += 1
        facts.append({"t": t, "who": presenter, "fact": fact, "src": src})
    for n, f in enumerate(facts, 1):
        f["n"] = n
    return facts


def unmatched(what, keys, known):
    """Curated data is keyed by the episode's file name; after a rename the entry is
    silently ignored. Say which keys match no episode, so they can be renamed too."""
    lost = sorted(k for k in keys if k not in known and not k.startswith("_"))   # "_about": a note
    if lost:
        print(f"WARNING: {len(lost)} {what} key(s) match no episode (file renamed?):")
        for k in lost:
            print("   ", k)


def main(argv=None):
    global DB, OUT
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db"); ap.add_argument("--out")
    a = ap.parse_args(argv)
    DB, OUT = os.path.abspath(a.db or DB), os.path.abspath(a.out or OUT)
    con = sqlite3.connect(DB)
    meta = {(p, e): d for p, e, d in con.execute("select podcast, episode, description from episode_meta")}
    extra = json.load(open(EXTRA, encoding="utf-8")) if os.path.exists(EXTRA) else {}
    out = []
    eps = con.execute("select id, podcast, episode from episodes where media_type='podcast' and podcast=? "
                      "order by episode", (FISH,)).fetchall()
    found = short = 0
    for eid, podcast, ep in eps:
        if SKIP_RE.search(ep) and f"{podcast}|{ep}" not in extra:
            continue
        segs = con.execute("select start_seconds, text, coalesce(speaker,'') from segments "
                           "where episode_id=? order by start_seconds, id", (eid,)).fetchall()
        guests = sorted({s[2] for s in segs if s[2] and s[2] not in HOSTS})
        facts = fish_facts(segs, guests, meta.get((podcast, ep), ""))
        key = f"{podcast}|{ep}"
        if key in extra:
            mine = [f for f in facts if f.get("src") == "my"]
            if extra[key]:
                facts = [dict(f, src="read") for f in extra[key]]
            elif len(mine) < 3:
                facts = []
        if not facts:
            continue
        found += 1
        short += len(facts) < 4
        out.append({"podcast": podcast, "episode": ep, "date": episodes.date(ep), "num": episodes.number(ep),
                    "title": episodes.title(ep), "facts": facts})
    unmatched("facts_extra.json", extra, {f"{p}|{e}" for p, e in con.execute(
        "select podcast, episode from episodes where media_type='podcast'")})
    json.dump({"fish": out}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    total = sum(len(e["facts"]) for e in out)
    print("who:", stats)
    print(f"Fish: {total} facts from {found} episodes ({short} with fewer than 4) -> {OUT}")


if __name__ == "__main__":
    main()
