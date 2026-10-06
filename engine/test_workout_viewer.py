#!/usr/bin/env python3
"""Offline regression for viewing an existing Garmin Connect workout."""

import workout as W


def enum(field, key):
    names = {
        "stepType": "stepTypeKey",
        "endCondition": "conditionTypeKey",
        "targetType": "workoutTargetTypeKey",
    }
    return {names[field]: key}


DETAIL = {
    "workoutName": "Tempo intervals",
    "sportType": {"sportTypeKey": "running"},
    "workoutSegments": [{
        "sportType": {"sportTypeKey": "running"},
        "workoutSteps": [
            {
                "type": "ExecutableStepDTO",
                "stepType": enum("stepType", "warmup"),
                "endCondition": enum("endCondition", "time"),
                "endConditionValue": 600.0,
                "targetType": enum("targetType", "heart.rate.zone"),
                "targetValueOne": 120.0,
                "targetValueTwo": 140.0,
            },
            {
                "type": "RepeatGroupDTO",
                "stepType": enum("stepType", "repeat"),
                "numberOfIterations": 4,
                "workoutSteps": [
                    {
                        "type": "ExecutableStepDTO",
                        "stepType": enum("stepType", "interval"),
                        "endCondition": enum("endCondition", "distance"),
                        "endConditionValue": 1000.0,
                        "targetType": enum("targetType", "power.zone"),
                        "targetValueOne": 106.0,
                        "targetValueTwo": 110.0,
                        "targetValueUnit": {"unitKey": "percent"},
                    },
                    {
                        "type": "ExecutableStepDTO",
                        "stepType": enum("stepType", "recovery"),
                        "endCondition": enum("endCondition", "time"),
                        "endConditionValue": 120.0,
                        "targetType": enum("targetType", "heart.rate.zone"),
                        "targetValueOne": 120.0,
                        "targetValueTwo": 140.0,
                    },
                ],
            },
            {
                "type": "ExecutableStepDTO",
                "stepType": enum("stepType", "cooldown"),
                "endCondition": enum("endCondition", "time"),
                "endConditionValue": 300.0,
                "targetType": enum("targetType", "no.target"),
            },
            {
                "type": "ExecutableStepDTO",
                "stepType": enum("stepType", "other"),
                "endCondition": enum("endCondition", "reps"),
                "endConditionValue": 8.0,
                "targetType": enum("targetType", "no.target"),
                "category": "SQUAT",
                "exerciseName": "BARBELL_BACK_SQUAT",
                "weightValue": 22.5,
                "weightUnit": {"unitKey": "kilogram"},
            },
        ],
    }],
}


def test_dto_to_spec():
    spec = W.dto_to_spec(DETAIL)
    assert spec["name"] == "Tempo intervals"
    assert spec["sport"] == "running"
    assert len(spec["steps"]) == 4
    assert spec["steps"][0]["warmup"] == 600
    assert spec["steps"][0]["hr"] == [120, 140]

    group = spec["steps"][1]
    assert group["repeat"] == 4
    assert len(group["do"]) == 2
    assert group["do"][0]["meters"] == 1000
    assert group["do"][0]["power_pct"] == [106, 110]
    assert group["do"][1]["recover"] == 120
    assert group["do"][1]["hr"] == [120, 140]

    assert spec["steps"][2]["cooldown"] == 300
    strength = spec["steps"][3]
    assert strength["exercise"] == "SQUAT/BARBELL_BACK_SQUAT"
    assert strength["reps"] == 8
    assert strength["weight_kg"] == 22.5


if __name__ == "__main__":
    test_dto_to_spec()
    print("OK")
