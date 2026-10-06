#!/usr/bin/env python3
"""health_archive.py — local custody of the intraday health data Garmin stops serving.

Not "destroys": Garmin keeps it, and request_reload can pull a past day back out of
cold storage. But rehydration is rate-limited to roughly 30 days per day and it is
TEMPORARY. Measured 2026-07-26 against a year of stored days: 2026-01-05, 2026-01-15
and 2026-02-10 had each been rescued successfully a week earlier and were already
unreadable from the live API again, while days on either side of them stayed warm.

So the value here is not saving a round trip. Without a local copy, a year of
full-resolution intraday is not something you can sit down and read: you would be
requesting it back 30 days at a time while the far end goes cold behind you.

Garmin keeps daily summaries ~forever but archives the INTRADAY series (HR every
few seconds, stress, respiration, SpO2, sleep detail) into cold storage, after
which normal reads return summary-only. Measured on this account 2026-07-17: the
boundary is a HARD CALENDAR-MONTH CLIFF, not the documented rolling six months --
2026-02-28 returned 0 intraday points, 2026-03-01 returned 720. Every intraday
series dies on the same date, so this is one monthly archival job server-side.

This module reads live months and stores them locally before that happens.

Design notes:
  - Stores the RAW payload per (date, metric). Lossless and future-proof: the
    chart view's schema is not designed yet, and guessing it now would bake in
    assumptions we would have to migrate. Derive views from raw later.
  - SQLite, not a time-series DB. ~1100 days x ~700 pts x ~5 metrics ~= 4M rows;
    SQLite is unbothered by that. InfluxDB would be a distributed-scale answer to
    a laptop-scale problem.
  - Idempotent + resumable: a day already stored with points is skipped, so an
    interrupted backfill is restarted by re-running the same command.
  - Rate limited by default. garmin-grafana waits 5s/day as a courtesy to Garmin's
    servers; 2s is the default here, --sleep tunes it.

Reads only. Never writes to Garmin.
"""
import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
import paths

HERE = os.path.dirname(os.path.abspath(__file__))
TOKENSTORE = paths.state(".tokens")
# Deliberately FIXED, and deliberately not following GARMIN_BRIDGE_ROOT the way the
# Courses, Locations and route-tag paths do. Those name a backup destination; this
# names a live database holding years of intraday data that Garmin stops serving
# after about a week. It can in principle be re-requested, but only ~30 days per
# day and only until it goes cold again, so losing track of this file costs weeks
# of grinding to rebuild and may not fully succeed.
#
# Making it follow the root would relocate an existing archive with no migration:
# reads would find nothing, the daily job would quietly start a second empty
# database at the new location, and the original would still be on disk but
# invisible to both. A configured root can also sit inside a synced folder, and
# iCloud syncing a live SQLite file risks corrupting it.
#
# Move this only together with a migration that copies the DB first and verifies
# the row count at the destination.
ARCHIVE_DIR = os.path.expanduser("~/Documents/Garmin Bridge/Health")
DB_PATH = os.path.join(ARCHIVE_DIR, "health.db")

# metric -> (client method, key holding the intraday array)
# The array key is what proves we captured the high-resolution layer rather than
# the summary husk the API returns for archived dates. Every metric needs a real
# key: without one the point count reads 0 and the status display implies the
# metric was not captured when its payload is in fact stored in full.
METRICS = {
    "heart_rates": ("get_heart_rates", "heartRateValues"),
    "stress": ("get_stress_data", "stressValuesArray"),
    "body_battery": ("get_body_battery", "bodyBatteryValuesArray"),
    "respiration": ("get_respiration_data", "respirationValuesArray"),
    "spo2": ("get_spo2_data", "spO2HourlyAverages"),
    "sleep": ("get_sleep_data", "sleepMovement"),
    "hrv": ("get_hrv_data", "hrvReadings"),
}


