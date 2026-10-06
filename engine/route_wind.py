#!/usr/bin/env python3
"""Directional wind along a route.

The forecast provider is intentionally small and swappable: compute_route_wind()
only needs per-sample hourly weather rows. Open-Meteo is the v1 provider.
"""
import argparse
import datetime as dt
import json
import math
import urllib.parse

import route_geo

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
DEFAULT_SPEED_KMH = 22.0
STEP_M = 400.0
SAMPLE_SPACING_M = 5000.0
RHO = 1.225       # kg/m3
CDA = 0.40        # relaxed road/gravel cyclist
MASS_KG = 85.0    # rider + bike
G = 9.80665


def _haversine(a, b):
    R = 6371000.0
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(min(1.0, h)))


def _bearing(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _ang_diff(a, b):
    return ((a - b + 180) % 360) - 180


def _parse_start(value):
    if not value:
        now = dt.datetime.now(dt.timezone.utc)
        return now.replace(minute=0, second=0, microsecond=0)
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    out = dt.datetime.fromisoformat(s)
    if out.tzinfo is None:
        out = out.replace(tzinfo=dt.timezone.utc)
    return out.astimezone(dt.timezone.utc)


def _clean_track(track):
    pts = []
    for p in track or []:
        if len(p) < 2 or p[0] is None or p[1] is None:
            continue
        q = (float(p[0]), float(p[1]), p[2] if len(p) > 2 else None)
        if not pts or (pts[-1][0], pts[-1][1]) != (q[0], q[1]):
            pts.append(q)
    if len(pts) < 2:
        raise ValueError("route has fewer than 2 GPS points")
    return pts


def _interp(a, b, f):
    ele = None
    if a[2] is not None and b[2] is not None:
        ele = a[2] + (b[2] - a[2]) * f
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, ele)


def resample_track(track, step_m=STEP_M):
    """Return [{lat, lon, dist_m}] at roughly step_m spacing, including both ends."""
    clean = _clean_track(track)
    cum = [0.0]
    for i in range(1, len(clean)):
        cum.append(cum[-1] + _haversine(clean[i - 1], clean[i]))
    total = cum[-1]
    if total <= 1:
        raise ValueError("route is too short for wind analysis")
    targets = [0.0]
    d = step_m
    while d < total:
        targets.append(d)
        d += step_m
    targets.append(total)

    out, j = [], 1
    for target in targets:
        while j < len(cum) - 1 and cum[j] < target:
            j += 1
        prev_i = max(0, j - 1)
        span = cum[j] - cum[prev_i]
        f = 0.0 if span <= 0 else (target - cum[prev_i]) / span
        p = _interp(clean[prev_i], clean[j], f)
        out.append({"lat": p[0], "lon": p[1], "dist_m": target})
    return out


def _sample_points(points, spacing_m=SAMPLE_SPACING_M):
    total = points[-1]["dist_m"]
    targets = {0.0, total / 2, total}
    d = spacing_m
    while d < total:
        targets.add(d)
        d += spacing_m
    rows = []
    used = set()
    for target in sorted(targets):
        idx = min(range(len(points)), key=lambda i: abs(points[i]["dist_m"] - target))
        if idx in used:
            continue
        used.add(idx)
        p = points[idx]
        rows.append({"idx": len(rows), "lat": p["lat"], "lon": p["lon"], "dist_m": p["dist_m"]})
    return rows


def _forecast_days(end_utc):
    today = dt.datetime.now(dt.timezone.utc).date()
    days = (end_utc.date() - today).days + 2
    return max(1, min(16, days))


def _dt_utc(value):
    return dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc)


