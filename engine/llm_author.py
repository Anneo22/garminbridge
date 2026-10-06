#!/usr/bin/env python3
"""BYO-key LLM authoring — the app's natural-language / image → spec bridge.

The `/garmin-workout` skill uses Claude in-session to turn plain English or a photo
into a workout spec. The shipped app can't call "Claude in-session", so app users
bring THEIR OWN model key (Anthropic / OpenAI / Google) and the app sends the same
shared authoring prompt (workout_authoring.md) + their input to it, gets a spec back,
then builds + pushes it through the SAME engine (workout.py).

We never call a paid API on our account — the key is the user's, stored locally.

Providers are called over plain HTTPS (urllib) so there is no SDK dependency to bundle
into the app. Each adapter is tiny: build the request, POST, pull the text out.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request
import urllib.error

import oauth_openai  # the "Sign in with ChatGPT" subscription path (OpenAI only)
import paths

HERE = os.path.dirname(os.path.abspath(__file__))
AUTHORING_MD = paths.asset("workout_authoring.md")
SETTINGS_FILE = paths.state(".app_settings.json")  # gitignored (holds the key)

# Curated defaults are used when no provider key is saved or a model-list call fails.
CURATED_MODELS = {
    "anthropic": ["claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5", "claude-fable-5"],
    "openai": ["gpt-5.5", "gpt-5.4-mini", "gpt-5.4", "gpt-5.4-nano"],
    "gemini": ["gemini-3.5-flash", "gemini-3.1-flash-lite"],
}
DEFAULT_MODEL = {provider: models[0] for provider, models in CURATED_MODELS.items()}
PROVIDERS = tuple(DEFAULT_MODEL)

# The ChatGPT subscription surface has no public model-list endpoint.
OPENAI_OAUTH_DEFAULT_MODEL = "gpt-5.5"
OPENAI_OAUTH_FALLBACK_MODEL = "gpt-5.4"
OPENAI_OAUTH_MODELS = [OPENAI_OAUTH_DEFAULT_MODEL, OPENAI_OAUTH_FALLBACK_MODEL]

_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".gif": "image/gif", ".webp": "image/webp"}


# ---- settings (provider / model / key) ----------------------------------------

def _raw_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _provider_name(s, provider=None):
    p = provider or s.get("provider") or "anthropic"
    return p if p in PROVIDERS else "anthropic"


def _api_keys_from_raw(s):
    keys = {p: "" for p in PROVIDERS}
    raw = s.get("api_keys") if isinstance(s.get("api_keys"), dict) else {}
    for p in PROVIDERS:
        v = raw.get(p, "")
        keys[p] = v if isinstance(v, str) else ""
    legacy = s.get("key", "")
    if legacy and not keys[_provider_name(s)]:
        keys[_provider_name(s)] = legacy
    return keys


def load_settings_for_provider(provider=None):
    s = _raw_settings()
    current = _provider_name(s)
    prov = _provider_name(s, provider)
    keys = _api_keys_from_raw(s)
    model = s.get("model") if prov == current else None
    return {
        "provider": prov,
        "model": model or DEFAULT_MODEL.get(prov, ""),
        "key": keys.get(prov, ""),
        "api_keys": keys,
        # OpenAI auth mode: "key" (bring-your-own API key, the always-on default) or "oauth"
        # (Sign in with ChatGPT). Only meaningful when provider == "openai".
        "openai_auth": s.get("openai_auth", "key"),
        # The model used on the OAuth/ChatGPT-backend surface — different ids from the API surface.
        "openai_oauth_model": s.get("openai_oauth_model") or OPENAI_OAUTH_DEFAULT_MODEL,
    }


def load_settings():
    return load_settings_for_provider()


def save_settings(provider=None, model=None, key=None,
                  openai_auth=None, openai_oauth_model=None):
    # Read the RAW stored dict so we preserve keys we don't touch (e.g. the openai_oauth bundle
    # owned by oauth_openai.py) instead of dropping them.
    s = _raw_settings()
    keys = _api_keys_from_raw(s)
    active_provider = _provider_name(s)
    if provider:
        prev = active_provider
        provider = _provider_name(s, provider)
        s["provider"] = provider
        active_provider = provider
        # if the provider changed and no explicit model given, reset to that provider's default
        if not model and provider != prev:
            s["model"] = DEFAULT_MODEL.get(provider, "")
    if model is not None:
        s["model"] = model
    if key is not None:
        keys[active_provider] = key
    if openai_auth is not None:
        s["openai_auth"] = openai_auth if openai_auth in ("key", "oauth") else "key"
    if openai_oauth_model is not None:
        s["openai_oauth_model"] = openai_oauth_model
    if not s.get("provider"):
        s["provider"] = "anthropic"
    if not s.get("model"):
        s["model"] = DEFAULT_MODEL.get(s["provider"], "")
    s["api_keys"] = {p: v for p, v in keys.items() if v}
    s.pop("key", None)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f)
    os.chmod(SETTINGS_FILE, 0o600)  # the key / token bundle are secrets
    return public_settings()


def public_settings(s=None):
    """Settings safe to return to the UI — never the key/token itself, just connected-state."""
    s = s or load_settings()
    oauth = oauth_openai.status()  # {connected, plan, account_id, expires_at} — no secrets
    keys = s.get("api_keys") or _api_keys_from_raw(_raw_settings())
    has_key_by_provider = {p: bool(keys.get(p)) for p in PROVIDERS}
    return {"provider": s["provider"], "model": s["model"],
            "has_key": has_key_by_provider.get(s["provider"], False),
            "has_key_by_provider": has_key_by_provider,
            "providers": list(PROVIDERS),
            "default_model": DEFAULT_MODEL, "curated_models": CURATED_MODELS,
            "openai_auth": s.get("openai_auth", "key"),
            "openai_oauth_model": s.get("openai_oauth_model") or OPENAI_OAUTH_DEFAULT_MODEL,
            "openai_oauth_default_model": OPENAI_OAUTH_DEFAULT_MODEL,
            "openai_oauth_models": OPENAI_OAUTH_MODELS,
            "openai_oauth_connected": bool(oauth.get("connected")),
            "openai_oauth_plan": oauth.get("plan")}


# ---- HTTP helper --------------------------------------------------------------

def _post(url, headers, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{e.code} from provider: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not reach provider: {e.reason}")


def _get(url, headers):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{e.code} from provider: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not reach provider: {e.reason}")


def _sort_model_items(items):
    return [mid for _, mid in sorted(items, reverse=True)]


def _openai_text_model(mid):
    bad = ("embedding", "embed", "whisper", "tts", "dall-e", "image",
           "audio", "moderation", "realtime", "transcribe", "speech")
    return mid and not any(b in mid for b in bad) and mid.startswith(("gpt-", "o"))


def _list_openai_models(key):
    data = _get("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"})
    items = []
    for m in data.get("data", []) or []:
        mid = m.get("id", "")
        if _openai_text_model(mid):
            items.append((m.get("created") or 0, mid))
    return _sort_model_items(items)


def _list_anthropic_models(key):
    data = _get("https://api.anthropic.com/v1/models",
                {"x-api-key": key, "anthropic-version": "2023-06-01"})
    items = []
    for m in data.get("data", []) or []:
        mid = m.get("id", "")
        if mid.startswith("claude") and "embedding" not in mid:
            items.append((m.get("created_at") or "", mid))
    return _sort_model_items(items)


def _list_gemini_models(key):
    url = ("https://generativelanguage.googleapis.com/v1beta/models?key="
           + urllib.parse.quote(key))
    data = _get(url, {})
    items = []
    for m in data.get("models", []) or []:
        methods = m.get("supportedGenerationMethods") or []
        name = m.get("name", "")
        mid = name.split("/", 1)[1] if name.startswith("models/") else name
        if "generateContent" in methods and mid.startswith("gemini") and "embedding" not in mid:
            items.append((m.get("createTime") or m.get("version") or "", mid))
    return _sort_model_items(items)


_MODEL_LISTERS = {
    "anthropic": _list_anthropic_models,
    "openai": _list_openai_models,
    "gemini": _list_gemini_models,
}


def model_options(provider=None, auth=None):
    s = load_settings_for_provider(provider)
    provider = s["provider"]
    if provider == "openai" and auth == "oauth":
        return {"provider": provider, "auth": "oauth", "source": "curated",
                "default_model": OPENAI_OAUTH_DEFAULT_MODEL, "models": OPENAI_OAUTH_MODELS}
    error = None
    if s.get("key"):
        try:
            models = _MODEL_LISTERS[provider](s["key"])
            if models:
                return {"provider": provider, "auth": "key", "source": "provider",
                        "default_model": DEFAULT_MODEL[provider], "models": models}
        except RuntimeError as e:
            error = str(e)
    out = {"provider": provider, "auth": "key", "source": "curated",
           "default_model": DEFAULT_MODEL[provider], "models": CURATED_MODELS[provider]}
    if error:
        out["error"] = error
    return out


def _image_block(image_path):
    ext = os.path.splitext(image_path)[1].lower()
    media = _MEDIA.get(ext, "image/png")
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return media, b64


# ---- provider adapters (each returns the model's raw text) --------------------

def _call_anthropic(model, key, prompt, user_text, image_path):
    content = []
    if image_path:
        media, b64 = _image_block(image_path)
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": media, "data": b64}})
    content.append({"type": "text", "text": user_text})
    body = {"model": model, "max_tokens": 2000, "system": prompt,
            "messages": [{"role": "user", "content": content}]}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    resp = _post("https://api.anthropic.com/v1/messages", headers, body)
    parts = [b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"]
    return "".join(parts)


def _call_openai(model, key, prompt, user_text, image_path):
    user_content = [{"type": "text", "text": user_text}]
    if image_path:
        media, b64 = _image_block(image_path)
        user_content.append({"type": "image_url",
                             "image_url": {"url": f"data:{media};base64,{b64}"}})
    body = {"model": model, "max_tokens": 2000, "messages": [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_content}]}
    headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
    resp = _post("https://api.openai.com/v1/chat/completions", headers, body)
    return resp["choices"][0]["message"]["content"]


def _call_gemini(model, key, prompt, user_text, image_path):
    # Gemini has no system role in v1beta contents; use system_instruction.
    parts = [{"text": user_text}]
    if image_path:
        media, b64 = _image_block(image_path)
        parts.append({"inline_data": {"mime_type": media, "data": b64}})
    body = {"system_instruction": {"parts": [{"text": prompt}]},
            "contents": [{"role": "user", "parts": parts}]}
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    resp = _post(url, {"content-type": "application/json"}, body)
    cand = resp["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in cand)


_ADAPTERS = {"anthropic": _call_anthropic, "openai": _call_openai, "gemini": _call_gemini}


# ---- OpenAI subscription (Sign in with ChatGPT) adapter -----------------------
# Calls the ChatGPT backend's Responses surface with the OAuth bearer token instead of an API
# key. This is an UNDOCUMENTED private surface (see oauth_openai.py): it can change, so the
# request shape below is exact and the caller falls back to the API key on failure.

CHATGPT_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"


def _oauth_responses_body(model, prompt, user_text, image_path):
    """Build the EXACT Responses-API body the ChatGPT backend requires.
    Two non-obvious, both-mandatory requirements: store:false, and (because of it)
    include:["reasoning.encrypted_content"]. The image, when present, is a data-URL STRING."""
    content = [{"type": "input_text", "text": user_text or ""}]
    if image_path:
        media, b64 = _image_block(image_path)
        content.append({"type": "input_image",
                        "image_url": f"data:{media};base64,{b64}", "detail": "high"})
    return {
        "model": model,
        "instructions": prompt,
        "input": [{"type": "message", "role": "user", "content": content}],
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "stream": True,
    }


def _aggregate_sse_text(stream_text):
    """Aggregate the assistant text from a Responses SSE stream.
    Collects `response.output_text.delta` deltas; if none, falls back to the final aggregated
    `output_text` on the `response.completed` event. `stream_text` is the raw event-stream body."""
    deltas = []
    completed_text = None
    for block in stream_text.replace("\r\n", "\n").split("\n\n"):
        data_lines = [ln[5:].lstrip() for ln in block.split("\n") if ln.startswith("data:")]
        if not data_lines:
            continue
        payload = "\n".join(data_lines).strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            evt = json.loads(payload)
        except Exception:
            continue
        etype = evt.get("type")
        if etype == "response.output_text.delta":
            d = evt.get("delta")
            if isinstance(d, str):
                deltas.append(d)
        elif etype == "response.completed":
            completed_text = _extract_completed_output_text(evt)
    if deltas:
        return "".join(deltas)
    return completed_text or ""


def _extract_completed_output_text(evt):
    """Pull the aggregated assistant text out of a `response.completed` event."""
    resp = evt.get("response", evt)
    # Prefer a top-level convenience field if present.
    if isinstance(resp.get("output_text"), str):
        return resp["output_text"]
    parts = []
    for item in resp.get("output", []) or []:
        for c in item.get("content", []) or []:
            if c.get("type") in ("output_text", "text") and isinstance(c.get("text"), str):
                parts.append(c["text"])
    return "".join(parts)


def _post_sse(url, headers, body):
    """POST a JSON body and read a text/event-stream response fully, returning the raw stream text.
    Raises RuntimeError with a friendly message on HTTP errors (usage-limit 404 called out)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        if e.code == 404 and ("usage" in detail.lower() or "limit" in detail.lower()):
            raise RuntimeError(f"usage limit reached on your ChatGPT plan: {detail}")
        raise RuntimeError(f"{e.code} from ChatGPT: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not reach ChatGPT: {e.reason}")


