"""
Shared helpers for speaker identification (voiceprints).

Used by enroll_speakers.py (build profiles from voiceprints/) and
name_speakers.py (match each episode's [Speaker N] labels to those profiles).

Audio is decoded with the bundled FFmpeg 7 (scripts/ffmpeg7) straight to
16 kHz mono floats, so pyannote's own decoder (torchcodec) is never used.
"""

import os
import subprocess
import warnings

import numpy as np

warnings.filterwarnings("ignore")

from paths import ROOT, FFMPEG_BIN, VOICEPRINTS_DIR, PROFILES_PATH, read_secret

# Bundled FFmpeg 7 on Windows; on Linux the bundle is Windows-only, so use the system one.
FFMPEG = os.path.join(FFMPEG_BIN, "ffmpeg.exe") if os.name == "nt" else "ffmpeg"

SR = 16000
EMBED_MODEL = "pyannote/wespeaker-voxceleb-resnet34-LM"


def hf_token():
    """HuggingFace token: env var HF_TOKEN, else hf_token.txt (gitignored) beside the scripts."""
    return read_secret("hf_token.txt", env="HF_TOKEN")


def load_audio(path, start=None, duration=None):
    """Decode (part of) an audio file to a 16 kHz mono float32 array."""
    cmd = [FFMPEG, "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.2f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.2f}"]
    cmd += ["-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


class Embedder:
    def __init__(self):
        import torch
        from pyannote.audio import Model, Inference
        self.torch = torch
        model = Model.from_pretrained(EMBED_MODEL, token=hf_token())
        self.inf = Inference(model, window="whole")

    def embed(self, samples):
        """Unit-length voice embedding for a chunk of 16 kHz samples."""
        wav = self.torch.from_numpy(np.ascontiguousarray(samples))[None]
        e = np.asarray(self.inf({"waveform": wav, "sample_rate": SR})).ravel()
        return e / (np.linalg.norm(e) + 1e-9)


def is_speech(samples, min_rms=0.005):
    return len(samples) > 0 and float(np.sqrt((samples ** 2).mean())) >= min_rms


def load_profiles(path=PROFILES_PATH):
    """{name: (n_chunks x dim) array of unit embeddings}"""
    if not os.path.exists(path):
        raise SystemExit(f"No voiceprint profiles at {path} - run enroll_speakers.py first.")
    data = np.load(path, allow_pickle=False)
    return {k: data[k] for k in data.files}


def person_score(emb, chunks, top_k=3):
    """Similarity of one embedding to a person: mean of the top-k matches
    against that person's enrolled chunks (multi-sample profile)."""
    sims = chunks @ emb
    k = min(top_k, len(sims))
    return float(np.sort(sims)[-k:].mean())
