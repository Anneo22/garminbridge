#!/usr/bin/env python3
"""Route thumbnails: turn a course's GPS track into a small line-art SVG the UI
draws on the route row, plus the data the hover preview needs (direction, km
markers, distance / ascent / descent, an elevation profile).

Two sources, one shape pipeline:
  - Mac-backup FITs (~/Documents/Garmin Bridge/Settings/Courses): the on-watch /
    re-addable routes. Read record.position + altitude + the lap totals.
  - Connect course-service geoPoints (routes with no local FIT): the payload
    carries latitude/longitude/elevation/distance per point + distanceMeter /
    elevationGainMeter / elevationLossMeter. api.py fetches those and calls here.

Per track we compute, in one 44x28 viewBox:
  d        the route shape (equirectangular projection, cos-lat corrected so it
           isn't stretched, north up; Ramer-Douglas-Peucker simplified)
  start/end the endpoints (so the UI can dot the start and cap the end)
  km       [(x, y, km), ...] markers at an ADAPTIVE round interval (~5 total, so
           a 5km loop shows 1..4 and a 60km route shows 10,20,..,50 not 60 dots)
  prof     the elevation profile as its own small path (0 0 100 24)
  dist_m / asc_m / desc_m  the metrics strip values

Cheap + cached forever (a route's geometry never changes): local by mtime+size,
Connect by course id. Bump GEOM_VER when the schema or fidelity changes so every
old cache entry is treated as a miss and rebuilt.
"""
import json
import math
import os
import sys
import threading

import fitdecode

from clean_watch import _norm
import paths

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = paths.state("route_traces.json")          # keyed by backup filename (mtime+size)
CONNECT_CACHE = paths.state("route_traces_connect.json")  # keyed by Connect course id


def course_backup_dir():
    """Mac course FIT library (under GVE_SET_DEST)."""
    return os.path.join(paths.bridge_settings_dir(), "Courses")


# COURSE_BACKUP lives under iCloud-synced Documents; when that filesystem stalls,
# os.listdir / os.stat on it can block for minutes with no timeout. build_cache is
# on the snapshot hot path, so a stall would freeze the whole app. Time-box the
# metadata scan (listdir + stat) and fall back to the on-disk cache when it hangs.
COURSE_SCAN_TIMEOUT = 5.0  # seconds

SEMI = 180.0 / 2 ** 31  # semicircles -> degrees
W, H, PAD = 44.0, 28.0, 3.0            # route-shape viewBox
PW, PH, PPAD = 100.0, 24.0, 1.5        # elevation-profile viewBox
GEOM_VER = 6   # schema/fidelity version -> baked into cache keys to auto-invalidate (v6: +created)
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
EPS = 0.14     # RDP tolerance in shape viewBox units (lower = more points, smoother zoom)
MAX_PTS = 220  # hard cap on shape vertices, keeps the `d` string small
MAX_KM_MARKERS = 6


def _u_track_fit(path):
    """Every GPS record of a course FIT as (lat_deg, lon_deg, altitude_m|None),
    plus the lap totals {dist_m, asc_m, desc_m} and the FIT's own creation time
    (file_id.time_created -> `created` epoch seconds, for a "recently added" sort)."""
    pts, meta = [], {"dist_m": None, "asc_m": None, "desc_m": None, "created": None}
    try:
        with fitdecode.FitReader(open(path, "rb")) as fr:
            for m in fr:
                if not isinstance(m, fitdecode.FitDataMessage):
                    continue
                if m.name == "file_id" and m.has_field("time_created") \
                        and m.get_value("time_created") is not None and meta["created"] is None:
                    tc = m.get_value("time_created")
                    meta["created"] = int(tc.timestamp()) if hasattr(tc, "timestamp") else None
                if m.name == "record" and m.has_field("position_lat") and m.has_field("position_long"):
                    la, lo = m.get_value("position_lat"), m.get_value("position_long")
                    if la is None or lo is None:
                        continue
                    alt = None
                    for k in ("enhanced_altitude", "altitude"):
                        if m.has_field(k) and m.get_value(k) is not None:
                            alt = m.get_value(k); break
                    pts.append((la * SEMI, lo * SEMI, alt))
                elif m.name == "lap":
                    for src, dst in (("total_distance", "dist_m"), ("total_ascent", "asc_m"),
                                     ("total_descent", "desc_m")):
                        if m.has_field(src) and m.get_value(src) is not None and meta[dst] is None:
                            meta[dst] = m.get_value(src)
    except Exception as e:
        sys.stderr.write(f"  trace decode failed {os.path.basename(path)}: {e!r}\n")
    return pts, meta


