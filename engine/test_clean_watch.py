#!/usr/bin/env python3
"""Regression guard for the SAFETY-CRITICAL classification in clean_watch.py.

The dangerous direction is a false STRANDED (would delete a live workout). These
tests pin the conservative behaviour so it can never silently regress. Pure-function
tests — no watch, no network. Run: ./.venv/bin/python test_clean_watch.py
"""
from clean_watch import _norm, _stem, classify

# what Connect currently holds = the KEEP set (name only matters here)
LIVE = [
    (_norm("Mona Fartlek (Lactate Shuttle)"), "Mona Fartlek (Lactate Shuttle)"),
    (_norm("Full Body Strength A"), "Full Body Strength A"),
    (_norm("VO2max 6x140s"), "VO2max 6x140s"),
    (_norm("Cycling Over-Unders"), "Cycling Over-Unders"),
]

# (idx, filename, expected: True=stranded / False=live)
CASES = [
    (1, "Mona_Fartlek_(Lactate_Shuttle)_workout.fit", False),   # exact match
    (2, "Full_Body_Strength_A_workout.fit", False),             # exact match
    (3, "VO2max_6x140s_workout.fit", False),                    # exact match
    (4, "Cycling_Over-Unders_workout.fit", False),              # punctuation differs, norm matches
    (5, "Active_Recovery_Tempo_Cadence_workout.fit", True),     # HumanGO leftover -> stranded
    (6, "GarminBridge_—_provider_spoof_workout.fit", True),     # my throwaway -> stranded
    (7, "Easy_Run_30min_workout.fit", True),                    # not in Connect -> stranded
    (8, "VO2max_6x140s_v2_workout.fit", False),                 # truncation/suffix: prefix-contains a live key -> KEEP (biased safe)
]


def run():
    files = [(idx, fn) for idx, fn, _ in CASES]
    live, stranded = classify(files, LIVE, protect=[])
    stranded_idx = {idx for idx, _ in stranded}
    live_idx = {idx for idx, *_ in live}

    failures = []
    for idx, fn, want_stranded in CASES:
        got_stranded = idx in stranded_idx
        if got_stranded != want_stranded:
            failures.append(
                f"  #{idx} {fn!r}: expected {'STRANDED' if want_stranded else 'LIVE'}, "
                f"got {'STRANDED' if got_stranded else 'LIVE'}")

    # the cardinal safety invariant: no file matching a live Connect name is ever stranded
    for idx, fn, _ in CASES:
        if idx in stranded_idx and idx in live_idx:
            failures.append(f"  #{idx} {fn!r}: classified BOTH live and stranded")

    # --protect keeps an otherwise-stranded file
    live2, stranded2 = classify([(9, "Morning_Pilates_workout.fit")], LIVE,
                                protect=[_norm("pilates")])
    if stranded2:
        failures.append("  --protect failed: 'Morning_Pilates' was flagged stranded")

    if failures:
        print("FAIL:")
        print("\n".join(failures))
        raise SystemExit(1)
    print(f"PASS — {len(CASES)} classification cases + protect + safety invariant.")
    print(f"  stranded: {sorted(stranded_idx)}  live: {sorted(live_idx)}")


if __name__ == "__main__":
    run()
