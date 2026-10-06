#!/usr/bin/env bash
# garmin-diag.sh — is a failed sync the CABLE or the SOFTWARE?
#
# Layer 1  USB detection (gphoto2 --auto-detect, sampled) — does the Mac even see
#          the watch, and stay seeing it? Flaky here = charging clip / cable / port.
#          NO software can fix that; reseat / swap cable / try another port.
# Layer 2  MTP read (gphoto2 storage-info + list VoiceNotes) — can we read it?
#          Fails here while Layer 1 is solid = contention (Express / PTPCamera)
#          or transient MTP flakiness; the exporter retries through it.
#
# Run with the watch plugged in.   Usage: garmin-diag.sh [--samples N]

set -uo pipefail
# This is a diagnostic someone runs from a terminal, so it must find the bundled gphoto2
# on its own; Homebrew stays behind it as a development fallback.
. "$(dirname "${BASH_SOURCE[0]}")/gve-paths.sh"; gve_resolve_runtime "${BASH_SOURCE[0]}"
SAMPLES=8
while [ $# -gt 0 ]; do case "$1" in
  --samples) SAMPLES="$2"; shift 2 ;;
  -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;;
esac; done

g=$'\033[32m'; r=$'\033[31m'; y=$'\033[33m'; d=$'\033[2m'; o=$'\033[0m'
ok(){ echo "${g}[ ok ]${o} $*"; }; warn(){ echo "${y}[warn]${o} $*"; }
bad(){ echo "${r}[FAIL]${o} $*"; }; info(){ echo "${d}      $*${o}"; }; hdr(){ echo; echo "=== $* ==="; }

# run gphoto2, poll output for marker, then kill (skip its session-close hang)
gp(){ local out="$1" mark="$2" max="$3"; shift 3; : >"$out"
  ( gphoto2 "$@" >"$out" 2>&1 ) & local p=$!
  local i; for i in $(seq 1 "$max"); do grep -qE "$mark" "$out" 2>/dev/null && break
    kill -0 "$p" 2>/dev/null || break; sleep 1; done
  sleep 1; kill -9 "$p" 2>/dev/null; pkill -9 -x gphoto2 2>/dev/null; }

echo "garmin-diag — $(date '+%Y-%m-%d %H:%M:%S %z')"
hdr "Environment"
command -v gphoto2 >/dev/null && ok "gphoto2 present" || { bad "gphoto2 missing from the app; reinstall Garmin Bridge"; exit 1; }

hdr "Contention"
pgrep -fil "garmin express" >/dev/null && { warn "Garmin Express running — will hold the device; the exporter quits it."; } || ok "Garmin Express not running."
killall PTPCamera 2>/dev/null && info "killed PTPCamera (macOS auto-claims MTP; harmless)" || true

hdr "Layer 1 — USB detection ($SAMPLES samples)"
hits=0
for i in $(seq 1 "$SAMPLES"); do
  gp /tmp/diag_det.out "Garmin|usb:" 25 --auto-detect
  if grep -qi garmin /tmp/diag_det.out; then hits=$((hits+1)); printf "%b" "${g}#${o}"; else printf "%b" "${r}.${o}"; fi
done
echo; echo "detected: ${hits}/${SAMPLES}"
if   [ "$hits" -eq 0 ]; then bad "Watch never detected at USB level.";
  info "=> PHYSICAL: reseat the charging clip (press firmly), try another cable or USB port."
  info "   Garmin's clip is contact-fussy; a non-original/charge-only cable often fails."; echo; hdr "Verdict"; echo "  cable/physical problem"; exit 1
elif [ "$hits" -lt "$SAMPLES" ]; then warn "Intermittent detection (${hits}/${SAMPLES}).";
  info "=> PHYSICAL link is marginal (clip/cable/port). Reseat or swap before trusting auto-sync."
else ok "Stable detection (${hits}/${SAMPLES}). The physical link is good."; fi

hdr "Layer 2 — MTP read"
base="$(gp /tmp/diag_store.out '^basedir=' 40 --storage-info; awk -F= '/^basedir=/{print $2;exit}' /tmp/diag_store.out)"
if [ -z "$base" ]; then bad "Detected but storage unreadable (contention or transient). Re-run; the exporter retries."; echo; hdr "Verdict"; echo "  detected but read failed — retryable"; exit 1; fi
ok "Storage readable: $base"
VF="$base/GARMIN/Audio/VoiceNotes"
gp /tmp/diag_list.out "There are [0-9]+ file|No files" 40 --folder "$VF" --list-files
n="$(grep -cE '^#[0-9]+.*[Vv]oice[Nn]otes[0-9]+\.[Ww][Aa][Vv]' /tmp/diag_list.out)"
if grep -qE "There are [0-9]+ file" /tmp/diag_list.out; then ok "VoiceNotes folder readable: $n voice note(s)."
  grep -E '^#[0-9]+.*VoiceNotes[0-9]+\.wav' /tmp/diag_list.out | sed 's/^/      /'
else bad "Could not list VoiceNotes folder. Re-run; the exporter retries."; fi

echo; hdr "Verdict"; echo "  USB detection: ${hits}/${SAMPLES}   |   storage: $([ -n "$base" ] && echo readable || echo NO)   |   notes: ${n:-?}"