def _u_track_geopoints(payload):
    """Connect course-service payload -> ([(lat, lon, elev)], {dist_m, asc_m, desc_m})."""
    gp = payload.get("geoPoints") or []
    pts = [(p["latitude"], p["longitude"], p.get("elevation"))
           for p in gp if p.get("latitude") is not None and p.get("longitude") is not None]
    meta = {"dist_m": payload.get("distanceMeter"),
            "asc_m": payload.get("elevationGainMeter"),
            "desc_m": payload.get("elevationLossMeter"),
            # Connect stores the course's creation time as ms epoch (createdDate); keep it in
            # seconds so a Connect-only route can still sort by "recently added".
            "created": (int(payload["createdDate"] // 1000)
                        if isinstance(payload.get("createdDate"), (int, float)) else None)}
    return pts, meta


def _haversine(a, b):
    R = 6371000.0
    lat1, lon1, lat2, lon2 = math.radians(a[0]), math.radians(a[1]), math.radians(b[0]), math.radians(b[1])
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(min(1.0, h)))


def _bearing(a, b):
    """Initial great-circle bearing a->b in degrees (0=N, 90=E)."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _route_heading(latlon):
    """The route's dominant outbound direction: the bearing from the start to its
    farthest point (the turnaround for out-and-backs, the far reach for loops).
    This is what a cyclist means by 'which way does it go' (and later: wind-vs-you)."""
    start = latlon[0]
    far = max(latlon, key=lambda p: _haversine(start, p))
    if _haversine(start, far) < 1.0:
        return None, None
    b = _bearing(start, far)
    return round(b), COMPASS[round(b / 45) % 8]


def _project(pts):
    """[(lat, lon), ...] degrees -> viewBox coords: cos-lat corrected, north up,
    aspect preserved, centred in WxH with PAD margin."""
    mean_lat = sum(la for la, _ in pts) / len(pts)
    k = math.cos(math.radians(mean_lat)) or 1e-9
    xs = [lo * k for _, lo in pts]
    ys = [-la for la, _ in pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    dx, dy = (maxx - minx) or 1e-9, (maxy - miny) or 1e-9
    s = min((W - 2 * PAD) / dx, (H - 2 * PAD) / dy)
    ox = (W - s * dx) / 2 - s * minx
    oy = (H - s * dy) / 2 - s * miny
    return [(x * s + ox, y * s + oy) for x, y in zip(xs, ys)]


def _rdp(pts, eps):
    """Ramer-Douglas-Peucker, iterative (no recursion-depth risk on long tracks)."""
    if len(pts) < 3:
        return pts[:]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i0, i1 = stack.pop()
        ax, ay = pts[i0]; bx, by = pts[i1]
        dx, dy = bx - ax, by - ay
        seg = math.hypot(dx, dy)
        dmax, idx = 0.0, -1
        for i in range(i0 + 1, i1):
            px, py = pts[i]
            if seg < 1e-9:
                # degenerate segment (closed loop: start == end) -> perpendicular
                # distance is 0 for every point and collapses the track; fall back
                # to distance from the shared endpoint so the shape survives.
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs((px - ax) * dy - (py - ay) * dx) / seg
            if d > dmax:
                dmax, idx = d, i
        if dmax > eps and idx != -1:
            keep[idx] = True
            stack.append((i0, idx)); stack.append((idx, i1))
    return [p for p, k in zip(pts, keep) if k]


def _path_d(pts):
    return "M" + f"{pts[0][0]:.1f} {pts[0][1]:.1f}" + "".join(f"L{x:.1f} {y:.1f}" for x, y in pts[1:])


def _km_step(total_km):
    """A round km interval that yields roughly 3-6 markers across the route."""
    for step in (1, 2, 5, 10, 20, 50, 100, 200):
        if total_km / step <= MAX_KM_MARKERS:
            return step
    return 500


def _km_markers(proj, cum):
    """[(x, y, km), ...] at round-km distances, placed on the nearest track point."""
    total_km = cum[-1] / 1000.0
    if total_km < 1.5:
        return []
    step = _km_step(total_km)
    marks, j = [], 0
    v = step
    while v < total_km:
        target = v * 1000.0
        while j + 1 < len(cum) and cum[j + 1] < target:
            j += 1
        x, y = proj[j]
        marks.append([round(x, 1), round(y, 1), v])
        v += step
    return marks


def _profile(cum, elev):
    """Elevation profile as its own path in a 0 0 PW PH box, or None if flat/absent."""
    have = [(c, e) for c, e in zip(cum, elev) if e is not None]
    if len(have) < 2:
        return None
    es = [e for _, e in have]
    lo, hi = min(es), max(es)
    total = have[-1][0] or 1e-9
    span = (hi - lo) or 1e-9
    proj = [((c / total) * (PW - 2 * PPAD) + PPAD,
             PH - PPAD - ((e - lo) / span) * (PH - 2 * PPAD)) for c, e in have]
    simp = _rdp(proj, 0.4)
    if len(simp) > 120:
        step = math.ceil(len(simp) / 120)
        simp = simp[::step] + [simp[-1]]
    return {"vb": f"0 0 {PW:g} {PH:g}", "d": _path_d(simp)}


def _ascent_descent(cum, elev, meta):
    """Prefer Garmin's stored totals; else derive from elevation with a small
    threshold so GPS jitter doesn't inflate the numbers."""
    asc, desc = meta.get("asc_m"), meta.get("desc_m")
    if asc is not None and desc is not None:
        return round(asc), round(desc)
    up = down = 0.0
    prev = None
    for e in elev:
        if e is None:
            continue
        if prev is not None:
            d = e - prev
            if d >= 1.0:
                up += d
            elif d <= -1.0:
                down += -d
        prev = e
    return round(up), round(down)