# Logical read series. These intentionally do not mirror METRICS: body battery's
# high-resolution series is embedded in the stress payload, while the separate
# body_battery metric is only a low-resolution decoy.
SERIES = {
    "hr": {
        "source_metric": "heart_rates", "array_key": "heartRateValues",
        "kind": "descriptor", "descriptors_key": "heartRateValueDescriptors",
        "descriptor_index_key": "index", "descriptor_name_key": "key",
        "timestamp_key": "timestamp", "value_key": "heartrate",
        "default_timestamp_index": 0, "default_value_index": 1, "unit": "bpm",
    },
    "stress": {
        "source_metric": "stress", "array_key": "stressValuesArray",
        "kind": "descriptor", "descriptors_key": "stressValueDescriptorsDTOList",
        "descriptor_index_key": "index", "descriptor_name_key": "key",
        "timestamp_key": "timestamp", "value_key": "stressLevel",
        "default_timestamp_index": 0, "default_value_index": 1,
        "negative_is_null": True, "unit": "score",
    },
    "body_battery": {
        "source_metric": "stress", "array_key": "bodyBatteryValuesArray",
        "kind": "body_battery", "descriptors_key": "bodyBatteryValueDescriptorsDTOList",
        "descriptor_index_key": "bodyBatteryValueDescriptorIndex",
        "descriptor_name_key": "bodyBatteryValueDescriptorKey",
        "timestamp_key": "timestamp", "status_key": "bodyBatteryStatus",
        "value_key": "bodyBatteryLevel", "default_timestamp_index": 0,
        "default_status_index": 1, "default_value_index": 2, "unit": "score",
    },
    "respiration": {
        "source_metric": "respiration", "array_key": "respirationValuesArray",
        "kind": "descriptor", "descriptors_key": "respirationValueDescriptorsDTOList",
        "descriptor_index_key": "index", "descriptor_name_key": "key",
        "timestamp_key": "timestamp", "value_key": "respiration",
        "default_timestamp_index": 0, "default_value_index": 1,
        "negative_is_null": True, "unit": "brpm",
    },
    "spo2": {
        "source_metric": "spo2", "array_key": "spO2HourlyAverages",  # gitleaks:allow (Garmin API field name)
        "kind": "descriptor", "descriptors_key": "spO2ValueDescriptorsDTOList",
        "descriptor_index_key": "spo2ValueDescriptorIndex",
        "descriptor_name_key": "spo2ValueDescriptorKey",
        "timestamp_key": "timestamp", "value_key": "spo2Reading",
        "default_timestamp_index": 0, "default_value_index": 1, "unit": "%",
    },
    "sleep_movement": {
        "source_metric": "sleep", "array_key": "sleepMovement", "kind": "fields",
        "timestamp_key": "startGMT", "value_key": "activityLevel", "unit": "level",
    },
    "sleep_hr": {
        "source_metric": "sleep", "array_key": "sleepHeartRate", "kind": "fields",
        "timestamp_key": "startGMT", "value_key": "value", "unit": "bpm",
    },
    "sleep_stress": {
        "source_metric": "sleep", "array_key": "sleepStress", "kind": "fields",
        "timestamp_key": "startGMT", "value_key": "value",
        "negative_is_null": True, "unit": "score",
    },
    "sleep_body_battery": {
        "source_metric": "sleep", "array_key": "sleepBodyBattery", "kind": "fields",
        "timestamp_key": "startGMT", "value_key": "value", "unit": "score",
    },
    "hrv": {
        "source_metric": "hrv", "array_key": "hrvReadings", "kind": "fields",
        "timestamp_key": "readingTimeGMT", "value_key": "hrvValue", "unit": "ms",
    },
}


TIMESTAMP_NOTE = (
    "epoch ms in GMT; a day's series may start at about 23:00 GMT on the previous "
    "day because Garmin labels days locally"
)


def open_ro():
    """Open the irreplaceable archive without any write capability."""
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def _unwrap_payload(payload):
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        return payload[0]
    return payload


