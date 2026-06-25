#!/usr/bin/env bash
# export-voice-notes.sh — copy Garmin voice notes to the Mac, named by recording time.
#
# Backend: gphoto2 (reads only the VoiceNotes folder; libmtp's full scan hangs).
# Naming:  each note is saved as  YYYY-MM-DD_HH-MM-SS.wav  using its RECORDING time
#          (the watch stores it as the file's timestamp). Meaningful, sortable, and
#          immune to the watch reusing "VoiceNotes1.wav" after notes are removed.
#
# DELETION is OPT-IN (--delete). Why not default: removing the .wav over USB frees
# the audio but the watch's voice-note LIBRARY INDEX is not MTP-accessible, so the
# watch keeps a stale entry that shows in the UI but won't play ("ghost entry").
# The clean way to clear the watch is from the watch itself. If you use --delete,
# the order is strict and safe: download -> verify complete WAV (header + size) ->
# place + re-verify -> only THEN delete (matched by name+size) -> verify it is gone.
#
# Contention handled each run: quit Garmin Express, kill macOS PTPCamera daemon.
#
# Usage:
#   export-voice-notes.sh                         export only (leaves notes on the watch)
#   export-voice-notes.sh --delete                export, then delete from watch (leaves a
#                                                 ghost library entry until cleared on the watch)
#   export-voice-notes.sh --delete-after-transcript  delete from watch only once a transcript exists
#   export-voice-notes.sh --auto                  once-per-connection (launchd agent)
# Config: GARMIN_VOICE_DEST (default ~/Documents/Voice Memos)
#         GARMIN_VOICE_DELETE = keep | now | transcribed   (delete policy)
#         GVE_AUDIO_RETENTION_DAYS = N    drop the local .wav N days after recording but
#                                         keep the .txt transcript (0 = as soon as transcribed)

set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"

# optional config: transcription/Obsidian settings + API keys (and may set
# GARMIN_VOICE_* too). Absent by default -> transcription/Obsidian stay OFF.
GVE_CONFIG="${GVE_CONFIG:-$HOME/.config/garmin-voice-export/config}"
[ -f "$GVE_CONFIG" ] && . "$GVE_CONFIG"

# NOTE on ~/Documents: macOS TCC blocks background launchd agents from writing here
# (also ~/Desktop, ~/Downloads) WITHOUT a manual Full Disk Access grant for the
# agent's interpreter (/bin/bash). With FDA granted it works. Override with env var.
DEST="${GARMIN_VOICE_DEST:-$HOME/Documents/Voice Memos}"
# on-watch folder (relative to the discovered storage base). Standard across Garmin
# voice-note watches; overridable in case a model differs.
SUBPATH="${GARMIN_VOICE_SUBPATH:-GARMIN/Audio/VoiceNotes}"
NOTE_REGEX="${GARMIN_VOICE_REGEX:-VoiceNotes[0-9]+\.[Ww][Aa][Vv]}"
MANIFEST="$DEST/.synced.tsv"
LOG="$DEST/export.log"
PAUSE_FLAG="${GVE_PAUSE_FLAG:-$HOME/.config/garmin-voice-export/paused}"  # see bin/garmin-voice
MAX_TRIES=5
MODE="once"; DELETE_MODE="keep"; NOTIFY=1
# delete policy default from config/env (CLI flags below override). Three modes:
#   keep        leave notes on the watch (default)
#   now         remove from the watch after a verified local copy
#   transcribed remove from the watch after a verified local copy AND a transcript
case "${GARMIN_VOICE_DELETE:-}" in
  ""|0|keep|no|off)             DELETE_MODE="keep" ;;
  transcribed|after-transcript) DELETE_MODE="transcribed" ;;
  *)                            DELETE_MODE="now" ;;   # --delete, 1, now, yes, true, ...
esac
for a in "$@"; do case "$a" in
  --auto) MODE="auto" ;;
  --delete) DELETE_MODE="now" ;;
  --delete-after-transcript) DELETE_MODE="transcribed" ;;
  --keep) DELETE_MODE="keep" ;;
  --no-notify) NOTIFY=0 ;;
