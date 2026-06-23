#!/usr/bin/env bash
# install-menubar.sh — build the menu-bar app and start it at login.
# Optional convenience UI on top of the importer; safe to skip.

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SRC="$ROOT/src/garmin-voice-menubar.swift"
APP="$ROOT/GarminBridge.app"
BIN="$APP/Contents/MacOS/garmin-voice-menubar"
CTL="$HERE/garmin-voice"
DEST="${GARMIN_VOICE_DEST:-$HOME/Documents/Voice Memos}"
LABEL="com.garminvoice.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"

mkdir -p "$APP/Contents/MacOS" 2>/dev/null || true
if [ ! -x "$BIN" ] || { [ "$SRC" -nt "$BIN" ] && [ -w "$APP/Contents/MacOS" ]; }; then
  echo "Building menu-bar app..."
  swiftc -O "$SRC" -o "$BIN" 2>/dev/null || [ -x "$BIN" ] || { echo "Failed to build menu-bar app (Xcode CLT required)"; exit 1; }
fi
if [ -w "$APP/Contents" ]; then cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>GarminBridge</string>
  <key>CFBundleIdentifier</key><string>$LABEL</string>
  <key>CFBundleExecutable</key><string>garmin-voice-menubar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PL
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$BIN</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>GVE_CTL</key><string>$CTL</string>
  </dict>
  <key>RunAtLoad</key><true/>
</dict></plist>
PL

# bootout is async — wait for it to drain, then bootstrap (retrying past the transient
# "Input/output error" that happens when the old job hasn't fully unloaded yet).
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
for _ in 1 2 3 4 5; do launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || break; sleep 1; done
ok=0; for _ in 1 2 3 4; do launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>/dev/null && { ok=1; break; }; sleep 1; done
[ "$ok" = 1 ] || launchctl bootstrap "gui/$UID_NUM" "$PLIST"     # final attempt, surface a real error
launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true
echo "Menu-bar app installed and started (look for the ◎ waveform icon in the menu bar)."
echo "  Remove with: launchctl bootout gui/$UID_NUM/$LABEL; rm -rf '$APP' '$PLIST'"