def trace_from_track(track, meta):
    """[(lat, lon, elev), ...] + {dist_m, asc_m, desc_m} -> the full thumbnail dict
    (vb, d, pts, start, end, km, prof, dist_m, asc_m, desc_m), or None if no shape."""
    clean = []
    for p in track:
        if not clean or (clean[-1][0], clean[-1][1]) != (p[0], p[1]):
            clean.append(p)
    if len(clean) < 2:
        return None
    latlon = [(la, lo) for la, lo, _ in clean]
    elev = [e for _, _, e in clean]
    cum = [0.0]
    for i in range(1, len(latlon)):
        cum.append(cum[-1] + _haversine(latlon[i - 1], latlon[i]))
    proj = _project(latlon)
    simp = _rdp(proj, EPS)
    if len(simp) > MAX_PTS:
        step = math.ceil(len(simp) / MAX_PTS)
        simp = simp[::step] + [simp[-1]]
    asc, desc = _ascent_descent(cum, elev, meta)
    bearing, compass = _route_heading(latlon)
    return {
        "vb": f"0 0 {W:g} {H:g}",
        "d": _path_d(simp),
        "pts": len(simp),
        "start": [round(proj[0][0], 1), round(proj[0][1], 1)],
        "end": [round(proj[-1][0], 1), round(proj[-1][1], 1)],
        "km": _km_markers(proj, cum),
        "prof": _profile(cum, elev),
        "dist_m": round(meta.get("dist_m") or cum[-1]),
        "asc_m": asc,
        "desc_m": desc,
        "bearing": bearing,
        "compass": compass,
        # the route's real-world start (for a forecast-wind lookup + start-place filter per
        # route); the geometry cache is the natural home since it already parses the GPS track.
        "slat": round(latlon[0][0], 5),
        "slon": round(latlon[0][1], 5),
        # the FIT/Connect creation time (epoch seconds, may be None) — the "recently added" sort key
        "created": meta.get("created"),
    }


def trace(path):
    """FIT path -> full thumbnail dict or None when there's no usable GPS track."""
    track, meta = _u_track_fit(path)
    return trace_from_track(track, meta)


def _public(entry):
    """The frontend-facing slice of a cache entry (drop bookkeeping keys)."""
    return {k: v for k, v in entry.items() if k not in ("key", "name", "ver")}


def _key(path):
    st = os.stat(path)
    return f"v{GEOM_VER}-{int(st.st_mtime)}-{st.st_size}"  # version baked in -> auto-invalidate


def _call_with_timeout(fn, timeout):
    """Run fn() in a daemon thread and wait up to `timeout` seconds. Returns
    (True, result) if it finished, (False, None) if it's still running. The thread
    is a daemon so a wedged filesystem call (iCloud/Documents stall) is abandoned at
    process exit instead of keeping the whole app alive."""
    box = {}

    def worker():
        try:
            box["r"] = fn()
        except BaseException as e:  # surfaced to the caller below
            box["e"] = e

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return False, None
    if "e" in box:
        raise box["e"]
    return True, box.get("r")


