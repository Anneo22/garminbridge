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

  # settings/profile backup (sport faces, device settings, the device's own backup blob, etc.)
  if [ -n "${GARMIN_SETTINGS_DEST:-}" ]; then GVE_SET_DEST="$GARMIN_SETTINGS_DEST"
  elif [ -n "$base" ];                    then GVE_SET_DEST="$base/Settings"
  else                                         GVE_SET_DEST="$HOME/Documents/Garmin Settings"; fi
}

gve_pause_flag(){
  printf '%s\n' "${GVE_PAUSE_FLAG:-${PAUSE_FLAG:-$HOME/.config/garmin-voice-export/paused}}"
}

gve_pause_deadline_epoch(){
  local clean iso main tz
  clean="$(printf '%s' "${1:-}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  [ -n "$clean" ] || return 1
  case "$clean" in
    *[!0-9]*) ;;
    *) printf '%s\n' "$clean"; return 0 ;;
  esac
  iso="$clean"
  case "$iso" in *Z) iso="${iso%Z}+0000" ;; esac
  case "$iso" in *[+-][0-9][0-9]:[0-9][0-9]) iso="${iso%:*}${iso##*:}" ;; esac
  if [[ "$iso" =~ ^(.*)([+-][0-9][0-9][0-9][0-9])$ ]]; then
    main="${BASH_REMATCH[1]}"; tz="${BASH_REMATCH[2]}"
  else
    main="$iso"; tz=""
  fi
  main="${main%%.*}"
  if [ -n "$tz" ]; then
    date -j -f "%Y-%m-%dT%H:%M:%S%z" "${main}${tz}" "+%s" 2>/dev/null
  else
    date -j -f "%Y-%m-%dT%H:%M:%S" "$main" "+%s" 2>/dev/null ||
      date -j -f "%Y-%m-%d %H:%M:%S" "$main" "+%s" 2>/dev/null
  fi
}

is_paused(){
  local flag raw trimmed deadline now
  flag="$(gve_pause_flag)"
  [ -f "$flag" ] || return 1
  raw="$(cat "$flag" 2>/dev/null || true)"
  trimmed="$(printf '%s' "$raw" | tr -d '[:space:]')"
  [ -n "$trimmed" ] || return 0
  deadline="$(gve_pause_deadline_epoch "$trimmed" 2>/dev/null || true)"
  case "$deadline" in ""|*[!0-9]*) return 0 ;; esac
  now="$(date +%s)"
  [ "$now" -lt "$deadline" ] && return 0
  rm -f "$flag"
  return 1
}
