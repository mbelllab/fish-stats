"""
Name the speakers in existing transcripts using the voiceprints.

For every episode, the transcript's anonymous [Speaker N] labels are matched
to the enrolled voiceprints (enroll_speakers.py) by sampling that speaker's
audio from the mp3. Voices that match nobody are guessed from the episode
title/description (guests), when that is unambiguous enough.

Nothing in the .vtt files is changed. Results go to a small JSON per episode:
    data/work/speakers/<Podcast>/<episode>.json
    {"Speaker 3": {"name": "Dan", "source": "voiceprint", "score": 0.71, ...}, ...}
The indexer reads these and stores the names in the database.

Usage:
    python name_speakers.py                 # all episodes not done yet
    python name_speakers.py --force         # redo everything (after new voiceprints)
    python name_speakers.py --only "604."   # just episodes whose name contains this
    python name_speakers.py --report ...    # also print every score (for tuning)

Speed: ~10-20 s per episode on the 5700G (mostly decoding the mp3).
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time

import numpy as np

from voiceid import (ROOT, PROFILES_PATH, SR, Embedder, load_audio, is_speech,
                     load_profiles, person_score)

from paths import SPEAKERS_DIR as OUT_DIR, PODCASTS, FISH, CURATED
import episodes                    # title(name): the episode name without date/number
from name_fixes import fix_name    # "Ella Al Shamahi" -> "Ella Al-Shamahi"

# Who is a regular on which show (the hosts in curated/shows.json).
# Anyone enrolled can be matched on any show, but people who aren't regulars
# there need a stronger match (OFFSHOW_BONUS).
from paths import REGULARS
# Names that are hosts, never guests (used to filter guest guesses).
HOST_WORDS = {"dan", "schreiber", "james", "harkin", "andy", "andrew", "hunter",
              "murray", "anna", "ptaszynski", "rhys", "darby"}

THRESHOLD = 0.55        # min score to accept a regular   (tuned: see --report)
OFFSHOW_BONUS = 0.08    # extra score needed for a non-regular on this show
MARGIN = 0.05           # best must beat the runner-up by this much
REGULAR_THRESHOLD = 0.47  # second chance: a show regular missing from the episode
REGULAR_MARGIN = 0.12     #   must still clearly beat everyone else
REGULAR_MIN_SPEECH = 120  #   and talk for at least 2 minutes
MIN_SPEECH = 15        # seconds of speech needed to try matching a label
GUEST_MIN_SPEECH = 60   # seconds a leftover voice needs to be guessed as a guest
# Split voices: the transcriber sometimes splits one person into two or three voices; the
# main one is matched and the pieces are left nameless. A nameless voice that sounds like
# someone already found in the same episode (same microphone, same day) is folded into them.
# Measured on 26 episodes: the same person scores 0.84 against themselves, different people
# 0.30-0.60 (most under 0.47), so 0.65 with a clear lead over the next person is safe.
FOLD_SIM = 0.65
FOLD_MARGIN = 0.15
WINDOW = 4.0            # seconds per sampled chunk
MAX_WINDOWS = 24        # chunks sampled per speaker

TS = r"(\d{1,2}:\d{2}(?::\d{2})?\.\d{3})"
CUE_RE = re.compile(TS + r"\s*-->\s*" + TS + r"[^\n]*\n(.*?)(?=\n\s*\n|\n\d{1,2}:\d{2}|\Z)", re.S)
LABEL_RE = re.compile(r"^\s*\[(Speaker \d+)\]\s*")


def secs(t):
    v = 0.0
    for p in t.split(":"):
        v = v * 60 + float(p)
    return v


def parse_turns(vtt_path):
    """[(label, start, end, text)] with consecutive same-label cues merged."""
    content = open(vtt_path, encoding="utf-8", errors="replace").read()
    turns = []
    for s, e, txt in CUE_RE.findall(content):
        m = LABEL_RE.match(txt)
        if not m:
            continue
        label, text = m.group(1), txt[m.end():].strip()
        s, e = secs(s), secs(e)
        if turns and turns[-1][0] == label and s - turns[-1][2] < 1.0:
            turns[-1][2] = e
            turns[-1][3] += " " + text
        else:
            turns.append([label, s, e, text])
    return turns


def pick_windows(turns):
    """label -> list of (start, dur) windows spread across the episode."""
    by = {}
    for label, s, e, _ in turns:
        # trim the edges of each turn, where voices overlap
        s, e = s + 0.3, e - 0.3
        t = s
        while e - t >= 2.0:
            d = min(WINDOW, e - t)
            by.setdefault(label, []).append((t, d))
            t += d
    out = {}
    for label, wins in by.items():
        if len(wins) > MAX_WINDOWS:
            idx = np.linspace(0, len(wins) - 1, MAX_WINDOWS).round().astype(int)
            wins = [wins[i] for i in idx]
        out[label] = wins
    return out


def match_voices(audio, turns, profiles, podcast, emb, report):
    speech = {}
    for label, s, e, _ in turns:
        speech[label] = speech.get(label, 0) + (e - s)
    result, cent = {}, {}
    for label, wins in pick_windows(turns).items():
        info = {"name": None, "source": "", "seconds": round(speech.get(label, 0))}
        result[label] = info
        if speech.get(label, 0) < MIN_SPEECH:
            continue
        embs = []
        for s, d in wins:
            chunk = audio[int(s * SR):int((s + d) * SR)]
            if is_speech(chunk):
                embs.append(emb.embed(chunk))
        if len(embs) < 3:
            continue
        c = np.mean(embs, axis=0)
        cent[label] = c / (np.linalg.norm(c) or 1)
        scores = {}
        for name, chunks in profiles.items():
            scores[name] = float(np.median([person_score(x, chunks) for x in embs]))
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        best, bs = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        need = THRESHOLD + (0 if best in REGULARS.get(podcast, ()) else OFFSHOW_BONUS)
        if report:
            print(f"      {label:<11} {info['seconds']:>5}s  " +
                  "  ".join(f"{n}={v:.2f}" for n, v in ranked))
        info["score"] = round(bs, 3)
        info["best"], info["margin"] = best, round(bs - second, 3)
        if bs >= need and bs - second >= MARGIN:
            info["name"], info["source"] = best, "voiceprint"
    # Second chance for regulars: older/live recordings often score just under
    # the threshold. If a show regular hasn't been found in this episode, a
    # long unmatched voice that clearly points to them is accepted.
    found = {i["name"] for i in result.values() if i["name"]}
    for label, info in sorted(result.items(), key=lambda kv: -kv[1].get("score", 0)):
        b = info.get("best")
        if (not info["name"] and b in REGULARS.get(podcast, ()) and b not in found
                and info["seconds"] >= REGULAR_MIN_SPEECH
                and info["score"] >= REGULAR_THRESHOLD and info["margin"] >= REGULAR_MARGIN):
            info["name"], info["source"] = b, "voiceprint"
            found.add(b)
    fold_split_voices(result, cent)
    return result


def fold_split_voices(result, cent):
    named = {l: i["name"] for l, i in result.items() if i["name"] and i["source"] == "voiceprint" and l in cent}
    for label, info in result.items():
        if info["name"] or label not in cent:
            continue
        best = {}
        for other, who in named.items():
            best[who] = max(best.get(who, -1.0), float(cent[label] @ cent[other]))
        ranked = sorted(best.items(), key=lambda kv: -kv[1])
        if not ranked:
            continue
        lead = ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0.0)
        if ranked[0][1] >= FOLD_SIM and lead >= FOLD_MARGIN:
            info["name"], info["source"], info["fold"] = ranked[0][0], "voiceprint", round(ranked[0][1], 3)


# Guest guessing
# Only names in patterns that actually introduce a person are used, e.g.
#   Fish:     "Dan, James, Andy and Melanie Bracewell discuss ..."
#   any:      "joined by X", "with special guest X", "welcome X"
NAME = r"((?:Dr\.? |Professor |Sir |Dame )?[A-Z][a-z'’\-]+(?: (?:[A-Z][a-zA-Z'’\-]+|de|van|von|al|el|Al-[A-Z][a-z]+)){1,2})"
INTRO_RES = [
    re.compile(r"(?:^|[.!?\"'”“]\s*)" + NAME + r"(?:,| is | was | has | joins | returns | tells | talks | shares )"),
    # "joined by comedian, storyteller and future explorer Will Seaward": job words may sit in between
    re.compile(r"(?:joined by|special guest|guest|welcome|welcomes|featuring|ft\.?) (?:[a-z][\w'’\-]*,? ){0,8}" + NAME),
]
# Known people who aren't enrolled yet can still be named from the blurb.
# Maps how descriptions write them -> the label their voiceprint will use.
ALIASES = {
    "Rhys": re.compile(r"\bRhys D[ae]rby\b"),
}
ALIAS_SAID = {"Rhys": ("rhys",)}
# Full names of enrolled people (their voices are matched, never guessed).
FULL_NAMES = {"Dan": {"dan schreiber"}, "James": {"james harkin"},
              "Andy": {"andrew hunter murray", "andrew hunter", "hunter murray", "andy murray"},
              "Anna": {"anna ptaszynski"},
              "Rhys": {"rhys darby", "rhys derby"}}


# Words that mean it's a thing, not a person ("Ig Nobel Prizes", "Horrible Histories").
NOT_PERSON = re.compile(r"\b(Prizes?|Awards?|Histor(y|ies)|Festival|Museum|Society|University|"
                        r"College|Show|Club|Podcast|Radio|Theatre|Theater|Channel|News|Times|"
                        r"Institute|Library|Records|Company|Park|Street|Island|Day|Week|Year|"
                        r"Game|Games|Book|Books|Guide|Magazine|Hall|Centre|Center|School|Family)\b")




def guest_candidates(title, description, transcript_text, enrolled=(), listed_only=False):
    """[(name, score)] people the episode info introduces who are also talked
    about in the episode itself, best first. People in `enrolled` are
    skipped (their voiceprint decides)."""
    description = description or ""
    low = transcript_text.lower()
    skip_full = set().union(*(FULL_NAMES.get(n, set()) for n in enrolled)) if enrolled else set()
    skip_first = {n.split()[0].lower() for n in enrolled}
    found = {}
    listed = set()   # named as one of the people on the episode: no need to be heard saying it
    m = re.search(r"^(.{0,200}?)\s+(?:are |were )?(?:discuss|chat|talk)", description)
    if m:  # Fish style list of who's on
        for part in re.split(r",|\band\b|&", m.group(1)):
            # "Dan, James, Anna, and special guest Josh Thomson discuss ..."
            part = re.sub(r"^(?:(?:very )?special guests?|guests?)\s+", "", part.strip())
            # "Craig Glenday of Guinness World Records", "Maxïmo Park frontman Paul Smith",
            # "QI creator John Lloyd", "Original Elf Molly Oldfield": the name is the last
            # two capitalised words, before any "of ..."
            part = re.sub(r"\s+of\s.*$", "", part)
            if re.match(r"(?:live|recorded|in|at|from|on)\b", part, re.I):
                continue    # "Live from The Aces and Eights Bar in Tufnell Park": a place, not a person
            if not re.fullmatch(NAME, part):
                tail = re.search(r"(?:^|\s)((?!The |A |An )[A-Z][\w'’\-]+ [A-Z][\w'’\-]+)$", part)
                part = tail.group(1) if tail else part
            if re.fullmatch(NAME, part):
                found[part] = found.get(part, 0) + 10
                listed.add(part)
    # Newer Fish style: "Zoe Lyons joins Dan, James and Andy to discuss ...", also "Bertie
    # Terrilliams of the Margate Crab Museum joins Dan", "Anne Miller returns to join Dan"
    for m in re.finditer(NAME + r"(?: of [^,.]{1,50}?)? (?:joins|join|returns to join) (?:Dan|James|Andy|Anna|the)\b", description):
        found[m.group(1)] = found.get(m.group(1), 0) + 10
        listed.add(m.group(1))
    if not listed_only:
        for rx in INTRO_RES:
            for m in rx.finditer(description):
                found[m.group(1)] = found.get(m.group(1), 0) + 5
    out = {}
    # Enrolled too (Rhys): the blurb still names them when the voiceprint
    # misses (341, 461, 587 = Rhys Darby); guess_guests skips anyone already matched.
    for label, rx in ALIASES.items():
        if not rx.search(description):
            continue
        said = sum(low.count(w) for w in ALIAS_SAID[label])
        if said >= 2:
            out[label] = 15 + min(said, 20)
    for name, score in found.items():
        bare = re.sub(r"^(Dr\.?|Professor|Sir|Dame) ", "", name)
        words = [w for w in bare.split() if not (w.isupper() and len(w) > 1)]  # drop MBE, OBE...
        if not words:
            continue
        bare = " ".join(words).strip("'’\"“”")
        if NOT_PERSON.search(bare):
            continue
        if all(w.lower() in {"dan", "james", "andy", "anna", "andrew", "al"} for w in bare.split()):
            continue  # "James Anna" = two hosts from a list missing a comma
        if any(rx.fullmatch(bare) for rx in ALIASES.values()):
            continue  # handled above under its short label
        if bare.lower() in skip_full or (len(words) == 1 and words[0].lower() in skip_first):
            continue
        if bare.lower() in {n for v in FULL_NAMES.values() for n in v}:
            continue
        first, last = words[0].lower(), words[-1].lower()
        said = low.count(bare.lower()) * 3 + low.count(last) + low.count(first)
        if said < 2 and name not in listed:
            continue  # named in the blurb but never mentioned on the show (a listed guest is on it anyway)
        if bare in title:
            score += 5
        if name in listed:
            score += 5      # listed as on the episode: enough on its own
        out[bare] = max(out.get(bare, 0), score + min(said, 20))
    # "Reading John Higgs" vs "John Higgs": keep the shorter real name.
    for a in list(out):
        if any(b != a and b in a for b in out):
            del out[a]
    # Weak evidence isn't enough to put a name on someone's voice.
    return sorted(((n, v) for n, v in out.items() if v >= 15), key=lambda kv: -kv[1])


# Guests read by hand from the description where the rules above miss them (curated/episode_guests.json).
HAND_GUESTS = {}
_hand = os.path.join(CURATED, "episode_guests.json")
if os.path.exists(_hand):
    with open(_hand, encoding="utf-8") as _f:
        HAND_GUESTS = {k: v for k, v in json.load(_f).items() if not k.startswith("_")}


def candidates_for(podcast, base, title, description, text, enrolled):
    """The hand-read guests when there are any, else guest_candidates' reading."""
    hand = HAND_GUESTS.get(podcast, {}).get(base)
    if hand:
        return [(n, 100) for n in hand]
    return guest_candidates(title, description, text, enrolled, listed_only=(podcast == FISH))


