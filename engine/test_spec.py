#!/usr/bin/env python3
"""Offline regression guard for the spec compiler (no network/upload).

Guards the 2026-06-27 bug where an exercise step carrying a "rest" key was
silently replaced by a bare rest (exercises vanished, workout = all rest)."""
import catalog
import spec as S


def test_exercise_with_rest_survives():
    cats = catalog.load()  # uses local cache
    w = S.from_spec({"name": "t", "sport": "strength", "steps": [
        {"repeat": 2, "do": [
            {"exercise": "SQUAT/AIR_SQUAT", "reps": 10, "rest": 30},
            {"exercise": "PUSH_UP/PUSH_UP", "reps": 8},
        ]},
    ]}, cats)
    names = [s.get("exerciseName")
             for seg in w["workoutSegments"]
             for grp in seg["workoutSteps"]
             for s in (grp["workoutSteps"] if grp.get("type") == "RepeatGroupDTO" else [grp])
             if s.get("exerciseName")]
    assert names == ["AIR_SQUAT", "PUSH_UP"], f"exercises dropped/changed: {names}"
    print("OK: exercise-with-rest survives compilation ->", names)


if __name__ == "__main__":
    test_exercise_with_rest_survives()
