#!/usr/bin/env python3
"""GarminBridge workout SPEC — the canonical contract.

This is the ONE source of truth for the shape of a workout spec: the dict that
`spec.py` compiles into a Garmin workout and pushes via Connect. It exists so that
BOTH surfaces that produce specs speak the same language:

  * an external authoring client (reads plain English / a photo / markdown and emits
    a spec), and
  * the future in-app authoring feature (the app feeds this same schema + the
    authoring prompt to the user's own LLM, then pushes the spec through the same
    engine).

So the contract lives HERE, once, next to the engine — not duplicated in the client.

What this module gives you:
  SCHEMA_VERSION        - bump when the spec shape changes.
  validate_spec(spec)   - structural + semantic validation. Returns {"errors": [...],
                          "warnings": [...]}. Errors block a push; warnings inform.
                          Validates exercise (category, name) against the live catalog
                          so a bad move fails loudly HERE, not silently on the watch.
  json_schema()         - a JSON-Schema (draft-07) description of the shape, for app
                          devs / an LLM's structured-output constraint. (JSON Schema
                          cannot check catalog membership; validate_spec is the
                          authoritative gate.)

The spec shape (see workout_authoring.md for the full human guide + examples):

    {
      "name": "Tempo 3x8min",
      "sport": "running",              # running | cycling | swimming | strength
      "animate": true,                  # optional; default true (Garmin animation tag)
      "profile": {...},                 # optional athlete thresholds override
      "steps": [ <step>, ... ]          # non-empty
    }

A <step> is exactly ONE of:
  {"warmup": <sec>, <targets>}
  {"cooldown": <sec>, <targets>}
  {"work"|"run": <sec>, "meters": <m>, <targets>}   # give seconds OR meters (or neither = lap-press)
  {"recover"|"recovery": <sec>, <targets>}
  {"rest": <sec>}                                    # untargeted
  {"exercise": "CAT/NAME"|{...}, "reps": N | "seconds": S, "weight_kg": W, "rest": <sec>}
  {"repeat": N, "do": [ <step>, ... ]}              # N>=1, non-empty do

<targets> (at most one primary makes sense per step; the compiler picks by precedence):
  ABSOLUTE:            hr | pace | power | cadence   as [lo, hi]
  ADAPTIVE (rescale):  power_pct [lo,hi] (cycling %FTP) | power_zone | hr_zone | pace_zone  (int)
  THRESHOLD-RELATIVE:  pace_pct | run_power_pct | hr_pct  as [lo,hi]  (baked to absolute at build)
  SECONDARY ALERT:     power_alert_zone (int) | power_alert [lo,hi] | hr_alert_zone (int)
"""
from __future__ import annotations

SCHEMA_VERSION = 1

SPORTS = ("running", "cycling", "swimming", "strength", "strength_training")

# Step primary keys, in the SAME precedence spec.py._leaf uses to classify a step.
# (A dict with both "exercise" and "rest" is an exercise with a rest-after, not a
# bare rest — the precedence encodes that, and validation must match the compiler.)
_STEP_PRIMARY = ("warmup", "cooldown", "exercise", "work", "run",
                 "recover", "recovery", "rest")

# Target keys grouped by kind.
_ABS_PAIR = ("hr", "pace", "power", "cadence")
_THRESH_PAIR = ("pace_pct", "run_power_pct", "hr_pct")
_ADAPT_PAIR = ("power_pct",)
_SEC_PAIR = ("power_alert",)
_ZONE_INT = ("power_zone", "hr_zone", "pace_zone")
_SEC_INT = ("power_alert_zone", "hr_alert_zone")
_PAIR_KEYS = _ABS_PAIR + _THRESH_PAIR + _ADAPT_PAIR + _SEC_PAIR
_INT_KEYS = _ZONE_INT + _SEC_INT
_TARGET_KEYS = set(_PAIR_KEYS + _INT_KEYS)
_PRIMARY_TARGET_KEYS = set(_ABS_PAIR + _THRESH_PAIR + _ADAPT_PAIR + _ZONE_INT)

