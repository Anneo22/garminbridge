#!/usr/bin/env bash
# backup-settings.sh — copy the watch's SETTINGS and SPORT PROFILES to the Mac so a new or
# upgraded watch can be set back up the way you had it. COPY-ONLY: it never deletes anything
# from the watch.
#
# What it backs up (all plain files over MTP, confirmed by on-watch recon): the per-sport
# profiles / data screens (GARMIN/Sports), device settings (GARMIN/Settings), the watch's own
# restorable backup blobs (GARMIN/Backup/Backups, incl. settings_backup.bak), Connect IQ app +
# watch-face settings and data (GARMIN/Apps/SETTINGS + Apps/DATA), routes (GARMIN/Courses),
# workouts, saved locations, gear, segments, pace bands, power guides, records, goals, schedule,
# custom maps. The full list is the TARGETS array below. Heavy health telemetry is excluded by
# default (syncs to Connect; large) — opt in with GARMIN_BACKUP_HEALTH=1.
#
# It reuses the voice importer's reliable device engine (PTPCamera suppression + single-session
# bulk download), so a folder full of files comes across in one go instead of one-per-session.
#
# Usage:
#   backup-settings.sh           back up now
#   backup-settings.sh --auto    once-per-connection (honours the pause switch)
# Config (shared ~/.config/garmin-voice-export/config):
#   GARMIN_SETTINGS_BACKUP=1     enable on connect
#   GARMIN_SETTINGS_DEST         where it goes (default <root>/Settings, or ~/Documents/Garmin Settings)
#   GARMIN_BACKUP_HEALTH=1       also mirror heavy health telemetry (Monitor/Metrics/Sleep)

set -uo pipefail
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"

GVE_CONFIG="${GVE_CONFIG:-$HOME/.config/garmin-voice-export/config}"
[ -f "$GVE_CONFIG" ] && . "$GVE_CONFIG"
NOTIFY=1; MODE="once"
for a in "$@"; do case "$a" in --auto) MODE="auto" ;; --no-notify) NOTIFY=0 ;; esac; done

# reuse the engine's device primitives (prep, gp_capture, gp_get_all, present, kill_gp,
# ptp_suppress_*, notify, log, gve_icon) and the path resolver. Load-only: no TMP/lock/trap.
export GVE_LOAD_ONLY=1
. "$SELF_DIR/export-voice-notes.sh"
unset GVE_LOAD_ONLY

SET_DEST="${GVE_SET_DEST:-$HOME/Documents/Garmin Settings}"
LOG="$SET_DEST/backup.log"
SIGFILE="$SET_DEST/.folders.tsv"   # label<TAB>signature, so unchanged folders are skipped
TMP="$(mktemp -d /tmp/gvs.XXXXXX)"
mkdir -p "$SET_DEST"; touch "$SIGFILE"
LOCK="/tmp/garmin_voice_export.lock"   # SAME lock as voice/activities: never touch the USB at once
if ! mkdir "$LOCK" 2>/dev/null; then echo "another Garmin transfer is already running; skipping." >&2; exit 0; fi
trap 'ptp_suppress_stop; rm -rf "$TMP" "$LOCK"' EXIT

# targets: "label|on-watch path under the storage base|dest subfolder"
TARGETS=(
  "Sports|GARMIN/Sports|Sports"                       # per-sport profiles / data screens
  "Device|GARMIN/Settings|Device"                     # device settings
  "Device Backup|GARMIN/Backup/Backups|Device Backup" # the watch's own restorable backup blobs
  "Connect IQ|GARMIN/Apps/SETTINGS|Connect IQ"        # CIQ app + watch-face settings (.SET)
  "Connect IQ Data|GARMIN/Apps/DATA|Connect IQ Data"  # CIQ app data store (incl. watch-face data)
  "Courses|GARMIN/Courses|Courses"                    # routes
  "Workouts|GARMIN/Workouts|Workouts"                 # structured workouts
  "Locations|GARMIN/Location|Locations"               # saved positions / waypoints
  "Gear|GARMIN/Gear|Gear"                             # gear (shoes/bikes) tracking
  "Segments|GARMIN/Seg_List|Segments"                 # segments list
  "Pace Bands|GARMIN/PaceBands|Pace Bands"
  "Power Guides|GARMIN/PowerGuide|Power Guides"
  "Records|GARMIN/Records|Records"                    # personal records
  "Goals|GARMIN/Goals|Goals"
  "Schedule|GARMIN/Schedule|Schedule"
  "Custom Maps|GARMIN/CustomMaps|Custom Maps"
)
# Heavy health telemetry (steps/HR/sleep/...) is excluded by default: it's large, churny, and
# already syncs to Garmin Connect. Opt in with GARMIN_BACKUP_HEALTH=1.
if [ "${GARMIN_BACKUP_HEALTH:-0}" = "1" ]; then
  TARGETS+=("Monitor|GARMIN/Monitor|Health/Monitor" "Metrics|GARMIN/Metrics|Health/Metrics" "Sleep|GARMIN/Sleep|Health/Sleep")
