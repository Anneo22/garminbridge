#!/usr/bin/env python3
"""Content inventory — the backend for the on-watch content manager.

Lists everything you have in Garmin Connect (workouts + courses) so GarminBridge
can show "what's where". The Connect side is fully reachable from the Mac. The
WATCH side (what's actually on the device) needs USB and is read by the existing
backup engine (GARMIN/Workouts/*.fit, GARMIN/Courses/*.fit) — stubbed here until
the watch is attached. Key fact (verified 2026-06-27): deleting in Connect does
NOT remove from the watch, so the two sides are tracked independently.
"""
import os
from garminconnect import Garmin
import paths

HERE = os.path.dirname(os.path.abspath(__file__))


def connect_workouts(g):
    out = []
    for w in g.connectapi("/workout-service/workouts?start=0&limit=200&myWorkoutsOnly=true"):
        out.append({"kind": "workout", "id": w.get("workoutId"),
                    "name": w.get("workoutName"),
                    "sport": (w.get("sportType") or {}).get("sportTypeKey"),
                    "provider": w.get("workoutProvider")})
    return out


def connect_courses(g):
    for url in ("/course-service/course",  # LIVE endpoint (singular); plural /courses 404s now
                "/course-service/courses?includeGeoPoints=false",
                "/course-service/courses"):
        try:
            r = g.connectapi(url)
            rows = r if isinstance(r, list) else r.get("coursesForUser", r.get("courses", []))
            return [{"kind": "course", "id": c.get("courseId") or c.get("id"),
                     "name": c.get("courseName") or c.get("name"),
                     "sport": (c.get("activityType") or {}).get("typeKey") if isinstance(c.get("activityType"), dict) else c.get("activityType"),
                     "provider": "Connect"} for c in rows]
        except Exception:
            continue
    return []


def watch_inventory():
    """STUB (needs USB): read GARMIN/Workouts/*.fit + GARMIN/Courses/*.fit via the
    backup engine (bin/export-voice-notes.sh gp_get_all path). Returns on-watch
    file ids so each item can be marked on_watch = yes/no for the manager UI."""
    return None  # attach the watch to populate


def inventory(g):
    items = connect_workouts(g) + connect_courses(g)
    on_watch = watch_inventory()
    for it in items:
        it["connect"] = True
        it["watch"] = (None if on_watch is None else (it["id"] in on_watch))
    return items


def delete_connect(g, item):
    if item["kind"] == "workout":
        return g.delete_workout(item["id"])
    raise NotImplementedError("course delete via Connect: use the USB file path (watch reads GARMIN/Courses)")


if __name__ == "__main__":
    g = Garmin(); g.login(paths.state(".tokens"))
    items = inventory(g)
    wk = [i for i in items if i["kind"] == "workout"]
    co = [i for i in items if i["kind"] == "course"]
    print(f"=== Connect inventory: {len(wk)} workouts, {len(co)} courses "
          f"(watch side = needs USB) ===")
    for i in (wk + co)[:40]:
        w = "?" if i["watch"] is None else ("on-watch" if i["watch"] else "not-on-watch")
        print(f"  [{i['kind'][:4]}] {i['name']!r:48} sport={i['sport']} prov={i['provider']} watch={w}")
