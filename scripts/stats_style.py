"""Build statistics/style.json for the How they talk page (statistics/style.html).

Three looks at how people talk, all from one pass over the transcripts:
  sig    - each host's signature words: what they say far more than everyone else on that show
  years  - word of the year: what each show talked about that year more than in its other years
  vocab  - how many different words each person uses, compared fairly (see vocab())

Signature words and word of the year use weighted log-odds with an informative Dirichlet prior
(Monroe, Colaresi & Quinn 2008, "Fightin' Words"): a word scores high when one side says it
much more often than the other AND there are enough uses to be sure, so a word said 3 times
doesn't beat one said 300 times. Word lists (stopwords, host names) live in
curated/style_words.json.

    python stats_style.py          (or build_stats.py imports build())
"""
import collections, json, math, os, re, time

import build_stats as B

STYLE = os.path.join(B.CURATED, "style_words.json")
TOKEN = re.compile(r"[a-z]+(?:'[a-z]+)*")
SPLIT = re.compile(r"(?<=[a-z]) '(?=(?:s|t|re|ve|m|ll|d)\b)")     # the transcriber's "don 't" -> "don't"

# Signature words: a host must say the word this often, in this many episodes, so one long
# story about one thing doesn't take over; the top SIG_TOP make the list.
SIG_MIN, SIG_EPS, SIG_TOP = 30, 10, 20
SIG_LIFT = 1.5            # and say it at least 1.5 times as often (per word) as the others
# Word of the year: said this often that year, in at least YEAR_EPS episodes (or YEAR_SHARE of
# that year's episodes, whichever is more), so one episode's subject isn't "the word of the year".
YEAR_MIN, YEAR_EPS, YEAR_SHARE, YEAR_TOP = 15, 3, 0.08, 8
YEAR_CAP = 3
YEAR_LIFT = 2             # ...and in at least twice the share of episodes it gets in the show's other years
YEAR_MIN_EPS = 6          # years with fewer episodes than this are too thin to say anything
# Vocabulary: people with at least VOCAB_MIN words, measured in stretches of VOCAB_N words; listed
# under a show once they have said VOCAB_SHOW words on it.
VOCAB_MIN, VOCAB_N, VOCAB_SHOW = 10000, 1000, 3000
# Housekeeping is left out of everything: the first INTRO and last OUTRO seconds of each episode
# (intros, sign-offs and plugs: Dan does most of them, so they made him look like he says
# "microphones" a lot) and the bonus episodes that are mostly reading emails or plugs.
INTRO = OUTRO = 180
SKIP_KINDS = {"Drop Us A Line", "Club Fish", "Compilation", "Bonus", "Best Of"}
PRIOR = 1000              # strength of the prior (how many "pretend" words the whole-corpus rates are worth)


def tokens(text):
    return TOKEN.findall(SPLIT.sub("'", text.lower().replace("’", "'")))


def log_odds(a, b, prior, n_prior):
    """Weighted log-odds z-scores of each word for side a against side b (Counters), with the
    whole corpus as the informative prior. Positive = more typical of a."""
    na, nb = sum(a.values()), sum(b.values())
    out = {}
    for w, ya in a.items():
        aw = PRIOR * prior[w] / n_prior
        yb = b.get(w, 0)
        d = math.log((ya + aw) / (na + PRIOR - ya - aw)) - math.log((yb + aw) / (nb + PRIOR - yb - aw))
        out[w] = d / math.sqrt(1 / (ya + aw) + 1 / (yb + aw))
    return out


def fragments(words, seq, ids, total):
    """Which of these words are mostly half of a longer word the transcriber split with a space:
    at least a third of the time, gluing it to the word before or after makes a word that is said
    elsewhere in one piece."""
    word = {i: w for w, i in ids.items()}
    want = {ids[w] for w in words if w in ids}
    seen, glued = collections.Counter(), collections.Counter()
    for s in seq.values():
        last = len(s) - 1
        for i, x in enumerate(s):
            if x in want:
                seen[x] += 1
                if (i and total[word[s[i - 1]] + word[x]] >= 3) or (i < last and total[word[x] + word[s[i + 1]]] >= 3):
                    glued[x] += 1
    return {word[x] for x in want if glued[x] * 3 >= seen[x]}


