#!/usr/bin/env python3
"""clean_watch.py — the content manager's first end-to-end slice.

Problem (seen on the real Fenix 8): the watch's GARMIN/Workouts holds ~58 files,
most STRANDED — deleted from Garmin Connect (incl. 285 HumanGO) yet still on the
device, because a Connect delete does NOT remove from the watch. This finds the
stranded ones and (only on explicit go) removes them over USB.

Safety is the whole point (see memory delete-only-by-explicit-id — a name-substring
filter once deleted 6 real workouts):
  - A watch file is "stranded" ONLY if its name matches NO current Connect workout.
  - Matching is CONSERVATIVE and biased toward KEEP: equality OR prefix-containment
    on a normalised key (lowercased, alphanumerics only). A false KEEP just leaves a
    junk file; a false STRANDED would delete a live workout — so when unsure, keep.
  - DRY-RUN by default: prints the full manifest, deletes nothing.
  - --delete requires --yes, refuses to touch any Connect-matched file, and honours
    --protect substrings as belt-and-suspenders. After deleting it RE-VERIFIES that
    every Connect-live file still survives; if one vanished it screams.

Usage:
  ./clean_watch.py                 # dry-run: show live vs stranded on the watch
  ./clean_watch.py --delete --yes  # remove stranded files from the watch (guarded)
  ./clean_watch.py --delete --yes --protect "Full_Body,Pilates"
"""
import os
import re
import subprocess
import sys

from garminconnect import Garmin
import paths

HERE = os.path.dirname(os.path.abspath(__file__))
MTP = paths.asset("watch_mtp.sh")

# Date-named files (YYYY-MM-DD.fit, optionally _N) in GARMIN/Workouts are SCHEDULED
# workouts a training plan (e.g. HumanGO) pushes for the coming days. They re-sync
# daily, so deleting them is futile — kept in their own bucket and skipped unless
# --include-scheduled is passed. They are NOT the static junk we want to clear.
SCHED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(_\d+)?\.fit$", re.I)


def _norm(s):
    """Normalise a name/filename stem to a comparison key: lowercase, alphanumerics
    only. 'Mona Fartlek (Lactate Shuttle)' -> 'monafartleklactateshuttle'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _stem(filename):
    """Strip the Garmin watch-file wrapper: '<name>_workout.fit' -> '<name>'.
    Falls back to dropping just '.fit' if the '_workout' suffix isn't present."""
    f = filename
    if f.lower().endswith(".fit"):
        f = f[:-4]
    if f.lower().endswith("_workout"):
        f = f[:-8]
    return f


def connect_live_keys(g):
    """Normalised keys for every workout currently in Connect = the KEEP set."""
    keys = []
    rows = g.connectapi(
        "/workout-service/workouts?start=0&limit=400&myWorkoutsOnly=true")
    for w in rows:
        name = w.get("workoutName")
        if name:
            keys.append((_norm(name), name))
    return keys


