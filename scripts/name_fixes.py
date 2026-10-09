"""
name_fixes.py - correct names the transcriber reliably gets wrong.

Whisper has never once spelled "Anna Ptaszynski" right: it writes Tashinsky,
Chesinski, Tushinsky, Chizinski, Duszynski... and the old transcripts often split
it over two cues ("sitting here with Anna Tash" / "insky, Andrew Hunter-Murray,
and"); "James Hark" / "in" too. Also Rhys Darby (Reese/Reece/Rich Darby...) and James Hawkins/Harkins/Harker.
fix_vtt() fixes both; transcriber_diarize.py runs it on every new transcript.

Also Dr Karl Kruszelnicki ("Dr. Carl Krushelnitsky").

Usage (existing transcripts):
    python name_fixes.py          # just LIST what would change (safe)
    python name_fixes.py --go     # change them (originals backed up first)
    python name_fixes.py --test   # check fix_vtt against the CASES below
--go runs the --test cases first and changes nothing if one fails. After changing
a regex, add a line to CASES for the new case (and one for what it must NOT touch).
The .vtt keeps its date (keep_date), so website edits on the episode stay live.
"""

import os
import re
import shutil
import sys

# The surname as Whisper hears it: T/Tu/Ty/Du/Pta + s/sh/sz/sch + ... + -insky/-ski.
# Anchored on that shape so other -sky names (Tchaikovsky, Tarkovsky) never match.
_SURNAME = (r"(?:P?t|T|D)[auy]s?(?:z|sh|sch|ch|s)(?:[iy]n|yn|in|y|e|i)?"
            r"(?:sk[iy]|nsk[iy]|tsk[iy]|wsk[iy])")
SURNAME_RE = re.compile(r"\b" + _SURNAME + r"\b", re.I)
# "Anna Tash" / "Anna Tyshyn" at the end of a cue, rest of the name on the next one.
SPLIT_RE = re.compile(
    r"(\bAnna )(?:P?t|T|D)[auy](?:s|sz|sh|sch|ch)[a-z]{0,3}[ \t]*(\r?\n)"  # cue ends mid-name
    r"((?:\d[\d:.]* --> [\d:.]+[^\n]*\n)?(?:\[[^\]\n]+\] )?)"              # next cue's timing/label
    r"(?:i?n?sk[iy]|[iy]?n?sk[iy]|ski|sky)\b([,.;!?]?)[ \t]*", re.I)
# Any "Anna <something>-ski/-sky/-ska" is her (Chesinski, Chizinski, Tijinsky, ...).
ANNA_SKI_RE = re.compile(r"\b(Anna )(?!Ptaszynski\b)[A-Z][a-z]{2,}(?:sk[iy]|ska)\b")
# Same split as above with any first piece: "Anna Ches" / "inski, and James Hark" / "in."
_NEXT = r"((?:\d[\d:.]* --> [\d:.]+[^\n]*\n)?(?:\[[^\]\n]+\] )?)"
SPLIT_ANY_RE = re.compile(r"(\bAnna )[A-Z][a-z]{0,6}[ \t]*(\r?\n)" + _NEXT +
                          r"[a-z]*(?:sk[iy]|ska)\b([,.;!?]?)[ \t]*")
# "James Hark" / "in", "James H" / "arkin," ...
JAMES_SPLIT_RE = re.compile(r"(\bJames )(?:H|Ha|Har|Hark)[ \t]*(\r?\n)" + _NEXT +
                            r"(?:arkin|rkin|kin|in|orkin)\b([,.;!?]?)[ \t]*")
# James Harkin as Whisper sometimes hears him ("And this is James Hawkins").
JAMES_RE = re.compile(r"\bJames (?:Hawkins|Harkins|Harker)(?:'s|'|’s|’)?(?![\w'’])")
# Rhys Darby: "Reese/Reece/Rez/Riz/Rich Darby" (not "Rise Darby": a joke), "Rhys Derby", and "Rh" / "ys Darby".
RHYS_RE = re.compile(r"\b(?:Reese|Reece|Rez|Riz|Rich|Rhys)\s+D[ae]rby\b", re.I)
RHYS_SPLIT_RE = re.compile(r"\bR(?:h|hy|e|ee)?[ \t]*(\r?\n)" + _NEXT + r"(?:ys|iz|ese|ece) (D[ae]rby)\b")
# "Anna Tash" with nothing after it (an earlier cut that lost the end).
SHORT_RE = re.compile(r"\b(Anna )(?:Tash|Tush|Tuch|Tysh|Tyshyn|Tyszy)\b(?!\w)")
# Marc Abrahams (Ig Nobel Prizes) is spelled with a c.
MARC_RE = re.compile(r"\bMark ?Abrahams?\b")
# Dr Karl Kruszelnicki (Australian science broadcaster): Whisper writes "Dr. Carl" and
# "Krushelnitsky" / "Krushelnetsky". Neighbours' Dr Karl Kennedy is a Karl too.
KARL_RE = re.compile(r"\b(Dr\.? |Doctor )Carl\b")
KRUSZ_RE = re.compile(r"\bKrush[a-z]*(?:sk[iy])\b")
# Ella Al-Shamahi (Fish guest): Whisper writes Alshamahi, al-Shamahi, Al Shamahi, Ashamahi, and mishears
# "Ella" as El / LL / Jail / Jamie. Her handle, read out as "Ella underscore Alshamahi", is left alone.
ELLA_RE = re.compile(r"\b(?:Ella|El|LL|Jail|Jamie)\s+(?:[Aa]l[- ]?|A)?[Ss]hamahi\b")
SHAMAHI_RE = re.compile(r"(?<![Uu]nderscore )\b(?:[Aa]l[- ]?|A)[Ss]hamahi\b")
# QI Elves: whisper writes "QILs" / "QIL", and "QI elves" in lower case.
ELVES_RE = re.compile(r"\bQIL(s'|'s|s)?(?![\w'’])|\bQI (elf|elves)\b", re.I)

