#!/usr/bin/env bash
# install-transcription.sh, set up transcription. Two ways to call it:
#   interactive (no args): pick a backend, install it, write the config.
#   headless (used by the menu-bar app): --backend <name> [--key <key>], no prompts,
#     posts a desktop notification when done.
# Local backends (parakeet, whisper) are not available for one-click setup yet; they
# report the manual prerequisite clearly instead of trying to install system tools.
# Cloud backends (openai, gemini, groq, deepgram) just store your API key in the config.

set -uo pipefail
CONFIG="${GVE_CONFIG:-$HOME/.config/garmin-voice-export/config}"
mkdir -p "$(dirname "$CONFIG")"

local_backend_unavailable(){
  echo "Local $1 transcription is not available for one-click setup yet. It needs a one-time manual install of Python, ffmpeg, and the $1 backend." >&2
  return 1
}
# upsert one KEY=VALUE WITHOUT clobbering other settings, the config is shared with the
# importer (dest / delete policy / retention). Single-quoted so values with spaces survive.
cfg_put(){ mkdir -p "$(dirname "$CONFIG")"; touch "$CONFIG"
  grep -vE "^$1=" "$CONFIG" > "$CONFIG.tmp" 2>/dev/null || true; mv "$CONFIG.tmp" "$CONFIG"
  printf "%s='%s'\n" "$1" "$2" >> "$CONFIG"; chmod 600 "$CONFIG"; }
write_config(){  # backend [keyvar keyval]
  cfg_put GVE_TRANSCRIBE 1
  cfg_put GVE_TRANSCRIBE_BACKEND "$1"
  [ -n "${3:-}" ] && cfg_put "$2" "$3"   # only write the key when one is given (else reuse existing)
}
ask_key(){ local v; printf "Paste your %s API key: " "$1"; read -rs v; echo; echo "$v"; }
notify_done(){ # message
  if command -v terminal-notifier >/dev/null 2>&1; then terminal-notifier -title "GarminBridge" -message "$1" >/dev/null 2>&1 || true
  else osascript -e "display notification \"${1//\"/}\" with title \"GarminBridge\"" >/dev/null 2>&1 || true; fi; }
key_var_for(){ case "$1" in openai) echo GVE_OPENAI_KEY;; gemini) echo GVE_GEMINI_KEY;; groq) echo GVE_GROQ_KEY;; deepgram) echo GVE_DEEPGRAM_KEY;; esac; }

# install one backend non-interactively (used headless and by the interactive flow)
install_backend(){ # backend [key]
  local b="$1" k="${2:-}"
  case "$b" in
    parakeet|whisper) local_backend_unavailable "$b" ;;
    openai|gemini|groq|deepgram) write_config "$b" "$(key_var_for "$b")" "$k" ;;
    *) echo "unknown backend: $b" >&2; return 2 ;;
  esac
}

# ---- headless mode: --backend <name> [--key <key>] ----
BACKEND_ARG=""; KEY_ARG=""
while [ $# -gt 0 ]; do case "$1" in
  --backend) BACKEND_ARG="${2:-}"; shift 2 ;;
  --key)     KEY_ARG="${2:-}";     shift 2 ;;
  *) shift ;;
esac; done

if [ -n "$BACKEND_ARG" ]; then
  if install_backend "$BACKEND_ARG" "$KEY_ARG"; then
    notify_done "Transcription on ($BACKEND_ARG)."
  else
    notify_done "Could not set up $BACKEND_ARG transcription."; exit 1
  fi
  exit 0
fi

# ---- interactive mode ----
echo "Transcription backend:"
echo "  1) Parakeet   local, offline, free (Apple MLX) [recommended]"
echo "  2) Whisper    local, offline, free (mlx-whisper, more languages)"
echo "  3) OpenAI     cloud, your API key"
echo "  4) Gemini     cloud, your API key"
echo "  5) Groq       cloud, your API key (whisper-large-v3-turbo, fast/cheap)"
echo "  6) Deepgram   cloud, your API key"
printf "Choice [1]: "; read -r choice; choice="${choice:-1}"

case "$choice" in
  1) install_backend parakeet ;;
  2) install_backend whisper ;;
  3) install_backend openai   "$(ask_key OpenAI)" ;;
  4) install_backend gemini   "$(ask_key Gemini)" ;;
  5) install_backend groq     "$(ask_key Groq)" ;;
  6) install_backend deepgram "$(ask_key Deepgram)" ;;
  *) echo "invalid choice"; exit 1 ;;
esac
rc=$?
[ $rc -eq 0 ] || exit $rc
echo "Updated $CONFIG"

printf "Also write each memo into an Obsidian vault? Enter vault path (or leave blank): "
read -r vault
if [ -n "$vault" ]; then cfg_put GVE_OBSIDIAN_VAULT "${vault/#\~/$HOME}"; fi

# optional: LLM cleanup of the raw transcript (punctuation, drop "um"/"uh", fix slips)
echo
printf "Clean up transcripts with an LLM (fix punctuation, remove filler), using your API key? (y/N): "
read -r dc
if [[ "$dc" =~ ^[Yy] ]]; then
  echo "Cleanup provider:"
  echo "  1) OpenAI     gpt-4o-mini"
  echo "  2) Groq       llama-3.3-70b (fast, cheap)"
  echo "  3) Anthropic  claude-3-5-haiku"
  echo "  4) Gemini     gemini-2.5-flash"
  printf "Choice [1]: "; read -r cc; cc="${cc:-1}"
  case "$cc" in
    2) cb=groq;      cv=GVE_GROQ_KEY ;;
    3) cb=anthropic; cv=GVE_ANTHROPIC_KEY ;;
    4) cb=gemini;    cv=GVE_GEMINI_KEY ;;
    *) cb=openai;    cv=GVE_OPENAI_KEY ;;
  esac
  if grep -qE "^$cv=" "$CONFIG" 2>/dev/null; then echo "Reusing the $cb API key already in your config."
  else k="$(ask_key "$cb")"; cfg_put "$cv" "$k"; fi
  cfg_put GVE_TRANSCRIPT_CLEANUP 1
  cfg_put GVE_CLEANUP_BACKEND "$cb"
  echo "Transcript cleanup ON via $cb."
fi

echo
echo "Done. Transcription is ON. New memos will be transcribed automatically on import."