def build(eps, cfg=None):
    sw = json.load(open(STYLE, encoding="utf-8"))
    # Names that the transcriber spells many ways (Ptaszynski comes out as Chesinski, Chizinski...)
    name_re = re.compile("(?:%s)$" % "|".join(v["first_re"] for v in sw["names"].values() if v.get("first_re")))
    # Words never shown as signature words or words of the year: grammar words, short words, and names
    # (the hosts' as listed, plus every word of every speaker label, which covers guests).
    skip = set(sw["stopwords"]) | {w for v in sw["names"].values() for k in ("first", "last", "before") for w in v.get(k, [])}
    furniture = set(sw["furniture"])
    skip |= set(sw["housekeeping"])
    phrases = re.compile(r"\b(?:" + "|".join(re.escape(x) for x in sw["housekeeping_phrases"]) + r")\b", re.I)
    labels = {s for e in eps for _t, _x, s in e["segs"]}
    skip |= {w for s in labels for w in tokens(s)}

    total = collections.Counter()                                        # every word, every show: the prior
    total_cap = collections.Counter()                                    # ... counting at most YEAR_CAP a word an episode
    spk = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))     # show -> speaker -> words
    spk_df = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))  # ... -> episodes with the word
    yr = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))      # show -> year -> words
    yr_df = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))   # ... episodes with the word
    yr_cap = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))  # ... capped counts
    yr_eps = collections.defaultdict(collections.Counter)
    seq = collections.defaultdict(list)                                  # person -> every word they said, in order (as ids)
    ids = {}
    spoke = collections.defaultdict(collections.Counter)                 # person -> show -> words

    for e in eps:
        if e["kind"] in SKIP_KINDS:
            continue
        p, y = e["podcast"], e["date"][:4]
        per = collections.defaultdict(collections.Counter)
        allw = collections.Counter()
        start, end = INTRO, e["segs"][-1][0] - OUTRO
        for t, text, s in e["segs"]:
            if not start <= t <= end:
                continue
            toks = tokens(phrases.sub(" ", text))
            allw.update(toks)
            if not B.is_person(s):
                continue
            per[s].update(toks)
            who = B.ALIASES.get(s, s)
            seq[who].extend(ids.setdefault(w, len(ids)) for w in toks)
            spoke[who][p] += len(toks)
        total.update(allw)
        cap = {w: min(n, YEAR_CAP) for w, n in allw.items()}
        total_cap.update(cap)
        yr[p][y].update(allw)
        yr_cap[p][y].update(cap)
        yr_df[p][y].update(allw.keys())
        yr_eps[p][y] += 1
        for s, c in per.items():
            spk[p][s].update(c)
            spk_df[p][s].update(c.keys())
    n_total = sum(total.values())
    ok = lambda w: len(w) >= 3 and w not in skip and not name_re.match(w)

    # 1. Signature words: each host against everyone else (named) on the same show.
    sig = {}
    for p, hosts in B.HOSTS.items():
        sig[p] = {}
        for h in hosts:
            mine = spk[p].get(h)
            if not mine:
                continue
            rest = collections.Counter()
            for s, c in spk[p].items():
                if s != h:
                    rest.update(c)
            z = log_odds(mine, rest, total, n_total)
            df = spk_df[p][h]
            na, nr = sum(mine.values()), sum(rest.values())
            lift = lambda w: (mine[w] / na) / (max(rest[w], 0.5) / nr)
            good = [w for w in z if ok(w) and mine[w] >= SIG_MIN and df[w] >= SIG_EPS and z[w] > 0 and lift(w) >= SIG_LIFT]
            good.sort(key=lambda w: -z[w])
            # [word, z, times they said it, how many times more often than the others (per word)]
            sig[p][h] = [[w, round(z[w], 1), mine[w], round(lift(w), 1)] for w in good[:SIG_TOP * 2]]

    # 2. Word of the year: each year of a show against that show's other years. Each episode
    # counts a word at most YEAR_CAP times, so one episode about one thing can't carry it alone.
    years = {}
    n_cap = sum(total_cap.values())
    for p in B.PODCASTS:
        whole, whole_df = collections.Counter(), collections.Counter()
        for y in yr[p]:
            whole.update(yr_cap[p][y])
            whole_df.update(yr_df[p][y])
        all_eps = sum(yr_eps[p].values())
        years[p] = []
        for y in sorted(yr[p], reverse=True):
            n_eps = yr_eps[p][y]
            if n_eps < YEAR_MIN_EPS:
                continue
            c, df, cap = yr[p][y], yr_df[p][y], yr_cap[p][y]
            z = log_odds(cap, whole - cap, total_cap, n_cap)
            need = max(YEAR_EPS, math.ceil(YEAR_SHARE * n_eps))
            good = sorted((w for w in cap if ok(w) and c[w] >= YEAR_MIN and df[w] >= need and z[w] > 0
                           # in at least YEAR_LIFT times the share of episodes it gets in the other years
                           and df[w] / n_eps >= YEAR_LIFT * (whole_df[w] - df[w] + 1) / max(all_eps - n_eps, 1)
                           # not the furniture: said once an episode in many episodes, or in most episodes (the intro, the credits, an advert)
                           and not (c[w] < 1.25 * df[w] and df[w] >= max(10, n_eps / 4)) and df[w] < 0.75 * n_eps
                           and w not in furniture), key=lambda w: -z[w])
            years[p].append({"y": y, "eps": n_eps, "w": [[w, round(z[w], 1), c[w], df[w]] for w in good[:YEAR_TOP * 2]]})

    # Drop halves of words the transcriber split in two ("masturb ating", "superst ition"), then trim.
    frag = fragments({r[0] for v in sig.values() for l in v.values() for r in l} | {r[0] for v in years.values() for y in v for r in y["w"]},
                     seq, ids, total)
    for v in sig.values():
        for h in v:
            v[h] = [r for r in v[h] if r[0] not in frag][:SIG_TOP]
    for v in years.values():
        for y in v:
            y["w"] = [r for r in y["w"] if r[0] not in frag][:YEAR_TOP]

    # 3. Vocabulary: different words in each 1,000 words a person says, averaged over all their
    # talk (a "standardised type-token ratio"). Counting every different word they have ever said
    # would just reward talking more; a fixed-size stretch makes it fair.
    vocab, short = [], {v: k for k, v in B.ALIASES.items()}      # hosts by the names the rest of the page uses
    for who, s in seq.items():
        if len(s) < VOCAB_MIN or not B.is_person(who):
            continue
        blocks = [len(set(s[i:i + VOCAB_N])) for i in range(0, len(s) - VOCAB_N + 1, VOCAB_N)]
        shows = [i for i, p in enumerate(B.PODCASTS) if spoke[who][p] >= VOCAB_SHOW]
        vocab.append([short.get(who, who), round(sum(blocks) / len(blocks)), len(s), shows])
    vocab.sort(key=lambda r: -r[1])

    return {"sig": sig, "years": years, "vocab": vocab, "vocab_n": VOCAB_N, "shows": B.PODCASTS, "hosts": B.HOSTS}


if __name__ == "__main__":
    t0 = time.time()
    data = build(B.load())
    data["built"] = time.strftime("%Y-%m-%d %H:%M")
    out = os.path.join(B.OUT, "style.json")
    json.dump(data, open(out, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"Style: {time.time() - t0:.0f}s -> statistics/style.json ({os.path.getsize(out) // 1024}K)")
