"""Where things are. Every script imports this.

  ROOT      data/: one folder per show (<show>/mp3s/, <show>/vtts/) and the database
  DATA      data/work/: speaker names, voiceprints, logs (all regenerable)
  CURATED   curated/: the shows and the hand-made lists (in git)
  SITE      site/: the built website

The shows and their hosts come from curated/shows.json.
"""
import json
import os

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(SCRIPTS)
ROOT = os.path.join(HERE, "data")
DATA = os.path.join(ROOT, "work")
CURATED = os.path.join(HERE, "curated")
SITE = os.path.join(HERE, "site")
PAGES = os.path.join(HERE, "pages")
SPEAKERS_DIR = os.path.join(DATA, "speakers")            # name_speakers.py output, one JSON per episode
PROFILES_PATH = os.path.join(DATA, "voiceprints.npz")    # enroll_speakers.py output
LOGS = os.path.join(DATA, "logs")
VOICEPRINTS_DIR = os.path.join(ROOT, "voiceprints")      # your voice clips, one file per person
STATUS = os.path.join(DATA, "status.md")
# pyannote's audio decoder needs FFmpeg 4-7. If your system FFmpeg is newer, point this at an
# FFmpeg 7 bin/ folder (or set FFMPEG7_BIN); the transcriber puts it first on PATH.
FFMPEG_BIN = os.environ.get("FFMPEG7_BIN") or os.path.join(SCRIPTS, "ffmpeg7", "bin")

with open(os.path.join(CURATED, "shows.json"), encoding="utf-8") as _f:
    SHOWS = json.load(_f)["shows"]
PODCASTS = [s["name"] for s in SHOWS]
_BY_KEY = {s["key"]: s["name"] for s in SHOWS}
FISH = _BY_KEY.get("fish", "No Such Thing As A Fish")


class _ByShow(dict):
    """{show: hosts}; a show that isn't in shows.json has none."""
    def __init__(self, items, empty):
        super().__init__(items)
        self.empty = empty

    def __missing__(self, show):
        return self.empty()


HOSTS = _ByShow({s["name"]: list(s["hosts"]) for s in SHOWS}, list)
REGULARS = _ByShow({s["name"]: set(s["hosts"]) for s in SHOWS}, set)


def read_secret(name, env=None):
    """First line of scripts/<name> (e.g. hf_token.txt, not in git), or "" if missing.
    env: an environment variable that wins if set."""
    if env and os.environ.get(env):
        return os.environ[env]
    try:
        with open(os.path.join(SCRIPTS, name), encoding="utf-8") as f:
            return f.readline().strip()
    except OSError:
        return ""


os.makedirs(LOGS, exist_ok=True)
