"""
transcriber_diarize.py: transcriber with speaker labels, on the AMD GPU under Linux.

whisper.cpp (Vulkan build in scripts/whispercpp/) writes the words, then
WhisperX word alignment + pyannote speaker labels run on the GPU through ROCm torch
(in a child process: this file with --label). It writes WEBVTT with "[Speaker N]"
labels, the format the indexer and website expect.

run_all.py is the only driver: it imports this file and calls preflight(),
find_pending(), prefetch() and transcribe_one(). bench_gpu.py times one episode.
Setup (venv, whisper.cpp build, HuggingFace token and the gated pyannote models):
notes (technical)/the README and the README.
"""

import os
import re
import sys
import json
import time
import shutil
import tempfile
import subprocess

import paths

# Settings
# torch CPU threads in the --label child (the 5700G has 8 cores; 16 was no faster).
THREADS = 8

# The words come from whisper.cpp on the RX 6750 XT (Vulkan) using GPU_MODEL. There is
# no CPU fallback. If whisper.cpp's text fails the punctuation check (or whisper.cpp
# crashes) it is run once more with a second decoding strategy (see words_stage); if
# that fails too the episode is given up and written to REDO_FAILED so run_all.py
# skips it from then on.
GPU_MODEL = "large-v3-turbo"      # file whispercpp/models/ggml-<GPU_MODEL>.bin
GPU_PROMPT = "Hello, and welcome to the show. Today, we're talking about fish, facts, and history."

# HuggingFace token: env var HF_TOKEN, else the file hf_token.txt next to this
# script (one line). It is kept out of git (.gitignore) so it's never pushed.
HF_TOKEN = paths.read_secret("hf_token.txt", env="HF_TOKEN")

# Optional: constrain speaker count (NSTAAF is usually ~4-5 voices). None = automatic.
MIN_SPEAKERS = None
MAX_SPEAKERS = None

PODCASTS = paths.PODCASTS     # the shows (curated/shows.json)

# pyannote (the diarizer) decodes audio via torchcodec, which only supports
# FFmpeg 4-7, but the system FFmpeg is v9. Prepend a bundled FFmpeg 7 (in
# scripts/ffmpeg7/bin) to PATH so torchcodec finds compatible libraries.
# Without this, diarization silently fails and transcripts come out with NO
# speaker labels. Inherited by the --label child via os.environ.
_ff7_bin = paths.FFMPEG_BIN
if os.path.isdir(_ff7_bin):
    os.environ["PATH"] = _ff7_bin + os.pathsep + os.environ.get("PATH", "")

WCPP_DIR = os.path.join(paths.SCRIPTS, "whispercpp")       # whisper.cpp Vulkan build + models
WCPP_CLI = os.path.join(WCPP_DIR, "whisper-cli")

GPU_FAILURES_LOG = os.path.join(paths.LOGS, "gpu_failures.log")   # why each GPU try failed
REDO_FAILED = os.path.join(paths.DATA, "redo_failed.txt")          # episodes given up on


def log(msg):
    """Progress line (timings, retries). Printed by default; run_all.py replaces this
    with its own log() so the lines reach logs/run_all.log (its output goes to
    /dev/null under nohup). Called from the words worker thread too."""
    print(msg, flush=True)


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def preflight():
    """Stop now (exit 1) if something the run needs is missing."""
    import importlib.util
    problems = []
    if importlib.util.find_spec("whisperx") is None:
        problems.append("whisperx is not installed in this python (use the venv, see the README)")
    if not shutil.which("ffmpeg"):
        problems.append("ffmpeg not found on PATH")
    if not HF_TOKEN:
        problems.append("HF_TOKEN not set (needed for speaker labels), see the README")
    if not os.path.isfile(WCPP_CLI):
        problems.append(f"{WCPP_CLI} is missing (whisper.cpp build, see the README)")
    if problems:
        log("Setup incomplete:\n" + "\n".join("  - " + p for p in problems))
        sys.exit(1)


def find_pending():
    pending = []
    for podcast in PODCASTS:
        mp3_dir = os.path.join(podcast, "mp3s")
        vtt_dir = os.path.join(podcast, "vtts")
        if not (os.path.isdir(mp3_dir) and os.path.isdir(vtt_dir)):
            continue
        for filename in sorted(os.listdir(mp3_dir)):
            if not filename.endswith(".mp3"):
                continue
            base = filename[:-4]
            vtt_path = os.path.join(vtt_dir, base + ".vtt")
            if not os.path.exists(vtt_path):
                pending.append({
                    "filename": filename,
                    "podcast": podcast,
                    "mp3_path": os.path.join(mp3_dir, filename),
                    "vtt_path": vtt_path,
                })
    return pending