def _scan_backup():
    """List course backup dir and stat each course FIT -> [(basename, path, key)], or
    None when it isn't a directory. This is the only part of build_cache that touches
    the (possibly iCloud-stalled) backup folder, so it runs under a timeout."""
    cbd = course_backup_dir()
    if not os.path.isdir(cbd):
        return None
    rows = []
    for f in sorted(os.listdir(cbd)):
        if not f.lower().endswith(".fit"):
            continue
        path = os.path.join(cbd, f)
        rows.append((f, path, _key(path)))
    return rows


def build_cache(refresh=False):
    """{basename: {key, name, ...trace}} for every backup FIT. Reuses entries whose
    (version, mtime, size) is unchanged; (re)parses only new/changed/stale files.
    If the backup folder stalls (iCloud), falls back to the on-disk cache so the app
    still renders whatever thumbs are already cached instead of hanging."""
    from build_watch_manifest import internal_name  # lazy: fitdecode already loaded
    cache = {}
    if not refresh and os.path.exists(CACHE):
        try:
            cache = json.load(open(CACHE))
        except Exception:
            cache = {}
    ok, listing = _call_with_timeout(_scan_backup, COURSE_SCAN_TIMEOUT)
    if not ok:
        sys.stderr.write(f"  route thumbs: scanning {course_backup_dir()} exceeded "
                         f"{COURSE_SCAN_TIMEOUT:g}s (filesystem stall?); using cached traces\n")
        return cache
    if listing is None:          # backup folder absent
        return cache
    out, dirty = {}, False
    for f, path, key in listing:
        hit = cache.get(f)
        if hit and hit.get("key") == key and "d" in hit:
            out[f] = hit
            continue
        dirty = True
        t = trace(path)
        base = {"key": key, "name": internal_name(path, "course", "name")}
        out[f] = {**base, **(t or {"vb": None, "d": None, "pts": 0})}
    if dirty or set(out) != set(cache):
        json.dump(out, open(CACHE, "w"))
    return out


def name_lookup(cache=None):
    """[(norm_internal_name, backup_fit_basename, thumb)] for rows to match
    against with keys_match. Entries without a drawable trace are included so
    callers can still use the backup FIT basename as the route's stable key."""
    cache = cache if cache is not None else build_cache()
    return [(_norm(e["name"]), fname, _public(e)) for fname, e in cache.items()
            if e.get("name")]


# ---- Connect geoPoints traces (id-keyed) --------------------------------------

def connect_load():
    if os.path.exists(CONNECT_CACHE):
        try:
            return json.load(open(CONNECT_CACHE))
        except Exception:
            pass
    return {}


def connect_save(cache):
    json.dump(cache, open(CONNECT_CACHE, "w"))


def connect_fresh(entry):
    """Usable only if built at the current geometry version (else refetch/rebuild)."""
    return bool(entry) and entry.get("ver") == GEOM_VER


def connect_store(cache, cid, payload):
    """Build + cache a trace from a Connect course-service payload. Stores a
    versioned no-trace marker for a trackless course so we don't refetch it."""
    track, meta = _u_track_geopoints(payload or {})
    t = trace_from_track(track, meta) or {"vb": None, "d": None, "pts": 0}
    cache[str(cid)] = {"ver": GEOM_VER, **t}
    return cache[str(cid)]


def connect_lookup(cache=None):
    """{str(id): thumb} for id-matched rows; only current-version real paths."""
    cache = cache if cache is not None else connect_load()
    return {cid: _public(e) for cid, e in cache.items()
            if connect_fresh(e) and e.get("d")}


if __name__ == "__main__":
    c = build_cache(refresh="--refresh" in sys.argv)
    ok = [e for e in c.values() if e.get("d")]
    print(f"{len(c)} FITs cached, {len(ok)} with a trace, {len(c) - len(ok)} without GPS")
    for f, e in list(c.items()):
        if e.get("d") and "Toppa" in (e.get("name") or ""):
            print(f"  {e['name']!r}: {e['pts']} pts, {e['dist_m']/1000:.1f}km "
                  f"+{e['asc_m']}/-{e['desc_m']}m, {len(e['km'])} km-marks, "
                  f"prof={'yes' if e['prof'] else 'no'}")
