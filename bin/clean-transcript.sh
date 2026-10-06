#!/usr/bin/env bash
# clean-transcript.sh <txt> — optional LLM cleanup of a raw transcript, IN PLACE.
# Fixes punctuation/capitalisation/paragraphs, removes filler ("um", "uh") and false
# starts, fixes obvious speech-to-text slips from context. It is told NOT to change
# meaning, add/remove information, summarise, or translate. The audio stays the source
# of truth; if cleanup fails the raw transcript is left untouched.
#
# Off unless GVE_TRANSCRIPT_CLEANUP=1. Provider via GVE_CLEANUP_BACKEND
# (openai | groq | anthropic | gemini); key from GVE_<PROVIDER>_KEY (same keys the
# cloud transcription backends use). Set GVE_TRANSCRIPT_KEEP_RAW=1 to also keep the
# untouched transcript next to it as <name>.raw.txt.

set -uo pipefail
TXT="${1:-}"
[ -n "$TXT" ] && [ -f "$TXT" ] || { echo "clean-transcript: usage: clean-transcript.sh <txt>" >&2; exit 2; }

# read provider/keys from the config if present (so it works standalone too)
GVE_CONFIG="${GVE_CONFIG:-$HOME/.config/garmin-voice-export/config}"
[ -f "$GVE_CONFIG" ] && . "$GVE_CONFIG"

[ "${GVE_TRANSCRIPT_CLEANUP:-0}" = "1" ] || exit 0          # feature off -> no-op success
[ -s "$TXT" ] || exit 0                                      # empty transcript -> nothing to do

BACKEND="${GVE_CLEANUP_BACKEND:-openai}"
case "$BACKEND" in
  openai)    KEY="${GVE_OPENAI_KEY:-}";    MODEL="${GVE_CLEANUP_MODEL:-gpt-4o-mini}" ;;
  groq)      KEY="${GVE_GROQ_KEY:-}";      MODEL="${GVE_CLEANUP_MODEL:-llama-3.3-70b-versatile}" ;;
  anthropic) KEY="${GVE_ANTHROPIC_KEY:-}"; MODEL="${GVE_CLEANUP_MODEL:-claude-3-5-haiku-latest}" ;;
  gemini)    KEY="${GVE_GEMINI_KEY:-}";    MODEL="${GVE_CLEANUP_MODEL:-gemini-2.5-flash}" ;;
  *) echo "clean-transcript: unknown backend '$BACKEND'" >&2; exit 2 ;;
esac
[ -n "$KEY" ] || { echo "clean-transcript: no API key for $BACKEND (set GVE_$(echo "$BACKEND" | tr a-z A-Z)_KEY)" >&2; exit 3; }

PROMPT='You are a transcript editor. Clean up the following voice-memo transcript: fix punctuation, capitalisation, and paragraph breaks; remove filler words (um, uh), false starts, and stutters; fix obvious speech-to-text errors using context. Do NOT change the meaning, do NOT add or remove information, do NOT summarise, and do NOT translate (keep the original language). Output ONLY the cleaned transcript text, with no preamble, labels, or quotation marks.'

cleaned="$(GVE_CLEANUP_KEY="$KEY" /usr/bin/python3 - "$BACKEND" "$MODEL" "$PROMPT" "$TXT" <<'PY' || true
import json, os, sys, urllib.request, urllib.error
backend, model, prompt, txt = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
key = os.environ["GVE_CLEANUP_KEY"]
text = open(txt, encoding="utf-8").read().strip()
if not text:
    sys.exit(1)

def post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)

try:
    if backend in ("openai", "groq"):
        url = ("https://api.openai.com/v1/chat/completions" if backend == "openai"
               else "https://api.groq.com/openai/v1/chat/completions")
        r = post(url, {"model": model, "temperature": 0.2,
                       "messages": [{"role": "system", "content": prompt},
                                    {"role": "user", "content": text}]},
                 {"Authorization": f"Bearer {key}"})
        out = r["choices"][0]["message"]["content"]
    elif backend == "anthropic":
        r = post("https://api.anthropic.com/v1/messages",
                 {"model": model, "max_tokens": 8192, "system": prompt,
                  "messages": [{"role": "user", "content": text}]},
                 {"x-api-key": key, "anthropic-version": "2023-06-01"})
        out = r["content"][0]["text"]
    elif backend == "gemini":
        r = post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                 {"contents": [{"parts": [{"text": prompt + "\n\n---\n\n" + text}]}],
                  "generationConfig": {"temperature": 0.2}}, {})
        out = r["candidates"][0]["content"]["parts"][0]["text"]
    else:
        sys.exit(2)
except (urllib.error.URLError, KeyError, IndexError, ValueError) as e:
    sys.stderr.write(f"clean-transcript: {backend} request failed: {e}\n")
    sys.exit(4)

out = out.strip()
if not out:
    sys.exit(5)
sys.stdout.write(out)
PY
)"

# only replace the transcript if cleanup actually returned something
if [ -n "${cleaned// /}" ]; then
  [ "${GVE_TRANSCRIPT_KEEP_RAW:-0}" = "1" ] && cp -f "$TXT" "${TXT%.txt}.raw.txt"
  printf '%s\n' "$cleaned" > "$TXT"
else
  echo "clean-transcript: no cleaned text returned; kept raw transcript" >&2
  exit 4
fi