esac; done
DELETE=0; [ "$DELETE_MODE" != "keep" ] && DELETE=1     # 1 = deletion is in play (now or transcribed)
# transcribed-delete is meaningless without transcription (no transcript would ever exist,
# so nothing would be deleted). Fall back to keep and say why, rather than silently never deleting.
if [ "$DELETE_MODE" = "transcribed" ] && [ "${GVE_TRANSCRIBE:-0}" != "1" ]; then
  DELETE_MODE="keep"; DELETE=0
  GVE_DELETE_WARN="delete-after-transcript needs transcription ON; keeping notes on the watch for now"
fi
SUM_NEW=0 SUM_FAIL=0 SUM_DELFAIL=0 SUM_TOTAL=0 SUM_REMAIN=0

# Side effects only when actually running. When sourced load-only (GVE_LOAD_ONLY=1, e.g.
# by backup-activities.sh) we expose just the functions/globals — no TMP, lock, or trap;
# the caller sets up its own. The single-instance lock keeps the USB-trigger and a manual
# run from colliding on the device.
if [ -z "${GVE_LOAD_ONLY:-}" ]; then
  TMP="$(mktemp -d /tmp/gvn.XXXXXX)"
  mkdir -p "$DEST"; touch "$MANIFEST"
  LOCK="/tmp/garmin_voice_export.lock"
  if ! mkdir "$LOCK" 2>/dev/null; then echo "another export is already running; skipping." >&2; exit 0; fi
  trap 'ptp_suppress_stop; rm -rf "$TMP" "$LOCK"' EXIT
fi
log(){ printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }
kill_gp(){ pkill -9 -x gphoto2 2>/dev/null; }
# macOS keeps relaunching PTPCamera, which re-grabs the watch's single USB channel in the
# gaps between our ops and wedges the MTP session — the #1 cause of "only got 2-3 notes,
# had to disconnect/reconnect". Killing it once per op is not enough; suppress it
# continuously for the whole run (the kill loop only reaps it in the dangerous idle gaps;
# while gphoto2 holds the device, PTPCamera can't grab it anyway).
SUPPRESS_PID=""
ptp_suppress_start(){ [ -n "${SUPPRESS_PID:-}" ] && return 0
  ( while :; do killall PTPCamera 2>/dev/null; sleep 1; done ) >/dev/null 2>&1 & SUPPRESS_PID=$!
  disown "$SUPPRESS_PID" 2>/dev/null || true; }   # no job-control "Terminated" noise on stop
ptp_suppress_stop(){ [ -n "${SUPPRESS_PID:-}" ] && kill "$SUPPRESS_PID" 2>/dev/null; SUPPRESS_PID=""; }
# macOS desktop notification. With terminal-notifier (brew) the notification is
# CLICKABLE and opens $3 (a folder) on click; otherwise falls back to a plain
# (non-clickable) osascript notification. notify <title> <message> [path-to-open]
notify(){ [ "$NOTIFY" -eq 1 ] || return 0
  local t="$1" m="$2" openpath="${3:-}"
  if command -v terminal-notifier >/dev/null 2>&1; then
    if [ -n "$openpath" ]; then
      terminal-notifier -title "$t" -message "$m" -sound Glass -execute "open \"$openpath\"" >/dev/null 2>&1 || true
    else
      terminal-notifier -title "$t" -message "$m" -sound Glass >/dev/null 2>&1 || true
    fi
  else
    osascript -e "display notification \"${m//\"/}\" with title \"${t//\"/}\" sound name \"Glass\"" >/dev/null 2>&1 || true
  fi; }

prep(){
  if pgrep -fi "garmin express" >/dev/null; then
    osascript -e 'quit app "Garmin Express"' >/dev/null 2>&1; pkill -f "Garmin Express" 2>/dev/null
    sleep 2; log "Garmin Express stopped."
  fi
  killall PTPCamera 2>/dev/null; kill_gp; sleep 1
}

# run a gphoto2 read in background; once the data marker appears, let gphoto2 exit
# CLEANLY (its session-close can take a few seconds) and only force-kill if it truly
# hangs. A hard kill mid-cleanup leaves the USB session dirty and the NEXT op fails,
# so we also settle briefly afterwards. Output captured in $1.
gp_capture(){ local out="$1" mark="$2" max="$3"; shift 3; : >"$out"
  killall PTPCamera 2>/dev/null                 # respawns and re-grabs the device between ops
  ( gphoto2 "$@" >"$out" 2>&1 ) & local p=$! i
  for i in $(seq 1 "$max"); do grep -qE "$mark" "$out" 2>/dev/null && break
    kill -0 "$p" 2>/dev/null || break; sleep 1; done
  for i in $(seq 1 8); do kill -0 "$p" 2>/dev/null || break; sleep 1; done   # let it close cleanly
  kill -9 "$p" 2>/dev/null; kill_gp; sleep 2; }                              # settle before next session

