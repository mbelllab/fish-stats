"""Transcribe every mp3 that has no transcript yet, on the GPU.

  Audio:        data/<show>/mp3s/<episode>.mp3
  Transcripts:  data/<show>/vtts/<episode>.vtt   (WEBVTT, one "[Speaker N]" label per line)

Run it through run_transcribe.sh, which starts the GPU heat guard first. Re-running is
safe: finished episodes are skipped. See the README for the one-time setup.

    python scripts/transcribe.py
"""
import os, re, sys, time

import paths

os.chdir(paths.SCRIPTS)                 # the transcriber finds whispercpp/ from here
import transcriber_diarize as td       # noqa: E402

LABEL_RE = re.compile(r"^\[Speaker \d+\]", re.M)


def has_labels(path):
    try:
        return bool(LABEL_RE.search(open(path, encoding="utf-8", errors="replace").read()))
    except OSError:
        return False


def main():
    td.preflight()                     # stops with a list if the setup is incomplete
    todo = []
    for show in paths.PODCASTS:
        mp3s, vtts = os.path.join(paths.ROOT, show, "mp3s"), os.path.join(paths.ROOT, show, "vtts")
        os.makedirs(vtts, exist_ok=True)
        if os.path.isdir(mp3s):
            todo += [(os.path.join(mp3s, f), os.path.join(vtts, f[:-4] + ".vtt")) for f in sorted(os.listdir(mp3s))
                     if f.endswith(".mp3") and not has_labels(os.path.join(vtts, f[:-4] + ".vtt"))]
    print(f"{len(todo)} episode(s) to transcribe", flush=True)
    ok = failed = 0
    for i, (mp3, vtt) in enumerate(todo, 1):
        # Queue this episode's words and the next one's, so the GPU works on the next
        # while the speaker labels for this one are made.
        for m, _ in todo[i - 1:i + 1]:
            td.prefetch(m)
        name, new, t0 = os.path.basename(vtt)[:-4], vtt + ".new", time.time()
        try:
            td.transcribe_one(mp3, new)
        except Exception as e:
            print(f"[{i}/{len(todo)}] {name}: FAILED ({e})", flush=True)
            failed += 1
            if os.path.exists(new):
                os.remove(new)
            continue
        if not has_labels(new):
            os.remove(new)
            print(f"[{i}/{len(todo)}] {name}: FAILED (no speaker labels)", flush=True)
            failed += 1
            continue
        os.replace(new, vtt)
        ok += 1
        print(f"[{i}/{len(todo)}] {name}: {(time.time() - t0) / 60:.0f} min", flush=True)
    print(f"Done: {ok} transcribed, {failed} failed.", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
