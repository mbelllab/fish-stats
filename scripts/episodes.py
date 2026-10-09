"""
Reading an episode's name (its file name without .mp3/.vtt, as stored in search_index.db).
The one copy for the Python scripts; the website has its own in site.js/stats.js and api.php.

The shapes in use:
  Fish     "2024-07-11 - 539. Title", "2021-05-14 - 373 Title", "2026-06-07 - Little Fish Title"

  date(name)            "2024-07-11", or "" if the name doesn't start with a date
  number(name)          "539" (the first number after the date), or ""
  title(name)           the name without date or number
  kind(podcast, name)   "Main", "Little Fish", "Bonus", ... (see FISH_KIND)
"""
import re

from paths import FISH


def date(name):
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)", name)
    return m[0] if m else ""


def number(name):
    m = re.match(r"\d{4}-\d\d-\d\d\s*-?\s*#?(\d+(?:\.\d+)?)\b", name)
    return m[1] if m else ""


def title(name):
    """The title without date or episode number: "2023-05-17 #1 Title", "2008-10-11 1 #001 Title",
    "2021-05-14 - 373 Title" and "2023-01-26 - 463. Title" all give "Title"."""
    t = re.sub(r"^\d{4}-\d\d-\d\d\s*-?\s*", "", name)
    return re.sub(r"^#?\d+(?:\.\d+)?\.?\s+", "", t) or t


# Episode types, matched against the title after the date. Anchored, so "Especially" isn't a
# Special and a numbered Christmas episode stays a main episode.
FISH_KIND = [("Little Fish", r"^(\d+\.\s+)?little fish\b"), ("Drop Us A Line", r"^(bonus\s+)?drop us a line\b"),
             ("Compilation", r"^(bonus\s+)?(compilation|best of)\b"), ("Club Fish", r"^club fish\b"), ("Bonus", r"^bonus\b"),
             ("Special", r"^(?!\d)(?=.*\b(factball|christmas|fishmas|live|special)\b)")]


def kind(podcast, name):
    t = re.sub(r"^\d{4}-\d\d-\d\d\s*-?\s*", "", name)
    table = FISH_KIND if podcast == FISH else None
    if table is None:
        return "Issue"
    for k, pat in table:
        if re.search(pat, t, re.I):
            return k
    return "Main" if podcast == FISH else "Guest episode"
