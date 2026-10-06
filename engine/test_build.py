#!/usr/bin/env python3
"""Build + push a strength workout (with animating exercise IDs) and an interval run,
then read the strength one back to prove exerciseName is populated."""
import json
import os

from garminconnect import Garmin
import catalog
import workout_builder as wb
import paths

HERE = os.path.dirname(os.path.abspath(__file__))


def pick(cats, category, *preferred):
    """Return a real exerciseName in `category`: first preferred that exists, else first."""
    avail = cats.get(category.upper(), {})
    for p in preferred:
        if p.upper() in avail:
            return p.upper()
    return next(iter(avail))


def main():
    cats = catalog.load()
    g = Garmin(); g.login(paths.state(".tokens"))

    sq = pick(cats, "SQUAT", "AIR_SQUAT", "BODYWEIGHT_SQUAT")
    pu = pick(cats, "PUSH_UP", "PUSH_UP", "BICEPS_PUSH_UP")
    pl = pick(cats, "PLANK", "PLANK", "FRONT_PLANK", "BEAR_CRAWL")
    print(f"chosen exercises: SQUAT/{sq}, PUSH_UP/{pu}, PLANK/{pl}\n")

    strength = wb.build("GarminBridge — strength (animated)", "strength", [
        wb.warmup(300),
        wb.repeat(3, [
            *wb.exercise("SQUAT", sq, reps=12, rest_after=30, _cats=cats),
            *wb.exercise("PUSH_UP", pu, reps=10, rest_after=30, _cats=cats),
            *wb.exercise("PLANK", pl, seconds=45, rest_after=30, _cats=cats),
        ]),
        wb.cooldown(180),
    ])
    sid = wb.upload(g, strength)
    print(f"strength workout created: id={sid}")

    run = wb.build("GarminBridge — interval run", "running", [
        wb.warmup(600, hr=(120, 141)),
        wb.repeat(4, [
            wb.step("interval", seconds=180, hr=(160, 175)),
            wb.step("recovery", seconds=120, hr=(120, 140)),
        ]),
        wb.cooldown(300),
    ])
    rid = wb.upload(g, run)
    print(f"interval run created:    id={rid}\n")

    # read the strength workout back -> prove exerciseName is populated (animation)
    back = g.connectapi(f"/workout-service/workout/{sid}")
    # Diagnostic output is state, not source: keep it out of the shipped tree and
    # create the directory, which a fresh checkout will not have.
    probe_out = os.path.join(paths.state_dir(), "probe-out")
    os.makedirs(probe_out, exist_ok=True)
    json.dump(back, open(os.path.join(probe_out, "built-strength.json"), "w"), indent=2)
    print("=== strength workout as stored by Garmin (proof of animation IDs) ===")

    def show(steps, d=0):
        for s in steps:
            if s.get("type") == "RepeatGroupDTO":
                print("  " * d + f"REPEAT x{s.get('numberOfIterations')}")
                show(s.get("workoutSteps", []), d + 1)
            else:
                k = (s.get("stepType") or {}).get("stepTypeKey")
                c, e = s.get("category"), s.get("exerciseName")
                ec = (s.get("endCondition") or {}).get("conditionTypeKey")
                ev = s.get("endConditionValue")
                tag = f"  {c}/{e}" if c else ""
                print("  " * d + f"[{k}] {ec}={ev}{tag}")
    show(back["workoutSegments"][0]["workoutSteps"])
    print(f"\nIDs: strength={sid}  run={rid}  (sync the watch to see them)")


if __name__ == "__main__":
    main()