def _call_openai_oauth(model, prompt, user_text, image_path):
    """Author via the user's ChatGPT subscription (OAuth). Returns the model's raw text.
    Raises RuntimeError on any failure so author() can fall back to the API key path."""
    token = oauth_openai.valid_access_token()
    if not token:
        raise RuntimeError("not signed in to ChatGPT")
    acct = oauth_openai.account_id()
    if not acct:
        raise RuntimeError("no ChatGPT account id — sign in again")
    headers = {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": acct,      # omitting this 401s
        "OpenAI-Beta": "responses=experimental",
        "originator": "codex_cli_rs",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }
    residency = oauth_openai.residency()  # only workspace/enterprise accounts carry this
    if residency:
        headers["x-openai-internal-codex-residency"] = residency
    body = _oauth_responses_body(model, prompt, user_text, image_path)
    stream = _post_sse(CHATGPT_RESPONSES_URL, headers, body)
    text = _aggregate_sse_text(stream)
    if not text.strip():
        raise RuntimeError("ChatGPT returned an empty response")
    return text


def _oauth_model_not_supported(err):
    msg = str(err).lower()
    return msg.startswith("400 from chatgpt:") and "not supported" in msg


def _call_openai_oauth_with_model_fallback(model, prompt, user_text, image_path, notes=None):
    try:
        return _call_openai_oauth(model, prompt, user_text, image_path)
    except RuntimeError as e:
        if model != OPENAI_OAUTH_FALLBACK_MODEL and _oauth_model_not_supported(e):
            raw = _call_openai_oauth(OPENAI_OAUTH_FALLBACK_MODEL, prompt, user_text, image_path)
            if notes is not None:
                notes.append(f"ChatGPT did not support {model}; used {OPENAI_OAUTH_FALLBACK_MODEL}.")
            return raw
        raise


