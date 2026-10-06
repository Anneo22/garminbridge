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

resolve_stable() {
  local path="$1"
  if command -v brew &>/dev/null; then
    local bp
    bp=$(brew --prefix garminbridge 2>/dev/null || true)
    if [ -n "$bp" ] && [ -d "$bp" ]; then
      local suffix="${path#*/Cellar/*/*/}"
      if [ "$suffix" != "$path" ]; then
        local candidate="${bp}/${suffix}"
        [ -e "$candidate" ] && echo "$candidate" && return 0
      fi
    fi
  fi
  if [[ "$path" =~ ^(.*)/Cellar/([^/]+)/[^/]+/(.*)$ ]]; then
    local candidate="${BASH_REMATCH[1]}/opt/${BASH_REMATCH[2]}/${BASH_REMATCH[3]}"
    [ -e "$candidate" ] && echo "$candidate" && return 0
  fi
  echo "$path"
}
STABLE_HERE="$(resolve_stable "$HERE")"
WATCHER_SRC="$ROOT/src/garmin-usb-watcher.swift"
WATCHER_BIN="$STABLE_HERE/garmin-usb-watcher"
ON_CONNECT="$STABLE_HERE/on-connect.sh"
LABEL="com.garminvoice.watcher"
OLD_POLLER="com.garminvoice.export"     # previous 90s-poll agent, if present
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs"
UID_NUM="$(id -u)"
DEST="${GARMIN_VOICE_DEST:-$HOME/Documents/Voice Memos}"
DELETE_FLAG="${GARMIN_VOICE_DELETE:-}"   # "" | keep | now | transcribed
RETENTION="${GVE_AUDIO_RETENTION_DAYS:-}"  # "" | N days to keep local audio (transcript kept)
ACTIVITY_BACKUP="${GARMIN_ACTIVITY_BACKUP:-}"  # "" | 1 to also back up activity .fit on connect
ACT_DEST="${GARMIN_ACTIVITY_DEST:-}"           # optional custom activity destination

mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"
[ -x "$EXPORT" ] || chmod +x "$EXPORT"
for s in "$ON_CONNECT" "$HERE/backup-activities.sh"; do [ -x "$s" ] || chmod +x "$s" 2>/dev/null || true; done

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
case "$DELETE_FLAG" in
  ""|keep) "$HERE/garmin-voice" unset GARMIN_VOICE_DELETE >/dev/null ;;
  *)       "$HERE/garmin-voice" set   GARMIN_VOICE_DELETE "$DELETE_FLAG" >/dev/null ;;
esac
if [ -n "$RETENTION" ]; then "$HERE/garmin-voice" set GVE_AUDIO_RETENTION_DAYS "$RETENTION" >/dev/null
else "$HERE/garmin-voice" unset GVE_AUDIO_RETENTION_DAYS >/dev/null; fi
if [ "$ACTIVITY_BACKUP" = 1 ]; then "$HERE/garmin-voice" set GARMIN_ACTIVITY_BACKUP 1 >/dev/null
else "$HERE/garmin-voice" unset GARMIN_ACTIVITY_BACKUP >/dev/null; fi
[ -n "$ACT_DEST" ] && "$HERE/garmin-voice" set GARMIN_ACTIVITY_DEST "$ACT_DEST" >/dev/null || true

# Self-heal: if an existing plist points at a path that no longer exists,
# remove and reload it rather than leaving a dangling job behind.
if [ -f "$PLIST" ]; then
  existing_bin=$(plutil -extract ProgramArguments.0 raw "$PLIST" 2>/dev/null || true)
  if [ -n "$existing_bin" ] && [ ! -e "$existing_bin" ]; then
    echo "Removing stale LaunchAgent (target gone: $existing_bin)"
    launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
  fi
fi

[ -x "$WATCHER_BIN" ] || { echo "ERROR: watcher binary not found at $WATCHER_BIN"; exit 1; }
[ -x "$ON_CONNECT" ] || { echo "ERROR: on-connect script not found at $ON_CONNECT"; exit 1; }

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$WATCHER_BIN</string>
    <string>$ON_CONNECT</string>
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
# bootout is async — wait for it to drain, then bootstrap (retrying past the transient
# "Input/output error" that happens when the old job hasn't fully unloaded yet).
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
for _ in 1 2 3 4 5; do launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || break; sleep 1; done
ok=0; for _ in 1 2 3 4; do launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>/dev/null && { ok=1; break; }; sleep 1; done
[ "$ok" = 1 ] || launchctl bootstrap "gui/$UID_NUM" "$PLIST"     # final attempt, surface a real error
launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true

echo
echo "Installed the instant on-connect watcher."
echo "  Trigger: fires within a few seconds of plugging in a Garmin watch"
echo "  Dest:    $DEST"
echo "  Delete:  $([ -n "$DELETE_FLAG" ] && echo "$DELETE_FLAG" || echo 'keep (export only)') from watch   Local retention: ${RETENTION:-off}   (adjust from the menu-bar app)"
echo "  Activities: $([ "$ACTIVITY_BACKUP" = 1 ] && echo "backing up to ${ACT_DEST:-$HOME/Documents/Garmin Activities}" || echo 'off')"
echo "  Logs:    tail -f '$DEST/export.log'   |   $LOGDIR/garmin-voice-export.err.log"
echo "  Remove:  $HERE/uninstall-autorun.sh"
