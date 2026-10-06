# SPEC — health archive read interface

Build a read/query interface for the local intraday health archive. The archive is currently
write-only: `engine/health_archive.py` can fill it (`backfill`, `rescue`) and describe it
(`status`, `verify`, `recount`) but nothing can read a day or a range back out.

Every fact below was measured against the live archive on 2026-07-22. Do not re-derive them and
do not assume the payloads look like anything other than what is written here.

---

## HARD CONSTRAINTS — violating any of these fails the task

1. **The archive is READ-ONLY to you.** `~/Documents/Garmin Bridge/Health/health.db` holds 70 MB
   of irreplaceable data. Every connection you open MUST use the read-only URI form:
   `sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)`. Never DROP, DELETE, ALTER, INSERT,
   UPDATE or VACUUM. Never add an index: `PRIMARY KEY (date, metric)` already indexes exactly
   this access pattern.
2. **Do not modify any existing function** in `health_archive.py`. `connect_db`, `count_points`,
   `store_day`, `fetch_day`, `cmd_backfill`, `cmd_rescue`, `cmd_status`, `cmd_verify`,
   `cmd_recount`, `daterange`, `stored_days` and the `METRICS` dict stay byte-identical. A
   LaunchAgent runs the backfill/rescue path daily and may fire while you work.
3. **Do not touch `prototype/` at all.** Not to read-and-edit, not to sync, not to copy. A
   near-identical copy of this module lives there and is the one the LaunchAgent executes. The
   divergence is known and deliberate for this task.
4. **Never run `engine/test_build.py`.** It uploads real workouts to a live Garmin account.
5. **No network calls.** This is a pure local-read feature. Do not import `garminconnect` at
   module top level (the existing code imports it inside functions on purpose; keep that).
6. **Capture exit codes directly.** Never read the exit status of a pipeline ending in `grep`,
   `head` or `tail` — that reports the filter's status. This has caused three false passes in
   this repo already. Run the command, then read `$?` or use `subprocess` and check `returncode`.
7. Do not stage or commit `CHANGES.log`.

---

## MEASURED FACTS ABOUT THE DATA

Schema (unchanged, do not migrate):
```sql
CREATE TABLE raw (
    date TEXT NOT NULL, metric TEXT NOT NULL, payload TEXT,
    points INTEGER NOT NULL DEFAULT 0, fetched_at TEXT NOT NULL,
    PRIMARY KEY (date, metric))
```
`payload` is the raw Garmin JSON for that (date, metric), stored verbatim.

Current contents: 2025-10-22 .. 2026-07-22, 273 stored dates across 274 calendar days,
665,655 points, 70.1 MB, 7 metrics.

### Performance — already measured, do NOT build a cache or a normalized points table

Parsing the entire 273-day archive: heart rate 70 ms, stress 83 ms, sleep (47.7 MB) 276 ms,
single day 0.09 ms. Parsing raw JSON at read time is fast enough by two orders of magnitude.
Any proposal to denormalize, cache, or add a points table is rejected in advance.

### Trap 1 — body battery's own row is a decoy

The `body_battery` metric row holds only ~6 points/day (1,638 total). The REAL body battery
series is ~480 points/day (126,636 total) and lives inside the **`stress`** payload under
`bodyBatteryValuesArray`. Source the `body_battery` series from the `stress` metric row.
Do not expose the 6-point series at all.

### Trap 2 — stress uses negative sentinels

`stressValuesArray` values of **-1 and -2 mean "no reading"**, not a stress level. They occur
20,511 and 6,164 times respectively (21% of all stress points). The payload's own
`stressChartYAxisOrigin: -1` confirms -1 is the chart floor. **Exclude values < 0 from every
aggregate** (min/max/avg/count). Keep them in `--points` output but as `null`, so a chart draws
a gap rather than a spike to the floor.

### Trap 3 — body battery rows carry a status column

Row shape is `[timestamp_ms, status, level, version]`, e.g. `[1784070000000, "MEASURED", 58, 3.0]`.
Status values across the archive: `MEASURED` 116,149 · `ADJUSTED` 7,061 · `MODELED` 2,772 ·
`RESET` 191 · `ADJUSTED_MODELED` 119 · `None` 344. Treat all non-null rows as valid points, but
**skip rows where status is null OR level is null**. Do not filter to MEASURED only.

### Trap 4 — timestamps are inconsistent WITHIN a single payload

