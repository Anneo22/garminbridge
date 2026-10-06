#!/usr/bin/env bash
# watch_mtp.sh — the content-manager's WATCH side over USB/MTP.
# Promoted from the proven scratchpad pattern (export-voice-notes.sh lineage):
# gphoto2 + a continuous PTPCamera-suppression loop (macOS keeps relaunching
# PTPCamera, which wedges the single MTP channel). One gphoto2 action per
# backgrounded process; wait for a marker; let it close cleanly.
#
# Subcommands (machine-parseable stdout, human noise on stderr):
#   list                 -> one line per workout file:  <idx>\t<filename>
#   delete <i1,i2,...>   -> delete those indices from GARMIN/Workouts, then re-list.
#                           Indices are deleted HIGH->LOW so earlier indices don't
#                           shift under us. Caller (clean_watch.py) owns the policy
#                           (which indices are safe); this script just executes +
#                           reports. It REFUSES an empty/garbage index list.
#
# This script has NO notion of "stranded" — it never decides what to delete. That
# decision lives in clean_watch.py (cross-ref vs Connect, dry-run, confirm). Keeps
# the dangerous mechanism dumb and the policy auditable. See memory
# delete-only-by-explicit-id.
set -u
# api.py reaches this script both from the app (which injects the runtime) and from a
# terminal (which does not), so resolve it here too. bin/ sits beside engine/ in the
# checkout and in the bundle alike. Guarded: a stripped-down copy without bin/ still runs
# on whatever gphoto2 the PATH already has.
_gve_paths="$(dirname "${BASH_SOURCE[0]}")/../bin/gve-paths.sh"
[ -f "$_gve_paths" ] && { . "$_gve_paths"; gve_resolve_runtime "${BASH_SOURCE[0]}"; }

SUB="${1:-}"
FOLDER_LEAF="GARMIN/Workouts"

log(){ printf '%s\n' "$*" >&2; }

kill_gp(){ pkill -9 -x gphoto2 2>/dev/null; }
SUPPRESS_PID=""
suppress_start(){ ( while :; do killall PTPCamera 2>/dev/null; sleep 1; done ) >/dev/null 2>&1 & SUPPRESS_PID=$!; disown "$SUPPRESS_PID" 2>/dev/null||true; }
suppress_stop(){ [ -n "$SUPPRESS_PID" ] && kill "$SUPPRESS_PID" 2>/dev/null; }
trap 'suppress_stop; kill_gp' EXIT

TMP="$(mktemp -d /tmp/wmtp.XXXXXX)"

# run a READ op (storage-info / list-files) and return the instant gphoto2 EXITS on
# its own — which it reliably does for reads (verified 2026-07-01, exit=0). The old
# runner waited for a marker then burned a FIXED 8s grace + 2s sleep on EVERY read
# call; that tail was ~5-6s of pure waiting per call and the bulk of the "refresh is
# slow" pain. Kill only if it overruns the cap (USB wedge). No trailing sleep on success.
gp_read(){ local out="$1" max="$2"; shift 2; : >"$out"
  killall PTPCamera 2>/dev/null
  ( gphoto2 "$@" >"$out" 2>&1 ) & local p=$! i
  for i in $(seq 1 "$max"); do kill -0 "$p" 2>/dev/null || return 0; sleep 1; done
  kill -9 "$p" 2>/dev/null; kill_gp; sleep 1; return 0; }

# run gphoto2 and wait for it to FINISH (up to max secs) — for writes/deletes where
# a marker would short-circuit early and a fixed 8s grace would truncate a long op
# (e.g. deleting 50 files in one session). Only kills if it overruns the cap.
gp_run(){ local out="$1" max="$2"; shift 2; : >"$out"
  killall PTPCamera 2>/dev/null
  ( gphoto2 "$@" >"$out" 2>&1 ) & local p=$! i
  for i in $(seq 1 "$max"); do kill -0 "$p" 2>/dev/null || break; sleep 1; done
  kill -9 "$p" 2>/dev/null; kill_gp; sleep 2; }

# like gp_run but runs gphoto2 with cwd=<dir> (for --get-all-files, which writes to cwd)
gp_run_in(){ local dir="$1" out="$2" max="$3"; shift 3; : >"$out"
  killall PTPCamera 2>/dev/null
  ( cd "$dir" && gphoto2 "$@" >"$out" 2>&1 ) & local p=$! i
  for i in $(seq 1 "$max"); do kill -0 "$p" 2>/dev/null || break; sleep 1; done
  kill -9 "$p" 2>/dev/null; kill_gp; sleep 2; }

settle(){
  if pgrep -fi "garmin express" >/dev/null; then osascript -e 'quit app "Garmin Express"' >/dev/null 2>&1; pkill -f "Garmin Express" 2>/dev/null; sleep 2; fi
  killall PTPCamera 2>/dev/null; kill_gp; sleep 1; suppress_start
}

find_base(){
  gp_read "$TMP/store.out" 30 --storage-info
  awk -F= '/^basedir=/{print $2; exit}' "$TMP/store.out"
}

# emit "<idx>\t<filename>" for every workout file, sorted by idx
list_files(){ local wf="$1"
  gp_read "$TMP/ls.out" 45 --folder "$wf" --list-files
  # gphoto2 lines look like:  #12  Some_Name_workout.fit  rd  34 KB ...  BUT it pads
  # the filename to a fixed width, so for long names the "rd" flag column abuts the
  # name with no space ("...workout.fitrd"). So don't split on whitespace — capture
  # greedily up to the LAST ".fit"/".FIT" and discard the trailing flag/size columns.
  sed -nE 's/^#([0-9]+)[[:space:]]+(.*\.[Ff][Ii][Tt]).*/\1\t\2/p' "$TMP/ls.out"
}