def _timestamp_ms(value):
    """Normalize either timestamp form Garmin uses to epoch milliseconds.

    Garmin writes one-digit fractional seconds ("2026-07-14T22:22:05.0"), which
    fromisoformat only accepts from Python 3.11. macOS still ships 3.9 at
    /usr/bin/python3, and this module is stdlib-only precisely so it can run
    under any interpreter, so pad the fraction rather than assume a modern one.
    Without this the hrv and sleep_movement series raise and take the whole
    query down with them, not just their own rows.
    """
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    text = re.sub(r"\.(\d{1,6})(?=$|[+-])",
                  lambda m: "." + m.group(1).ljust(6, "0"), text)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp() * 1000)


def _descriptor_index(payload, spec, key, default):
    """Use a payload descriptor when supplied; defaults apply only when absent."""
    descriptors = payload.get(spec["descriptors_key"])
    if descriptors is None:
        return default
    for descriptor in descriptors:
        if descriptor.get(spec["descriptor_name_key"]) == key:
            return descriptor.get(spec["descriptor_index_key"])
    return None


def _sentinel_to_null(spec, value):
    """Blank the negative "no reading" markers Garmin mixes into some series.

    Measured over the whole archive: stress carries -1/-2 for 21% of its points,
    respiration for 16.7%, sleepStress for 1%. They are not low readings, and
    averaging them in is wrong by ~7 stress points. Proof they are sentinels:
    excluding them puts our daily stress average within 1.0 of Garmin's own
    avgStressLevel on 266 of 273 days, against 8 of 273 if they are kept.
    Held here rather than at each call site so a new series cannot miss it.
    """
    if value is None:
        return None
    if spec.get("negative_is_null") and value < 0:
        return None
    return value


def extract(payload, spec):
    """Extract and sort one logical series as (epoch_ms, value-or-None) pairs."""
    payload = _unwrap_payload(payload)
    if not isinstance(payload, dict):
        return []
    rows = payload.get(spec["array_key"])
    if not isinstance(rows, list):
        return []

    points = []
    if spec["kind"] in ("descriptor", "body_battery"):
        ts_index = _descriptor_index(payload, spec, spec["timestamp_key"],
                                     spec["default_timestamp_index"])
        value_index = _descriptor_index(payload, spec, spec["value_key"],
                                        spec["default_value_index"])
        status_index = None
        if spec["kind"] == "body_battery":
            status_index = _descriptor_index(payload, spec, spec["status_key"],
                                             spec["default_status_index"])
        if ts_index is None or value_index is None or status_index is None and \
                spec["kind"] == "body_battery":
            return []
        for row in rows:
            if not isinstance(row, (list, tuple)):
                continue
            try:
                timestamp = _timestamp_ms(row[ts_index])
                value = row[value_index]
                status = row[status_index] if status_index is not None else True
            except IndexError:
                continue
            if timestamp is None:
                continue
            if spec["kind"] == "body_battery" and (status is None or value is None):
                continue
            points.append((timestamp, _sentinel_to_null(spec, value)))
    else:
        for row in rows:
            if not isinstance(row, dict):
                continue
            timestamp = _timestamp_ms(row.get(spec["timestamp_key"]))
            if timestamp is None:
                continue
            points.append(
                (timestamp, _sentinel_to_null(spec, row.get(spec["value_key"]))))
    return sorted(points, key=lambda point: point[0])


def summarize(pts):
    """Summarize extracted points while excluding null values from aggregates."""
    valid = [value for _, value in pts if value is not None]
    return {
        "n": len(pts),
        "n_valid": len(valid),
        "min": min(valid) if valid else None,
        "max": max(valid) if valid else None,
        "avg": round(sum(valid) / len(valid), 1) if valid else None,
        "first_ts": pts[0][0] if pts else None,
        "last_ts": pts[-1][0] if pts else None,
    }


