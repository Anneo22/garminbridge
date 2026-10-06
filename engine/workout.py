#!/usr/bin/env python3
"""GarminBridge workout ENGINE — the single entrypoint.

One clean surface for: validate a spec → build the workout payload → push it to the
watch via Garmin Connect. BOTH an external authoring client AND the future in-app
authoring feature call THIS (as a CLI, or by importing its functions). There is no
client-only path: a client writes a spec JSON and shells these commands.

Pipeline:  spec (dict)  --validate-->  --compile (spec.py)-->  Garmin payload  --push-->  watch

CLI:
  workout.py validate  [--spec-file F | --stdin]     -> JSON {ok, errors[], warnings[]}
  workout.py summary   [--spec-file F | --stdin]     -> human-readable preview (no network)
  workout.py build     [--spec-file F | --stdin]     -> JSON {ok, name, summary, workout}  (offline)
  workout.py push      [--spec-file F | --stdin]     -> JSON {ok, workoutId, name, summary}
  workout.py catalog   --search TERM [--limit N]     -> JSON [[CATEGORY, NAME], ...]
  workout.py schema    [--save]                       -> the JSON Schema (contract)

Spec input: --spec-file PATH (.json, or .yaml/.yml if PyYAML is present) or --stdin (JSON).
Auth for push: cached tokens in ./.tokens (run login.py once). Nothing is deleted here.

Importable API (what api.py / the app can call directly instead of shelling out):
  load_spec(text_or_path) -> dict
  validate(spec) -> {"errors":[...], "warnings":[...]}
  summarize(spec) -> str
  build(spec, prof=None) -> Garmin payload dict          (offline)
  push(spec) -> {"workoutId":..., "name":..., "summary":...}
"""
from __future__ import annotations

import argparse
import json
import os
import paths
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOKENSTORE = paths.state(".tokens")
SCHEMA_FILE = paths.asset("workout_spec.schema.json")

import catalog
import profile as P
import spec as S
import workout_spec as WS

# threshold-relative keys need a profile baked in at build time
_THRESHOLD_KEYS = {"pace_pct", "run_power_pct", "hr_pct"}


# ---- spec I/O -----------------------------------------------------------------

def load_spec(source: str, is_path: bool = True) -> dict:
    """Load a spec from a file path or a raw JSON/YAML string."""
    if is_path:
        if source.endswith((".yaml", ".yml")):
            return S.load_yaml(source)
        with open(source) as f:
            text = f.read()
    else:
        text = source
    return json.loads(text)


def _read_spec(args) -> dict:
    if getattr(args, "stdin", False):
        return json.loads(sys.stdin.read())
    if getattr(args, "spec_file", None):
        return load_spec(args.spec_file, is_path=True)
    raise SystemExit("error: give --spec-file PATH or --stdin")


def _uses_thresholds(spec) -> bool:
    return any(_THRESHOLD_KEYS & set(st)
               for st in WS._iter_leaf_steps(spec.get("steps")))


# ---- core (importable) --------------------------------------------------------

def validate(spec, cats=None):
    cats = cats if cats is not None else _safe_catalog()
    return WS.validate_spec(spec, cats)


def _safe_catalog():
    try:
        return catalog.load()
    except Exception:
        return None


def build(spec, prof=None):
    """Compile a spec to a Garmin payload (offline). prof defaults to DEFAULTS."""
    return S.from_spec(spec, cats=_safe_catalog(), prof=prof or P.DEFAULTS)


def _clean_num(v):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    return int(v) if float(v).is_integer() else float(v)


def _clean_int(v):
    if isinstance(v, bool) or v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _enum_key(obj, field):
    enum = obj.get(field) or {}
    if not isinstance(enum, dict):
        return ""
    return (enum.get(field + "Key") or enum.get("stepTypeKey")
            or enum.get("conditionTypeKey") or enum.get("workoutTargetTypeKey"))


def _weight_kg(value, unit):
    v = _clean_num(value)
    if v is None:
        return None
    key = ((unit or {}).get("unitKey") or "").lower()
    if key in ("gram", "grams"):
        return _clean_num(v / 1000)
    if key in ("pound", "pounds", "lb", "lbs"):
        return _clean_num(v * 0.45359237)
    return v


