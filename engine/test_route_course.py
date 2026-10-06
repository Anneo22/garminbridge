#!/usr/bin/env python3
"""Tests for route_course.py — GPX/FIT -> Garmin course FIT. Pure, no watch, no network."""
import collections
import os
import tempfile

import fitdecode

import route_course as RC

SAMPLE_GPX = """<?xml version="1.0"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Jesus Green Loop</name>
    <type>running</type>
    <trkseg>
      <trkpt lat="52.2100" lon="0.1200"><ele>10</ele></trkpt>
      <trkpt lat="52.2110" lon="0.1215"><ele>12</ele></trkpt>
      <trkpt lat="52.2120" lon="0.1210"><ele>15</ele></trkpt>
      <trkpt lat="52.2112" lon="0.1195"><ele>13</ele></trkpt>
      <trkpt lat="52.2100" lon="0.1200"><ele>10</ele></trkpt>
    </trkseg>
  </trk>
</gpx>
"""


def _decode(path):
    counts, first = collections.Counter(), {}
    with fitdecode.FitReader(open(path, "rb")) as fr:
        for m in fr:
            if isinstance(m, fitdecode.FitDataMessage):
                counts[m.name] += 1
                first.setdefault(m.name, {f.name: f.value for f in m.fields})
    return counts, first


def test_gpx_import_builds_valid_course():
    d = tempfile.mkdtemp(prefix="gb_rc_")
    gpx = os.path.join(d, "loop.gpx")
    open(gpx, "w").write(SAMPLE_GPX)
    res = RC.import_route(gpx)
    assert res["ok"] and res["n_points"] == 5
    assert res["name"] == "Jesus Green Loop"     # picked up <name>
    assert res["sport"] == "running"             # picked up <type>
    assert 300 < res["dist_m"] < 2000            # a small loop, sane distance
    assert abs(res["slat"] - 52.2100) < 1e-3 and abs(res["slon"] - 0.1200) < 1e-3

    counts, first = _decode(res["path"])
    assert counts["file_id"] == 1 and counts["course"] == 1
    assert counts["lap"] == 1 and counts["event"] == 2 and counts["record"] == 5
    assert first["file_id"]["type"] == "course"
    assert first["file_id"]["manufacturer"] == "garmin"
    assert first["course"]["name"] == "Jesus Green Loop"
    assert first["course"]["sport"] == "running"
    # positions round-trip through semicircles back to ~the input degrees
    from route_thumb import SEMI
    assert abs(first["record"]["position_lat"] * SEMI - 52.2100) < 1e-3
    # distance is cumulative: the first record is 0, the last is the total
    assert first["record"]["distance"] == 0.0


def test_fit_roundtrip_and_sport_override():
    d = tempfile.mkdtemp(prefix="gb_rc_")
    gpx = os.path.join(d, "loop.gpx")
    open(gpx, "w").write(SAMPLE_GPX)
    fit1 = RC.import_route(gpx, name="First Cut", sport="cycling")["path"]
    assert os.path.exists(fit1)
    # re-import the built FIT: the track survives a GPX->FIT->FIT round-trip
    track, name, sport = RC.parse_fit(fit1)
    assert len(track) == 5 and name == "First Cut" and sport == "cycling"


def test_short_track_rejected():
    d = tempfile.mkdtemp(prefix="gb_rc_")
    dst = os.path.join(d, "x.fit")
    try:
        RC.build_course_fit([(1.0, 2.0, None)], "One point", "cycling", dst)
    except ValueError as e:
        assert "2 GPS points" in str(e)
    else:
        raise AssertionError("a 1-point track should be rejected")


def test_unsupported_extension():
    try:
        RC.load_track("/tmp/route.tcx")
    except ValueError as e:
        assert "unsupported" in str(e)
    else:
        raise AssertionError("a .tcx should be rejected")


if __name__ == "__main__":
    test_gpx_import_builds_valid_course()
    test_fit_roundtrip_and_sport_override()
    test_short_track_rejected()
    test_unsupported_extension()
    print("all route_course tests passed")
