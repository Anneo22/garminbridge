#!/usr/bin/env python3
"""Offline regression for the workout ENGINE (validate + build + schema). No network,
no upload. Run: ./.venv/bin/python test_workout_engine.py"""
import json

import catalog
import workout as W
import workout_spec as WS

CATS = catalog.load()  # local cache; the only "IO", already on disk

GOOD_STRENGTH = {
    "name": "t-strength", "sport": "strength", "animate": True,
    "steps": [
        {"warmup": 300, "hr": [110, 140]},
        {"repeat": 3, "do": [
            {"exercise": "SQUAT/AIR_SQUAT", "reps": 12, "rest": 45},
            {"exercise": "PLANK/PLANK", "seconds": 40},
        ]},
        {"cooldown": 180},
    ],
}
GOOD_RUN = {
    "name": "t-run", "sport": "running", "animate": False,
    "steps": [
        {"warmup": 600, "pace_pct": [61, 72]},
        {"repeat": 4, "do": [{"work": 480, "pace_pct": [98, 102]},
                             {"recover": 120}]},
        {"cooldown": 600},
    ],
}


def test_good_specs_pass():
    for spec in (GOOD_STRENGTH, GOOD_RUN):
        res = WS.validate_spec(spec, CATS)
        assert not res["errors"], f"{spec['name']} unexpectedly errored: {res['errors']}"
    print("OK: good specs validate clean")


def test_bad_spec_errors_are_caught():
    bad = {"name": "", "sport": "jogging", "steps": [
        {"warmpu": 300},                                   # typo → unrecognised
        {"exercise": "SQUAT/NOT_A_MOVE", "reps": 12},      # unknown exercise
        {"work": 300, "power": [300, 200]},                # reversed pair
        {"repeat": 0, "do": []},                           # bad repeat + empty do
    ]}
    res = WS.validate_spec(bad, CATS)
    joined = " | ".join(res["errors"])
    for needle in ("name:", "sport:", "unrecognised", "NOT_A_MOVE",
                   "low > high", "repeat must be", "non-empty 'do'"):
        assert needle in joined, f"expected error containing {needle!r}; got {res['errors']}"
    print(f"OK: bad spec produced {len(res['errors'])} distinct errors")


def test_running_power_pct_warns():
    spec = {"name": "t", "sport": "running",
            "steps": [{"work": 60, "power_pct": [90, 95]}]}
    res = WS.validate_spec(spec, CATS)
    assert not res["errors"]
    assert any("running has no" in w for w in res["warnings"]), res["warnings"]
    print("OK: running power_pct raises a guidance warning, not an error")


def test_build_payload_is_well_formed():
    payload = W.build(GOOD_STRENGTH)
    assert payload["workoutProvider"] == "Garmin", "animation tag missing"
    assert payload["sportType"]["sportTypeId"] == 5
    steps = payload["workoutSegments"][0]["workoutSteps"]

    orders, names = [], []
    def walk(ss):
        for s in ss:
            orders.append(s["stepOrder"])
            if s.get("type") == "RepeatGroupDTO":
                walk(s["workoutSteps"])
            elif s.get("exerciseName"):
                names.append(s["exerciseName"])
    walk(steps)
    assert orders == sorted(orders) and orders[0] == 1, f"stepOrder not sequential: {orders}"
    assert names == ["AIR_SQUAT", "PLANK"], f"exercise names wrong: {names}"
    print("OK: built payload well-formed (animation tag, sequential order, real moves)")


def test_summary_runs():
    text = W.summarize(GOOD_RUN)
    assert "t-run" in text and "4x" in text, text
    print("OK: summary renders")


def test_schema_is_valid_json():
    sch = WS.json_schema()
    json.dumps(sch)  # serialisable
    assert sch["required"] == ["name", "steps"]
    assert "step" in sch["$defs"]
    print("OK: JSON schema is well-formed")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} ENGINE TESTS PASSED")
