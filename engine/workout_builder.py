#!/usr/bin/env python3
"""GarminBridge workout builder — build structured & strength workouts on the Mac
and push them to the watch via Garmin Connect (the proven write path).

The strength path is the differentiator: every exercise is validated against
Garmin's live catalog and carries a real (category, exerciseName) pair, which is
what makes the move ANIMATE on the watch — the thing custom strength workouts
normally lack (GarminBridge roadmap #8).

All enum IDs below were read off the user's own real workouts, not guessed:
  sport: running=1, cycling=2, strength_training=5
  step:  warmup=1 cooldown=2 interval=3 recovery=4 rest=5 repeat=6 work(other)=7
  end:   lap.button=1 time=2 distance=3 calories=4 reps=10
  target: no.target=1 power=2 cadence=3 heart.rate.zone=4 speed=5 pace.zone=6
"""
from __future__ import annotations

import catalog

SPORT = {"running": 1, "cycling": 2, "swimming": 4,
         "strength": 5, "strength_training": 5}
STEP = {"warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4, "rest": 5,
        "repeat": 6, "work": 7, "other": 7}
END = {"lap.button": 1, "time": 2, "distance": 3, "calories": 4, "reps": 10}
TARGET = {"no.target": 1, "power.zone": 2, "cadence.zone": 3,
          "heart.rate.zone": 4, "speed.zone": 5, "pace.zone": 6}


def _step_type(key):
    return {"stepTypeId": STEP[key], "stepTypeKey": key}


def _end(key):
    return {"conditionTypeId": END[key], "conditionTypeKey": key}


def _target(key):
    return {"workoutTargetTypeId": TARGET[key], "workoutTargetTypeKey": key}


# ---- leaf steps ---------------------------------------------------------------

def step(kind, *, seconds=None, meters=None, reps=None, lap=False,
         hr=None, pace=None, power=None, cadence=None,
         power_pct=None, power_zone=None, hr_zone=None, pace_zone=None,
         sec_power_zone=None, sec_power=None, sec_hr_zone=None,
         description=None, category=None, exercise=None, weight_kg=None):
    """One executable step. Give exactly one end condition (seconds/meters/reps/lap).

    One target may be set. Two flavours:
      ABSOLUTE (baked, static): hr=(bpm,bpm) | pace=(m/s,m/s) | power=(W,W) |
        cadence=(rpm,rpm).
      ADAPTIVE (re-scales when the athlete's thresholds/zones change in Garmin):
        power_pct=(lo,hi)  -> cycling Custom Power as % of FTP (the watch scales
          live). Canonical encoding read off a UI-made workout 2026-06-27:
          targetType=power.zone + literal percents + percent unit (id 253). In the
          FIT the watch reads, values <1000 are interpreted as %FTP (verified:
          106/110 stored raw; absolute watts are stored as watts+1000 instead).
        power_zone=N / hr_zone=N / pace_zone=N -> reference zone N, which moves
          with the athlete's zone definitions (this is the ONLY adaptive option
          Garmin exposes for running power — there is no % of running threshold).
    category/exercise build a strength move (validated against the catalog).
    """
    s = {"type": "ExecutableStepDTO", "stepType": _step_type(kind)}

    if lap:
        s["endCondition"] = _end("lap.button"); s["endConditionValue"] = None
    elif seconds is not None:
        s["endCondition"] = _end("time"); s["endConditionValue"] = float(seconds)
    elif meters is not None:
        s["endCondition"] = _end("distance"); s["endConditionValue"] = float(meters)
    elif reps is not None:
        s["endCondition"] = _end("reps"); s["endConditionValue"] = float(reps)
    else:
        s["endCondition"] = _end("lap.button"); s["endConditionValue"] = None

    # target precedence: an adaptive target (percent / zone) wins over absolute.
    if power_pct is not None:
        lo, hi = power_pct
        s["targetType"] = _target("power.zone")
        s["targetValueOne"], s["targetValueTwo"] = float(lo), float(hi)
        s["targetValueUnit"] = {"unitId": 253, "unitKey": "percent", "factor": 1.0}
    elif power_zone is not None:
        s["targetType"] = _target("power.zone"); s["zoneNumber"] = int(power_zone)
    elif hr_zone is not None:
        s["targetType"] = _target("heart.rate.zone"); s["zoneNumber"] = int(hr_zone)
    elif pace_zone is not None:
        s["targetType"] = _target("pace.zone"); s["zoneNumber"] = int(pace_zone)
    else:
        _tg = {"heart.rate.zone": hr, "pace.zone": pace,
               "power.zone": power, "cadence.zone": cadence}
        chosen = next(((k, v) for k, v in _tg.items() if v), None)
        if chosen:
            key, (lo, hi) = chosen
            s["targetType"] = _target(key)
            s["targetValueOne"], s["targetValueTwo"] = float(lo), float(hi)
        else:
            s["targetType"] = _target("no.target")

    # Secondary target = a parallel ALERT alongside the primary. CAVEAT (2026-06-29):
    # the field LANDS in the workout FIT (secondary_target_power_zone confirmed by
    # readback), but that only proves the file is correct — NOT that the watch acts
    # on it. The user believes secondary targets are a CYCLING (+maybe a few sports)
    # feature and are ignored for RUNNING; treat running secondary targets as
    # unconfirmed-on-device until watched on the Fenix. Kept because it's harmless
    # and works for cycling. A zone adapts; absolute watts use the watts+1000 rule.
    if sec_power_zone is not None:
        s["secondaryTargetType"] = _target("power.zone")
        s["secondaryZoneNumber"] = int(sec_power_zone)
    elif sec_power is not None:
        lo, hi = sec_power
        s["secondaryTargetType"] = _target("power.zone")
        s["secondaryTargetValueOne"], s["secondaryTargetValueTwo"] = float(lo), float(hi)
    elif sec_hr_zone is not None:
        s["secondaryTargetType"] = _target("heart.rate.zone")
        s["secondaryZoneNumber"] = int(sec_hr_zone)

    if description:
        s["description"] = description
    if category:
        s["category"] = category.upper()
        s["exerciseName"] = exercise.upper() if exercise else None
    if weight_kg:
        # weight lands ONLY as kg + a kilogram unit object (grams gets dropped);
        # verified via FIT readback 2026-06-27 -> exercise_weight=kg, unit=kilogram.
        s["weightValue"] = float(weight_kg)
        s["weightUnit"] = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}
    return s