RIGHT = "Ptaszynski"


def fix_vtt(text):
    """Return (fixed_text, number_of_fixes)."""
    n = 0
    def joined(m):
        nonlocal n
        n += 1
        # "Anna Tash" / "insky, Andrew" -> "Anna Ptaszynski," / "Andrew"
        return f"{m.group(1)}{RIGHT}{m.group(4)}{m.group(2)}{m.group(3)}"
    def surname(m):
        nonlocal n
        if m.group(0) == RIGHT:
            return RIGHT
        n += 1
        return RIGHT
    def short(m):
        nonlocal n
        n += 1
        return m.group(1) + RIGHT
    def james(m):
        nonlocal n
        n += 1
        return f"{m.group(1)}Harkin{m.group(4)}{m.group(2)}{m.group(3)}"
    text = SPLIT_RE.sub(joined, text)
    text = SPLIT_ANY_RE.sub(joined, text)
    text = JAMES_SPLIT_RE.sub(james, text)
    def rhys(m):
        nonlocal n
        if m.group(0) == "Rhys Darby":
            return m.group(0)
        n += 1
        return "Rhys Darby"
    def rhys_split(m):
        nonlocal n
        n += 1
        return f"Rhys{m.group(1)}{m.group(2)}Darby"
    def james_name(m):
        nonlocal n
        n += 1
        return "James Harkin's" if m.group(0).endswith(("'", "’", "'s", "’s")) else "James Harkin"
    text = JAMES_RE.sub(james_name, text)
    text = RHYS_SPLIT_RE.sub(rhys_split, text)
    text = RHYS_RE.sub(rhys, text)
    text = SURNAME_RE.sub(surname, text)
    text = ANNA_SKI_RE.sub(short, text)
    text = SHORT_RE.sub(short, text)
    def elves(m):
        nonlocal n
        if m.group(0).startswith(("QI Elf", "QI Elves")) and not m.group(1):
            return m.group(0)
        if m.group(0)[:3].lower() == "qil":
            if m.group(0)[:3] != "QIL":   # "qil" in lower case: not this
                return m.group(0)
            n += 1
            suffix = m.group(1)
            return "QI Elf" if not suffix else ("QI Elves'" if suffix == "s'" else "QI Elves")
        n += 1
        return "QI Elf" if m.group(2).lower() == "elf" else "QI Elves"
    text = ELVES_RE.sub(elves, text)
    def marc(m):
        nonlocal n
        n += 1
        return "MarcAbrahams" if " " not in m.group(0) else "Marc Abrahams"
    text = MARC_RE.sub(marc, text)
    def karl(m):
        nonlocal n
        n += 1
        return m[1] + "Karl"
    text = KARL_RE.sub(karl, text)
    def krusz(m):
        nonlocal n
        n += 1
        return "Kruszelnicki"
    text = KRUSZ_RE.sub(krusz, text)
    def ella(m):
        nonlocal n
        if m[0] in ("Ella Al-Shamahi", "Al-Shamahi"):
            return m[0]
        n += 1
        return "Ella Al-Shamahi" if m[0][0] in "EJL" and " " in m[0] and not m[0].startswith("Al") else "Al-Shamahi"
    text = ELLA_RE.sub(ella, text)
    text = SHAMAHI_RE.sub(ella, text)
    return text, n


def fix_name(name):
    """A speaker name with the same fixes as the text ("Ella Al Shamahi" -> "Ella Al-Shamahi"),
    for names guessed from episode descriptions, which have the feed's spelling."""
    return fix_vtt(name)[0] if name else name