# download one note by listing-number, polling until its size is stable, then kill
gp_get(){ local num="$1" name="$2" last=-1 cur stable=0 i
  killall PTPCamera 2>/dev/null
  ( cd "$TMP" && gphoto2 --folder "$VF" --get-file "$num" --filename "$name" >/dev/null 2>&1 ) & local p=$!
  for i in $(seq 1 90); do
    cur=$( [ -f "$TMP/$name" ] && stat -f%z "$TMP/$name" 2>/dev/null || echo -1 )
    if [ "$cur" -gt 0 ] && [ "$cur" = "$last" ]; then stable=$((stable+1)); [ "$stable" -ge 2 ] && break
    else stable=0; fi
    kill -0 "$p" 2>/dev/null || { [ "$cur" -gt 0 ] && break; }
    last="$cur"; sleep 1
  done
  for i in $(seq 1 8); do kill -0 "$p" 2>/dev/null || break; sleep 1; done   # let it close cleanly
  kill -9 "$p" 2>/dev/null; kill_gp; sleep 2; [ -s "$TMP/$name" ]
}

# download SEVERAL notes (by listing index) in a SINGLE gphoto2 session into $TMP, keeping
# each device filename (VoiceNotesN.wav). One open/close for the whole batch instead of one
# per note: Garmin's MTP stack wedges on rapid session churn, so batching the reads is what
# lets a multi-note backlog drain in a single connection. Deletes stay OUT of this pass.
gp_get_all(){
  [ "$#" -gt 0 ] || return 0
  rm -f "$TMP"/VoiceNotes*.wav 2>/dev/null            # avoid gphoto2's overwrite prompt on a retry
  local args=() n; for n in "$@"; do args+=(--get-file "$n"); done
  killall PTPCamera 2>/dev/null
  ( cd "$TMP" && gphoto2 --folder "$VF" "${args[@]}" >"$TMP/getall.out" 2>&1 ) & local p=$! i prev=0 cur stalls=0
  for i in $(seq 1 300); do                            # patient: the watch is slow over USB
    kill -0 "$p" 2>/dev/null || break
    cur="$(find "$TMP" -name 'VoiceNotes*.wav' -exec stat -f%z {} + 2>/dev/null | awk '{s+=$1} END{print s+0}')"
    if [ "$cur" = "$prev" ]; then stalls=$((stalls+1)); else stalls=0; fi
    [ "$stalls" -ge 25 ] && break                      # ~25s with no new bytes -> stuck, stop waiting
    prev="$cur"; sleep 1
  done
  for i in $(seq 1 8); do kill -0 "$p" 2>/dev/null || break; sleep 1; done   # let it close cleanly
  kill -9 "$p" 2>/dev/null; kill_gp; sleep 2
}

# recording time -> filename base, using GVE_NAME_FORMAT (a date(1) format string).
# Examples: "%Y-%m-%d_%H-%M-%S" (default), "%Y%m%d-%H%M" (compact),
# "%Y/%m/%Y-%m-%d_%H-%M-%S" (organised into year/month subfolders), "Memo %Y-%m-%d %H.%M".
# Falls back to export time if the note carries no recording timestamp.
ts_name(){ local e="$1" fmt="${GVE_NAME_FORMAT:-%Y-%m-%d_%H-%M-%S}"
  if [ "${e:-0}" -ge 1262304000 ] 2>/dev/null; then date -r "$e" "+$fmt"
  else echo "undated_$(date '+%Y-%m-%d_%H-%M-%S')"; fi; }

