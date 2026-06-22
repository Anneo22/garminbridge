#!/usr/bin/env bash
# on-connect.sh — what the USB watcher runs when a Garmin watch attaches.
# Imports voice notes, then (if enabled) backs up activities. They run one after the
# other, never at the same time, so they don't fight over the watch's single USB channel.

set -uo pipefail
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
GVE_CONFIG="${GVE_CONFIG:-$HOME/.config/garmin-voice-export/config}"
[ -f "$GVE_CONFIG" ] && . "$GVE_CONFIG"

"$SELF_DIR/export-voice-notes.sh" --auto || true
[ "${GARMIN_ACTIVITY_BACKUP:-0}" = "1" ] && "$SELF_DIR/backup-activities.sh" --auto || true
exit 0
