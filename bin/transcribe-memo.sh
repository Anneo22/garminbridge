#!/usr/bin/env bash
# transcribe-memo.sh <audio> — transcribe ONE memo to <audio>.txt using the configured
# backend. Prints the .txt path on success. Optional feature; off unless configured.
#
# Backends (GVE_TRANSCRIBE_BACKEND):
#   local (offline, free, Apple MLX):  parakeet | whisper
#   cloud (bring-your-own API key):    openai | gemini | groq | deepgram
#
# Local backends are installed into an isolated venv by bin/install-transcription.sh.
# Cloud keys come from the config file / env (GVE_<PROVIDER>_KEY).

set -uo pipefail
WAV="${1:-}"
[ -n "$WAV" ] && [ -f "$WAV" ] || { echo "transcribe: usage: transcribe-memo.sh <audio>" >&2; exit 2; }
SELF="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SELF/.." && pwd)"
VENV="$ROOT/.venv/bin"
# read backend choice + API keys + cleanup settings from the config (so cloud backends
# and transcript cleanup work both standalone and when the importer calls us).
GVE_CONFIG="${GVE_CONFIG:-$HOME/.config/garmin-voice-export/config}"
[ -f "$GVE_CONFIG" ] && . "$GVE_CONFIG"
BACKEND="${GVE_TRANSCRIBE_BACKEND:-parakeet}"
DIR="$(dirname "$WAV")"; BASE="$(basename "${WAV%.*}")"; OUT="$DIR/$BASE.txt"
PARAKEET_MODEL="${GVE_PARAKEET_MODEL:-mlx-community/parakeet-tdt-0.6b-v3}"
WHISPER_MODEL="${GVE_WHISPER_MODEL:-mlx-community/whisper-large-v3-mlx}"

# prefer the isolated venv binary, fall back to one on PATH
bin_for(){ if [ -x "$VENV/$1" ]; then echo "$VENV/$1"; else command -v "$1" 2>/dev/null; fi; }
need_key(){ [ -n "${!1:-}" ] || { echo "transcribe: $1 not set (run bin/install-transcription.sh)" >&2; exit 3; }; }

case "$BACKEND" in
  parakeet)
    p="$(bin_for parakeet-mlx)"; [ -n "$p" ] || { echo "transcribe: parakeet-mlx not installed (bin/install-transcription.sh)" >&2; exit 3; }
    "$p" "$WAV" --model "$PARAKEET_MODEL" --output-dir "$DIR" --output-format txt --output-template "{filename}" >/dev/null 2>&1 || exit 4
    ;;
  whisper)
    p="$(bin_for mlx_whisper)"; [ -n "$p" ] || { echo "transcribe: mlx-whisper not installed (bin/install-transcription.sh)" >&2; exit 3; }
    "$p" "$WAV" -f txt -o "$DIR" --model "$WHISPER_MODEL" >/dev/null 2>&1 || exit 4
    ;;
  openai)
    need_key GVE_OPENAI_KEY
    curl -fsS https://api.openai.com/v1/audio/transcriptions \
      -H "Authorization: Bearer $GVE_OPENAI_KEY" \
      -F file=@"$WAV" -F model="${GVE_OPENAI_MODEL:-whisper-1}" -F response_format=text > "$OUT" || exit 4
    ;;
  groq)
    need_key GVE_GROQ_KEY
    curl -fsS https://api.groq.com/openai/v1/audio/transcriptions \
      -H "Authorization: Bearer $GVE_GROQ_KEY" \
      -F file=@"$WAV" -F model="${GVE_GROQ_MODEL:-whisper-large-v3-turbo}" -F response_format=text > "$OUT" || exit 4
    ;;
  deepgram)
    need_key GVE_DEEPGRAM_KEY
    curl -fsS "https://api.deepgram.com/v1/listen?model=${GVE_DEEPGRAM_MODEL:-nova-2}&smart_format=true" \
      -H "Authorization: Token $GVE_DEEPGRAM_KEY" -H "Content-Type: audio/wav" --data-binary @"$WAV" \
      | /usr/bin/python3 -c 'import sys,json; print(json.load(sys.stdin)["results"]["channels"][0]["alternatives"][0]["transcript"])' > "$OUT" || exit 4
    ;;
  gemini)
    need_key GVE_GEMINI_KEY
    GVE_GEMINI_MODEL="${GVE_GEMINI_MODEL:-gemini-2.5-flash}" /usr/bin/python3 - "$WAV" "$OUT" <<'PY' || exit 4
import base64, json, os, sys, urllib.request
wav, out = sys.argv[1], sys.argv[2]
key = os.environ["GVE_GEMINI_KEY"]; model = os.environ["GVE_GEMINI_MODEL"]
data = base64.b64encode(open(wav, "rb").read()).decode()
body = {"contents":[{"parts":[{"text":"Transcribe this audio verbatim. Output only the transcript."},
        {"inline_data":{"mime_type":"audio/wav","data":data}}]}]}
req = urllib.request.Request(
    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
    data=json.dumps(body).encode(), headers={"Content-Type":"application/json"})
r = json.load(urllib.request.urlopen(req, timeout=120))
open(out,"w").write(r["candidates"][0]["content"]["parts"][0]["text"].strip()+"\n")
PY
    ;;
  *) echo "transcribe: unknown backend '$BACKEND'" >&2; exit 2 ;;
esac

[ -s "$OUT" ] || { echo "transcribe: no transcript produced" >&2; exit 5; }

# optional LLM cleanup of the raw transcript, in place (no-op unless GVE_TRANSCRIPT_CLEANUP=1).
# Never fatal: if cleanup fails the raw transcript is kept and we still report success.
if [ "${GVE_TRANSCRIPT_CLEANUP:-0}" = "1" ]; then
  GVE_CONFIG="$GVE_CONFIG" "$SELF/clean-transcript.sh" "$OUT" 1>/dev/null \
    || echo "transcribe: cleanup skipped (kept raw transcript)" >&2
fi

echo "$OUT"