case "$SUB" in
  list)
    LEAF="${2:-$FOLDER_LEAF}"   # optional: list any GARMIN subfolder (Courses, Location, ...)
    settle
    BASE="$(find_base)"; [ -n "$BASE" ] || { log "NO BASEDIR — watch not readable over MTP"; exit 1; }
    WF="$BASE/$LEAF"
    list_files "$WF"
    ;;
  delete)
    IDXS="${2:-}"
    LEAF="${3:-$FOLDER_LEAF}"   # optional: delete from any GARMIN subfolder (Courses, ...)
    [ -n "$IDXS" ] || { log "ABORT: no indices given"; exit 2; }
    # sanitise: digits + commas only
    echo "$IDXS" | grep -qE '^[0-9]+(,[0-9]+)*$' || { log "ABORT: index list must be comma-separated integers, got '$IDXS'"; exit 2; }
    settle
    BASE="$(find_base)"; [ -n "$BASE" ] || { log "NO BASEDIR — watch not readable over MTP"; exit 1; }
    WF="$BASE/$LEAF"
    # delete HIGH->LOW (one gphoto2 session, comma list) so lower indices stay valid
    SORTED_CSV=$(echo "$IDXS" | tr ',' '\n' | sort -rn | uniq | paste -sd, -)
    n_before=$(list_files "$WF" | wc -l | tr -d ' ')
    log "delete (high->low): $SORTED_CSV (folder had $n_before files)"
    gp_run "$TMP/del.out" 240 --folder "$WF" --delete-file "$SORTED_CSV"
    log "  gphoto2: $(tr '\n' ' ' <"$TMP/del.out" | sed 's/  */ /g' | cut -c1-200)"
    n_after=$(list_files "$WF" | wc -l | tr -d ' ')
    log "delete done: folder $n_before -> $n_after"
    # emit the surviving listing so the caller can verify what's left
    list_files "$WF"
    ;;
  upload)
    LF="${2:-}"; LEAF="${3:-$FOLDER_LEAF}"
    [ -n "$LF" ] && [ -f "$LF" ] || { log "ABORT: upload needs an existing local file, got '$LF'"; exit 2; }
    settle
    BASE="$(find_base)"; [ -n "$BASE" ] || { log "NO BASEDIR — watch not readable over MTP"; exit 1; }
    WF="$BASE/$LEAF"
    n_before=$(list_files "$WF" | wc -l | tr -d ' ')
    log "upload: $LF -> $WF (folder had $n_before files)"
    gp_run "$TMP/up.out" 120 --folder "$WF" --upload-file "$LF"
    log "  gphoto2: $(tr '\n' ' ' <"$TMP/up.out" | sed 's/  */ /g' | cut -c1-200)"
    n_after=$(list_files "$WF" | wc -l | tr -d ' ')
    log "upload done: folder $n_before -> $n_after"
    list_files "$WF"
    ;;
  upload-all)
    LEAF="${2:-$FOLDER_LEAF}"; SRC="${3:-}"
    [ -d "$SRC" ] || { log "ABORT: upload-all needs a source dir"; exit 2; }
    settle
    BASE="$(find_base)"; [ -n "$BASE" ] || { log "NO BASEDIR — watch not readable over MTP"; exit 1; }
    WF="$BASE/$LEAF"
    n_before=$(list_files "$WF" | wc -l | tr -d ' ')
    log "upload-all: $SRC -> $WF (folder had $n_before)"
    n=0
    for lf in "$SRC"/*; do
      [ -f "$lf" ] || continue
      gp_run "$TMP/up.out" 120 --folder "$WF" --upload-file "$lf"
      log "  uploaded $(basename "$lf")"; n=$((n+1))
    done
    n_after=$(list_files "$WF" | wc -l | tr -d ' ')
    log "upload-all done: $n sent, folder $n_before -> $n_after"
    ;;
  get-all)
    LEAF="${2:-$FOLDER_LEAF}"; DEST="${3:-}"
    [ -n "$DEST" ] || { log "ABORT: get-all needs a destination dir"; exit 2; }
    mkdir -p "$DEST"
    settle
    BASE="$(find_base)"; [ -n "$BASE" ] || { log "NO BASEDIR — watch not readable over MTP"; exit 1; }
    WF="$BASE/$LEAF"
    log "get-all: $WF -> $DEST"
    gp_run_in "$DEST" "$TMP/ga.out" 600 --folder "$WF" --get-all-files --force-overwrite
    log "  files now in dest: $(ls -1 "$DEST" 2>/dev/null | wc -l | tr -d ' ')"
    ;;
  scan)
    # FAST snapshot path: list SEVERAL folders in ONE MTP session (one settle + one base
    # probe, then a clean-exit list per leaf) instead of the app calling `list` once per
    # folder — each of which re-settled and re-probed. Emits "<leaf>\t<idx>\t<filename>".
    LEAVES="${2:-}"
    [ -n "$LEAVES" ] || { log "ABORT: scan needs comma-separated leaves"; exit 2; }
    settle
    BASE="$(find_base)"; [ -n "$BASE" ] || { log "NO BASEDIR — watch not readable over MTP"; exit 1; }
    for leaf in $(printf '%s' "$LEAVES" | tr ',' ' '); do
      [ -n "$leaf" ] || continue
      list_files "$BASE/$leaf" | awk -F'\t' -v leaf="$leaf" 'BEGIN{OFS="\t"} NF>=2 {print leaf,$1,$2}'
    done
    ;;
  *)
    log "usage: watch_mtp.sh list [leaf] | scan <l1,l2> | delete <i1,..> [leaf] | upload <file> [leaf] | get-all <leaf> <destdir>"; exit 64
    ;;
esac
