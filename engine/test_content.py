#!/usr/bin/env python3
"""Regression guard for content.py's SAFETY-CRITICAL course matching + clean-watch.

Pins the lesson from the real incident (2026-06-30): a FILENAME-only match false-flagged
6 CURRENT routes as orphaned and deleted them. The fix is the internal-name manifest +
a clean-watch guard that REFUSES to delete any course not internal-name-verified. These
pure-function tests (no watch, no network) make that guard impossible to silently regress.

Run: ./.venv/bin/python test_content.py
"""
from content import build, state

# Connect-side courses (what the cloud holds). Names only matter for matching.
CONNECT = [
    {"kind": "course", "id": 1, "name": "10k Parker Piece Loop"},
    {"kind": "course", "id": 2, "name": "Cambridge - River Cam Pathway"},
    {"kind": "course", "id": 3, "name": "Sheep's Green Bridge Intervals"},
]

# Watch files. Garmin truncates/noises the FILENAME; the manifest carries the clean
# INTERNAL name read from the FIT. Cases below mix verified and unverified on purpose.
# #11 is the REAL incident: Garmin truncated the name to ~26 chars and appended .fit.fit,
# so the normalised filename key ('...campathfit') no longer prefix-matches the Connect
# name ('...campathway') -> the filename matcher false-flags this CURRENT route as orphaned.
WATCH = [
    (10, "10k_Parker_Piece_Loop.fit"),          # current route, manifest-verified -> SYNCED
    (11, "Cambridge_-_River_Cam_Path.fit.fit"),  # current route, truncated filename, NO manifest
    (12, "Old_5k_The_Rush.fit"),                 # genuine orphan, manifest-verified
    (13, "Mystery_Route_x9.fit"),                # genuine orphan, NO manifest entry
]
MANIFEST = {"course": {
    "10k_Parker_Piece_Loop.fit": "10k Parker Piece Loop",   # matches Connect id=1
    "Old_5k_The_Rush.fit": "5k The Rush (old)",             # internal name, NOT in Connect
}}


def deletable(rows):
    """Mirror cmd_clean_watch's course guard: only internal-name-verified watch-only rows
    are safe to delete; everything else is refused."""
    return [r for r in rows if state(r) == "watch-only" and r.get("manifest_verified")]


def run():
    rows = build(CONNECT, WATCH, "course", MANIFEST)
    by_idx = {r["watch_idx"]: r for r in rows if r.get("watch_idx") is not None}
    fail = []

    def expect(idx, want_state, want_verified):
        r = by_idx.get(idx)
        if r is None:
            fail.append(f"  #{idx}: missing from rows"); return
        if state(r) != want_state:
            fail.append(f"  #{idx} {r['watch_file']!r}: state {state(r)!r}, want {want_state!r}")
        if bool(r.get("manifest_verified")) != want_verified:
            fail.append(f"  #{idx} {r['watch_file']!r}: verified={r.get('manifest_verified')}, "
                        f"want {want_verified}")

    expect(10, "synced", True)        # current route, matched via internal name
    expect(11, "watch-only", False)   # THE BUG: current route looks orphaned on filename...
    expect(12, "watch-only", True)    # genuine orphan, internal-name-verified
    expect(13, "watch-only", False)   # genuine orphan but unverified -> must not auto-delete
    if by_idx[12]["name"] != "5k The Rush (old)":
        fail.append(f"  #12 display name {by_idx[12]['name']!r}, want internal FIT name")
    if by_idx[12]["name"] == by_idx[12]["watch_file"]:
        fail.append("  #12 display leaked the lossy watch filename")
    if by_idx[13]["name"] != "Mystery_Route_x9.fit":
        fail.append("  #13 unverified display should fall back to the exact watch filename")

    # the cardinal guard: the deletable set must contain ONLY verified orphans, and must
    # NEVER include #11 (a CURRENT route the lossy filename mislabelled).
    dset = {r["watch_idx"] for r in deletable(rows)}
    if 11 in dset:
        fail.append("  CARDINAL FAIL: current route #11 is in the deletable set (the 6-route bug)")
    if 13 in dset:
        fail.append("  FAIL: unverified file #13 is in the deletable set (filename trust)")
    if dset != {12}:
        fail.append(f"  deletable set is {sorted(dset)}, want just [12] (the verified orphan)")

    if fail:
        print("FAIL:"); print("\n".join(fail)); raise SystemExit(1)
    print("PASS — course matching + clean-watch guard:")
    print("  synced(10), watch-only-verified(12, deletable), "
          "watch-only-UNverified(11=current route, 13=unknown) both REFUSED.")
    print(f"  deletable = {sorted(dset)} (only the internal-name-verified orphan).")


if __name__ == "__main__":
    run()
