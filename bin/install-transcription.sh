#!/usr/bin/env bash
# install-transcription.sh — opt into transcription. Interactive: pick a backend,
# install it (local backends go in an isolated venv; nothing else is touched), and
# write the config the importer reads. Re-run anytime to change backend/keys.

set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv"
CONFIG="${GVE_CONFIG:-$HOME/.config/garmin-voice-export/config}"
mkdir -p "$(dirname "$CONFIG")"

echo "Transcription backend:"
echo "  1) Parakeet  — local, offline, free (NVIDIA Parakeet TDT 0.6B v3 via Apple MLX) [recommended]"
echo "  2) Whisper   — local, offline, free (mlx-whisper, more languages)"
echo "  3) OpenAI    — cloud, your API key"
echo "  4) Gemini    — cloud, your API key"
echo "  5) Groq      — cloud, your API key (whisper-large-v3-turbo, fast/cheap)"
echo "  6) Deepgram  — cloud, your API key"
printf "Choice [1]: "; read -r choice; choice="${choice:-1}"

ensure_ffmpeg(){ command -v ffmpeg >/dev/null || { echo "Installing ffmpeg..."; brew install ffmpeg; }; }
ensure_venv(){
  command -v python3 >/dev/null || { echo "python3 required (xcode-select --install or brew install python)"; exit 1; }
  [ -d "$VENV" ] || python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
}
# upsert one KEY=VALUE WITHOUT clobbering other settings — the config is shared with the
# importer (dest / delete policy / retention). Single-quoted so values with spaces survive.
cfg_put(){ mkdir -p "$(dirname "$CONFIG")"; touch "$CONFIG"
  grep -vE "^$1=" "$CONFIG" > "$CONFIG.tmp" 2>/dev/null || true; mv "$CONFIG.tmp" "$CONFIG"
  printf "%s='%s'\n" "$1" "$2" >> "$CONFIG"; chmod 600 "$CONFIG"; }
write_config(){  # backend [keyvar keyval]
  cfg_put GVE_TRANSCRIBE 1
  cfg_put GVE_TRANSCRIBE_BACKEND "$1"
  [ -n "${2:-}" ] && cfg_put "$2" "$3"
  echo "Updated $CONFIG"
}
ask_key(){ local v; printf "Paste your %s API key: " "$1"; read -rs v; echo; echo "$v"; }

case "$choice" in
  1) ensure_ffmpeg; ensure_venv; echo "Installing parakeet-mlx (first transcribe downloads the model ~600MB)..."
     "$VENV/bin/pip" install -q -U parakeet-mlx; write_config parakeet ;;
  2) ensure_ffmpeg; ensure_venv; echo "Installing mlx-whisper..."
     "$VENV/bin/pip" install -q -U mlx-whisper; write_config whisper ;;
  3) k="$(ask_key OpenAI)";   write_config openai   GVE_OPENAI_KEY   "$k" ;;
  4) k="$(ask_key Gemini)";   write_config gemini   GVE_GEMINI_KEY   "$k" ;;
  5) k="$(ask_key Groq)";     write_config groq     GVE_GROQ_KEY     "$k" ;;
  6) k="$(ask_key Deepgram)"; write_config deepgram GVE_DEEPGRAM_KEY "$k" ;;
  *) echo "invalid choice"; exit 1 ;;
esac

printf "Also write each memo into an Obsidian vault? Enter vault path (or leave blank): "
read -r vault
if [ -n "$vault" ]; then cfg_put GVE_OBSIDIAN_VAULT "${vault/#\~/$HOME}"; fi

# --- optional: LLM cleanup of the raw transcript (punctuation, drop "um"/"uh", fix slips) ---
echo
printf "Clean up transcripts with an LLM (fix punctuation, remove filler), using your API key? (y/N): "
read -r dc
if [[ "$dc" =~ ^[Yy] ]]; then
  echo "Cleanup provider:"
  echo "  1) OpenAI    — gpt-4o-mini"
  echo "  2) Groq      — llama-3.3-70b (fast, cheap)"
  echo "  3) Anthropic — claude-3-5-haiku"
  echo "  4) Gemini    — gemini-2.5-flash"
  printf "Choice [1]: "; read -r cc; cc="${cc:-1}"
  case "$cc" in
    2) cb=groq;      cv=GVE_GROQ_KEY ;;
    3) cb=anthropic; cv=GVE_ANTHROPIC_KEY ;;
    4) cb=gemini;    cv=GVE_GEMINI_KEY ;;
    *) cb=openai;    cv=GVE_OPENAI_KEY ;;
  esac
  if grep -qE "^$cv=" "$CONFIG" 2>/dev/null; then
    echo "Reusing the $cb API key already in your config."
  else
    k="$(ask_key "$cb")"; cfg_put "$cv" "$k"
  fi
  cfg_put GVE_TRANSCRIPT_CLEANUP 1
  cfg_put GVE_CLEANUP_BACKEND "$cb"
  echo "Transcript cleanup ON via $cb."
fi

echo
echo "Done. Transcription is ON. Test it on an existing memo:"
echo "  GVE_CONFIG='$CONFIG' bin/transcribe-memo.sh '<some>.wav'"
echo "New memos will be transcribed automatically on import."