def watch_files():
    """[(idx, filename)] for GARMIN/Workouts via the proven MTP helper. Read-only."""
    p = subprocess.run(["bash", MTP, "list"], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        raise SystemExit(f"watch list failed (is the watch on USB?) rc={p.returncode}")
    out = []
    for line in p.stdout.splitlines():
        if "\t" not in line:
            continue
        idx, fname = line.split("\t", 1)
        if idx.strip().isdigit() and fname.strip():
            out.append((int(idx), fname.strip()))
    return out


def is_live(file_key, live_keys, protect):
    """Conservative: a file is LIVE (keep) if it matches any Connect key by equality
    OR prefix-containment in EITHER direction, or matches a --protect substring.
    Bias to keep — only a clean no-match anywhere makes it stranded."""
    if any(p and p in file_key for p in protect):
        return ("protected", None)
    for k, name in live_keys:
        if not k:
            continue
        if file_key == k or file_key.startswith(k) or k.startswith(file_key):
            return ("connect", name)
    return (None, None)


def classify(files, live_keys, protect):
    live, stranded = [], []
    for idx, fname in files:
        key = _norm(_stem(fname))
        why, match = is_live(key, live_keys, protect)
        if why:
            live.append((idx, fname, why, match))
        else:
            stranded.append((idx, fname))
    return live, stranded


def main():
    do_delete = "--delete" in sys.argv
    confirmed = "--yes" in sys.argv
    protect = []
    for i, a in enumerate(sys.argv):
        if a == "--protect" and i + 1 < len(sys.argv):
            protect = [_norm(x) for x in sys.argv[i + 1].split(",") if x.strip()]

    include_sched = "--include-scheduled" in sys.argv

    g = Garmin()
    g.login(paths.state(".tokens"))
    live_keys = connect_live_keys(g)
    files = watch_files()
    live, stranded = classify(files, live_keys, protect)

    # split the stranded set: date-named scheduled files (re-sync, skip) vs real junk
    scheduled = [(i, f) for i, f in stranded if SCHED_RE.match(f)]
    junk = [(i, f) for i, f in stranded if not SCHED_RE.match(f)]

    print(f"\n=== watch GARMIN/Workouts: {len(files)} files | {len(live)} live | "
          f"{len(junk)} junk | {len(scheduled)} scheduled ===\n")
    print(f"Connect currently has {len(live_keys)} workouts (the keep set).\n")

    if junk:
        print("JUNK (stranded — deleted from Connect, safe to remove from the watch):")
        for idx, fname in junk:
            print(f"  #{idx:<4} {fname}")
    else:
        print("No junk files — the watch is clean of stranded workouts.")
    print()
    if scheduled:
        print("SCHEDULED (date-named, a training plan re-syncs these — SKIPPED by "
              "default; --include-scheduled to clear them too, but they come back):")
        for idx, fname in scheduled:
            print(f"  #{idx:<4} {fname}")
        print()
    print("LIVE (matched to a Connect workout — will NEVER be deleted):")
    for idx, fname, why, match in live:
        tag = f"-> {match!r}" if match else f"[{why}]"
        print(f"  #{idx:<4} {fname:50} {tag}")
    print()

    to_delete = junk + (scheduled if include_sched else [])

    if not do_delete:
        print(f"DRY-RUN. Nothing deleted. Re-run with  --delete --yes  to remove the "
              f"{len(to_delete)} file(s)"
              f"{' (junk + scheduled)' if include_sched else ' (junk only)'}.")
        return
    if not to_delete:
        return
    if not confirmed:
        print("Refusing to delete without --yes. (Review the lists above first.)")
        return

    # belt-and-suspenders: never pass a LIVE index to the deleter
    live_idx = {idx for idx, *_ in live}
    targets = [idx for idx, _ in to_delete if idx not in live_idx]
    assert len(targets) == len(to_delete), "a live file leaked into the delete set — ABORT"
    print(f"Deleting {len(targets)} file(s) over USB...")
    idx_csv = ",".join(str(i) for i in targets)
    p = subprocess.run(["bash", MTP, "delete", idx_csv], capture_output=True, text=True)
    sys.stderr.write(p.stderr)
    if p.returncode != 0:
        raise SystemExit(f"delete failed rc={p.returncode}")

    # re-verify: every Connect-live filename must still be present
    survivors = {f for _, f in watch_files()}
    lost_live = [f for _, f, *_ in live if f not in survivors]
    if lost_live:
        print("\n!!! WARNING: a Connect-live file disappeared during cleanup:")
        for f in lost_live:
            print(f"    {f}")
        raise SystemExit("live-file loss detected — investigate before trusting this tool")
    still = [f for _, f in to_delete if f in survivors]
    print(f"\nDone. Removed {len(to_delete) - len(still)}/{len(to_delete)} target file(s). "
          f"All {len(live)} live files intact.")
    if still:
        print("Some target files survived (MTP hiccup?) — re-run to finish:")
        for f in still:
            print(f"    {f}")


if __name__ == "__main__":
    main()