def warmup(seconds, **kw):
    return step("warmup", seconds=seconds, **kw)


def cooldown(seconds, **kw):
    return step("cooldown", seconds=seconds, **kw)


def rest(seconds):
    return step("rest", seconds=seconds)


def exercise(category, name=None, *, reps=None, seconds=None, weight_kg=None,
             rest_after=None, _cats=None):
    """A strength move. Validates (category, name) against the catalog. Optionally
    append a rest step via rest_after=<seconds> -> returns a list."""
    catalog.validate(category, name, _cats)
    s = step("work", reps=reps, seconds=seconds, category=category,
             exercise=name, weight_kg=weight_kg)
    return [s, rest(rest_after)] if rest_after else [s]


def repeat(times, steps):
    return {"type": "RepeatGroupDTO", "stepType": _step_type("repeat"),
            "numberOfIterations": int(times), "smartRepeat": False,
            "workoutSteps": list(steps)}


# ---- assembly -----------------------------------------------------------------

def _order(steps, counter, child_id=None):
    """Assign sequential stepOrder across the whole (possibly nested) tree."""
    for s in steps:
        counter[0] += 1
        s["stepOrder"] = counter[0]
        if child_id is not None:
            s["childStepId"] = child_id
        if s.get("type") == "RepeatGroupDTO":
            cid = counter[0]
            s["childStepId"] = cid
            _order(s["workoutSteps"], counter, child_id=cid)
    return steps


def _source_id(name):
    import hashlib
    return "GG" + hashlib.sha1(name.encode()).hexdigest()[:5]


def build(name, sport, steps, *, animate=True):
    """Wrap ordered steps into a Garmin workout payload.

    animate=True stamps the workout as Garmin-authored (workoutProvider="Garmin"
    + a GG-style workoutSourceId). The watch ONLY plays exercise animations for
    Garmin-authored workouts; this spoof unlocks animation for our custom builds.
    VERIFIED on a Fenix 8 (2026-06-27): a from-scratch strength workout carrying
    this tag animates. Set animate=False for a plain user workout.
    """
    flat = []
    for s in steps:
        flat.extend(s if isinstance(s, list) else [s])
    _order(flat, [0])
    sid = SPORT[sport]
    st = {"sportTypeId": sid, "sportTypeKey":
          "strength_training" if sid == 5 else sport}
    w = {
        "sportType": st,
        "workoutName": name,
        "workoutSegments": [
            {"segmentOrder": 1, "sportType": st, "workoutSteps": flat}
        ],
    }
    if animate:
        w["workoutProvider"] = "Garmin"
        w["workoutSourceId"] = _source_id(name)
    return w


def upload(garmin, workout):
    resp = garmin.upload_workout(workout)
    return resp.get("workoutId") if isinstance(resp, dict) else resp