# a downloaded note is safe to rely on (and to delete from the watch) only if it is
# a real WAV and its byte size matches the device's reported KB (within rounding)
valid_wav(){ local f="$1" kb="$2" sz exp
  [ -s "$f" ] || return 1
  file -b "$f" 2>/dev/null | grep -qi "WAVE" || return 1
  sz=$(stat -f%z "$f" 2>/dev/null || echo 0); exp=$(( kb * 1024 ))
  [ "$sz" -ge $((exp - 4096)) ] && [ "$sz" -le $((exp + 4096)) ]
}

manifest_has(){ grep -q "^$1	" "$MANIFEST"; }
manifest_file(){ awk -F'\t' -v id="$1" '$1==id{print $2; exit}' "$MANIFEST"; }
# retention bookkeeping: a 3rd column "pruned" records that we once held a verified copy
# but intentionally deleted the local .wav, so we must never re-download that note.
manifest_pruned(){ awk -F'\t' -v id="$1" '$1==id{f=($3=="pruned")} END{exit !f}' "$MANIFEST"; }
manifest_mark_pruned(){ awk -F'\t' -v id="$1" 'BEGIN{OFS="\t"} $1==id{print $1,$2,"pruned"; next} {print}' \
    "$MANIFEST" > "$MANIFEST.tmp" && mv "$MANIFEST.tmp" "$MANIFEST"; }
txt_for(){ echo "${1%.*}.txt"; }                  # transcript path next to an audio file

# optional, opt-in post-processing of a freshly-saved memo: transcription + Obsidian.
# Both are no-ops unless configured (GVE_TRANSCRIBE=1 / GVE_OBSIDIAN_VAULT set).
write_obsidian(){ local wav="$1" txt="${2:-}" vault="$GVE_OBSIDIAN_VAULT" base note body=""
  base="$(basename "${wav%.*}")"; mkdir -p "$vault" 2>/dev/null || return 0; note="$vault/$base.md"
  [ -n "$txt" ] && [ -f "$txt" ] && body="$(cat "$txt")"
  { echo "---"; echo "type: voice-memo"; echo "recorded: $base"; echo "audio: \"$wav\""; echo "---"; echo
    [ -n "$body" ] && echo "$body" || echo "_(no transcript)_"
    echo; echo "[recording]($wav)"; } > "$note"
  log "    obsidian note -> $note"; }
post_process(){ local wav="$1" txt=""
  if [ "${GVE_TRANSCRIBE:-0}" = "1" ]; then
    if txt="$(GVE_CONFIG="$GVE_CONFIG" "$SELF_DIR/transcribe-memo.sh" "$wav" 2>>"$LOG")"; then
      log "    transcribed -> $(basename "$txt")"
    else log "    (transcription failed; audio kept)"; txt=""; fi
  fi
  [ -n "${GVE_OBSIDIAN_VAULT:-}" ] && write_obsidian "$wav" "$txt"; }

# optional local-disk retention (opt-in via GVE_AUDIO_RETENTION_DAYS). Drops the heavy
# .wav after N days but KEEPS the .txt transcript. 0 = drop as soon as it is transcribed.
# Safety: while transcription is ON it never deletes a .wav that has no transcript yet.
# Pruned notes are marked in the manifest so they are never re-downloaded from the watch.
prune_local_audio(){
  local days="${GVE_AUDIO_RETENTION_DAYS:-}"
  [ -n "$days" ] || return 0
  case "$days" in *[!0-9]*) log "audio retention: GVE_AUDIO_RETENTION_DAYS='$days' is not a whole number; skipping"; return 0;; esac
  local ton=0; [ "${GVE_TRANSCRIBE:-0}" = "1" ] && ton=1
  if [ "$days" -eq 0 ] && [ "$ton" -eq 0 ]; then
    log "audio retention: days=0 needs transcription ON (won't delete un-transcribed audio); skipping"; return 0
  fi
  local now w mt age txt sz key pruned=0 freed=0
  now="$(date +%s)"
  while IFS= read -r w; do
    [ -f "$w" ] || continue
    mt="$(stat -f%m "$w" 2>/dev/null || echo "$now")"
    age=$(( (now - mt) / 86400 ))
    [ "$age" -ge "$days" ] || continue
    txt="$(txt_for "$w")"
    [ "$ton" -eq 1 ] && [ ! -f "$txt" ] && continue          # keep audio we haven't transcribed
    sz="$(stat -f%z "$w" 2>/dev/null || echo 0)"
    if rm -f "$w"; then
      pruned=$((pruned+1)); freed=$((freed+sz))
      key="$(awk -F'\t' -v f="$(basename "$w")" '$2==f{print $1; exit}' "$MANIFEST")"
      [ -n "$key" ] && manifest_mark_pruned "$key"
    fi
  done < <(find "$DEST" -type f -iname '*.wav' 2>/dev/null)
  [ "$pruned" -gt 0 ] && log "audio retention: pruned $pruned old .wav (~$((freed/1024/1024)) MB freed; transcripts kept)"
  return 0
}

