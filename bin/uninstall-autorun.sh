#!/usr/bin/env bash
# uninstall-autorun.sh — remove the auto agent (instant watcher and/or old poller).
set -uo pipefail
UID_NUM="$(id -u)"
for LABEL in com.garminvoice.watcher com.garminvoice.export; do
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || launchctl unload "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
done
rm -f /tmp/garmin_voice_export.session /tmp/garmin_voice_export.lock 2>/dev/null
rmdir /tmp/garmin_voice_export.lock 2>/dev/null || true
echo "Removed auto agent(s). Your exported memos and manifest are untouched."
