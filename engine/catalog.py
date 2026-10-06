#!/usr/bin/env python3
"""Garmin exercise catalog — the source of truth for strength-workout animation.

On-watch exercise animations are keyed to a valid (category, exerciseName) pair.
Garmin publishes the full catalog at a public, unauthenticated URL; we cache it
locally so the workout builder can validate every strength step before upload.

Categories like SQUAT/LUNGE/PUSH_UP each hold dozens of named exercises
(e.g. SQUAT -> AIR_SQUAT, GOBLET_SQUAT, BACK_SQUATS). Using the bare category
with exerciseName=None (what Garmin's coach plans do) shows the name but no
animation; pairing it with a real exerciseName is what makes it animate.
"""
import json
import os
import urllib.request
import paths

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL = os.path.join(paths.state_dir(), "catalog", "Exercises.json")
LIVE_URL = "https://connect.garmin.com/web-data/exercises/Exercises.json"


def load(refresh: bool = False) -> dict:
    """Return {CATEGORY: {EXERCISE_NAME: {...}}}. Refresh pulls Garmin's live copy."""
    if refresh or not os.path.exists(LOCAL):
        os.makedirs(os.path.dirname(LOCAL), exist_ok=True)
        # A User-Agent is required, not cosmetic: Garmin's CDN answers 403 to
        # urllib's default "Python-urllib/3.x". This only ever bit a fresh
        # install, because an existing local cache skips the fetch entirely --
        # so it was invisible on a developer machine and fatal on first run.
        req = urllib.request.Request(
            LIVE_URL, headers={"User-Agent": "GarminBridge/1.0 (+https://github.com/Anneo22/garminbridge)"}
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        with open(LOCAL, "wb") as f:
            f.write(data)
    raw = json.load(open(LOCAL))
    cats = raw["categories"]
    return {c: cats[c]["exercises"] for c in cats}


def validate(category: str, exercise_name: str | None, cat_map: dict | None = None) -> None:
    """Raise ValueError if (category, exercise_name) is not a real Garmin pair."""
    cat_map = cat_map or load()
    cu = category.upper()
    if cu not in cat_map:
        raise ValueError(f"unknown category {category!r} (have {len(cat_map)} categories)")
    if exercise_name is not None and exercise_name.upper() not in cat_map[cu]:
        sample = list(cat_map[cu])[:6]
        raise ValueError(
            f"{exercise_name!r} is not in {cu}; e.g. {sample} ({len(cat_map[cu])} total)"
        )


def search(term: str, cat_map: dict | None = None, limit: int = 20):
    """Find (category, exerciseName) pairs whose name contains term (case-insensitive)."""
    cat_map = cat_map or load()
    t = term.upper()
    hits = []
    for cat, exs in cat_map.items():
        for name in exs:
            if t in name or t in cat:
                hits.append((cat, name))
                if len(hits) >= limit:
                    return hits
    return hits


if __name__ == "__main__":
    import sys

    m = load(refresh="--refresh" in sys.argv)
    print(f"{len(m)} categories, {sum(len(v) for v in m.values())} exercises")
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        for cat, name in search(sys.argv[1], m):
            print(f"  {cat:18} {name}")