def speaker_label(raw, mapping):
    """SPEAKER_00 -> [Speaker 1]: the diarizer's own number plus one (not first-seen
    order), so the first voice to speak is not always Speaker 1."""
    if not raw:
        return ""
    if raw not in mapping:
        m = re.search(r"(\d+)$", raw)
        mapping[raw] = (int(m.group(1)) + 1) if m else (len(mapping) + 1)
    return f"[Speaker {mapping[raw]}] "


# A sentence where someone else cuts in is split into one cue per speaker, from the speaker
# the diarizer gives each word. A run shorter than this (seconds) and under 2 words is
# taken as noise at a turn's edge and joins the run before it (or after, at the start).
RUN_MIN = 0.5
CUT_REACH = 3                                  # words: how far a cut may move to reach a break
# A word that ends a sentence (. ! ? ... or a dash). Not a comma: the diarizer hears a new voice
# a beat late, so "Yeah, | you're going to need..." would hand the opener to the last speaker.
BREAK_RE = re.compile(r"[.!?\u2026\u2014-]['\"\u2019\u201d)]*$")


def speaker_runs(seg):
    """[(start, end, text, speaker)]: the segment cut where someone else starts talking.
    One piece (the whole segment) when the words carry no speakers.

    From the speaker the diarizer gives each word:
      1. a word whose neighbours on both sides are someone else's is theirs (noise);
      2. runs of one speaker; a run under 2 words and RUN_MIN seconds joins the one before;
      3. a cut only where a sentence ends (full stop, question mark, "...", a dash),
         moved to the nearest one within CUT_REACH words; with none that near, no cut. The
         diarizer's change is often a word or two off, and a cut in mid-phrase ("You'll
         knock | some away") is worse than none;
      4. each piece goes to whoever has most of its words."""
    words = [w for w in seg.get("words") or [] if (w.get("word") or "").strip()]
    if len(words) < 2 or not any(w.get("speaker") for w in words):
        return [(seg["start"], seg["end"], (seg.get("text") or "").strip(), seg.get("speaker", ""))]
    spk, t = seg.get("speaker", ""), seg["start"]
    who, when = [], []
    for w in words:
        spk = w.get("speaker") or spk           # a word with no speaker (no turn under it) stays with the last
        a = w.get("start", t); b = w.get("end", a)
        t = b
        who.append(spk); when.append((a, b))
    text = [w["word"].strip() for w in words]
    was = list(who)                             # 1 (judged on the diarizer's own answer, not earlier flips)
    for i in range(1, len(who) - 1):
        if was[i - 1] == was[i + 1] != was[i]:
            who[i] = was[i - 1]
    runs = []                                   # 2: [speaker, first word, last word + 1]
    for i, sp in enumerate(who):
        if runs and runs[-1][0] == sp:
            runs[-1][2] = i + 1
        else:
            runs.append([sp, i, i + 1])
    changed = True
    while changed and len(runs) > 1:
        changed = False
        slivers = [r for r in runs if r[2] - r[1] < 2 and when[r[2] - 1][1] - when[r[1]][0] < RUN_MIN]
        if len(slivers) == len(runs):           # nothing but slivers: leave them be
            break
        for r in slivers:
            for i in range(r[1], r[2]):
                who[i] = None                   # joins the run before it (after, at the start)
            changed = True
        runs2 = []
        for i, sp in enumerate(who):
            sp = sp if sp is not None else (runs2[-1][0] if runs2 else next(x for x in who if x is not None))
            who[i] = sp
            if runs2 and runs2[-1][0] == sp:
                runs2[-1][2] = i + 1
            else:
                runs2.append([sp, i, i + 1])
        changed = changed and len(runs2) < len(runs)
        runs = runs2
    breaks = [i + 1 for i, w in enumerate(text[:-1]) if BREAK_RE.search(w)]   # a cut may go before word i+1
    cuts = set()
    for r in runs[1:]:                          # 3
        near = [c for c in breaks if abs(c - r[1]) <= CUT_REACH]
        if near:
            cuts.add(min(near, key=lambda c: (abs(c - r[1]), c)))
    edges = [0] + sorted(cuts) + [len(text)]
    out = []
    for lo, hi in zip(edges, edges[1:]):        # 4
        count = {}
        for sp in who[lo:hi]:
            count[sp] = count.get(sp, 0) + 1
        sp = max(count, key=count.get)
        piece = " ".join(text[lo:hi])
        if out and out[-1][3] == sp:
            out[-1] = (out[-1][0], when[hi - 1][1], out[-1][2] + " " + piece, sp)
        else:
            out.append((when[lo][0], when[hi - 1][1], piece, sp))
    if len(out) == 1:
        return [(seg["start"], seg["end"], (seg.get("text") or "").strip(), out[0][3])]
    return out


