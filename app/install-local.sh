#!/usr/bin/env bash
# Build Garmin Bridge and install it as the ONE canonical app.
#
# Every local update must go through this so there is never more than one copy
# floating around (multiple copies share the bundle id com.anneo22.garminbridge and
# confuse macOS TCC → the app re-prompts for Documents access on every open).
#
# Canonical install location: /Applications/Garmin Bridge.app
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

DEST="/Applications/Garmin Bridge.app"
BUNDLE="$HERE/src-tauri/target/release/bundle/macos/Garmin Bridge.app"

# Build ONLY the .app bundle, not the DMG. tauri build's final DMG step
# (bundle_dmg.sh → hdiutil) is flaky and can fail even on a clean tree; under
# `set -e` that failure would abort before the ditto below and silently skip the
# install, even though the .app itself built fine. The local DMG is never needed
# here, so we skip it. A real compile/build error still exits non-zero and aborts.
echo "==> Building the .app bundle only (skip flaky DMG packaging)…"
npm run tauri build -- --bundles app

[ -d "$BUNDLE" ] || { echo "ERROR: bundle not found at $BUNDLE"; exit 1; }

echo "==> Installing → $DEST"
# ditto overlays the fresh bundle onto the canonical location in place, so the app
# keeps the same path/identity across updates (no second copy is ever created).
ditto "$BUNDLE" "$DEST"

# Refresh the LaunchServices registration so Spotlight points at the one true copy.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST" 2>/dev/null || true

echo "==> Done. The one and only app lives at: $DEST"
echo "    (If macOS asks for Documents access on first open after an update, click Allow once.)"
