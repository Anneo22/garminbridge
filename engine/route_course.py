#!/usr/bin/env python3
"""route_course.py — import a route (GPX or FIT) into a Garmin course FIT the Fenix reads.

The watch DOES read GARMIN/Courses (unlike settings), so a course FIT dropped there over USB
shows up as a navigable route (proven end-to-end 2026-06-30). This module builds that FIT from
an arbitrary GPX or FIT track, matching the exact message layout of a real Garmin-Connect course
file (verified by decoding one: file_id[type=course] · course[name,sport] · lap[bounds,totals] ·
event[timer start/stop] · record[position,distance,altitude,timestamp]). Turn-by-turn
course_points are Garmin-generated and optional — the breadcrumb + off-course alerts work without
them — so we omit them; the watch still navigates the line.

Positions are stored in semicircles (fit_tool converts from degrees on assignment). record.distance
is cumulative metres. We synthesise monotonic 1 s/point timestamps off a fixed base — a course uses
distance, not time, to navigate, so the exact clock is cosmetic.

Two inputs, one shape:
  - GPX: parsed with the stdlib XML parser (namespace-agnostic), no third-party gpx lib.
  - FIT: reuse route_thumb._u_track_fit (already decodes record.position + altitude + lap totals).
"""
import datetime
import math
import os
import re
import xml.etree.ElementTree as ET

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.course_message import CourseMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.profile_type import FileType, Manufacturer, Sport, Event, EventType

# The sports a course can carry on the Fenix (course.sport). Keep the set the watch actually
# supports for navigation; default to cycling (the most common "follow this route" case).
SPORTS = {
    "cycling": Sport.CYCLING, "running": Sport.RUNNING, "walking": Sport.WALKING,
    "hiking": Sport.HIKING, "swimming": Sport.SWIMMING,
}
DEFAULT_SPORT = "cycling"
# GPX <type> hints Garmin/Strava write; map the common ones to a course sport.
GPX_TYPE_SPORT = {
    "running": "running", "run": "running", "9": "running",
    "hiking": "hiking", "hike": "hiking", "walking": "walking", "walk": "walking",
    "cycling": "cycling", "biking": "cycling", "road": "cycling", "1": "cycling", "ride": "cycling",
}

_BASE_TS = int(datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)


def _localname(tag):
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _haversine(a, b):
    R = 6371000.0
    la1, lo1, la2, lo2 = math.radians(a[0]), math.radians(a[1]), math.radians(b[0]), math.radians(b[1])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(min(1.0, h)))


def parse_gpx(path):
    """GPX file -> ([(lat, lon, ele|None)], name, sport_hint). Reads <trkpt>/<rtept> lat/lon,
    optional <ele>; picks up <name> and <type> if present. Namespace-agnostic (matches on the
    local tag name), so it works for Garmin, Strava, komoot and plain GPX 1.1 alike."""
    tree = ET.parse(path)
    root = tree.getroot()
    pts, name, sport_hint = [], None, None
    for el in root.iter():
        ln = _localname(el.tag)
        if ln in ("trkpt", "rtept") and el.get("lat") and el.get("lon"):
            try:
                lat, lon = float(el.get("lat")), float(el.get("lon"))
            except ValueError:
                continue
            ele = None
            for ch in el:
                if _localname(ch.tag) == "ele" and (ch.text or "").strip():
                    try:
                        ele = float(ch.text.strip())
                    except ValueError:
                        ele = None
                    break
            pts.append((lat, lon, ele))
        elif ln == "name" and name is None and (el.text or "").strip():
            name = el.text.strip()
        elif ln == "type" and sport_hint is None and (el.text or "").strip():
            sport_hint = GPX_TYPE_SPORT.get(el.text.strip().lower())
    return pts, name, sport_hint


def parse_fit(path):
    """FIT file -> ([(lat, lon, ele|None)], name, sport_hint). Reuses the course/track decoder
    the thumbnailer already uses; reads course.name + course.sport when present."""
    import fitdecode
    from route_thumb import _u_track_fit
    pts, _meta = _u_track_fit(path)
    name, sport_hint = None, None
    try:
        with fitdecode.FitReader(open(path, "rb")) as fr:
            for m in fr:
                if isinstance(m, fitdecode.FitDataMessage) and m.name == "course":
                    if m.has_field("name") and m.get_value("name"):
                        name = m.get_value("name")
                    if m.has_field("sport") and m.get_value("sport"):
                        s = str(m.get_value("sport")).lower()
                        sport_hint = s if s in SPORTS else None
                    break
    except Exception:
        pass
    return pts, name, sport_hint