class OpenMeteoProvider:
    def fetch(self, samples, start_utc, end_utc):
        if not samples:
            return {}
        q = urllib.parse.urlencode({
            "latitude": ",".join(f"{s['lat']:.4f}" for s in samples),
            "longitude": ",".join(f"{s['lon']:.4f}" for s in samples),
            "hourly": ("temperature_2m,apparent_temperature,weather_code,precipitation_probability,"
                       "precipitation,wind_speed_10m,wind_direction_10m,wind_gusts_10m"),
            "wind_speed_unit": "kmh",
            "timezone": "GMT",
            "forecast_days": _forecast_days(end_utc),
        })
        data = route_geo._get_json(f"{OPEN_METEO}?{q}", timeout=18)
        locs = data if isinstance(data, list) else [data]
        out = {}
        for sample, loc in zip(samples, locs):
            hourly = (loc or {}).get("hourly") or {}
            times = [_dt_utc(t) for t in (hourly.get("time") or [])]
            rows = []
            for i, when in enumerate(times):
                def at(key):
                    col = hourly.get(key) or []
                    return col[i] if i < len(col) else None
                rows.append({
                    "time": when,
                    "wind_speed": at("wind_speed_10m"),
                    "wind_from": at("wind_direction_10m"),
                    "gust": at("wind_gusts_10m"),
                    "temp": at("temperature_2m"),
                    "feels": at("apparent_temperature"),
                    "rain_prob": at("precipitation_probability"),
                    "rain_mm": at("precipitation"),
                    "code": at("weather_code"),
                })
            out[sample["idx"]] = rows
        return out


def _nearest_weather(series, when):
    rows = [r for r in (series or []) if r.get("wind_speed") is not None and r.get("wind_from") is not None]
    if not rows:
        return None
    return min(rows, key=lambda r: abs((r["time"] - when).total_seconds()))


def _weather_at(forecast, samples, dist_m, when):
    sample = min(samples, key=lambda s: abs(s["dist_m"] - dist_m))
    wx = _nearest_weather(forecast.get(sample["idx"]), when)
    if not wx:
        raise ValueError("forecast has no wind for the route time window")
    return wx


def _segment_kind(tail_kmh, cross_kmh):
    if abs(tail_kmh) < 1 and cross_kmh < 1:
        return "cross"
    if abs(tail_kmh) >= cross_kmh:
        return "tail" if tail_kmh > 0 else "head"
    return "cross"


def _segment_rows(points, samples, forecast, start_utc, speed_kmh):
    speed_mps = speed_kmh / 3.6
    base_air = speed_mps
    rows = []
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        dist = max(0.0, b["dist_m"] - a["dist_m"])
        if dist <= 0:
            continue
        mid_m = (a["dist_m"] + b["dist_m"]) / 2
        when = start_utc + dt.timedelta(seconds=mid_m / speed_mps)
        wx = _weather_at(forecast, samples, mid_m, when)
        route_bearing = _bearing((a["lat"], a["lon"]), (b["lat"], b["lon"]))
        wind_toward = (float(wx["wind_from"]) + 180) % 360
        alpha = math.radians(_ang_diff(wind_toward, route_bearing))
        wind_speed = float(wx["wind_speed"])
        tail = wind_speed * math.cos(alpha)
        cross = abs(wind_speed * math.sin(alpha))
        air_mps = max(0.0, speed_mps - tail / 3.6)
        extra_force = 0.5 * RHO * CDA * (air_mps ** 2 - base_air ** 2)
        rows.append({
            "from_m": a["dist_m"],
            "to_m": b["dist_m"],
            "mid_m": mid_m,
            "lat": (a["lat"] + b["lat"]) / 2,
            "lon": (a["lon"] + b["lon"]) / 2,
            "bearing": route_bearing,
            "wind_from": float(wx["wind_from"]),
            "wind_speed": wind_speed,
            "tail_kmh": tail,
            "head_kmh": max(0.0, -tail),
            "cross_kmh": cross,
            "gust_kmh": None if wx.get("gust") is None else float(wx["gust"]),
            "temp_c": wx.get("temp"),
            "feels_c": wx.get("feels"),
            "rain_prob": wx.get("rain_prob"),
            "rain_mm": wx.get("rain_mm"),
            "code": wx.get("code"),
            "extra_work_j": extra_force * dist,
            "kind": _segment_kind(tail, cross),
            "time": when.isoformat().replace("+00:00", "Z"),
        })
    return rows


def _weighted_mean(rows, key, weight="dist_m"):
    vals = [(r[key], r[weight]) for r in rows if r.get(key) is not None and r.get(weight, 0) > 0]
    total = sum(w for _, w in vals)
    return None if total <= 0 else sum(v * w for v, w in vals) / total


