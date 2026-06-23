#!/usr/bin/env bash
# install.sh — one-command setup for GarminBridge.
# Installs dependencies, asks a few questions, and sets up the on-connect importer.
# Re-run anytime to change options. Everything it does is reversible.

set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HERE/bin"

echo "GarminBridge setup"
echo "=================="

# --- dependencies ---
if ! command -v brew >/dev/null; then
  echo "Homebrew is required: https://brew.sh"; exit 1
fi
for f in gphoto2 terminal-notifier; do
  command -v "$f" >/dev/null || { echo "Installing $f..."; brew install "$f"; }
done
xcrun -f swiftc >/dev/null 2>&1 || echo "NOTE: the instant-on-connect watcher needs Xcode CLT — run: xcode-select --install"

ask(){ local q="$1" def="$2" a; printf "%s [%s]: " "$q" "$def"; read -r a; echo "${a:-$def}"; }
# yesno <question> <default Y|N> — default applies on empty input
yesno(){ local q="$1" def="$2" a hint; [ "$def" = Y ] && hint="Y/n" || hint="y/N"
  printf "%s (%s): " "$q" "$hint"; read -r a; a="${a:-$def}"; [[ "$a" =~ ^[Yy] ]]; }

echo
DEST="$(ask "Where should voice memos be saved?" "$HOME/Documents/Voice Memos")"

# --- optional: transcription (asked first, so the options below that depend on it make sense) ---
echo
if yesno "Set up transcription now (local model or cloud key)?" N; then
  bash "$BIN/install-transcription.sh"
fi

# --- delete-from-watch policy ---
echo
echo "Remove each memo from the watch after import?"
echo "  1) No  — keep memos on the watch (default)"
echo "  2) Yes — after a verified local copy"
echo "  3) Yes — but only once it has also been transcribed"
case "$(ask "Choice" 1)" in
  2) DEL="now" ;;
  3) DEL="transcribed" ;;
  *) DEL="" ;;
esac

# --- local-disk retention ---
echo
RET=""
if yesno "Also auto-delete the local audio after a while (keeps the transcript if transcription is on)?" N; then
  RET="$(ask "Delete local .wav after how many days? (0 = as soon as it is transcribed)" 30)"
fi

# --- activity backup ---
echo
ACT=""
if yesno "Also back up your activity .fit files to the Mac on connect (copy-only, never deletes)?" N; then
  ACT="1"
  ACTDEST="$(ask "Where should activity files be saved?" "$HOME/Documents/Garmin Activities")"
fi
echo

# --- core agent ---
GARMIN_VOICE_DEST="$DEST" GARMIN_VOICE_DELETE="$DEL" GVE_AUDIO_RETENTION_DAYS="$RET" \
  GARMIN_ACTIVITY_BACKUP="$ACT" GARMIN_ACTIVITY_DEST="${ACTDEST:-}" bash "$BIN/install-autorun.sh"

# --- optional: menu-bar app ---
echo
if yesno "Install the menu-bar app?" Y; then
  GARMIN_VOICE_DEST="$DEST" bash "$BIN/install-menubar.sh"
fi

echo
echo "All set. Plug in your Garmin watch and new memos import automatically to:"
echo "  $DEST"
echo "Controls: $BIN/garmin-voice {status|pause|resume|free|sync}"
