"""Bring in transcripts made elsewhere, with the speakers already named.

Reads WEBVTT (.vtt) or SubRip (.srt) files whose lines say who is talking, in any of
these ways:
    <v Dan>My fact this week is ...</v>        (the WEBVTT voice tag)
    [Dan] My fact this week is ...
    Dan: My fact this week is ...
and writes them the way this code reads transcripts: data/<show>/vtts/<episode>.vtt,
every line starting "[Speaker N]", with the names in curated/speaker_names.json (the
voices named by hand, which voiceprint matching never overrides).
A line with no name keeps the speaker of the line before it; "Speaker 3" stays unnamed.

The file name is the episode name and must start with the date, as the stats read the
date, number and title from it:
    2024-03-14 - 522. No Such Thing As Monet's Bog Cottons.vtt

    python scripts/import_transcripts.py "No Such Thing As A Fish" path/to/transcripts/*.vtt
"""
import json, os, re, sys

import paths

TIME = r"(\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}"
CUE_RE = re.compile(rf"^({TIME})\s*-->\s*({TIME})[^\n]*\n(.*?)(?=\n\s*\n|\Z)", re.S | re.M)
NAMED = [re.compile(r"^<v(?:\.[\w.-]+)?\s+([^>]+)>\s*(.*?)(?:</v>)?$", re.S),
         re.compile(r"^\[([^\]\d][^\]]{0,40})\]\s*(.*)$", re.S),
         re.compile(r"^([A-Z][\w'.-]*(?: [A-Z][\w'.-]*){0,3}):\s+(.*)$", re.S)]


def vtt_time(t):
    t = t.replace(",", ".")
    return t if t.count(":") == 2 else "00:" + t


def convert(text):
    """The transcript as [Speaker N] cues, and {"Speaker N": name}."""
    labels, cues, current = {}, [], ""
    for start, _, end, _, body in CUE_RE.findall(text.replace("\r\n", "\n")):
        body = " ".join(body.split())
        for pat in NAMED:
            m = pat.match(body)
            if m:
                current, body = m[1].strip(), m[2].strip()
                break
        body = re.sub(r"</?[^>]+>", "", body).strip()
        if not body:
            continue
        label = labels.setdefault(current or "?", f"Speaker {len(labels)}")
        cues.append(f"{vtt_time(start)} --> {vtt_time(end)}\n[{label}] {body}")
    names = {lab: name for name, lab in labels.items() if name != "?" and not re.fullmatch(r"Speaker \d+", name)}
    return "WEBVTT\n\n" + "\n\n".join(cues) + "\n", names


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in paths.PODCASTS:
        sys.exit(f"Usage: import_transcripts.py SHOW FILE...   (SHOW one of: {', '.join(paths.PODCASTS)})")
    show = sys.argv[1]
    vtts = os.path.join(paths.ROOT, show, "vtts")
    os.makedirs(vtts, exist_ok=True)
    names_path = os.path.join(paths.CURATED, "speaker_names.json")
    hand = json.load(open(names_path, encoding="utf-8")) if os.path.exists(names_path) else {}
    for src in sys.argv[2:]:
        base = os.path.splitext(os.path.basename(src))[0]
        if not re.match(r"\d{4}-\d\d-\d\d", base):
            print(f"  skipped {base}: the name must start with the date (YYYY-MM-DD)")
            continue
        vtt, names = convert(open(src, encoding="utf-8", errors="replace").read())
        with open(os.path.join(vtts, base + ".vtt"), "w", encoding="utf-8") as f:
            f.write(vtt)
        hand.setdefault(show, {})[base] = names
        print(f"  {base}: {vtt.count('-->')} lines, speakers: {', '.join(names.values()) or 'none named'}")
    with open(names_path, "w", encoding="utf-8") as f:
        json.dump(hand, f, indent=1, ensure_ascii=False)
        f.write("\n")


if __name__ == "__main__":
    main()