In the same `sleep` payload: `sleepMovement[].startGMT` is an **ISO string**
(`"2026-07-14T21:18:00.0"`) while `sleepHeartRate[].startGMT` is **epoch milliseconds**
(`1784067480000`). `hrv.hrvReadings[].readingTimeGMT` is ISO. Normalize everything to epoch ms.
ISO strings have a `.0` fractional part and NO timezone suffix; they are GMT. Parse them as GMT.

### Trap 5 — use the descriptor lists, do not hardcode index 1

Each payload carries its own column order. Measured key names (note the two different shapes):
```
heart_rates.heartRateValueDescriptors          [{index,key}]                     key: timestamp | heartrate
stress.stressValueDescriptorsDTOList           [{index,key}]                     key: timestamp | stressLevel
stress.bodyBatteryValueDescriptorsDTOList      [{bodyBatteryValueDescriptorIndex,
                                                 bodyBatteryValueDescriptorKey}] key: timestamp | bodyBatteryStatus
                                                                                      | bodyBatteryLevel | bodyBatteryVersion
respiration.respirationValueDescriptorsDTOList [{index,key}]                     key: timestamp | respiration
spo2.spO2ValueDescriptorsDTOList               [{spo2ValueDescriptorIndex,
                                                 spo2ValueDescriptorKey}]        key: timestamp | spo2Reading
                                                                                      | singleReadingPlottable
```
Resolve the column index from the descriptor list when one is present; fall back to the
documented default index only if the descriptor list is missing. Handle both key-name shapes.

### Trap 6 — `get_body_battery` payloads are a list-of-one-dict

The `body_battery` metric's payload is `[{...}]`, not `{...}`. Other metrics are plain dicts.
`count_points` already handles this; your reader must too (unwrap a single-element list).

### Gaps are real and must be reportable

- **2025-10-30 is entirely absent** (no rows at all).
- 7 metric-rows have `points = 0`: `2025-10-28 sleep`, `2025-10-28 hrv`, `2025-10-29 spo2`,
  `2025-10-29 sleep`, `2025-10-29 hrv`, `2025-10-31 sleep`, `2025-10-31 hrv`.

Three distinct states, and the output must distinguish them — never conflate them into "no data":
- `stored` — row present, points > 0
- `husk` — row present, points == 0 (Garmin returned a summary, the intraday layer was gone)
- `absent` — no row for that (date, metric)

---

## WHAT TO BUILD

### A. `engine/health_archive.py` — add a read layer

Add a `SERIES` registry of **10 logical series** (not 7 metrics). Each entry declares its source
metric, the payload array path, and how to get (timestamp_ms, value) out of a row:

| series key | source metric | array key | timestamp | value |
|---|---|---|---|---|
| `hr` | heart_rates | `heartRateValues` | pair[ts_idx] | pair[val_idx] |
| `stress` | stress | `stressValuesArray` | pair[ts_idx] | pair[val_idx], `<0` → null |
| `body_battery` | **stress** | `bodyBatteryValuesArray` | descriptor `timestamp` | descriptor `bodyBatteryLevel` |
| `respiration` | respiration | `respirationValuesArray` | pair[ts_idx] | pair[val_idx] |
| `spo2` | spo2 | `spO2HourlyAverages` | pair[ts_idx] | pair[val_idx] |
| `sleep_movement` | sleep | `sleepMovement` | `startGMT` (ISO) | `activityLevel` |
| `sleep_hr` | sleep | `sleepHeartRate` | `startGMT` (epoch ms) | `value` |
| `sleep_stress` | sleep | `sleepStress` | `startGMT` (epoch ms) | `value` |
| `sleep_body_battery` | sleep | `sleepBodyBattery` | `startGMT` (epoch ms) | `value` |
| `hrv` | hrv | `hrvReadings` | `readingTimeGMT` (ISO) | `hrvValue` |

Each series also declares a `unit` string (`bpm`, `score`, `%`, `brpm`, `ms`, `level`) for the
consumer's benefit.

Then these functions, all read-only:

```python
def open_ro():                       # read-only URI connection to DB_PATH
def extract(payload, spec):          # -> list[(ts_ms, value_or_None)], sorted by ts
def summarize(pts):                  # -> {"n", "n_valid", "min", "max", "avg", "first_ts", "last_ts"}
def query_day(db, date, series=None, points=False)
def query_range(db, start, end, series=None, bucket=None)
```