# ---- public entry -------------------------------------------------------------

def _authoring_prompt():
    with open(AUTHORING_MD) as f:
        base = f.read()
    return (base + "\n\n---\nRespond with ONLY the JSON spec object — no prose, no code "
            "fences, no explanation. It must parse as JSON.")


def _extract_json(text):
    """Pull the spec object out of the model's reply (tolerate ```json fences / stray prose)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.startswith("json"):
            t = t[4:]
        t = t.strip()
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1:
        t = t[start:end + 1]
    return json.loads(t)


def _spec_from_raw(raw):
    try:
        return _extract_json(raw)
    except Exception as e:
        raise RuntimeError(f"the model did not return a valid spec ({e}). Raw start: {raw[:200]!r}")


def author(user_text, image_path=None, settings=None, notes=None):
    """Turn user text (+ optional image) into a spec dict using the configured model.

    Routing:
      - provider == "openai" AND openai_auth == "oauth" AND signed in  -> the ChatGPT subscription
        adapter. On OAuth failure (not signed in / 401 / token blocked / usage-limit): if a BYO
        OpenAI key is ALSO set, fall back to the key adapter and append a note to `notes`; else
        raise a friendly error telling the user to sign in again or add a key.
      - otherwise -> the bring-your-own-API-key adapter (unchanged; the always-on fallback).

    Returns the parsed spec. Raises RuntimeError with a friendly message on any failure.
    `notes` (optional list) collects user-facing notes such as an OAuth->key fallback."""
    s = settings or load_settings()
    provider = s.get("provider", "anthropic")
    key = s.get("key")
    if key is None and isinstance(s.get("api_keys"), dict):
        key = s["api_keys"].get(provider, "")
    if provider not in _ADAPTERS:
        raise RuntimeError(f"unknown provider {provider!r}")
    if not (user_text or "").strip() and not image_path:
        raise RuntimeError("give a workout description or an image")

    prompt = _authoring_prompt()

    # --- OpenAI subscription (Sign in with ChatGPT) route ---
    if provider == "openai" and s.get("openai_auth") == "oauth" and oauth_openai.status().get("connected"):
        oauth_model = s.get("openai_oauth_model") or OPENAI_OAUTH_DEFAULT_MODEL
        try:
            raw = _call_openai_oauth_with_model_fallback(oauth_model, prompt, user_text or "",
                                                         image_path, notes=notes)
            return _spec_from_raw(raw)
        except RuntimeError as e:
            # OAuth path failed (401 / blocked / usage-limit / not signed in / empty).
            if key:
                if notes is not None:
                    notes.append(f"ChatGPT sign-in failed ({e}); used your OpenAI API key instead.")
                raw = _call_openai(s.get("model") or DEFAULT_MODEL["openai"], key,
                                   prompt, user_text or "", image_path)
                return _spec_from_raw(raw)
            raise RuntimeError(str(e))

    # --- bring-your-own-API-key route (all providers) ---
    if not key:
        raise RuntimeError("no API key set — add your model key in settings first")
    model = s.get("model") or DEFAULT_MODEL.get(provider, "")
    raw = _ADAPTERS[provider](model, key, prompt, user_text or "", image_path)
    return _spec_from_raw(raw)