VF=""
detect_base(){
  gp_capture "$TMP/store.out" "^basedir=" 40 --storage-info
  local b; b="$(awk -F= '/^basedir=/{print $2; exit}' "$TMP/store.out")"
  [ -n "$b" ] || return 1; VF="$b/$SUBPATH"; return 0
}

present(){ killall PTPCamera 2>/dev/null; gp_capture "$TMP/det.out" "Garmin|usb:" 25 --auto-detect
  grep -qi garmin "$TMP/det.out"; }

# delete one note from the watch by NAME+SIZE (immune to index renumbering and to a
# reused filename being a different new recording). Verifies it is gone. 0 = gone.
delete_from_watch(){ local dn="$1" kb="$2" idx
  gp_capture "$TMP/dl.out" "in folder '" 40 --folder "$VF" --list-files
  idx="$(awk -v n="$dn" -v k="$kb" '/^#[0-9]+/ && $2==n && $4==k {sub(/#/,"",$1);print $1;exit}' "$TMP/dl.out")"
  [ -z "$idx" ] && return 0                       # already gone
  killall PTPCamera 2>/dev/null
  ( gphoto2 --folder "$VF" --delete-file "$idx" >/dev/null 2>&1 ) & local p=$!
  local i; for i in $(seq 1 30); do kill -0 "$p" 2>/dev/null || break; sleep 1; done
  kill -9 "$p" 2>/dev/null; kill_gp; sleep 2
  gp_capture "$TMP/dl2.out" "in folder '" 40 --folder "$VF" --list-files
  awk -v n="$dn" -v k="$kb" '/^#[0-9]+/ && $2==n && $4==k {f=1} END{exit !f}' "$TMP/dl2.out" && return 1 || return 0
}

do_sync(){
  prep
  if ! detect_base; then log "Watch NOT detected (reseat the clip / try another cable or port)."; return 10; fi
  [ -n "${GVE_DELETE_WARN:-}" ] && log "note: $GVE_DELETE_WARN"
  case "$DELETE_MODE" in
    now)         log "Detected. Reading $VF (delete: remove from watch after a verified copy; leaves a ghost library entry until cleared on the watch)" ;;
    transcribed) log "Detected. Reading $VF (delete-after-transcript: remove from watch once a verified copy AND transcript exist)" ;;
    *)           log "Detected. Reading $VF (export only; notes stay on watch)" ;;
  esac

  rm -f "$TMP"/VoiceNotes*.wav 2>/dev/null
  gp_capture "$TMP/list.out" "in folder '" 40 --folder "$VF" --list-files
  grep -q "in folder '" "$TMP/list.out" || { log "Detected but VoiceNotes folder unreadable; will retry."; return 1; }

  local listing; listing="$(awk -v re="$NOTE_REGEX" '
    /^#[0-9]+/ { num=$1; sub(/#/,"",num); if ($2 ~ re) print num"\t"$2"\t"$4"\t"$NF }' "$TMP/list.out")"
  local total new=0 kept=0 failed=0 deleted=0 delfail=0 remain=0
  total="$(printf '%s' "$listing" | grep -c .)"
  log "$total voice note(s) on watch."
  if [ "$total" -eq 0 ]; then
    SUM_NEW=0 SUM_FAIL=0 SUM_DELFAIL=0 SUM_TOTAL=0 SUM_REMAIN=0
    log "Done: 0 new, 0 kept, 0 failed | watch: 0 deleted, 0 not-deleted, 0 still on watch -> $DEST"
    return 0
  fi

  # Which listed notes do we still need to pull? Skip ones already verified on disk or
  # intentionally retention-pruned, so we never re-download. Collect their listing indices.
  local needidx=""
  while IFS=$'\t' read -r num dev kb epoch; do
    [ -z "${num:-}" ] && continue
    local id="${epoch}_${kb}" lf
    lf="$(manifest_file "$id")"
    if [ -n "$lf" ]; then
      valid_wav "$DEST/$lf" "$kb" && continue
      manifest_pruned "$id" && continue
    fi
    needidx="$needidx $num"
  done <<EOF