**AGGREGATES ARE THE DEFAULT. Points are opt-in.** This is the single most important shape
decision: the three consumers (a webview chart, a CLI, an LLM reading a day) all choke on a raw
654-point dump by default. `query_day` returns per-series summaries; `points=True` adds the raw
series. `query_range` returns one summary row per (date, series); `bucket=N` downsamples the
concatenated range to N buckets returning `{t, min, max, avg}` per bucket so a chart can draw a
band. Never return raw points from `query_range` without an explicit bucket or point request.

`avg` must be computed over VALID points only (sentinels and nulls excluded) and rounded to 1
decimal. `n` is total rows, `n_valid` is rows that counted toward the aggregate. When
`n_valid == 0`, min/max/avg are `null`, not `0`.

**Day boundary: GMT.** A deliberate choice. Do not shift, trim, or localize timestamps.
Return them as stored. Note in the output envelope that timestamps are epoch ms in GMT, and that
a day's series may start at ~23:00 GMT the previous day because Garmin labels days locally.

### B. `health_archive.py query` subcommand

```
query --date YYYY-MM-DD [--series a,b] [--points] [--json]
query --from YYYY-MM-DD --to YYYY-MM-DD [--series a,b] [--bucket N] [--json]
```
Without `--json`, print a readable aligned table matching the house style of `cmd_status`
(that is the reference for tone and column layout). With `--json`, print one JSON object.
`--series` accepts a comma-separated subset of the 10 keys; default is all 10. Unknown series
key is an error with the valid list, exit 2.

Register it in `main()` alongside the existing subparsers. Follow the existing style exactly:
short flag help strings, `set_defaults(func=...)`, return an int.

### C. `engine/api.py` — one new command `health-query`

Follow the existing pattern precisely: a `cmd_health_query(args)` returning a dict, a subparser in
`main()`, and an entry in the dispatch dict at the bottom. It calls into `health_archive.py`,
it does NOT reimplement any parsing.

Flags: `--date`, `--from`/`--to`, `--series`, `--bucket`. **Deliberately no `--points` flag on
the api.py surface**: `api.py` prints its whole envelope as a single `json.dumps` line, and a
full-resolution range dump would be a multi-megabyte line. Full-resolution export belongs to the
`health_archive.py query --points` subcommand only. Put that reasoning in a comment so the next
person does not "fix" it by adding the flag.

Return `{"ok": True, ...}` on success and let the existing top-level handler catch exceptions.

---

## VERIFICATION — run these yourself and paste the real output

Use the repo's engine interpreter. Capture each exit code directly, never through a pipe.

1. `query --date 2026-07-15 --json` — assert exactly these point counts, which were measured
   from the live archive today:
   `hr` 654 · `stress` 480 · `body_battery` 480 · `respiration` 720 · `spo2` 12 ·
   `sleep_movement` 625 · `sleep_hr` 253 · `sleep_stress` 170 · `sleep_body_battery` 170 ·
   `hrv` 101.
   If `body_battery` comes back 6, you sourced it from the wrong metric — re-read Trap 1.
2. `query --date 2026-07-15` (human) — renders a readable table, exit 0.
3. `query --from 2025-10-28 --to 2025-10-31 --json` — must report `2025-10-30` as **absent**
   and the seven listed metric-rows as **husk**, distinctly. This range is the gap fixture.
4. `query --date 2026-07-15 --series stress --json` — assert the stress `avg` is computed with
   negatives excluded, and that `n_valid < n`. State both numbers in your report.
5. `query --from 2025-10-22 --to 2026-07-22 --series hr --bucket 200 --json` — full-archive
   range, returns 200 buckets, and time it. Report the wall time.
6. `api.py health-query --date 2026-07-15` — exactly one line of valid JSON on stdout, exit 0.
   Verify it parses with `json.loads`.
7. Prove you did not write to the archive: report `md5` of `health.db` before and after your
   entire session. They must match.
8. Run the offline test suite. Baseline is 9 pass, 1 known host-bound failure
   (`test_locations.py`), 1 excluded. Do not run `test_build.py`. Report the actual counts.

## REPORT BACK

Conclusion first. Then: the verification output for all 8 checks with real numbers, the list of
changed paths, and anything in this spec you found to be wrong. If a measured fact above does not
match what you observe, STOP and report the discrepancy rather than coding around it — the facts
were measured today and a mismatch means something changed underneath us.