def _target_spec(step):
    target = {}
    key = _enum_key(step, "targetType")
    zone = _clean_int(step.get("zoneNumber"))
    lo = _clean_num(step.get("targetValueOne"))
    hi = _clean_num(step.get("targetValueTwo"))
    unit_key = ((step.get("targetValueUnit") or {}).get("unitKey") or "").lower()

    if key == "power.zone":
        if lo is not None and hi is not None and unit_key == "percent":
            target["power_pct"] = [lo, hi]
        elif zone:
            target["power_zone"] = zone
        elif lo is not None and hi is not None:
            target["power"] = [lo, hi]
    elif key == "heart.rate.zone":
        if zone:
            target["hr_zone"] = zone
        elif lo is not None and hi is not None:
            target["hr"] = [lo, hi]
    elif key == "pace.zone":
        if zone:
            target["pace_zone"] = zone
        elif lo is not None and hi is not None:
            target["pace"] = [lo, hi]
    elif key == "cadence.zone" and lo is not None and hi is not None:
        target["cadence"] = [lo, hi]

    sec_key = _enum_key(step, "secondaryTargetType")
    sec_zone = _clean_int(step.get("secondaryZoneNumber"))
    sec_lo = _clean_num(step.get("secondaryTargetValueOne"))
    sec_hi = _clean_num(step.get("secondaryTargetValueTwo"))
    if sec_key == "power.zone":
        if sec_zone:
            target["power_alert_zone"] = sec_zone
        elif sec_lo is not None and sec_hi is not None:
            target["power_alert"] = [sec_lo, sec_hi]
    elif sec_key == "heart.rate.zone" and sec_zone:
        target["hr_alert_zone"] = sec_zone
    return target


def _duration_spec(out, kind, step):
    cond = _enum_key(step, "endCondition")
    val = _clean_num(step.get("endConditionValue"))
    if cond == "time" and val is not None:
        out["seconds" if kind == "exercise" else kind] = val
    elif cond == "distance" and val is not None:
        out["meters"] = val
    elif cond == "reps" and val is not None:
        out["reps"] = val
    elif cond == "lap.button" and kind in ("warmup", "cooldown", "recover", "rest"):
        out[kind] = None


def _step_spec(step):
    raw_kind = _enum_key(step, "stepType") or ""
    if step.get("category") or step.get("exerciseName"):
        kind = "exercise"
    else:
        kind = {
            "warmup": "warmup",
            "cooldown": "cooldown",
            "interval": "work",
            "recovery": "recover",
            "rest": "rest",
            "work": "work",
            "other": "work",
        }.get(raw_kind, "work")

    out = {}
    if kind == "exercise":
        cat = step.get("category")
        name = step.get("exerciseName")
        out["exercise"] = f"{cat}/{name}" if cat and name else (name or cat or "Exercise")
        w = _weight_kg(step.get("weightValue"), step.get("weightUnit"))
        if w is not None:
            out["weight_kg"] = w

    _duration_spec(out, kind, step)
    out.update(_target_spec(step))
    return out or {"work": None}


def dto_to_spec(detail):
    """Convert a Garmin Connect workout detail DTO back to the app's workout spec."""
    detail = detail if isinstance(detail, dict) else {}
    segs = detail.get("workoutSegments")
    if not isinstance(segs, list) or not segs:
        steps = []
    else:
        steps = _dto_steps_to_spec((segs[0] or {}).get("workoutSteps") or [])
    sport = ((detail.get("sportType") or {}).get("sportTypeKey")
             or (((segs[0] or {}).get("sportType") or {}).get("sportTypeKey") if segs else None)
             or "")
    return {"name": detail.get("workoutName") or "", "sport": sport, "steps": steps}