# Which extra keys each step kind may carry (beyond its primary key + targets).
_ALLOWED = {
    "warmup":   {"warmup"} | _TARGET_KEYS,
    "cooldown": {"cooldown"} | _TARGET_KEYS,
    "work":     {"work", "meters"} | _TARGET_KEYS,
    "run":      {"run", "meters"} | _TARGET_KEYS,
    "recover":  {"recover"} | _TARGET_KEYS,
    "recovery": {"recovery"} | _TARGET_KEYS,
    "rest":     {"rest"},
    "exercise": {"exercise", "reps", "seconds", "weight_kg", "rest"},
}


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _check_pair(key, val, where, errors):
    if not (isinstance(val, (list, tuple)) and len(val) == 2
            and all(_is_num(v) for v in val)):
        errors.append(f"{where}: {key} must be [low, high] numbers, got {val!r}")
        return
    if val[0] > val[1]:
        errors.append(f"{where}: {key} low > high ({val[0]} > {val[1]})")


def _classify(step):
    """Return the step's kind by the compiler's precedence, or None if unrecognised.
    'repeat' is handled before this by the caller."""
    for k in _STEP_PRIMARY:
        if k in step:
            return k
    return None


def _validate_targets(step, kind, where, errors, warnings):
    for k, v in step.items():
        if k in _PAIR_KEYS:
            _check_pair(k, v, where, errors)
        elif k in _INT_KEYS:
            if not _is_int(v) or v < 1:
                errors.append(f"{where}: {k} must be a positive integer (zone/id), got {v!r}")

    # sport-aware guidance (warnings, never blocking)
    sport = step.get("_sport")
    primaries = [k for k in _PRIMARY_TARGET_KEYS if k in step]
    if len(primaries) > 1:
        warnings.append(f"{where}: multiple primary targets {primaries}; "
                        "the compiler keeps one by precedence (adaptive > absolute)")
    if sport == "running" and "power_pct" in step:
        warnings.append(f"{where}: running has no %-of-run-threshold in Garmin; "
                        "power_pct is a cycling(%FTP) target. Use pace_pct (baked) or a *_zone.")
    if sport in ("running", "swimming") and any(k in step for k in _SEC_PAIR + _SEC_INT):
        warnings.append(f"{where}: secondary/alert targets are honoured on cycling; "
                        "on running they encode but the watch likely ignores them.")


def _validate_step(step, where, cats, sport, errors, warnings, depth=0):
    if not isinstance(step, dict):
        errors.append(f"{where}: step must be an object, got {type(step).__name__}")
        return

    if "repeat" in step:
        r = step["repeat"]
        if not _is_int(r) or r < 1:
            errors.append(f"{where}: repeat must be an integer >= 1, got {r!r}")
        do = step.get("do")
        if not isinstance(do, list) or not do:
            errors.append(f"{where}: repeat needs a non-empty 'do' list of steps")
        else:
            if depth >= 1:
                warnings.append(f"{where}: nested repeats are flattened by Garmin; keep one level.")
            for i, sub in enumerate(do):
                _validate_step(sub, f"{where}.do[{i}]", cats, sport, errors, warnings, depth + 1)
        extra = set(step) - {"repeat", "do"}
        if extra:
            warnings.append(f"{where}: repeat carries unexpected keys {sorted(extra)} (ignored)")
        return

    kind = _classify(step)
    if kind is None:
        errors.append(f"{where}: unrecognised step (no warmup/cooldown/work/run/"
                      f"recover/rest/exercise/repeat key): {sorted(step)}")
        return

    # unexpected keys → warning (typo guard: e.g. 'warmpu', 'weight' vs 'weight_kg')
    extra = set(step) - _ALLOWED[kind] - {"_sport"}
    if extra:
        warnings.append(f"{where}: {kind} step has unexpected keys {sorted(extra)} "
                        "(ignored by the compiler — check for typos)")

    step["_sport"] = sport  # transient hint for target guidance; caller strips it
    try:
        if kind in ("warmup", "cooldown", "recover", "recovery"):
            if not _is_num(step[kind]) or step[kind] <= 0:
                errors.append(f"{where}: {kind} must be seconds > 0, got {step[kind]!r}")
            _validate_targets(step, kind, where, errors, warnings)

        elif kind in ("work", "run"):
            dur = step.get("work") or step.get("run")
            meters = step.get("meters")
            if dur is not None and (not _is_num(dur) or dur <= 0):
                errors.append(f"{where}: {kind} seconds must be > 0, got {dur!r}")
            if meters is not None and (not _is_num(meters) or meters <= 0):
                errors.append(f"{where}: meters must be > 0, got {meters!r}")
            if dur is None and meters is None:
                warnings.append(f"{where}: {kind} has no duration/meters → ends on lap-press.")
            _validate_targets(step, kind, where, errors, warnings)

        elif kind == "rest":
            if not _is_num(step["rest"]) or step["rest"] <= 0:
                errors.append(f"{where}: rest must be seconds > 0, got {step['rest']!r}")

        elif kind == "exercise":
            _validate_exercise(step, where, cats, errors, warnings)
    finally:
        step.pop("_sport", None)