def load_track(path):
    """Dispatch by extension. Returns ([(lat, lon, ele)], name_hint, sport_hint). GPX and FIT
    only (the two route formats a Garmin user actually has)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".gpx":
        return parse_gpx(path)
    if ext == ".fit":
        return parse_fit(path)
    raise ValueError(f"unsupported route file {ext!r} (need .gpx or .fit)")


def _clean(track):
    """Drop consecutive duplicate points (they add no shape and break distance)."""
    out = []
    for p in track:
        if not out or (out[-1][0], out[-1][1]) != (p[0], p[1]):
            out.append(p)
    return out


def _safe_filename(name):
    """A watch-safe course filename stem from the route name (Garmin's own files are ASCII)."""
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("_")[:40] or "Route"
    return stem + ".fit"


def build_course_fit(track, name, sport, dst):
    """[(lat, lon, ele|None)] + name + sport -> a Garmin course FIT written to `dst`.
    Returns {ok, dist_m, asc_m, desc_m, n_points, slat, slon, sport, name}. Raises ValueError
    on a track too short to navigate."""
    track = _clean(track)
    if len(track) < 2:
        raise ValueError("route has fewer than 2 GPS points")
    if sport not in SPORTS:
        sport = DEFAULT_SPORT
    latlon = [(la, lo) for la, lo, _ in track]
    elev = [e for _, _, e in track]

    cum = [0.0]
    for i in range(1, len(latlon)):
        cum.append(cum[-1] + _haversine(latlon[i - 1], latlon[i]))
    # ascent/descent with a 1 m threshold so GPS jitter doesn't inflate them
    asc = desc = 0.0
    prev = None
    for e in elev:
        if e is None:
            continue
        if prev is not None:
            d = e - prev
            if d >= 1.0:
                asc += d
            elif d <= -1.0:
                desc += -d
        prev = e

    builder = FitFileBuilder(auto_define=True)

    fid = FileIdMessage()
    fid.type = FileType.COURSE
    fid.manufacturer = Manufacturer.GARMIN.value
    fid.product = 0
    fid.time_created = _BASE_TS
    fid.serial_number = 1
    builder.add(fid)

    course = CourseMessage()
    course.course_name = (name or "Imported route")[:100]
    course.sport = SPORTS[sport]
    builder.add(course)

    n = len(latlon)
    lap = LapMessage()
    lap.start_time = _BASE_TS
    lap.timestamp = _BASE_TS + n * 1000
    lap.start_position_lat = latlon[0][0]
    lap.start_position_long = latlon[0][1]
    lap.end_position_lat = latlon[-1][0]
    lap.end_position_long = latlon[-1][1]
    lap.total_distance = cum[-1]
    lap.total_timer_time = float(n)
    lap.total_elapsed_time = float(n)
    if asc:
        lap.total_ascent = round(asc)
    if desc:
        lap.total_descent = round(desc)
    builder.add(lap)

    start_ev = EventMessage()
    start_ev.event = Event.TIMER
    start_ev.event_type = EventType.START
    start_ev.timestamp = _BASE_TS
    builder.add(start_ev)

    for i, (la, lo, al) in enumerate(track):
        rec = RecordMessage()
        rec.position_lat = la
        rec.position_long = lo
        rec.distance = cum[i]
        if al is not None:
            rec.altitude = al
        rec.timestamp = _BASE_TS + i * 1000
        builder.add(rec)

    stop_ev = EventMessage()
    stop_ev.event = Event.TIMER
    stop_ev.event_type = EventType.STOP_ALL
    stop_ev.timestamp = _BASE_TS + n * 1000
    builder.add(stop_ev)

    builder.build().to_file(dst)
    return {
        "ok": True, "dist_m": round(cum[-1]), "asc_m": round(asc), "desc_m": round(desc),
        "n_points": n, "slat": round(latlon[0][0], 5), "slon": round(latlon[0][1], 5),
        "sport": sport, "name": course.course_name,
    }


def import_route(path, name=None, sport=None, dst=None):
    """High-level: a GPX/FIT path -> a course FIT. name/sport override the file's own hints;
    dst defaults to a watch-safe filename beside the source. Returns build_course_fit's dict
    plus `path` (where the FIT was written) and `filename`."""
    track, name_hint, sport_hint = load_track(path)
    final_name = (name or name_hint or os.path.splitext(os.path.basename(path))[0]).strip()
    final_sport = sport or sport_hint or DEFAULT_SPORT
    if dst is None:
        dst = os.path.join(os.path.dirname(os.path.abspath(path)), _safe_filename(final_name))
    res = build_course_fit(track, final_name, final_sport, dst)
    res["path"] = dst
    res["filename"] = os.path.basename(dst)
    return res


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 2:
        raise SystemExit("usage: route_course.py <route.gpx|route.fit> [name] [sport]")
    src = sys.argv[1]
    nm = sys.argv[2] if len(sys.argv) > 2 else None
    sp = sys.argv[3] if len(sys.argv) > 3 else None
    out = os.path.join(os.path.dirname(os.path.abspath(src)),
                       _safe_filename(nm or os.path.splitext(os.path.basename(src))[0]))
    print(json.dumps(import_route(src, name=nm, sport=sp, dst=out), indent=2))