def _selected_series(series):
    if series is None:
        return list(SERIES)
    if isinstance(series, str):
        series = [item.strip() for item in series.split(",") if item.strip()]
    unknown = [item for item in series if item not in SERIES]
    if unknown:
        raise ValueError(
            f"unknown series: {', '.join(unknown)}; valid: {', '.join(SERIES)}")
    return series


def _source_rows(db, start, end, selected):
    metrics = sorted({SERIES[name]["source_metric"] for name in selected})
    placeholders = ",".join("?" for _ in metrics)
    rows = db.execute(
        f"SELECT date, metric, payload, points FROM raw "
        f"WHERE date BETWEEN ? AND ? AND metric IN ({placeholders})",
        [start, end, *metrics],
    ).fetchall()
    return {(date, metric): (payload, points) for date, metric, payload, points in rows}


def _settled_dates(db, start, end):
    """Dates in the range whose capture happened on a LATER day than the day itself.

    A day fetched on its own date only holds the hours that had elapsed when the
    archiver ran, so it is partial by construction. This is the same test
    cmd_backfill uses to decide what still needs re-fetching, kept identical on
    purpose: a caller must not be told a day is complete that backfill is still
    planning to replace.
    """
    return {
        row[0] for row in db.execute(
            "SELECT DISTINCT date FROM raw "
            "WHERE date BETWEEN ? AND ? AND substr(fetched_at, 1, 10) > date",
            [start, end],
        ).fetchall()
    }


def _series_result(row, spec, points):
    if row is None:
        extracted = []
        state = "absent"
    else:
        payload, stored_points = row
        extracted = extract(json.loads(payload) if payload else None, spec)
        state = "stored" if stored_points > 0 else "husk"
    result = {"state": state, "unit": spec["unit"], "summary": summarize(extracted)}
    if points:
        result["points"] = [{"t": timestamp, "v": value} for timestamp, value in extracted]
    return result, extracted


def query_day(db, date, series=None, points=False):
    """Return summaries for one Garmin-labelled day, with raw points opt-in."""
    dt.date.fromisoformat(date)
    selected = _selected_series(series)
    source_rows = _source_rows(db, date, date, selected)
    result = {"date": date, "timestamps": TIMESTAMP_NOTE, "series": {}}
    for name in selected:
        spec = SERIES[name]
        entry, _ = _series_result(source_rows.get((date, spec["source_metric"])), spec, points)
        result["series"][name] = entry
    return result


def _bucket_points(points, count):
    if not points:
        return []
    first = points[0][0]
    last = points[-1][0]
    width = max(1, (last - first + 1) / count)
    buckets = [[] for _ in range(count)]
    for timestamp, value in points:
        index = min(count - 1, int((timestamp - first) / width))
        buckets[index].append(value)
    out = []
    for index, values in enumerate(buckets):
        valid = [value for value in values if value is not None]
        out.append({
            "t": int(first + index * width),
            "min": min(valid) if valid else None,
            "max": max(valid) if valid else None,
            "avg": round(sum(valid) / len(valid), 1) if valid else None,
        })
    return out


def query_range(db, start, end, series=None, bucket=None):
    """Return one summary row per (date, series), optionally with chart buckets."""
    start_date = dt.date.fromisoformat(start)
    end_date = dt.date.fromisoformat(end)
    if start_date > end_date:
        raise ValueError("--from must not be later than --to")
    if bucket is not None and bucket < 1:
        raise ValueError("--bucket must be at least 1")
    selected = _selected_series(series)
    source_rows = _source_rows(db, start, end, selected)
    settled = _settled_dates(db, start, end)
    rows = []
    all_points = {name: [] for name in selected}
    for day in daterange(start_date, end_date):
        date = day.isoformat()
        for name in selected:
            spec = SERIES[name]
            entry, extracted = _series_result(
                source_rows.get((date, spec["source_metric"])), spec, False)
            rows.append({"date": date, "series": name,
                         "settled": date in settled, **entry})
            all_points[name].extend(extracted)
    result = {"from": start, "to": end, "timestamps": TIMESTAMP_NOTE, "rows": rows}
    if bucket is not None:
        result["bucket"] = bucket
        result["buckets"] = {
            # Sort on the timestamp alone. A bare sorted() falls through to
            # comparing values when two points share a timestamp, and None < int
            # raises TypeError. The archive already holds 21 duplicate stress
            # timestamps and 1 in respiration, both series that produce nulls.
            name: _bucket_points(sorted(points, key=lambda point: point[0]), bucket)
            for name, points in all_points.items()
        }
    return result


