#!/usr/bin/env bash
# backup-activities.sh — copy Garmin activity .fit files to the Mac.
#
# COPY-ONLY by design: it never deletes anything from the watch. Your activities also
# sync to Garmin Connect, and the watch manages its own storage; this just keeps a local
# copy of the raw .fit files (training data you own, readable by any FIT tool).
#
# It reuses the voice importer's proven USB/MTP engine (contention handling, poll-then-kill,
# retries) by sourcing it as a library, so there's one device layer, not two.
#
# Usage:
#   backup-activities.sh           back up new activities now
#   backup-activities.sh --auto    once-per-connection (honours the pause switch)
# Config (shared ~/.config/garmin-voice-export/config):
#   GARMIN_ACTIVITY_DEST     where .fit files go (default ~/Documents/Garmin Activities)
#   GARMIN_ACTIVITY_SUBPATH  on-watch folder (default GARMIN/Activity)
#   GARMIN_ACTIVITY_MAX      max files to fetch per run (default 0 = all; bound a huge first backup)

set -uo pipefail
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"

GVE_CONFIG="${GVE_CONFIG:-$HOME/.config/garmin-voice-export/config}"
[ -f "$GVE_CONFIG" ] && . "$GVE_CONFIG"
ACT_DEST="${GARMIN_ACTIVITY_DEST:-$HOME/Documents/Garmin Activities}"
ACT_SUBPATH="${GARMIN_ACTIVITY_SUBPATH:-GARMIN/Activity}"
ACT_REGEX="${GARMIN_ACTIVITY_REGEX:-.*\.[Ff][Ii][Tt]}"
ACT_MAX="${GARMIN_ACTIVITY_MAX:-0}"
NOTIFY=1; MODE="once"
for a in "$@"; do case "$a" in --auto) MODE="auto" ;; --no-notify) NOTIFY=0 ;; esac; done

# reuse the engine's device primitives (prep, gp_capture, gp_get, detect_base, present,
# kill_gp, notify, log). In load-only mode it creates no TMP/lock/trap — we own those.
export GVE_LOAD_ONLY=1
. "$SELF_DIR/export-voice-notes.sh"
unset GVE_LOAD_ONLY

# the engine (sourced above) resolved the organised-root layout; honour it for activities too.
ACT_DEST="${GVE_ACT_DEST:-$ACT_DEST}"
# point the shared globals at the activity destination, then set up our own run state
DEST="$ACT_DEST"; SUBPATH="$ACT_SUBPATH"; NOTE_REGEX="$ACT_REGEX"
MANIFEST="$ACT_DEST/.synced.tsv"; LOG="$ACT_DEST/backup.log"
TMP="$(mktemp -d /tmp/gva.XXXXXX)"
mkdir -p "$ACT_DEST"; touch "$MANIFEST"
LOCK="/tmp/garmin_voice_export.lock"   # SAME lock as the voice importer: never touch the USB at once
if ! mkdir "$LOCK" 2>/dev/null; then echo "another Garmin transfer is already running; skipping." >&2; exit 0; fi
trap 'rm -rf "$TMP" "$LOCK"' EXIT

# a downloaded .fit is good enough to keep if it's non-empty and its size matches the
# device's reported KB (we never delete, so we don't need the stricter header check voice uses).
valid_fit(){ local f="$1" kb="$2" sz exp
  [ -s "$f" ] || return 1
  sz=$(stat -f%z "$f" 2>/dev/null || echo 0); exp=$(( kb * 1024 ))
  [ "$sz" -ge $((exp - 4096)) ] && [ "$sz" -le $((exp + 4096)) ]; }

ACT_NEW=0
do_backup(){
  prep
  if ! detect_base; then log "Watch NOT detected (reseat the clip / try another cable or port)."; return 10; fi
  log "Reading $VF (activity backup; copy-only)"
  gp_capture "$TMP/list.out" "in folder '" 40 --folder "$VF" --list-files
  grep -q "in folder '" "$TMP/list.out" || { log "Activity folder unreadable; will retry."; return 1; }

  local listing total new=0 have=0 failed=0 fetched=0
  listing="$(awk -v re="$NOTE_REGEX" '
    /^#[0-9]+/ { num=$1; sub(/#/,"",num); if ($2 ~ re) print num"\t"$2"\t"$4"\t"$NF }' "$TMP/list.out")"
  total="$(printf '%s' "$listing" | grep -c .)"
  log "$total activity file(s) on watch."

  while IFS=$'\t' read -r num name kb epoch; do
    [ -z "${num:-}" ] && continue
    local id="${epoch}_${kb}"
    if manifest_has "$id"; then have=$((have+1)); continue; fi
    if [ "$ACT_MAX" -gt 0 ] && [ "$fetched" -ge "$ACT_MAX" ]; then
      log "reached GARMIN_ACTIVITY_MAX=$ACT_MAX for this run; $((total - have - fetched)) still pending (runs again on next connect)"
      break
    fi
    if ! gp_get "$num" "$name"; then failed=$((failed+1)); log "  ! download failed: $name"; continue; fi
    if ! valid_fit "$TMP/$name" "$kb"; then failed=$((failed+1)); log "  ! verify failed: $name"; rm -f "$TMP/$name"; continue; fi
    local final="$ACT_DEST/$name"
    if [ -e "$final" ] && [ "$(stat -f%z "$final" 2>/dev/null)" != "$(stat -f%z "$TMP/$name")" ]; then final="$ACT_DEST/${name%.fit}_${kb}KB.fit"; fi
    mv -f "$TMP/$name" "$final"
    manifest_has "$id" || printf '%s\t%s\n' "$id" "$(basename "$final")" >> "$MANIFEST"
    new=$((new+1)); fetched=$((fetched+1))
    log "  + $(basename "$final")  ($kb KB)  [$fetched]"
  done <<EOF
$listing
EOF

  ACT_NEW=$new
  log "Done: $new new, $have already backed up, $failed failed -> $ACT_DEST"
  [ "$failed" -gt 0 ] && return 1 || return 0
}

backup_with_retry(){ local try=1 rc=1
  while [ "$try" -le "$MAX_TRIES" ]; do
    do_backup; rc=$?
    [ "$rc" -eq 0 ] && return 0
    [ "$rc" -eq 10 ] && [ "$try" -ge 3 ] && return 10
    log "Attempt $try (rc=$rc); retrying..."; killall PTPCamera 2>/dev/null; kill_gp; sleep 6; try=$((try+1))
  done; return "$rc"; }

if [ "$MODE" = "auto" ]; then
  is_paused && exit 0                     # paused: leave the watch free for other apps
  present || exit 0                        # fired but no readable watch
  backup_with_retry; rc=$?
else
  log "=== manual activity backup ==="
  backup_with_retry; rc=$?
fi

if [ "${ACT_NEW:-0}" -gt 0 ]; then
  notify "Garmin Activities" "Backed up $ACT_NEW new activit$([ "$ACT_NEW" -gt 1 ] && echo ies || echo y) - click to open" "$ACT_DEST"
fi
exit "${rc:-0}"
