#!/usr/bin/env bash
# install-autorun.sh — install the INSTANT on-connect agent: a small IOKit watcher
# (garmin-usb-watcher) that runs the importer the moment a Garmin USB device attaches.
# No polling. KeepAlive restarts the watcher if it dies; on restart it also fires for
# an already-connected watch, so it self-heals.
#
# DESTINATION defaults to ~/Documents/Voice Memos. Under ~/Documents the agent needs
# Full Disk Access for /bin/bash (macOS TCC). Set GARMIN_VOICE_DEST to a home-root
# path to avoid that.

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
EXPORT="$HERE/export-voice-notes.sh"
WATCHER_SRC="$ROOT/src/garmin-usb-watcher.swift"
WATCHER_BIN="$HERE/garmin-usb-watcher"
LABEL="com.garminvoice.watcher"
OLD_POLLER="com.garminvoice.export"     # previous 90s-poll agent, if present
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs"
UID_NUM="$(id -u)"
DEST="${GARMIN_VOICE_DEST:-$HOME/Documents/Voice Memos}"
DELETE_FLAG="${GARMIN_VOICE_DELETE:-}"   # set to "--delete" to remove notes from watch

mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"
[ -x "$EXPORT" ] || chmod +x "$EXPORT"

# build the watcher if needed (skip when a prebuilt binary exists in a read-only
# location, e.g. a Homebrew cellar; rebuild only when our dir is writable and src is newer)
if [ ! -x "$WATCHER_BIN" ] || { [ "$WATCHER_SRC" -nt "$WATCHER_BIN" ] && [ -w "$(dirname "$WATCHER_BIN")" ]; }; then
  echo "Building garmin-usb-watcher..."
  swiftc -O "$WATCHER_SRC" -o "$WATCHER_BIN" 2>/dev/null || [ -x "$WATCHER_BIN" ] || { echo "Failed to build watcher (Xcode CLT required)"; exit 1; }
fi

# stop the old poller if it's still installed
launchctl bootout "gui/$UID_NUM/$OLD_POLLER" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$OLD_POLLER.plist" 2>/dev/null || true

# Settings live in the config the importer reads, so the menu-bar app can change them
# live without reinstalling. The plist stays minimal (just runs the importer).
"$HERE/garmin-voice" set GARMIN_VOICE_DEST "$DEST" >/dev/null
if [ -n "$DELETE_FLAG" ]; then "$HERE/garmin-voice" set GARMIN_VOICE_DELETE "--delete" >/dev/null
else "$HERE/garmin-voice" unset GARMIN_VOICE_DELETE >/dev/null; fi

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$WATCHER_BIN</string>
    <string>$EXPORT</string>
    <string>--auto</string>
  </array>
  <key>KeepAlive</key>        <true/>
  <key>RunAtLoad</key>        <true/>
  <key>ProcessType</key>      <string>Background</string>
  <key>StandardOutPath</key>  <string>$LOGDIR/garmin-voice-export.out.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/garmin-voice-export.err.log</string>
</dict>
</plist>
PLIST

echo "Wrote $PLIST"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true

echo
echo "Installed the instant on-connect watcher."
echo "  Trigger: fires within a few seconds of plugging in a Garmin watch"
echo "  Dest:    $DEST"
echo "  Delete:  $([ -n "$DELETE_FLAG" ] && echo on || echo 'off (export only)')   (toggle from the menu-bar app)"
echo "  Logs:    tail -f '$DEST/export.log'   |   $LOGDIR/garmin-voice-export.err.log"
echo "  Remove:  $HERE/uninstall-autorun.sh"
