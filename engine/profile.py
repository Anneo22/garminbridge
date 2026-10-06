#!/usr/bin/env python3
"""Athlete thresholds — turns "% of threshold" workout intensities into the
absolute pace/power Garmin needs. Values are fetched live from Garmin Connect
(the user's own auto-detected thresholds), with verified defaults as a fallback.

Fetched + cross-checked from Connect 2026-06-27:
  FTP (cycling threshold power) = 236 W   (auto-detected, unambiguous)
  threshold pace               = 4:00/km = 4.167 m/s
      (Connect lactateThresholdSpeed=0.41666 is scaled ×10; cross-checked vs
       recent runs: fastest 3:45/km, easy 5:30-5:55/km -> 4:00/km threshold is sane)
  threshold HR                 = 178 bpm
  running threshold POWER       = unknown (set if a Stryd/Garmin run-power FTP exists)
"""
DEFAULTS = {
    "ftp_w": 236,                   # cycling threshold power
    "threshold_speed_mps": 4.167,   # 4:00/km
    "threshold_hr": 178,
    "run_power_threshold_w": 355,   # running threshold power (Connect, 5.88 W/kg)
}


def fetch(garmin):
    """Pull live thresholds from Connect; fall back to DEFAULTS per field."""
    p = dict(DEFAULTS)
    for sport, key in (("CYCLING", "ftp_w"), ("RUNNING", "run_power_threshold_w")):
        try:
            r = garmin.connectapi(
                f"/biometric-service/biometric/latestFunctionalThresholdPower/{sport}")
            if r and r.get("functionalThresholdPower"):
                p[key] = int(r["functionalThresholdPower"])
        except Exception:
            pass
    try:
        us = garmin.connectapi("/userprofile-service/userprofile/user-settings")
        ud = (us or {}).get("userData", {})
        sp = ud.get("lactateThresholdSpeed")
        if sp:
            p["threshold_speed_mps"] = round(sp * 10, 3)   # API value is ×10 low
        if ud.get("lactateThresholdHeartRate"):
            p["threshold_hr"] = int(ud["lactateThresholdHeartRate"])
    except Exception:
        pass
    return p


def pace_mps(pct, prof=None):
    """% of threshold speed -> m/s (Garmin pace target unit)."""
    return round((prof or DEFAULTS)["threshold_speed_mps"] * pct / 100.0, 3)


def power_w(pct, prof=None):
    """% FTP -> watts."""
    return round((prof or DEFAULTS)["ftp_w"] * pct / 100.0)


def run_power_w(pct, prof=None):
    thr = (prof or DEFAULTS)["run_power_threshold_w"]
    if not thr:
        raise ValueError("no running threshold power set; give me your run FTP to use run_power_pct")
    return round(thr * pct / 100.0)


def pace_str(mps):
    s = 1000.0 / mps
    return f"{int(s)//60}:{int(s) % 60:02d}/km"
