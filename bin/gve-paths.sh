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

# ---- where the gphoto2 RUNTIME lives ------------------------------------------------
# Every watch operation shells out to gphoto2. When the app spawns a script it injects
# PATH/CAMLIBS/IOLIBS itself (lib.rs `cli_command`), but the same scripts are also a
# product surface on their own: `garminbridge` symlinked onto someone's PATH runs with no
# app in the picture and gets none of that. So they locate the runtime from their own
# position on disk, and treat Homebrew as a development fallback BEHIND it, never ahead.
#
# gve_script_dir resolves symlinks first, because that is exactly how the CLI is
# installed: $0 then names the symlink, whose directory is nowhere near the runtime.
gve_script_dir(){
  local src="$1" dir
  while [ -L "$src" ]; do
    dir="$(cd -P "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    case "$src" in /*) ;; *) src="$dir/$src" ;; esac
  done
  (cd -P "$(dirname "$src")" && pwd)
}

# Point PATH/CAMLIBS/IOLIBS at the bundled gphoto2. Pass the caller's own path, normally
# "${BASH_SOURCE[0]}". Safe before the config is loaded, and safe to call twice.
#
# The two layouts put the runtime in the same place relative to the calling script, since
# bin/ and engine/ are siblings in both:
#   installed app   <App>/Contents/Resources/{bin,engine}  ->  ../gphoto2
#   dev checkout    <repo>/{bin,engine}                    ->  ../vendor/gphoto2
# Without CAMLIBS/IOLIBS the binary runs and then finds no camera drivers, so the watch is
# invisible with no error worth the name — PATH alone is not enough.
gve_resolve_runtime(){
  local dir cand runtime=""
  dir="$(gve_script_dir "${1:-$0}")"
  for cand in "$dir/../gphoto2" "$dir/../vendor/gphoto2"; do
    if [ -x "$cand/bin/gphoto2" ]; then runtime="$(cd -P "$cand" && pwd)"; break; fi
  done
  if [ -n "$runtime" ]; then
    export GVE_GPHOTO2_RUNTIME="$runtime"
    export PATH="$runtime/bin${PATH:+:$PATH}"
    export CAMLIBS="$runtime/camlibs"
    export IOLIBS="$runtime/iolibs"
  fi
  # Homebrew last: a development Mac may have it, a stranger's will not, and it must never
  # shadow the runtime we ship.
  export PATH="${PATH:+$PATH:}/opt/homebrew/bin:/usr/local/bin"
}

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