# The aligned, diarized words of every episode transcribed, so its lines can
# be cut again (json_to_vtt, retranscribe.py --recut) without the GPU: data/work/words/
# <podcast>/<episode>.json.gz, about 1 MB each, regenerable by transcribing again.
WORDS_DIR = os.path.join(paths.DATA, "words")


def words_cache(mp3_path):
    mp3_path = os.path.abspath(mp3_path)
    podcast = os.path.basename(os.path.dirname(os.path.dirname(mp3_path)))
    return os.path.join(WORDS_DIR, podcast, os.path.splitext(os.path.basename(mp3_path))[0] + ".json.gz")


def json_to_vtt(json_path, vtt_path):
    import gzip
    with (gzip.open if json_path.endswith(".gz") else open)(json_path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    mapping = {}
    out = ["WEBVTT\n\n"]
    for seg in data.get("segments", []):
        if not (seg.get("text") or "").strip():
            continue
        for a, b, text, spk in speaker_runs(seg):
            if not text:
                continue
            label = speaker_label(spk, mapping)
            out.append(f"{format_timestamp(a)} --> {format_timestamp(b)}\n{label}{text}\n\n")
    from name_fixes import fix_vtt          # names Whisper always gets wrong (Ptaszynski)
    tmp = vtt_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(fix_vtt("".join(out))[0])
    os.replace(tmp, vtt_path)


def transcribe_one(mp3_path, vtt_path, speakers=None):
    """speakers: how many people talk in the episode, when known (retranscribe.py): the
    diarizer then looks for exactly that many voices instead of guessing."""
    transcribe_one_gpu(mp3_path, vtt_path, speakers)      # retries on the GPU, never the CPU
    add_bath_intro(mp3_path, vtt_path)


def log_gpu_failure(mp3_path, what, err):
    """Keep the reason in its own file, one line per failed try (run_all's log gets
    the same story through log(), mixed in with everything else)."""
    try:
        with open(GPU_FAILURES_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M')} {what} {os.path.basename(mp3_path)}: {err!r}\n")
    except OSError:
        pass


def give_up(mp3_path, err):
    """Both GPU decoding strategies failed: list the episode in REDO_FAILED so
    run_all.py's redo skips it on every later pass (delete the line to retry it)."""
    base = os.path.splitext(os.path.basename(mp3_path))[0]
    log_gpu_failure(mp3_path, "GAVE UP", err)
    log(f"  giving up on this episode (both GPU tries failed): {err}")
    try:
        listed = set()
        if os.path.exists(REDO_FAILED):
            with open(REDO_FAILED, encoding="utf-8") as fh:
                listed = {l.strip() for l in fh}
        with open(REDO_FAILED, "a", encoding="utf-8") as fh:
            if not listed:
                fh.write("# Episodes the GPU transcriber gave up on (both decoding strategies failed\n"
                         "# the punctuation check or crashed). run_all.py's redo skips these; delete\n"
                         "# a line to try that episode again. Reasons: logs/gpu_failures.log\n")
            if base not in listed:
                fh.write(base + "\n")
    except OSError as e:
        log(f"  (could not write {REDO_FAILED}: {e})")


def add_bath_intro(mp3_path, vtt_path):
    """Drop Us A Line opens with a Japanese bathtub voice that Whisper (told it's
    English) leaves out; intro_clip finds that exact recording and adds the line."""
    if "drop us a line" not in os.path.basename(mp3_path).lower():
        return
    try:
        import intro_clip
        clip = intro_clip.find(mp3_path)
        if clip:
            with open(vtt_path, encoding="utf-8") as f:
                text = f.read()
            tmp = vtt_path + ".tmp"         # a crash mid-write can't truncate the transcript
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(intro_clip.add_to_vtt(text, clip))
            os.replace(tmp, vtt_path)
    except Exception as e:
        log(f"  (bath intro check skipped: {e})")


_words_pool = None          # one background worker: whisper.cpp jobs run one at a time, in order
_words_jobs = {}            # abs mp3 path -> Future of words_stage()


def _submit_words(mp3_path, retry=False):
    """Run words_stage() on the single background worker, so whisper.cpp jobs never
    overlap on the card (heat), and return its Future."""
    global _words_pool
    if _words_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _words_pool = ThreadPoolExecutor(max_workers=1)
    return _words_pool.submit(words_stage, mp3_path, retry)


def prefetch(mp3_path):
    """Queue the GPU words step for an episode so it runs while the CPU is still
    labelling the previous one. run_all.py calls this for the current + next episode."""
    key = os.path.abspath(mp3_path)
    if key not in _words_jobs:
        _words_jobs[key] = _submit_words(mp3_path)


def collapse_loops(text):
    """Whisper sometimes gets stuck repeating itself ("Linda, Linda, Linda, ... x10"
    in 1.4 s). Any word or short phrase repeated 6+ times in a row -> 3 times
    (punctuation ignored when comparing). 6, not 4: real speech has "very, very,
    very, very" and "no, no, no, no, no"."""
    toks = text.split()
    norm = [re.sub(r"[^\w']", "", t).lower() for t in toks]
    out, i = [], 0
    while i < len(toks):
        for n in range(1, 6):
            k = 1
            while norm[i + k * n:i + (k + 1) * n] == norm[i:i + n] and norm[i]:
                k += 1
            if k >= 6:
                out += toks[i + (k - 3) * n:i + k * n]
                i += k * n
                break
        else:
            out.append(toks[i])
            i += 1
    return " ".join(out)


def check_punctuation(segs, window=600.0, min_ends=3.0):
    """Refuse a transcript with an unpunctuated stretch (normal speech here is ~10
    sentence ends per 100 words; the bad style is ~0-1). Raising makes
    transcribe_one_gpu() run words_stage() again with the second strategy."""
    t0 = segs[0]["start"]
    while t0 < segs[-1]["end"]:
        text = " ".join(s["text"] for s in segs if t0 <= s["start"] < t0 + window)
        n = len(text.split())
        if n >= 300 and 100 * sum(text.count(c) for c in ".?!") / n < min_ends:
            raise RuntimeError(f"unpunctuated text from {t0 / 60:.0f} min")
        t0 += window


def merge_sentences(segs, max_len=30.0, max_gap=1.0):
    """whisper.cpp breaks lines mid-sentence; join pieces until a sentence ends
    (WhisperX alignment then splits them into one sentence per line)."""
    out = []
    for s in segs:
        p = out[-1] if out else None
        if (p and not p["text"].endswith((".", "?", "!", '"'))
                and s["start"] - p["end"] <= max_gap and s["end"] - p["start"] <= max_len):
            p["text"] += " " + s["text"]
            p["end"] = s["end"]
        else:
            out.append(dict(s))
    return out


def words_stage(mp3_path, retry=False):
    """GPU half: mp3 -> 16 kHz wav -> whisper.cpp -> segments.json.
    Returns the temp folder holding audio.wav + segments.json (caller deletes it).
    retry=True is the second decoding strategy, used after the first one failed."""
    tdir = tempfile.mkdtemp(prefix="wcpp_")
    try:
        wav = os.path.join(tdir, "audio.wav")
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", mp3_path,
                        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav], check=True)
        t0 = time.time()
        out = os.path.join(tdir, "words")
        cmd = [
            WCPP_CLI,
            "-m", os.path.join(WCPP_DIR, "models", f"ggml-{GPU_MODEL}.bin"),
            "-f", wav, "-l", "en", "-t", "4", "-np",   # 4 CPU threads to feed the GPU (THREADS is for label_stage)
            # Whisper can slip into all-lowercase, unpunctuated text and then copy that
            # style for the rest of the episode ("Kimchi Pirates"). A
            # punctuated prompt re-sent with every window + only 64 tokens of earlier
            # text keeps it punctuated end to end (tested: ~10 sentence ends/100 words).
            "--prompt", GPU_PROMPT, "--carry-initial-prompt", "-mc", "64",
        ]
        if not retry:
            # Voice activity detection: skips music and silence, where Whisper hallucinates.
            cmd += ["--vad", "-vm", os.path.join(WCPP_DIR, "models", "ggml-silero-v5.1.2.bin")]
        # Second try = the same decoding WITHOUT the VAD. The VAD cuts the pauses out of
        # the audio before Whisper hears it, and for some episodes (Fish 278) that is enough to tip the model into lowercase,
        # unpunctuated text for 10-30 min at a time, with or without text context
        # (-mc 0 was tried and failed too). Without the VAD, 278 came out at 9-11
        # sentence ends/100 words in every 10 min window, with no repetition loops.
        subprocess.run(cmd + ["-oj", "-of", out], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(out + ".json", "rb") as f:
            data = json.loads(f.read().decode("utf-8", errors="replace"))
        segs = [{"start": s["offsets"]["from"] / 1000, "end": s["offsets"]["to"] / 1000,
                 "text": collapse_loops(s["text"].strip())}
                for s in data.get("transcription", []) if s["text"].strip()]
        if not segs:
            raise RuntimeError("whisper.cpp produced no text")
        try:
            check_punctuation(segs)
        except RuntimeError:
            # Keep the rejected text so the reason can be looked at later (the temp
            # folder is deleted): logs/rejected/<episode> (try N).json
            try:
                rej = os.path.join(paths.LOGS, "rejected")
                os.makedirs(rej, exist_ok=True)
                base = os.path.splitext(os.path.basename(mp3_path))[0]
                shutil.copy(out + ".json", os.path.join(rej, f"{base} (try {2 if retry else 1}).json"))
            except OSError:
                pass
            raise
        log(f"  words (GPU{', 2nd strategy' if retry else ''}): {(time.time() - t0) / 60:.1f} min"
            f"  {os.path.basename(mp3_path)}")
        with open(os.path.join(tdir, "segments.json"), "w", encoding="utf-8") as f:
            json.dump(merge_sentences(segs), f)
        return tdir
    except Exception:
        shutil.rmtree(tdir, ignore_errors=True)
        raise


def transcribe_one_gpu(mp3_path, vtt_path, speakers=None):
    """whisper.cpp (GPU) for the words -> label_stage() in a child process.
    Words: if the first pass fails (text rejected by check_punctuation, or whisper.cpp
    crashed) it runs once more with the second decoding strategy; if that fails too
    the episode is given up (give_up) and the error re-raised. Labels: one retry after
    a minute. Everything stays on the GPU; there is no CPU fallback."""
    key = os.path.abspath(mp3_path)
    fut = _words_jobs.pop(key, None) or _submit_words(mp3_path)   # queued by run_all, or now
    try:
        tdir = fut.result()
    except Exception as e:
        log_gpu_failure(mp3_path, "try 1", e)
        log(f"  words (GPU) failed ({e}); trying the 2nd decoding strategy")
        if not isinstance(e, RuntimeError):      # a crash, not bad text: give the card a minute
            time.sleep(60)
        try:
            # Goes through the same single worker, so if run_all has already queued the
            # next episode's words those run first (not wasted: they're needed anyway).
            tdir = _submit_words(mp3_path, retry=True).result()
        except Exception as e2:
            log_gpu_failure(mp3_path, "try 2", e2)
            give_up(mp3_path, e2)
            raise
    try:
        result_json = os.path.join(tdir, "result.json")
        # Child process so the align + pyannote models are freed after every episode.
        # Its timings (stdout) go through log(); on a failure, so do the last lines of
        # its stderr (the traceback), which would otherwise vanish with run_all's output.
        for attempt in (1, 2):
            r = subprocess.run([sys.executable, os.path.abspath(__file__), "--label",
                                os.path.join(tdir, "audio.wav"), os.path.join(tdir, "segments.json"),
                                result_json] + ([str(speakers)] if speakers else []),
                               capture_output=True, text=True, errors="replace")
            for line in r.stdout.splitlines():
                log(line)
            if r.returncode == 0:
                break
            e = subprocess.CalledProcessError(r.returncode, r.args)
            log_gpu_failure(mp3_path, f"labels try {attempt}", e)
            if r.stderr.strip():
                log("\n".join(r.stderr.splitlines()[-15:]))
            if attempt == 2:
                raise e
            log(f"  speaker labels failed ({e}); trying again in a minute")
            time.sleep(60)
        try:                                     # keep the words for re-cutting (WORDS_DIR), first:
            import gzip                          # a failure below then costs no GPU time to redo
            keep = words_cache(mp3_path)
            os.makedirs(os.path.dirname(keep), exist_ok=True)
            with open(result_json, "rb") as src, gzip.open(keep, "wb") as dst:
                shutil.copyfileobj(src, dst)
        except OSError as e:
            log(f"  could not keep the words ({e})")
        json_to_vtt(result_json, vtt_path)
    finally:
        shutil.rmtree(tdir, ignore_errors=True)


# Word alignment looks at a whole segment at once, and its memory grows with the square of the
# segment's length: a 6.5-minute segment asked the 12 GB card for
# 17 GB and crashed every time. Segments longer than ALIGN_MAX seconds are cut into even pieces,
# their words shared out by time; alignment then places each word within its piece.
ALIGN_MAX = 30


def split_long_segments(segs):
    out = []
    for seg in segs:
        words = seg.get("text", "").split()
        dur = seg["end"] - seg["start"]
        if dur <= ALIGN_MAX:
            out.append(seg)
            continue
        if len(words) < 2:                # a long stretch with a word or two (music, a pause): keep the
            out.append({**seg, "end": seg["start"] + ALIGN_MAX})   # words, in a window alignment can handle
            continue
        n = min(len(words), int(dur // ALIGN_MAX) + 1)
        for i in range(n):
            part = words[i * len(words) // n:(i + 1) * len(words) // n]
            if part:
                out.append({**seg, "start": seg["start"] + dur * i / n, "end": seg["start"] + dur * (i + 1) / n,
                            "text": " ".join(part)})
    return out


def label_stage(wav, seg_json, out_json, speakers=None):
    """Second half, run in the --label child: WhisperX word alignment + pyannote speakers
    on the GPU (ROCm torch; CPU only if torch sees no GPU). Prints its timings.
    speakers: exactly how many voices to find, when known."""
    import gc
    import torch
    import whisperx
    from whisperx.diarize import DiarizationPipeline
    torch.set_num_threads(THREADS)
    # With ROCm torch the AMD card shows up as "cuda".
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    # ROCm: MIOpen (cuDNN's AMD stand-in) compiles pyannote's LSTM dropout kernel at
    # runtime and needs rocRAND headers the torch wheel lacks -> crash. PyTorch's own
    # GPU kernels instead: same speed here (align 0.8 min, speakers 2.9 min per episode).
    if torch.version.hip:
        torch.backends.cudnn.enabled = False
    audio = whisperx.load_audio(wav)
    with open(seg_json, encoding="utf-8") as f:
        segs = json.load(f)

    longest = max((x["end"] - x["start"] for x in segs), default=0)
    segs = split_long_segments(segs)
    if longest > ALIGN_MAX:
        print(f"  longest segment {longest:.0f} s: split for alignment", flush=True)
    t0 = time.time()
    align_model, meta = whisperx.load_align_model("en", dev)
    result = whisperx.align(segs, align_model, meta, audio, dev)
    del align_model
    gc.collect()
    print(f"  align ({dev}): {(time.time() - t0) / 60:.1f} min", flush=True)

    t0 = time.time()
    # Same model the whisperx CLI uses by default, fed in-memory audio.
    diarizer = DiarizationPipeline(model_name="pyannote/speaker-diarization-community-1",
                                   token=HF_TOKEN, device=dev)
    if speakers:
        turns = diarizer(audio, num_speakers=int(speakers))
        print(f"  speakers: looking for exactly {speakers}", flush=True)
    else:
        turns = diarizer(audio, min_speakers=MIN_SPEAKERS, max_speakers=MAX_SPEAKERS)
    result = whisperx.assign_word_speakers(turns, result)
    print(f"  speakers ({dev}): {(time.time() - t0) / 60:.1f} min", flush=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f)


if __name__ == "__main__":
    if len(sys.argv) in (5, 6) and sys.argv[1] == "--label":
        label_stage(*sys.argv[2:])
    else:
        sys.exit("This file is run by run_all.py (or bench_gpu.py), not by hand.\n"
                 "Usage of the child stage: transcriber_diarize.py --label <audio.wav> <segments.json> <out.json> [speakers]")