fi

SET_NEW=0 SET_FAIL=0 SET_FOLDERS=0
sig_get(){ awk -F'\t' -v l="$1" '$1==l{print $2; exit}' "$SIGFILE"; }
sig_put(){ awk -F'\t' -v l="$1" -v s="$2" 'BEGIN{OFS="\t"} $1==l{next} {print} END{print l,s}' "$SIGFILE" > "$SIGFILE.tmp" && mv "$SIGFILE.tmp" "$SIGFILE"; }

# back up one watch folder (copy-only, mirror-latest). Skips entirely when nothing changed.
backup_folder(){ local label="$1" vf="$2" dst="$3"
  VF="$vf"   # gp_get_all reads the global VF
  gp_capture "$TMP/list.out" "in folder '" 40 --folder "$VF" --list-files
  if ! grep -q "in folder '" "$TMP/list.out"; then log "  [$label] not present / unreadable; skipping"; return 0; fi

  # signature = count:totalKB:maxEpoch. Fields are read from the RIGHT (epoch=$NF, KB size=$(NF-3))
  # so long filenames that collide with the perms column don't break the numbers.
  local sig; sig="$(awk '/^#[0-9]+/ { c++; sum+=$(NF-3); if ($NF+0>mx) mx=$NF } END{printf "%d:%d:%d", c+0, sum+0, mx+0}' "$TMP/list.out")"
  local cnt="${sig%%:*}"
  if [ "$cnt" = "0" ]; then log "  [$label] 0 files on watch"; sig_put "$label" "$sig"; return 0; fi
  if [ "$(sig_get "$label")" = "$sig" ] && [ -d "$dst" ]; then
    log "  [$label] unchanged ($cnt files) — skipped"; SET_FOLDERS=$((SET_FOLDERS+1)); return 0
  fi

  local indices; indices="$(awk '/^#[0-9]+/{n=$1; sub(/#/,"",n); printf "%s ", n}' "$TMP/list.out")"
  mkdir -p "$dst"
  find "$TMP" -maxdepth 1 -type f ! -name '*.out' -delete 2>/dev/null   # clean download area
  log "  [$label] pulling $cnt file(s) in one session..."
  gp_get_all $indices

  # place every downloaded file (real device names) into the dest, overwriting older versions.
  local f bn placed=0
  for f in "$TMP"/*; do
    [ -f "$f" ] || continue
    bn="$(basename "$f")"; case "$bn" in *.out) continue ;; esac
    [ -s "$f" ] || { SET_FAIL=$((SET_FAIL+1)); log "    ! empty: $bn"; rm -f "$f"; continue; }
    mv -f "$f" "$dst/$bn" && placed=$((placed+1))
  done
  log "  [$label] $placed file(s) -> $dst"
  SET_NEW=$((SET_NEW+placed)); SET_FOLDERS=$((SET_FOLDERS+1))
  # only record the signature if we got everything (so a partial pull retries next connect)
  [ "$placed" -ge "$cnt" ] && sig_put "$label" "$sig" || log "  [$label] partial ($placed/$cnt); will retry next connect"
}

do_backup(){
  prep
  if ! detect_base; then log "Watch NOT detected (reseat the clip / try another cable or port)."; return 10; fi
  local base="${VF%/$SUBPATH}"   # detect_base set VF=<base>/<voice subpath>; recover the bare base
  log "Settings backup (copy-only) from $base"
  local t label rel dst rc=0
  for t in "${TARGETS[@]}"; do
    label="${t%%|*}"; rel="${t#*|}"; rel="${rel%%|*}"; dst="$SET_DEST/${t##*|}"
    backup_folder "$label" "$base/$rel" "$dst" || rc=1
  done
  log "Done: $SET_NEW file(s) updated across $SET_FOLDERS folder(s), $SET_FAIL failed -> $SET_DEST"
  [ "$SET_FAIL" -gt 0 ] && return 1 || return "$rc"
}

backup_with_retry(){ local try=1 rc=1
  while [ "$try" -le "$MAX_TRIES" ]; do
    do_backup; rc=$?
    [ "$rc" -eq 0 ] && return 0
    [ "$rc" -eq 10 ] && [ "$try" -ge 3 ] && return 10
    log "Attempt $try (rc=$rc); retrying..."; killall PTPCamera 2>/dev/null; kill_gp; sleep 6; try=$((try+1))
  done; return "$rc"; }

ptp_suppress_start
if [ "$MODE" = "auto" ]; then
  is_paused && exit 0
  present || exit 0
  log "=== auto settings backup ==="
else
  log "=== manual settings backup ==="
fi
backup_with_retry; rc=$?

if [ "${SET_NEW:-0}" -gt 0 ]; then
  notify "Garmin settings backed up" "$SET_NEW file(s) updated (sport profiles + settings) - click to open" "$SET_DEST"
fi
exit "${rc:-0}"