$listing
EOF

  # PHASE 1 — pull everything we still need in ONE gphoto2 session. No interleaved deletes:
  # deleting after each get is what wedged Garmin's MTP channel and stranded the backlog.
  if [ -n "${needidx// /}" ]; then
    log "  downloading $(printf '%s' "$needidx" | wc -w | tr -d ' ') note(s) in one session..."
    gp_get_all $needidx
  fi

  # PHASE 2 — verify + place each note; QUEUE the safe ones for the deferred delete pass.
  local -a del_dev=() del_kb=() del_saved=()
  while IFS=$'\t' read -r num dev kb epoch; do
    [ -z "${num:-}" ] && continue
    local id="${epoch}_${kb}" base lf saved wavpath have=0 pruned=0
    base="$(ts_name "$epoch")"
    lf="$(manifest_file "$id")"
    if [ -n "$lf" ]; then
      saved="$lf"; wavpath="$DEST/$lf"
      if valid_wav "$wavpath" "$kb"; then have=1
      elif manifest_pruned "$id"; then have=1; pruned=1; fi
    fi

    if [ "$have" -eq 0 ]; then
      local src="$TMP/$dev"
      if [ ! -s "$src" ]; then failed=$((failed+1)); log "  ! download failed: $dev (kept on watch)"; continue; fi
      if ! valid_wav "$src" "$kb"; then failed=$((failed+1)); log "  ! verify failed: $dev (kept on watch)"; rm -f "$src"; continue; fi
      local final="$DEST/$base.wav"
      if [ -e "$final" ] && [ "$(stat -f%z "$final" 2>/dev/null)" != "$(stat -f%z "$src")" ]; then final="$DEST/${base}_${kb}KB.wav"; fi
      mkdir -p "$(dirname "$final")" 2>/dev/null   # GVE_NAME_FORMAT may include subfolders
      mv -f "$src" "$final"
      if ! valid_wav "$final" "$kb"; then failed=$((failed+1)); log "  ! post-move verify failed: $(basename "$final") (kept on watch)"; continue; fi
      saved="$(basename "$final")"; wavpath="$final"
      manifest_has "$id" || printf '%s\t%s\n' "$id" "$saved" >> "$MANIFEST"   # guard against dup lines
      new=$((new+1)); have=1
      log "  + $saved  (from $dev, $kb KB)"
      post_process "$final"
    else
      # already synced. keeping notes on the watch? then we're done with this one.
      [ "$DELETE" -eq 0 ] && { kept=$((kept+1)); continue; }
      # delete is on: if a transcript is still missing (earlier failure) and we still
      # hold the audio, try transcribing again before we remove it from the watch.
      if [ "$pruned" -eq 0 ] && [ "${GVE_TRANSCRIBE:-0}" = "1" ] && [ ! -f "$(txt_for "$wavpath")" ]; then
        post_process "$wavpath"
      fi
    fi

    # verified local copy in hand (now or earlier) -> queue for deletion AFTER all downloads
    if [ "$DELETE" -eq 1 ] && [ "$have" -eq 1 ]; then
      if [ "$DELETE_MODE" = "transcribed" ] && [ ! -f "$(txt_for "$wavpath")" ]; then
        kept=$((kept+1)); log "  · $dev kept on watch (waiting for a transcript)"
      else
        del_dev+=("$dev"); del_kb+=("$kb"); del_saved+=("$saved")
      fi
    fi
  done <<EOF