def _fmt(value):
    """Display form. Raw floats like 5.904590479981305 would break the columns."""
    if value is None:
        return "-"
    return f"{value:.1f}" if isinstance(value, float) else str(value)


def _print_query_day(result):
    print(f"date      : {result['date']}")
    print(f"timestamps: {result['timestamps']}")
    print("\nper series:")
    print(f"  {'series':20} {'state':8} {'n':>6} {'valid':>6} {'min':>8} {'max':>8} {'avg':>8}")
    for name, entry in result["series"].items():
        summary = entry["summary"]
        print(f"  {name:20} {entry['state']:8} {summary['n']:>6} {summary['n_valid']:>6} "
              f"{_fmt(summary['min']):>8} {_fmt(summary['max']):>8} {_fmt(summary['avg']):>8}")


def _print_query_range(result):
    print(f"range     : {result['from']} .. {result['to']}")
    print(f"timestamps: {result['timestamps']}")
    print("\nper day / series:")
    print(f"  {'date':10} {'series':20} {'state':8} {'n':>6} {'valid':>6} {'min':>8} {'max':>8} {'avg':>8}")
    for row in result["rows"]:
        summary = row["summary"]
        print(f"  {row['date']:10} {row['series']:20} {row['state']:8} {summary['n']:>6} "
              f"{summary['n_valid']:>6} {_fmt(summary['min']):>8} "
              f"{_fmt(summary['max']):>8} {_fmt(summary['avg']):>8}")
    if "bucket" in result:
        print(f"\nbuckets   : {result['bucket']}")


def cmd_query(args):
    try:
        if args.date:
            if args.start or args.end or args.bucket is not None:
                raise ValueError("use --date or --from/--to, not both")
            with open_ro() as db:
                result = query_day(db, args.date, args.series, args.points)
            if args.as_json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                _print_query_day(result)
        else:
            if not args.start or not args.end:
                raise ValueError("query needs --date or both --from and --to")
            if args.points:
                raise ValueError("--points is available only with --date")
            with open_ro() as db:
                result = query_range(db, args.start, args.end, args.series, args.bucket)
            if args.as_json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                _print_query_range(result)
    except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as error:
        print(f"query: {error}", file=sys.stderr)
        return 2
    return 0


def connect_db():
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS raw (
        date TEXT NOT NULL, metric TEXT NOT NULL, payload TEXT,
        points INTEGER NOT NULL DEFAULT 0, fetched_at TEXT NOT NULL,
        PRIMARY KEY (date, metric))""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_raw_date ON raw(date)")
    # Rescue bookkeeping: which cold-storage dates we have already asked about.
    # Kept apart from `raw` because it records questions asked, not data held --
    # notably NO_FILES_FOUND, a permanent answer that must never be re-asked
    # (each retry would burn a slot of the ~30/day budget forever).
    db.execute("""CREATE TABLE IF NOT EXISTS rescue (
        date TEXT PRIMARY KEY, status TEXT NOT NULL, last_attempt TEXT NOT NULL)""")
    db.commit()
    return db