def guess_guests(result, candidates):
    leftover = sorted((l for l, i in result.items()
                       if not i["name"] and i["seconds"] >= GUEST_MIN_SPEECH),
                      key=lambda l: -result[l]["seconds"])
    taken = {i["name"] for i in result.values() if i["name"]}
    cands = [c for c, _ in candidates if c not in taken]
    for label, name in zip(leftover, cands):
        result[label]["name"] = fix_name(name)     # the feed's spelling, with the transcript name fixes
        result[label]["source"] = "guess"
    # One unknown voice, one guest named for the episode, everyone else recognised
    # by voice: it can only be the guest, so it's not a guess (no "?" on the site).
    if len(leftover) == 1 and len(cands) == 1:
        others = [i for l, i in result.items() if l != leftover[0] and i["seconds"] >= GUEST_MIN_SPEECH]
        if all(i["source"] == "voiceprint" for i in others):
            result[leftover[0]]["source"] = "sure"


# Main
def load_descriptions():
    db = os.path.join(ROOT, "search_index.db")
    if not os.path.exists(db):
        return {}
    c = sqlite3.connect(db)
    try:
        return {(p, e): d for p, e, d in c.execute("SELECT podcast, episode, description FROM episode_meta")}
    except sqlite3.Error:
        return {}


