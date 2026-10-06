#!/usr/bin/env python3
"""route_geo.py — the geo helpers behind "filter routes by start place".

Two jobs, both keyless and done HERE in Python (never the webview): the app's CSP allows only
IPC, so every outbound HTTP call goes through the engine, exactly like the wind forecast.

  geocode(query)  a place name -> a few candidate points (open-meteo geocoding, keyless)
  here()          the user's present position, APPROXIMATE, from the network's IP (city-level).

Present-position honesty: IP geolocation resolves to the ISP's routing city, not a GPS fix — it
can be tens of km off (tested: a Cambridge user resolved to Watford). It is a convenience anchor,
clearly labelled approximate. The precise anchors are a searched place and the user's own saved
Places (exact lat/lon, already in the snapshot). A true GPS fix would need macOS CoreLocation
(entitlement + prompt) — deferred; not worth a native helper for v1.
"""
import json
import urllib.parse
import urllib.request

GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
# Keyless IP-geolocation providers, tried in order (first success wins). Low volume (one call per
# button press), so free tiers are fine; both return city-level lat/lon.
IP_PROVIDERS = (
    ("https://ipapi.co/json/", lambda d: (d.get("latitude"), d.get("longitude"),
                                          d.get("city"), d.get("region"), d.get("country_code"))),
    ("http://ip-api.com/json/", lambda d: (d.get("lat"), d.get("lon"),
                                           d.get("city"), d.get("regionName"), d.get("countryCode"))),
)
UA = {"User-Agent": "GarminBridge"}


def _get_json(url, timeout=12):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def geocode(query, count=6):
    """A place name -> [{name, admin, country, lat, lon, label}]. `label` is a ready-to-show
    'Cambridge, England, GB' string. Empty list if nothing matches."""
    query = (query or "").strip()
    if not query:
        return []
    q = urllib.parse.urlencode({"name": query, "count": count, "language": "en", "format": "json"})
    try:
        data = _get_json(f"{GEOCODE}?{q}")
    except Exception:
        return []
    out = []
    for r in data.get("results") or []:
        if r.get("latitude") is None or r.get("longitude") is None:
            continue
        admin = r.get("admin1") or r.get("admin2") or ""
        cc = r.get("country_code") or r.get("country") or ""
        label = ", ".join(p for p in (r.get("name"), admin, cc) if p)
        out.append({"name": r.get("name"), "admin": admin, "country": cc,
                    "lat": round(r["latitude"], 5), "lon": round(r["longitude"], 5),
                    "label": label})
    return out


def here():
    """Present position from the network IP (APPROXIMATE). Returns
    {ok, lat, lon, label, approximate:True} or {ok:False, message}.

    TODO(gps): replace with a TRUE GPS/CoreLocation fix. IP geolocation is only city-level
    (tested: a Cambridge user resolved to Watford, ~40 km off) — useless for a tight start-place
    radius. A real fix needs macOS CoreLocation via a tiny signed Swift/Obj-C helper (Info.plist
    NSLocationUsageDescription + a one-time permission prompt), surfaced through the Tauri shell
    and returned here instead of the IP lookup. Until then the UI must keep the result flagged
    `approximate` and steer the user to a searched place / saved place for precision. See the
    GarminBridge note "route usability" roadmap.
    """
    for url, pick in IP_PROVIDERS:
        try:
            data = _get_json(url, timeout=8)
        except Exception:
            continue
        lat, lon, city, region, cc = pick(data)
        if lat is None or lon is None:
            continue
        try:
            lat, lon = round(float(lat), 5), round(float(lon), 5)
        except (TypeError, ValueError):
            continue
        label = ", ".join(p for p in (city, region, cc) if p) or "your area"
        return {"ok": True, "lat": lat, "lon": lon, "label": label, "approximate": True}
    return {"ok": False, "message": "couldn't determine your location from the network"}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "here":
        print(json.dumps(here(), indent=2))
    else:
        print(json.dumps(geocode(" ".join(sys.argv[1:]) or "Cambridge"), indent=2))