def count_points(payload, array_key):
    """Points in the intraday array. 0 means Garmin returned a summary husk.

    Handles both shapes the API uses: the array hanging off the top-level dict,
    and get_body_battery's list-of-one-dict wrapper.
    """
    if payload is None or not array_key:
        return 0
    if isinstance(payload, list):
        if not payload:
            return 0
        inner = payload[0]
        if isinstance(inner, dict):
            v = inner.get(array_key)
            return len(v) if isinstance(v, list) else 0
        return len(payload)
    if isinstance(payload, dict):
        v = payload.get(array_key)
        return len(v) if isinstance(v, list) else 0
    return 0


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def stored_days(db, require_settled=False):
    """Dates already captured with real intraday content.

    require_settled additionally demands the capture happened on a LATER
    calendar day than the one it covers. The daily job runs mid-morning, so it
    stores "today" holding only the hours elapsed so far, and because this
    function then reported that day as done it was never completed. Measured
    2026-07-23: 7 of 7 same-day captures were truncated to ~250 of ~700 points,
    against 4% of days captured later. Re-fetching them costs one day per run
    and self-heals, which is the right trade for data Garmin will not serve
    again. Only backfill asks for this; rescue walks backwards from the oldest
    date and never reaches today.
    """
    sql = "SELECT date FROM raw WHERE metric='heart_rates' AND points > 0"
    if require_settled:
        sql += " AND substr(fetched_at, 1, 10) > date"
    return {r[0] for r in db.execute(sql).fetchall()}


def fetch_day(g, day):
    out = {}
    for metric, (method, array_key) in METRICS.items():
        try:
            payload = getattr(g, method)(day)
            out[metric] = (payload, count_points(payload, array_key), None)
        except Exception as e:
            out[metric] = (None, 0, f"{type(e).__name__}: {e}"[:200])
    return out


def store_day(db, day, results):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for metric, (payload, points, err) in results.items():
        db.execute(
            "INSERT OR REPLACE INTO raw (date, metric, payload, points, fetched_at)"
            " VALUES (?,?,?,?,?)",
            (day, metric, json.dumps(payload) if payload is not None else None,
             points, now))
    db.commit()


def cmd_backfill(args):
    from garminconnect import Garmin
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today()
    db = connect_db()
    done = set() if args.force else stored_days(db, require_settled=True)

    days = [d.isoformat() for d in daterange(start, end)]
    todo = [d for d in days if d not in done]
    print(f"range {start} .. {end}  ({len(days)} days)")
    print(f"already archived: {len(days) - len(todo)}   to fetch: {len(todo)}")
    if not todo:
        print("nothing to do.")
        return 0
    est = len(todo) * (args.sleep + 1.5) / 60
    print(f"~{est:.0f} min at {args.sleep}s/day. Ctrl-C is safe: re-run to resume.\n")

    g = Garmin()
    g.login(TOKENSTORE)
    print(f"resumed as: {g.get_full_name()}\n")

    captured = husks = 0
    for i, day in enumerate(todo, 1):
        results = fetch_day(g, day)
        store_day(db, day, results)
        hr = results["heart_rates"][1]
        total = sum(p for _, p, _ in results.values())
        if hr > 0:
            captured += 1
            flag = "ok"
        else:
            husks += 1
            flag = "SUMMARY-ONLY (already cold)"
        print(f"[{i:>4}/{len(todo)}] {day}  hr={hr:>4}  all={total:>5}  {flag}")
        if i < len(todo):
            time.sleep(args.sleep)

    print(f"\ncaptured {captured} days with intraday; {husks} were already archived.")
    print(f"archive: {DB_PATH}")
    return 0