def _dto_steps_to_spec(steps):
    out = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        if step.get("type") == "RepeatGroupDTO":
            reps = step.get("numberOfIterations")
            nested = _dto_steps_to_spec(step.get("workoutSteps") or [])
            if nested:
                try:
                    repeat = int(reps or 1)
                except (TypeError, ValueError):
                    repeat = 1
                out.append({"repeat": repeat, "do": nested})
        else:
            out.append(_step_spec(step))
    return out


def _login():
    from garminconnect import Garmin
    g = Garmin()
    g.login(TOKENSTORE)
    return g


def push(spec, g=None, schedule_date=None):
    """Validate → (fetch live profile only if needed) → compile → upload → optionally schedule.
    Returns {"workoutId", "name", "summary", "scheduled"}. Raises on validation errors.
    g = an existing garminconnect session; if None, logs in via the cached tokens.
    schedule_date = "YYYY-MM-DD" to also SCHEDULE the workout on the Garmin calendar. A
      scheduled workout syncs to the watch automatically on the next sync — no manual
      "Send to device" tap. Omit to leave it in the Connect library only.
    (The app injects its own content.login() session so it doesn't authenticate twice.)"""
    res = validate(spec)
    if res["errors"]:
        raise ValueError("spec has errors:\n  - " + "\n  - ".join(res["errors"]))
    g = g or _login()
    prof = P.fetch(g) if _uses_thresholds(spec) else P.DEFAULTS
    wid = S.push(g, spec, cats=_safe_catalog(), prof=prof)
    scheduled = None
    if schedule_date:
        g.schedule_workout(wid, schedule_date)
        scheduled = schedule_date
    return {"workoutId": wid, "name": spec.get("name"),
            "summary": summarize(spec), "scheduled": scheduled}


# ---- human-readable preview ---------------------------------------------------
# Written for a person, not a machine: durations as minutes / m:ss, paces as min/km.

def _fmt_dur(s):
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    return f"{m} min" if sec == 0 else f"{m}:{sec:02d}"


def _pace_rng(lo, hi):
    """lo/hi are m/s; faster pace = higher m/s = lower min/km. Show fast–slow in min/km."""
    fast = P.pace_str(hi).replace("/km", "")
    return f"{fast}–{P.pace_str(lo)}"


def _fmt_targets(step):
    bits = []
    if "pace" in step:
        bits.append(f"pace {_pace_rng(*step['pace'])}")
    if "power" in step:
        lo, hi = step["power"]; bits.append(f"{int(lo)}–{int(hi)} W")
    if "hr" in step:
        lo, hi = step["hr"]; bits.append(f"{int(lo)}–{int(hi)} bpm")
    if "cadence" in step:
        lo, hi = step["cadence"]; bits.append(f"{int(lo)}–{int(hi)} spm")
    if "power_pct" in step:
        lo, hi = step["power_pct"]; bits.append(f"{lo}–{hi}% FTP")
    if "pace_pct" in step:
        lo, hi = step["pace_pct"]; bits.append(f"{lo}–{hi}% threshold pace")
    if "run_power_pct" in step:
        lo, hi = step["run_power_pct"]; bits.append(f"{lo}–{hi}% run FTP")
    if "hr_pct" in step:
        lo, hi = step["hr_pct"]; bits.append(f"{lo}–{hi}% threshold HR")
    if "power_alert" in step:
        bits.append(f"power cap {int(step['power_alert'][1])} W")
    for k, lbl in (("power_zone", "power zone"), ("hr_zone", "HR zone"),
                   ("pace_zone", "pace zone"), ("power_alert_zone", "power-alert zone"),
                   ("hr_alert_zone", "HR-alert zone")):
        if k in step:
            bits.append(f"{lbl} {step[k]}")
    return ("  " + ", ".join(bits)) if bits else ""