def redo_guests(args, profiles, descriptions):
    """Re-run guest guessing on existing results (after the rules change).
    Voice matches are kept; only the "guess" names are redone."""
    changed = 0
    for podcast in (args.podcast or PODCASTS):
        if not REGULARS.get(podcast, set()) <= set(profiles):
            continue
        out_dir = os.path.join(OUT_DIR, podcast)
        for f in sorted(os.listdir(out_dir)):
            if args.only not in f:
                continue
            base = f[:-5]
            vtt = os.path.join(ROOT, podcast, "vtts", base + ".vtt")
            if not os.path.exists(vtt):
                continue
            path = os.path.join(out_dir, f)
            result = json.load(open(path, encoding="utf-8"))
            before = {l: (i["name"], i["source"]) for l, i in result.items()}
            for i in result.values():
                if i["source"] in ("guess", "sure"):
                    i["name"], i["source"] = None, ""
            text = " ".join(t[3] for t in parse_turns(vtt))
            title = episodes.title(base)
            guess_guests(result, candidates_for(podcast, base, title, descriptions.get((podcast, base), ""), text,
                                                set(profiles)))
            if {l: (i["name"], i["source"]) for l, i in result.items()} != before:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(result, fh, indent=1, ensure_ascii=False)
                changed += 1
                new = [f"{l}={i['name']}{'' if i['source'] == 'sure' else '?' if i['source'] == 'guess' else ''}"
                       for l, i in result.items() if (i["name"], i["source"]) != before.get(l)]
                print(f"  {base[:60]:<60} {', '.join(new)}")
    print(f"\nGuest guesses changed in {changed} episodes")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--no-guests", action="store_true")
    ap.add_argument("--podcast", action="append", default=[],
                    help="only this show (repeatable); default all three")
    ap.add_argument("--guests-only", action="store_true",
                    help="redo just the guest guesses on finished episodes (no audio, seconds)")
    args = ap.parse_args()

    profiles = load_profiles()
    prof_mtime = os.path.getmtime(PROFILES_PATH)
    descriptions = load_descriptions()
    print(f"Voiceprints: {', '.join(profiles)}")
    for pod, regs in REGULARS.items():
        missing = regs - set(profiles)
        if missing:
            print(f"  {pod}: no voiceprint yet for {', '.join(sorted(missing))} "
                  f"-> guest guessing OFF for this show (it would mislabel them)")
    if args.guests_only:
        return redo_guests(args, profiles, descriptions)
    emb = None
    done = 0
    for podcast in (args.podcast or PODCASTS):
        vtt_dir = os.path.join(ROOT, podcast, "vtts")
        mp3_dir = os.path.join(ROOT, podcast, "mp3s")
        out_dir = os.path.join(OUT_DIR, podcast)
        os.makedirs(out_dir, exist_ok=True)
        guests_ok = not args.no_guests and REGULARS.get(podcast, set()) <= set(profiles)
        for f in sorted(os.listdir(vtt_dir)):
            if not f.endswith(".vtt") or args.only not in f:
                continue
            base = f[:-4]
            vtt, mp3 = os.path.join(vtt_dir, f), os.path.join(mp3_dir, base + ".mp3")
            out = os.path.join(out_dir, base + ".json")
            if not os.path.exists(mp3):
                continue
            if (not args.force and os.path.exists(out)
                    and os.path.getmtime(out) > max(prof_mtime, os.path.getmtime(vtt))):
                continue
            turns = parse_turns(vtt)
            if not turns:
                continue
            t0 = time.time()
            if emb is None:
                emb = Embedder()
            audio = load_audio(mp3)
            if args.report:
                print(f"  {podcast} | {base}")
            result = match_voices(audio, turns, profiles, podcast, emb, args.report)
            if guests_ok:
                text = " ".join(t[3] for t in turns)
                title = episodes.title(base)
                guess_guests(result, candidates_for(podcast, base, title, descriptions.get((podcast, base), ""), text,
                                                    set(profiles)))
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=1, ensure_ascii=False)
            done += 1
            named = ", ".join(f"{l.split()[-1]}={i['name']}{'?' if i['source'] == 'guess' else ''}"
                              for l, i in sorted(result.items(), key=lambda kv: -kv[1]["seconds"]) if i["name"])
            print(f"  [{done}] {base[:60]:<60} {time.time() - t0:4.0f}s  {named or '(none matched)'}")
            sys.stdout.flush()
    print(f"\nDone: {done} episodes named -> {OUT_DIR}")


if __name__ == "__main__":
    main()