def cmd_status(args):
    if not os.path.exists(DB_PATH):
        print(f"no archive yet at {DB_PATH}")
        return 1
    db = connect_db()
    n_days, lo, hi = db.execute(
        "SELECT COUNT(DISTINCT date), MIN(date), MAX(date) FROM raw").fetchone()
    if not n_days:
        print("archive is empty.")
        return 1
    intraday = db.execute(
        "SELECT COUNT(*) FROM raw WHERE metric='heart_rates' AND points>0").fetchone()[0]
    pts = db.execute("SELECT SUM(points) FROM raw").fetchone()[0] or 0
    size = os.path.getsize(DB_PATH) / 1e6
    print(f"archive : {DB_PATH}")
    print(f"span    : {lo} .. {hi}  ({n_days} days)")
    print(f"intraday: {intraday} days captured at full resolution")
    print(f"points  : {pts:,} across all metrics")
    print(f"size    : {size:.1f} MB")
    print("\nper metric:")
    for metric, cnt, tot in db.execute(
            "SELECT metric, COUNT(*), SUM(points) FROM raw GROUP BY metric ORDER BY metric"):
        print(f"  {metric:14} {cnt:>5} days   {(tot or 0):>9,} pts")
    return 0


def cmd_verify(args):
    """Re-read a stored day and confirm it holds what the API returned."""
    from garminconnect import Garmin
    db = connect_db()
    day = args.date
    row = db.execute(
        "SELECT payload, points FROM raw WHERE date=? AND metric='heart_rates'",
        (day,)).fetchone()
    if not row:
        print(f"{day}: not in the archive.")
        return 1
    stored_payload, stored_points = json.loads(row[0]) if row[0] else None, row[1]
    g = Garmin()
    g.login(TOKENSTORE)
    live = g.get_heart_rates(day) or {}
    live_points = count_points(live, "heartRateValues")
    print(f"{day}\n  archived : {stored_points} pts\n  live API : {live_points} pts")
    ok = stored_points == live_points and stored_points > 0
    print(f"  => {'MATCH' if ok else 'MISMATCH'}")
    return 0 if ok else 1


def cmd_rescue(args):
    """Pull dates back out of Garmin's cold storage, oldest-first from the cliff.

    PROVEN on this account 2026-07-17: 2026-02-28 read 0 intraday points, one
    POST to request_reload returned status SUBMITTED, and within 10s the same
    date served 744 points. So archived history IS recoverable -- the "manual
    phone refresh" claim in garmin-grafana's README is stale.

    The constraint is a server-side budget of roughly 30 requests/day. Garmin
    answers DENIED once it is spent, which is not an error: it is the signal to
    stop and continue tomorrow. Rescued data also re-archives after ~7 days, so
    each date is stored immediately while warm rather than batched.
    """
    from garminconnect import Garmin
    db = connect_db()

    # Walk backwards from the oldest date we hold. Everything older is cold.
    oldest = db.execute("SELECT MIN(date) FROM raw").fetchone()[0]
    if not oldest:
        print("archive is empty -- run a backfill of the live months first.")
        return 1
    cursor = dt.date.fromisoformat(args.start) if args.start else \
        dt.date.fromisoformat(oldest) - dt.timedelta(days=1)
    floor = dt.date.fromisoformat(args.stop_at)

    have = stored_days(db)
    dead = {r[0] for r in db.execute(
        "SELECT date FROM rescue WHERE status='NO_FILES_FOUND'").fetchall()}

    g = Garmin()
    g.login(TOKENSTORE)
    print(f"resumed as: {g.get_full_name()}")
    print(f"rescuing backwards from {cursor} down to {floor}, budget {args.budget}\n")

    now = lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    used = rescued = empty = 0
    while used < args.budget and cursor >= floor:
        day = cursor.isoformat()
        cursor -= dt.timedelta(days=1)
        if day in have or day in dead:
            continue

        try:
            resp = g.request_reload(day) or {}
        except Exception as e:
            print(f"{day}  reload failed: {type(e).__name__}: {str(e)[:60]}")
            break
        status = (resp.get("status") or "UNKNOWN").upper()
        used += 1
        db.execute("INSERT OR REPLACE INTO rescue VALUES (?,?,?)", (day, status, now()))
        db.commit()

        if status == "DENIED":
            print(f"{day}  DENIED -- daily budget spent. Resuming tomorrow.")
            break
        if status == "NO_FILES_FOUND":
            empty += 1
            print(f"{day}  nothing to recover (no data that day)")
            continue

        time.sleep(args.settle)
        results = fetch_day(g, day)
        hr = results["heart_rates"][1]
        if hr > 0:
            store_day(db, day, results)
            db.execute("UPDATE rescue SET status='CAPTURED' WHERE date=?", (day,))
            db.commit()
            rescued += 1
            print(f"{day}  RESCUED  hr={hr:>4}  ({status})")
        else:
            print(f"{day}  {status} but still no intraday -- leaving for a retry")

    print(f"\nrescued {rescued} days; {empty} had nothing; {used} requests used.")
    remaining = db.execute(
        "SELECT COUNT(*) FROM rescue WHERE status='NO_FILES_FOUND'").fetchone()[0]
    print(f"archive now spans back to {db.execute('SELECT MIN(date) FROM raw').fetchone()[0]}"
          f"  ({remaining} dates known-empty)")
    return 0