def _fmt_leaf(step, indent="  "):
    for kind, label in (("warmup", "warm-up"), ("cooldown", "cool-down"),
                        ("recover", "recover"), ("recovery", "recover")):
        if kind in step:
            return f"{indent}{label} {_fmt_dur(step[kind])}{_fmt_targets(step)}"
    if "rest" in step and "exercise" not in step:
        return f"{indent}rest {_fmt_dur(step['rest'])}"
    if "exercise" in step:
        ex = step["exercise"]
        label = ex if isinstance(ex, str) else f"{ex.get('category')}/{ex.get('name')}"
        end = (f"{step['reps']} reps" if step.get("reps")
               else _fmt_dur(step["seconds"]) if step.get("seconds") else "to lap press")
        w = f" @ {step['weight_kg']} kg" if step.get("weight_kg") else ""
        r = f" (rest {_fmt_dur(step['rest'])})" if step.get("rest") else ""
        return f"{indent}{label}  {end}{w}{r}"
    if "work" in step or "run" in step or "meters" in step:
        dur = step.get("work") or step.get("run")
        end = _fmt_dur(dur) if dur else (f"{int(step['meters'])} m" if step.get("meters") else "to lap press")
        return f"{indent}run {end}{_fmt_targets(step)}"
    return f"{indent}{json.dumps(step)}"


def summarize(spec) -> str:
    sport = spec.get("sport", "strength")
    animate = spec.get("animate", True)
    lines = [f"{spec.get('name','(unnamed)')}  [{sport}, animate={'on' if animate else 'off'}]"]
    for st in spec.get("steps", []):
        if isinstance(st, dict) and "repeat" in st:
            lines.append(f"  {st['repeat']}x:")
            for sub in st.get("do", []):
                lines.append(_fmt_leaf(sub, indent="      "))
        else:
            lines.append(_fmt_leaf(st))
    return "\n".join(lines)


# ---- CLI commands -------------------------------------------------------------

def cmd_validate(args):
    spec = _read_spec(args)
    res = validate(spec)
    out = {"ok": not res["errors"], **res}
    print(json.dumps(out, indent=2))
    return 0 if out["ok"] else 1


def cmd_summary(args):
    print(summarize(_read_spec(args)))
    return 0


def cmd_build(args):
    spec = _read_spec(args)
    res = validate(spec)
    if res["errors"]:
        print(json.dumps({"ok": False, **res}, indent=2))
        return 1
    payload = build(spec)
    print(json.dumps({"ok": True, "name": spec.get("name"),
                      "warnings": res["warnings"], "summary": summarize(spec),
                      "workout": payload}, indent=2))
    return 0


def cmd_push(args):
    spec = _read_spec(args)
    res = validate(spec)
    if res["errors"]:
        print(json.dumps({"ok": False, **res}, indent=2))
        return 1
    result = push(spec, schedule_date=getattr(args, "schedule", None) or None)
    print(json.dumps({"ok": True, "warnings": res["warnings"], **result}, indent=2))
    return 0


def cmd_catalog(args):
    hits = catalog.search(args.search, limit=args.limit)
    print(json.dumps(hits, indent=2))
    return 0


def cmd_schema(args):
    sch = WS.json_schema()
    text = json.dumps(sch, indent=2)
    if args.save:
        with open(SCHEMA_FILE, "w") as f:
            f.write(text + "\n")
        print(f"wrote {SCHEMA_FILE}")
    else:
        print(text)
    return 0


def main():
    ap = argparse.ArgumentParser(description="GarminBridge workout engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_spec_args(p):
        p.add_argument("--spec-file", help="path to spec .json (or .yaml/.yml)")
        p.add_argument("--stdin", action="store_true", help="read spec JSON from stdin")

    for name in ("validate", "summary", "build", "push"):
        p = sub.add_parser(name)
        add_spec_args(p)
        if name == "push":
            p.add_argument("--schedule", help="YYYY-MM-DD to schedule → auto-syncs to the watch")
    c = sub.add_parser("catalog")
    c.add_argument("--search", required=True, help="term to find (category, exerciseName) pairs")
    c.add_argument("--limit", type=int, default=25)
    sc = sub.add_parser("schema")
    sc.add_argument("--save", action="store_true", help="write workout_spec.schema.json")

    args = ap.parse_args()
    return {
        "validate": cmd_validate, "summary": cmd_summary, "build": cmd_build,
        "push": cmd_push, "catalog": cmd_catalog, "schema": cmd_schema,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
