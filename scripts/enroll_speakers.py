"""
Build voiceprint profiles from the clips in voiceprints/.

Each file voiceprints/<Name>.mp3|wav|m4a|flac becomes one person, labelled
<Name> in the transcripts. A file named "<Name> - anything.mp3" (e.g.
"Rhys - voices.mp3") is added to <Name>'s profile as extra samples.

Every file is cut into 5-second chunks and each chunk is embedded, giving a
multi-sample profile per person (matching uses the best few chunks, which
copes with voices drifting across years/recording setups).

Usage:  python enroll_speakers.py
Re-run whenever you add or change a voiceprint file (takes ~1 min).
Then run name_speakers.py --force to re-name episodes with the new profiles.
"""

import os
import sys

import numpy as np

from voiceid import (VOICEPRINTS_DIR, PROFILES_PATH, SR, Embedder,
                     load_audio, is_speech)

AUDIO_EXT = (".mp3", ".wav", ".m4a", ".flac", ".ogg")
CHUNK_SECONDS = 5


def person_name(filename, all_files=()):
    """"Rhys - voices.mp3" or "Rhys Silly Voices.mp3" both add to [Rhys]
    when a plain "Rhys.mp3" exists."""
    base = os.path.splitext(filename)[0]
    base = base.split(" - ")[0].strip()
    plain = {os.path.splitext(f)[0] for f in all_files}
    for other in sorted(plain, key=len, reverse=True):
        if other != base and base.startswith(other + " "):
            return other
    return base


def main():
    files = sorted(f for f in os.listdir(VOICEPRINTS_DIR) if f.lower().endswith(AUDIO_EXT))
    if not files:
        sys.exit(f"No audio files in {VOICEPRINTS_DIR}")
    emb = Embedder()
    people = {}
    for f in files:
        audio = load_audio(os.path.join(VOICEPRINTS_DIR, f))
        w = CHUNK_SECONDS * SR
        chunks = [emb.embed(audio[s:s + w])
                  for s in range(0, len(audio) - w + 1, w)
                  if is_speech(audio[s:s + w])]
        if not chunks:
            print(f"  {f}: no usable speech, skipped")
            continue
        name = person_name(f, files)
        people.setdefault(name, []).extend(chunks)
        print(f"  {f}: {len(audio) / SR:.0f}s -> {len(chunks)} chunks for [{name}]")

    arrays = {n: np.stack(c) for n, c in people.items()}
    np.savez(PROFILES_PATH, **arrays)

    print(f"\nSaved {len(arrays)} people -> {PROFILES_PATH}")
    names = list(arrays)
    cents = {n: arrays[n].mean(0) / np.linalg.norm(arrays[n].mean(0)) for n in names}
    print("How alike the voices are (0 = different, 1 = same; want < ~0.5):")
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            print(f"  {a:>10} vs {b:<10} {cents[a] @ cents[b]:.2f}")


if __name__ == "__main__":
    main()