def _validate_exercise(step, where, cats, errors, warnings):
    val = step["exercise"]
    if isinstance(val, str):
        category, _, name = val.partition("/")
        name = name or None
    elif isinstance(val, dict):
        category = val.get("category")
        name = val.get("name")
    else:
        errors.append(f"{where}: exercise must be 'CAT/NAME' or {{category,name}}, got {val!r}")
        return
    if not category:
        errors.append(f"{where}: exercise is missing a category")
        return

    # Validate against the live Garmin catalog — the check that prevents a silent
    # no-animation / rejected-move on the watch.
    if cats is not None:
        try:
            import catalog
            catalog.validate(category, name, cats)
        except ValueError as e:
            errors.append(f"{where}: {e}")
        except Exception as e:  # catalog unavailable: warn, don't hard-fail offline
            warnings.append(f"{where}: could not validate exercise against catalog ({e})")

    reps, secs = step.get("reps"), step.get("seconds")
    if reps is not None and (not _is_int(reps) or reps < 1):
        errors.append(f"{where}: reps must be a positive integer, got {reps!r}")
    if secs is not None and (not _is_num(secs) or secs <= 0):
        errors.append(f"{where}: seconds must be > 0, got {secs!r}")
    if reps is None and secs is None:
        warnings.append(f"{where}: exercise has no reps/seconds → ends on lap-press.")
    w = step.get("weight_kg")
    if w is not None and (not _is_num(w) or w < 0):
        errors.append(f"{where}: weight_kg must be a non-negative number, got {w!r}")
    rest = step.get("rest")
    if rest is not None and (not _is_num(rest) or rest <= 0):
        errors.append(f"{where}: rest (rest-after) must be seconds > 0, got {rest!r}")


def validate_spec(spec, cats=None):
    """Validate a workout spec. Returns {"errors": [...], "warnings": [...]}.

    cats = the Garmin exercise catalog (catalog.load()); pass it to validate strength
    exercise (category, name) pairs. If omitted, exercise names are not checked
    (a warning is emitted per exercise).
    """
    errors, warnings = [], []
    if not isinstance(spec, dict):
        return {"errors": [f"spec must be an object, got {type(spec).__name__}"],
                "warnings": []}

    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("name: required non-empty string")

    sport = spec.get("sport", "strength")
    if sport not in SPORTS:
        errors.append(f"sport: must be one of {SPORTS}, got {sport!r}")

    if "animate" in spec and not isinstance(spec["animate"], bool):
        errors.append(f"animate: must be true/false, got {spec['animate']!r}")

    if "profile" in spec and not isinstance(spec["profile"], dict):
        errors.append("profile: must be an object of thresholds if present")

    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps: required non-empty list")
    else:
        for i, st in enumerate(steps):
            _validate_step(st, f"steps[{i}]", cats, sport, errors, warnings)

    # spec.py bakes threshold-relative targets from a profile; warn if the profile
    # is missing the field a threshold-relative target needs.
    _warn_missing_thresholds(spec, warnings)

    return {"errors": errors, "warnings": warnings}


