#!/bin/bash
# health-archive-daily.sh — keep the intraday archive current.
#
# Garmin archives intraday data in whole calendar months (measured 2026-07-17:
# 2026-02-28 = 0 points, 2026-03-01 = 720). Once a month falls off, the
# high-resolution layer is gone from normal reads. This runs daily so the
# archive is always ahead of that cliff.
#
# Looks back 7 days rather than 1 on purpose: backfill is idempotent and skips
# days already stored, so a week's window self-heals any gap left by the Mac
# being asleep, offline, or off. A normal run fetches one day (~3s).
#
# Moved here from prototype/connect/ on 2026-07-23. That directory is gitignored
# in full, so the job that maintains ~300 days of irreplaceable data was running
# from a file that could never be reviewed, tested or shipped, while every
# improvement landed in engine/. Same logic, now on the tracked copy and on the
# bundled runtime the rest of the engine already uses.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="$ROOT/vendor/python/bin/python3"
ARCHIVE="$ROOT/engine/health_archive.py"
export PYTHONPATH="$ROOT/vendor/site-packages"
FROM="$(date -v-7d +%Y-%m-%d)"

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) health-archive-daily (from $FROM) ==="

if [ ! -x "$PY" ]; then
  echo "FATAL: bundled interpreter missing at $PY" >&2
  echo "       run scripts/fetch-runtime.sh to rebuild it" >&2
  exit 1
fi
if [ ! -f "$ARCHIVE" ]; then
  echo "FATAL: archiver missing at $ARCHIVE" >&2
  exit 1
fi

"$PY" "$ARCHIVE" backfill --from "$FROM" --sleep 2
rc=$?

if [ $rc -ne 0 ]; then
  # Most likely cause is an expired Garmin session: the token store needs a
  # human login, which a background job must never attempt silently.
  echo "FAILED rc=$rc — if this persists, the Garmin session likely expired; re-login." >&2
  exit $rc
fi

# Then spend the day's cold-storage budget walking backwards through the years
# Garmin already archived. Garmin allows ~30 reload requests/day and answers
# DENIED past that, which rescue treats as "stop, resume tomorrow" rather than
# an error — so this is a slow grind by design, not a job that can be hurried.
# 25 leaves headroom for the ad-hoc reload of a specific day.
echo ""
echo "--- cold-storage rescue ---"
"$PY" "$ARCHIVE" rescue --budget 25 || echo "(rescue stopped early; not fatal)"

echo ""
"$PY" "$ARCHIVE" status | sed -n '2,4p'
echo "=== done ==="