def cmd_recount(args):
    """Re-derive point counts from stored payloads. No API calls, no refetch.

    Needed when a metric's array key is corrected after rows were already
    written: the payloads are intact, only the counts were wrong.
    """
    db = connect_db()
    rows = db.execute("SELECT date, metric, payload FROM raw").fetchall()
    changed = 0
    for date, metric, payload in rows:
        if metric not in METRICS or not payload:
            continue
        n = count_points(json.loads(payload), METRICS[metric][1])
        cur = db.execute(
            "SELECT points FROM raw WHERE date=? AND metric=?", (date, metric)).fetchone()[0]
        if n != cur:
            db.execute("UPDATE raw SET points=? WHERE date=? AND metric=?", (n, date, metric))
            changed += 1
    db.commit()
    print(f"recounted {len(rows)} rows; corrected {changed}.")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("recount", help="re-derive counts from stored payloads (no refetch)")
    r.set_defaults(func=cmd_recount)

    rs = sub.add_parser("rescue", help="pull archived dates back out of cold storage")
    rs.add_argument("--budget", type=int, default=25,
                    help="max reload requests this run (server allows ~30/day)")
    rs.add_argument("--stop-at", default="2019-01-01", dest="stop_at",
                    help="oldest date worth trying (YYYY-MM-DD)")
    rs.add_argument("--start", default=None,
                    help="start here instead of just below the oldest archived day")
    rs.add_argument("--settle", type=float, default=10.0,
                    help="seconds to wait after a reload before reading")
    rs.set_defaults(func=cmd_rescue)

    b = sub.add_parser("backfill", help="archive a date range (idempotent, resumable)")
    b.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    b.add_argument("--to", dest="end", default=None, help="YYYY-MM-DD (default: today)")
    b.add_argument("--sleep", type=float, default=2.0, help="seconds between days")
    b.add_argument("--force", action="store_true", help="re-fetch days already stored")
    b.set_defaults(func=cmd_backfill)

    s = sub.add_parser("status", help="what the archive currently holds")
    s.set_defaults(func=cmd_status)

    q = sub.add_parser("query", help="read archived health series")
    q.add_argument("--date", help="YYYY-MM-DD")
    q.add_argument("--from", dest="start", help="YYYY-MM-DD")
    q.add_argument("--to", dest="end", help="YYYY-MM-DD")
    q.add_argument("--series", help="comma-separated series keys")
    q.add_argument("--points", action="store_true", help="include points for one day")
    q.add_argument("--bucket", type=int, help="range chart bucket count")
    q.add_argument("--json", dest="as_json", action="store_true", help="print JSON")
    q.set_defaults(func=cmd_query)

    v = sub.add_parser("verify", help="re-read one stored day against the live API")
    v.add_argument("date", help="YYYY-MM-DD")
    v.set_defaults(func=cmd_verify)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