# (input, expected output) for --test. Cue-split cases use real VTT shape.
_CUE = "00:00:02.000 --> 00:00:03.000\n"
CASES = [
    ("sitting here with Anna Tashinsky and James", "sitting here with Anna Ptaszynski and James"),
    ("with Anna Chesinski, and", "with Anna Ptaszynski, and"),
    ("Anna Tash said", "Anna Ptaszynski said"),
    ("Duszynski", "Ptaszynski"),
    ("[Speaker 1] with Anna Tash\n" + _CUE + "[Speaker 1] insky, Andrew Hunter-Murray, and\n",
     "[Speaker 1] with Anna Ptaszynski,\n" + _CUE + "[Speaker 1] Andrew Hunter-Murray, and\n"),
    ("Anna Ches\n" + _CUE + "inski, and\n", "Anna Ptaszynski,\n" + _CUE + "and\n"),
    ("line one\r\nAnna Tashinsky\r\n", "line one\r\nAnna Ptaszynski\r\n"),       # CRLF kept
    ("and James Hark\n" + _CUE + "in. Hello\n", "and James Harkin.\n" + _CUE + "Hello\n"),
    ("And this is James Hawkins.", "And this is James Harkin."),
    ("James Harker's book", "James Harkin's book"),
    ("Reese Darby is here", "Rhys Darby is here"),
    ("Rhys Derby", "Rhys Darby"),
    ("with Rh\n" + _CUE + "[Speaker 2] ys Darby here\n", "with Rhys\n" + _CUE + "[Speaker 2] Darby here\n"),
    ("the QILs found that", "the QI Elves found that"),
    ("a QIL said", "a QI Elf said"),
    ("the QI elves", "the QI Elves"),
    ("Mark Abrahams of the Ig Nobels", "Marc Abrahams of the Ig Nobels"),
    ("a scientist called Dr. Carl Krushelnetsky.", "a scientist called Dr. Karl Kruszelnicki."),
    ("Dr. Carl's website", "Dr. Karl's website"),
    ("Doctor Carl said", "Doctor Karl said"),
    ("and Ella Alshamahi.", "and Ella Al-Shamahi."),
    ("with Ella al-Shamahi,", "with Ella Al-Shamahi,"),
    ("Ella Ashamahi, hello", "Ella Al-Shamahi, hello"),
    ("Jail Shamahi, welcome", "Ella Al-Shamahi, welcome"),
    ("LL Shamahi, and", "Ella Al-Shamahi, and"),
    ("El Al-Shamahi said", "Ella Al-Shamahi said"),
    ("Dr Al Shamahi said", "Dr Al-Shamahi said"),
    # must be left alone
    ("Anna Ptaszynski is here", "Anna Ptaszynski is here"),
    ("Rhys Darby is here", "Rhys Darby is here"),
    ("Rise Darby is here", "Rise Darby is here"),             # a joke
    ("Tchaikovsky and Tarkovsky", "Tchaikovsky and Tarkovsky"),
    ("QI Elves are great", "QI Elves are great"),
    ("the qil is", "the qil is"),
    ("Ella Al-Shamahi is here", "Ella Al-Shamahi is here"),
    ("Ella underscore Alshamahi.", "Ella underscore Alshamahi."),
    ("The House of Carlton", "The House of Carlton"),
    ("Carl Sagan", "Carl Sagan"),
]


def run_tests():
    """Check fix_vtt against CASES; print each failure. Returns True if all pass."""
    bad = 0
    for given, want in CASES:
        got = fix_vtt(given)[0]
        if got != want:
            bad += 1
            print(f"FAIL  {given!r}\n      want {want!r}\n      got  {got!r}")
    print(f"{len(CASES) - bad} of {len(CASES)} name-fix cases pass.")
    return bad == 0


def keep_date(path, st, spk_json):
    """Put the transcript's old date back after a small text fix. Website edits
    are matched to the .vtt's date (an edit older than the file counts as stale),
    so a new date would silently drop every edit on the episode. 1 s earlier and
    the speaker file touched, so the indexer still notices the new text. A whole
    second, not less: upload.sh's rsync compares dates to the second, so a fix
    that keeps the file's size would otherwise never reach the server."""
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns - 1_000_000_000))
    if os.path.exists(spk_json):
        os.utime(spk_json)


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from paths import ROOT, SPEAKERS_DIR, PODCASTS
    if "--test" in sys.argv:
        sys.exit(0 if run_tests() else 1)
    go = "--go" in sys.argv
    if go and not run_tests():
        sys.exit("A name-fix case fails, so no transcript was changed.")
    import datetime
    backup = os.path.join(ROOT, "local only", f"backup-{datetime.date.today()}", "name-fixes")
    total_files = total = 0
    for podcast in PODCASTS:
        d = os.path.join(ROOT, podcast, "vtts")
        for f in sorted(os.listdir(d)):
            if not f.endswith(".vtt"):
                continue
            path = os.path.join(d, f)
            with open(path, encoding="utf-8", newline="") as fh:   # keep CRLF files CRLF
                old = fh.read()
            new, n = fix_vtt(old)
            if new == old:
                continue
            total_files += 1
            total += n
            print(f"  {n:3}  {podcast[:12]} | {f[:70]}")
            if go:
                os.makedirs(os.path.join(backup, podcast), exist_ok=True)
                dest = os.path.join(backup, podcast, f)
                if not os.path.exists(dest):
                    shutil.copy2(path, dest)
                st = os.stat(path)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8", newline="") as fh:
                    fh.write(new)
                os.replace(tmp, path)
                keep_date(path, st, os.path.join(SPEAKERS_DIR, podcast, f[:-4] + ".json"))
    print(f"\n{total} fixes in {total_files} transcripts."
          + ("" if go else "  Nothing changed. Run with --go to apply."))


if __name__ == "__main__":
    main()