def _stretch(rows, predicate, metric_key, fallback_key=None):
    groups, cur = [], []
    for r in rows:
        if predicate(r):
            cur.append(r)
        elif cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    if not groups:
        key = fallback_key or metric_key
        best = max(rows, key=lambda r: r.get(key) or 0)
        groups = [[best]]

    def score(g):
        dist = sum(r["dist_m"] for r in g)
        avg = sum((r.get(metric_key) or 0) * r["dist_m"] for r in g) / max(1.0, dist)
        return avg * max(0.25, dist / 1000)

    best = max(groups, key=score)
    dist = sum(r["dist_m"] for r in best)
    mid = best[len(best) // 2]
    out = {
        "from_km": round(best[0]["from_m"] / 1000, 1),
        "to_km": round(best[-1]["to_m"] / 1000, 1),
        "distance_km": round(dist / 1000, 1),
        "lat": round(mid["lat"], 5),
        "lon": round(mid["lon"], 5),
    }
    out["avg_" + metric_key] = round(_weighted_mean([{**r, "dist_m": r["to_m"] - r["from_m"]} for r in best], metric_key) or 0, 1)
    if any(r.get("gust_kmh") is not None for r in best):
        out["max_gust_kmh"] = round(max(r.get("gust_kmh") or 0 for r in best))
    return out


def _bands(rows, total_m):
    out = []
    cur = None
    for r in rows:
        if not cur or cur["kind"] != r["kind"]:
            if cur:
                out.append(cur)
            cur = {"kind": r["kind"], "from": r["from_m"] / total_m, "to": r["to_m"] / total_m,
                   "tail_sum": r["tail_kmh"] * (r["to_m"] - r["from_m"]), "dist": r["to_m"] - r["from_m"]}
        else:
            cur["to"] = r["to_m"] / total_m
            cur["tail_sum"] += r["tail_kmh"] * (r["to_m"] - r["from_m"])
            cur["dist"] += r["to_m"] - r["from_m"]
    if cur:
        out.append(cur)
    return [{"kind": b["kind"], "from": round(b["from"], 4), "to": round(b["to"], 4),
             "avg_tail_kmh": round(b["tail_sum"] / max(1.0, b["dist"]), 1)} for b in out if b["to"] > b["from"]]


def _round_or_none(v, nd=0):
    if v is None:
        return None
    return round(v, nd)


def summarize(rows, total_m, speed_kmh, start_utc):
    weighted = [{**r, "dist_m": r["to_m"] - r["from_m"]} for r in rows]
    by_kind = {k: sum(r["dist_m"] for r in weighted if r["kind"] == k) for k in ("head", "tail", "cross")}
    net_tail = _weighted_mean(weighted, "tail_kmh") or 0.0
    climb = sum(r["extra_work_j"] for r in rows) / (MASS_KG * G)
    gusts = [r["gust_kmh"] for r in rows if r.get("gust_kmh") is not None]
    feels = [r["feels_c"] for r in rows if r.get("feels_c") is not None]
    temps = [r["temp_c"] for r in rows if r.get("temp_c") is not None]
    rain = [r for r in rows if r.get("rain_prob") is not None]
    rain_peak = max(rain, key=lambda r: r["rain_prob"]) if rain else None
    end_utc = start_utc + dt.timedelta(hours=(total_m / 1000) / speed_kmh)
    return {
        "distance_km": round(total_m / 1000, 1),
        "start_iso": start_utc.isoformat().replace("+00:00", "Z"),
        "end_iso": end_utc.isoformat().replace("+00:00", "Z"),
        "assumed_speed_kmh": round(speed_kmh, 1),
        "net_effective_kmh": round(net_tail, 1),
        "net_label": "tailwind" if net_tail > 0.5 else "headwind" if net_tail < -0.5 else "neutral",
        "pct": {k: round((v / total_m) * 100) for k, v in by_kind.items()},
        "wind_climb_m": round(climb),
        "avg_head_kmh": round(_weighted_mean(weighted, "head_kmh") or 0, 1),
        "avg_cross_kmh": round(_weighted_mean(weighted, "cross_kmh") or 0, 1),
        "max_gust_kmh": None if not gusts else round(max(gusts)),
        "weather": {
            "rain_probability_max": None if rain_peak is None else int(round(rain_peak["rain_prob"])),
            "rain_at_km": None if rain_peak is None else round(rain_peak["mid_m"] / 1000, 1),
            "rain_mm_at_peak": None if rain_peak is None or rain_peak.get("rain_mm") is None else round(rain_peak["rain_mm"], 1),
            "gust_max_kmh": None if not gusts else round(max(gusts)),
            "feels_min_c": None if not feels else round(min(feels)),
            "feels_max_c": None if not feels else round(max(feels)),
            "temp_min_c": None if not temps else round(min(temps)),
            "temp_max_c": None if not temps else round(max(temps)),
        },
        "worst_headwind": _stretch(weighted, lambda r: r["head_kmh"] >= 4, "head_kmh"),
        "worst_gust_cross": _stretch(weighted, lambda r: r["cross_kmh"] >= 8 or (r.get("gust_kmh") or 0) >= 30,
                                     "cross_kmh", fallback_key="gust_kmh"),
        "bands": _bands(rows, total_m),
        "source_note": "Open-Meteo grid wind, not lane-level shelter or exposure.",
        "model": {"step_m": STEP_M, "sample_spacing_m": SAMPLE_SPACING_M,
                  "mass_kg": MASS_KG, "cda": CDA, "rho": RHO},
    }


def compute_route_wind(track, start_iso=None, reverse=False, speed_kmh=DEFAULT_SPEED_KMH, provider=None):
    if speed_kmh <= 0:
        raise ValueError("speed_kmh must be positive")
    clean = _clean_track(track)
    if reverse:
        clean = list(reversed(clean))
    start_utc = _parse_start(start_iso)
    points = resample_track(clean)
    total_m = points[-1]["dist_m"]
    end_utc = start_utc + dt.timedelta(hours=(total_m / 1000) / speed_kmh)
    samples = _sample_points(points)
    forecast = (provider or OpenMeteoProvider()).fetch(samples, start_utc, end_utc)
    rows = _segment_rows(points, samples, forecast, start_utc, speed_kmh)
    summary = summarize(rows, total_m, speed_kmh, start_utc)
    summary["segments"] = [{
        "from_km": round(r["from_m"] / 1000, 2),
        "to_km": round(r["to_m"] / 1000, 2),
        "kind": r["kind"],
        "tail_kmh": round(r["tail_kmh"], 1),
        "cross_kmh": round(r["cross_kmh"], 1),
        "gust_kmh": None if r.get("gust_kmh") is None else round(r["gust_kmh"]),
    } for r in rows]
    return summary


class _ConstantProvider:
    def __init__(self, wind_from, speed, gust=None):
        self.wind_from = wind_from
        self.speed = speed
        self.gust = gust if gust is not None else speed

    def fetch(self, samples, start_utc, end_utc):
        rows = []
        t = start_utc.replace(minute=0, second=0, microsecond=0)
        while t <= end_utc + dt.timedelta(hours=1):
            rows.append({"time": t, "wind_speed": self.speed, "wind_from": self.wind_from,
                         "gust": self.gust, "temp": 12, "feels": 10,
                         "rain_prob": 20, "rain_mm": 0, "code": 1})
            t += dt.timedelta(hours=1)
        return {s["idx"]: rows for s in samples}


def self_test():
    # Cambridge-ish eastbound route. Wind FROM west blows east, so outbound is tailwind.
    track = [(52.2, 0.10, None), (52.2, 0.16, None)]
    tail = compute_route_wind(track, "2026-07-04T08:00:00Z", provider=_ConstantProvider(270, 20))
    head = compute_route_wind(track, "2026-07-04T08:00:00Z", reverse=True, provider=_ConstantProvider(270, 20))
    assert tail["net_effective_kmh"] > 18, tail["net_effective_kmh"]
    assert head["net_effective_kmh"] < -18, head["net_effective_kmh"]
    assert tail["pct"]["tail"] >= 90 and head["pct"]["head"] >= 90
    assert tail["wind_climb_m"] < head["wind_climb_m"]
    print("PASS route_wind self-test: vector decomposition, reverse direction, summary bands")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="GPX/FIT route file; omitted runs the offline self-test")
    ap.add_argument("--start-iso")
    ap.add_argument("--reverse", action="store_true")
    ap.add_argument("--speed-kmh", type=float, default=DEFAULT_SPEED_KMH)
    args = ap.parse_args()
    if not args.file:
        self_test()
        return
    import route_course
    track, _, _ = route_course.load_track(args.file)
    print(json.dumps(compute_route_wind(track, args.start_iso, args.reverse, args.speed_kmh), indent=2))


if __name__ == "__main__":
    main()
