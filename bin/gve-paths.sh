#!/usr/bin/env bash
# gve-paths.sh — the single source of truth for WHERE GarminBridge writes on the Mac.
# Sourced by the engine, the activity backup, and the control CLI AFTER the config is
# loaded. Resolves the organised "Garmin Bridge" root layout while staying 100% backward
# compatible: with no root set, the old default paths are unchanged.
#
# Organised layout (when GARMIN_BRIDGE_ROOT is set):
#   <root>/[<device>/]Voice Memo      voice notes        (+ Voice Memo/Archive for handled ones)
#   <root>/[<device>/]Backups         activity .fit files
# <device> (GARMIN_BRIDGE_DEVICE) is optional: a sub-level for several watches / an Edge / users.
#
# Precedence for each destination (highest first):
#   1. an explicit GARMIN_VOICE_DEST / GARMIN_ACTIVITY_DEST  (power-user override)
#   2. the organised root layout above                        (GARMIN_BRIDGE_ROOT set)
#   3. the legacy defaults ~/Documents/{Voice Memos,Garmin Activities}
#
# Sets: GVE_VOICE_DEST, GVE_ACT_DEST, GVE_BRIDGE_BASE (root[/device], empty if no root).
gve_resolve_paths(){
  local root="${GARMIN_BRIDGE_ROOT:-}" dev="${GARMIN_BRIDGE_DEVICE:-}" base=""
  if [ -n "$root" ]; then base="$root"; [ -n "$dev" ] && base="$root/$dev"; fi
  GVE_BRIDGE_BASE="$base"

  if [ -n "${GARMIN_VOICE_DEST:-}" ];   then GVE_VOICE_DEST="$GARMIN_VOICE_DEST"
  elif [ -n "$base" ];                  then GVE_VOICE_DEST="$base/Voice Memo"
  else                                       GVE_VOICE_DEST="$HOME/Documents/Voice Memos"; fi

  if [ -n "${GARMIN_ACTIVITY_DEST:-}" ]; then GVE_ACT_DEST="$GARMIN_ACTIVITY_DEST"
  elif [ -n "$base" ];                   then GVE_ACT_DEST="$base/Backups"
  else                                        GVE_ACT_DEST="$HOME/Documents/Garmin Activities"; fi
}