$listing
EOF

  # PHASE 3 — deferred deletes: only now, with every download byte-verified, remove from watch.
  if [ "${#del_dev[@]}" -gt 0 ]; then
    local di
    for di in "${!del_dev[@]}"; do
      if delete_from_watch "${del_dev[$di]}" "${del_kb[$di]}"; then
        deleted=$((deleted+1)); log "  - deleted ${del_dev[$di]} from watch (saved as ${del_saved[$di]})"
      else
        delfail=$((delfail+1)); log "  ~ saved but could NOT delete ${del_dev[$di]} from watch (will retry next time)"
      fi
    done
  fi

  # PHASE 4 — completeness: re-read the watch and report the GROUND TRUTH still on it, so the
  # "done" claim is checked against the device, not against this run's own bookkeeping.
  if gp_capture "$TMP/relist.out" "in folder '" 40 --folder "$VF" --list-files && grep -q "in folder '" "$TMP/relist.out"; then
    remain="$(awk -v re="$NOTE_REGEX" '/^#[0-9]+/ && $2 ~ re {c++} END{print c+0}' "$TMP/relist.out")"
  else
    remain=-1   # could not re-verify the watch this pass
  fi

  SUM_NEW=$new SUM_FAIL=$failed SUM_DELFAIL=$delfail SUM_TOTAL=$total SUM_REMAIN=$remain
  log "Done: $new new, $kept kept, $failed failed | watch: $deleted deleted, $delfail not-deleted, ${remain} still on watch -> $DEST"
  { [ "$failed" -gt 0 ] || [ "$delfail" -gt 0 ]; } && return 1 || return 0
}

sync_with_retry(){ local try=1 rc=1
  while [ "$try" -le "$MAX_TRIES" ]; do
    do_sync; rc=$?
    [ "$rc" -eq 0 ] && return 0
    [ "$rc" -eq 10 ] && [ "$try" -ge 3 ] && return 10
    log "Attempt $try (rc=$rc); retrying..."; killall PTPCamera 2>/dev/null; kill_gp; sleep 6; try=$((try+1))
  done; return "$rc"; }

# post-run desktop notification: a real COMPLETENESS verdict, not just a count. Silent only
# when the watch was empty and nothing was new (so it never spams routine connects).
finish_notify(){ local rc="$1"
  local n="${SUM_NEW:-0}" total="${SUM_TOTAL:-0}" remain="${SUM_REMAIN:-0}" delfail="${SUM_DELFAIL:-0}"
  [ "$rc" -eq 10 ] && return 0                          # not detected (cable/clip): stay quiet
  { [ "$n" -le 0 ] && [ "$total" -le 0 ]; } && return 0 # nothing on the watch, nothing new: silent
  local pre="" title msg
  [ "$n" -gt 0 ] && pre="Imported $n new memo$([ "$n" -gt 1 ] && echo s) - "
  if [ "$DELETE" -eq 1 ]; then
    if [ "$remain" = "-1" ]; then
      title="Voice Memos"; msg="${pre}couldn't re-check the watch - reconnect to confirm it's empty"
    elif [ "$remain" -eq 0 ] && [ "$delfail" -eq 0 ]; then
      title="Voice Memos ✓"; msg="${pre}watch is now empty, every note is on your Mac"
    else
      title="Voice Memos - not finished"; msg="${pre}$remain still on the watch; keep it connected / reconnect to finish"
    fi
  else
    if [ "$rc" -eq 0 ]; then
      title="Voice Memos ✓"; msg="${pre}all $total note(s) on the watch are backed up"
    else
      title="Voice Memos - issue"; msg="${pre}some notes failed; reconnect to finish"
    fi
  fi
  notify "$title" "$msg" "$DEST"
}

main(){
ptp_suppress_start   # hold PTPCamera off the watch for the whole run (detect + sync + delete)
if [ "$MODE" = "auto" ]; then
  # Triggered by the on-connect watcher (once per attach). Respect the pause switch;
  # no marker bookkeeping needed since it's event-driven, not polled.
  [ -f "$PAUSE_FLAG" ] && exit 0          # paused: leave the watch free for Garmin Express / MTP apps
  if present; then
    sync_with_retry; rc=$?
    prune_local_audio
    finish_notify "$rc"
    exit "$rc"
  else exit 0; fi                         # fired but no readable watch (e.g. a non-watch Garmin device)
else
  log "=== manual sync (delete: $DELETE_MODE) ==="
  sync_with_retry; rc=$?
  prune_local_audio
  finish_notify "$rc"
  exit "$rc"
fi
}
# run normally; set GVE_LOAD_ONLY=1 to source this file for its functions without
# running (used by the test harness). Default behaviour is unchanged.
if [ -z "${GVE_LOAD_ONLY:-}" ]; then main; fi
