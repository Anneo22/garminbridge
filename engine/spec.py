#!/usr/bin/env python3
"""GarminBridge workout spec — a compact, human-writable description of a workout
that compiles to a Garmin workout and pushes it.

This is the input layer for the "describe a workout and GarminBridge builds it"
idea. The spec is a plain dict (or YAML), so a person can write it by hand AND an
AI can emit it from a natural-language request — the natural-language → spec step
is done by the in-session assistant, not a wired paid API.

Spec shape:
    {
      "name": "Full Body A",
      "sport": "strength",            # running | cycling | swimming | strength
      "animate": true,                 # default true (Garmin-tag so it animates)
      "steps": [
        {"warmup": 300, "hr": [120, 141]},
        {"repeat": 3, "do": [
          {"exercise": "SQUAT/AIR_SQUAT", "reps": 12, "rest": 60},
          {"exercise": "ROW/BARBELL_ROW", "reps": 12, "rest": 60, "weight_kg": 30},
        ]},
        {"repeat": 4, "do": [
          {"work": 180, "power": [220, 260]},
          {"recover": 120, "hr": [120, 140]},
        ]},
        {"cooldown": 180},
      ],
    }

Step keys: warmup/cooldown/rest (seconds); work/run (seconds or meters=…);
recover/recovery (seconds); exercise ("CAT/NAME" or {category,name} + reps|seconds
+ rest + weight_kg); repeat (+ do: [...]). Targets on any step: hr|pace|power|cadence
as [low, high].
"""
import workout_builder as wb
import catalog
import profile as P

_ABS = ("hr", "pace", "power", "cadence")
_active_prof = None  # set per from_spec() call


def _targets(d):
    """Build the target kwargs for a step. Three kinds, freely mixable per step:

      ADAPTIVE (re-scales when Garmin settings change — preferred):
        power_pct=[lo,hi]  -> cycling Custom Power as % FTP. Stored AS percent and
          scaled by the watch live (NOT converted to watts here), so the workout
          tracks your FTP automatically.
        power_zone=N / hr_zone=N / pace_zone=N -> reference zone N. The only
          adaptive option Garmin gives for RUNNING power (no % of run threshold).

      STATIC, threshold-relative (no adaptive equivalent exists in Garmin, so we
      bake the absolute value from the profile at build time):
        pace_pct=[lo,hi]      -> % of threshold speed   -> pace (m/s)
        run_power_pct=[lo,hi] -> % running threshold pwr -> power (W), precise alert
        hr_pct=[lo,hi]        -> % of threshold HR       -> hr (bpm)

      ABSOLUTE: hr/pace/power/cadence as [lo,hi].
    """
    t = {k: tuple(d[k]) for k in _ABS if k in d}
    # adaptive: pass straight through to the builder (no profile conversion)
    if "power_pct" in d:
        t["power_pct"] = tuple(d["power_pct"])
    for zk in ("power_zone", "hr_zone", "pace_zone"):
        if zk in d:
            t[zk] = int(d[zk])
    # secondary ALERT (runs alongside the primary target, e.g. a power ceiling
    # while pace is the main target): power_alert_zone=N | power_alert=[lo,hi] W
    if "power_alert_zone" in d:
        t["sec_power_zone"] = int(d["power_alert_zone"])
    if "power_alert" in d:
        t["sec_power"] = tuple(d["power_alert"])
    if "hr_alert_zone" in d:
        t["sec_hr_zone"] = int(d["hr_alert_zone"])
    # static threshold-relative: bake to absolute via the active profile
    if "pace_pct" in d:
        lo, hi = d["pace_pct"]
        t["pace"] = (P.pace_mps(lo, _active_prof), P.pace_mps(hi, _active_prof))
    if "run_power_pct" in d:
        lo, hi = d["run_power_pct"]
        t["power"] = (P.run_power_w(lo, _active_prof), P.run_power_w(hi, _active_prof))
    if "hr_pct" in d:
        lo, hi = d["hr_pct"]
        thr = (_active_prof or P.DEFAULTS)["threshold_hr"]
        t["hr"] = (round(thr * lo / 100), round(thr * hi / 100))
    return t


def _resolve_exercise(val):
    if isinstance(val, str):
        cat, _, name = val.partition("/")
        return cat, (name or None)
    return val["category"], val.get("name")


def _leaf(d, cats):
    if "warmup" in d:
        return [wb.warmup(d["warmup"], **_targets(d))]
    if "cooldown" in d:
        return [wb.cooldown(d["cooldown"], **_targets(d))]
    # An exercise/work/recover step may ALSO carry a "rest" key (rest-after), so
    # the bare standalone "rest" step MUST be checked LAST — otherwise an
    # exercise-with-rest is silently replaced by just a rest (real bug, 2026-06-27).
    if "exercise" in d:
        cat, name = _resolve_exercise(d["exercise"])
        return wb.exercise(cat, name, reps=d.get("reps"), seconds=d.get("seconds"),
                           weight_kg=d.get("weight_kg"), rest_after=d.get("rest"),
                           _cats=cats)
    if "work" in d or "run" in d:
        return [wb.step("interval", seconds=d.get("work") or d.get("run"),
                        meters=d.get("meters"), **_targets(d))]
    if "recover" in d or "recovery" in d:
        return [wb.step("recovery", seconds=d.get("recover") or d.get("recovery"),
                        **_targets(d))]
    if "rest" in d:
        return [wb.rest(d["rest"])]
    raise ValueError(f"unrecognised step: {d}")


def from_spec(spec, cats=None, prof=None):
    """Compile a spec dict into a Garmin workout payload.

    prof = athlete profile dict (profile.fetch / DEFAULTS); used to convert any
    threshold-relative targets (pace_pct/power_pct/...) to absolute values.
    """
    global _active_prof
    _active_prof = prof or spec.get("profile") or P.DEFAULTS
    cats = cats or catalog.load()
    steps = []
    for d in spec["steps"]:
        if "repeat" in d:
            inner = []
            for sub in d["do"]:
                inner.extend(_leaf(sub, cats))
            steps.append(wb.repeat(d["repeat"], inner))
        else:
            steps.extend(_leaf(d, cats))
    return wb.build(spec["name"], spec.get("sport", "strength"), steps,
                    animate=spec.get("animate", True))


def push(garmin, spec, cats=None, prof=None):
    """Compile + upload. Returns the new workoutId."""
    return wb.upload(garmin, from_spec(spec, cats, prof))


def load_yaml(path):
    import yaml  # optional dependency
    with open(path) as f:
        return yaml.safe_load(f)