def _iter_leaf_steps(steps):
    for st in steps or []:
        if isinstance(st, dict) and "repeat" in st:
            yield from _iter_leaf_steps(st.get("do") or [])
        elif isinstance(st, dict):
            yield st


def _warn_missing_thresholds(spec, warnings):
    import profile as P
    prof = spec.get("profile") or P.DEFAULTS
    needs = {"pace_pct": "threshold_speed_mps", "hr_pct": "threshold_hr",
             "run_power_pct": "run_power_threshold_w"}
    used = set()
    for st in _iter_leaf_steps(spec.get("steps")):
        used |= (set(st) & set(needs))
    for k in used:
        field = needs[k]
        if not prof.get(field):
            warnings.append(f"{k} is used but profile.{field} is unset; a live Connect "
                            "profile fetch or an explicit profile is needed to bake it.")


def json_schema():
    """A draft-07 JSON Schema for the spec shape. Documents structure for app devs
    and constrains an LLM's structured output. Catalog membership is NOT expressible
    here — validate_spec is the authoritative semantic gate."""
    pair = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}
    zone = {"type": "integer", "minimum": 1}
    targets = {}
    for k in _PAIR_KEYS:
        targets[k] = pair
    for k in _INT_KEYS:
        targets[k] = zone

    def step_with(props, required=None):
        p = dict(props)
        p.update(targets)
        s = {"type": "object", "properties": p}
        if required:
            s["required"] = required
        return s

    step = {
        "oneOf": [
            step_with({"warmup": {"type": "number", "exclusiveMinimum": 0}}, ["warmup"]),
            step_with({"cooldown": {"type": "number", "exclusiveMinimum": 0}}, ["cooldown"]),
            step_with({"work": {"type": "number", "exclusiveMinimum": 0},
                       "meters": {"type": "number", "exclusiveMinimum": 0}}),
            step_with({"run": {"type": "number", "exclusiveMinimum": 0},
                       "meters": {"type": "number", "exclusiveMinimum": 0}}),
            step_with({"recover": {"type": "number", "exclusiveMinimum": 0}}, ["recover"]),
            step_with({"recovery": {"type": "number", "exclusiveMinimum": 0}}, ["recovery"]),
            {"type": "object", "required": ["rest"],
             "properties": {"rest": {"type": "number", "exclusiveMinimum": 0}}},
            {"type": "object", "required": ["exercise"], "properties": {
                "exercise": {"oneOf": [
                    {"type": "string", "description": "CATEGORY/EXERCISE_NAME"},
                    {"type": "object", "required": ["category"], "properties": {
                        "category": {"type": "string"}, "name": {"type": "string"}}},
                ]},
                "reps": {"type": "integer", "minimum": 1},
                "seconds": {"type": "number", "exclusiveMinimum": 0},
                "weight_kg": {"type": "number", "minimum": 0},
                "rest": {"type": "number", "exclusiveMinimum": 0}}},
            {"type": "object", "required": ["repeat", "do"], "properties": {
                "repeat": {"type": "integer", "minimum": 1},
                "do": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/step"}}}},
        ]
    }
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "https://garminbridge.anneo22/workout-spec-v%d.json" % SCHEMA_VERSION,
        "title": "GarminBridge workout spec",
        "description": "A human/AI-writable description of a Garmin workout. "
                       "See workout_authoring.md for the authoring guide.",
        "type": "object",
        "required": ["name", "steps"],
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "sport": {"enum": list(SPORTS), "default": "strength"},
            "animate": {"type": "boolean", "default": True},
            "profile": {"type": "object"},
            "steps": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/step"}},
        },
        "$defs": {"step": step},
    }


if __name__ == "__main__":
    import json
    import sys
    if "--schema" in sys.argv:
        print(json.dumps(json_schema(), indent=2))
    else:
        print(f"workout spec schema v{SCHEMA_VERSION}; sports={SPORTS}")
