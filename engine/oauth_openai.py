#!/usr/bin/env python3
"""Sign in with ChatGPT — OpenAI OAuth (PKCE) so a user authors workouts with their
ChatGPT Plus/Pro subscription instead of a paid API key.

This is the SUBSCRIPTION path for the "Describe" feature. It reuses the same public OAuth
client the Codex CLI uses (a PKCE flow, no client secret), then calls the ChatGPT backend's
Responses surface with the resulting bearer token. The bring-your-own-API-key path in
llm_author.py stays as the always-on fallback for all three providers.

Honest caveats, baked in as behaviour (not just comments):
  - This is an UNDOCUMENTED private surface. OpenAI can change or block it at any time; on a
    401 / blocked-token / usage-limit the caller falls back to the user's own API key.
  - Model names drift — the model is configurable in settings, we only default to one.
  - Token lifetime comes from the server's expires_in; we never hardcode it.

All stdlib: urllib, hashlib, secrets, base64, json, http.server, webbrowser, threading, time.
No SDK, no pip deps (matches llm_author.py).
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import webbrowser
import paths

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = paths.state(".app_settings.json")  # gitignored (holds the token bundle)

# Public OAuth client (PKCE, no secret) — the same client the Codex CLI uses.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_HOST = "localhost"
REDIRECT_PORT = 1455
REDIRECT_PATH = "/auth/callback"
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"
SCOPE = "openid profile email offline_access"

# The auth namespace inside the id_token's claims.
_AUTH_CLAIM = "https://api.openai.com/auth"

# Refresh the access token when it's within this many seconds of expiry.
_REFRESH_SKEW = 60


# ---- PKCE + JWT helpers -------------------------------------------------------

def _b64url(raw: bytes) -> str:
    """base64url with padding stripped (RFC 7636 / JWT convention)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pkce_pair():
    """Return (verifier, challenge) where challenge = base64url(sha256(verifier)), no padding."""
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _jwt_claims(token: str) -> dict:
    """Decode a JWT's payload (middle segment) WITHOUT verifying the signature.
    We only read our own freshly-issued id_token to pull account_id/plan out of it."""
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("not a JWT")
    seg = parts[1]
    seg += "=" * (-len(seg) % 4)  # restore base64 padding
    return json.loads(base64.urlsafe_b64decode(seg.encode("ascii")))


def _account_from_id_token(id_token: str):
    """Pull (account_id, plan, residency) out of the id_token's auth claim.
    residency is None unless the account is a workspace/enterprise one that carries it."""
    claims = _jwt_claims(id_token)
    auth = claims.get(_AUTH_CLAIM, {}) or {}
    account_id = auth.get("chatgpt_account_id")
    plan = auth.get("chatgpt_plan_type")
    residency = auth.get("chatgpt_data_residency") or auth.get("chatgpt_compute_residency")
    return account_id, plan, residency


# ---- authorize URL ------------------------------------------------------------

def build_authorize_url():
    """Build the authorize URL for the PKCE flow.
    Returns (url, verifier, state) — verifier + state must be kept for the code exchange."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_hex(16)
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "codex_cli_rs",
        "state": state,
    }
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    return url, verifier, state


# ---- token endpoint (exchange + refresh) --------------------------------------

def _post_form(url: str, form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{e.code} from OpenAI auth: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not reach OpenAI auth: {e.reason}")


def _exchange_code(code: str, verifier: str) -> dict:
    return _post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
    })


def _refresh(refresh_token: str) -> dict:
    return _post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    })


def _bundle_from_tokens(tok: dict, prev: dict | None = None) -> dict:
    """Assemble the stored bundle from a token response. Falls back to the previous bundle's
    id_token/account fields on a refresh that omits them (refresh often returns no id_token)."""
    prev = prev or {}
    id_token = tok.get("id_token") or prev.get("id_token", "")
    account_id = prev.get("account_id")
    plan = prev.get("plan")
    residency = prev.get("residency")
    if tok.get("id_token"):
        account_id, plan, residency = _account_from_id_token(tok["id_token"])
    expires_in = int(tok.get("expires_in") or 0)
    return {
        "access_token": tok.get("access_token", ""),
        "refresh_token": tok.get("refresh_token") or prev.get("refresh_token", ""),
        "id_token": id_token,
        "account_id": account_id,
        "plan": plan,
        "residency": residency,
        "expires_at": int(time.time()) + expires_in if expires_in else prev.get("expires_at", 0),
    }


# ---- token store (in .app_settings.json under "openai_oauth") ------------------

def _read_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_settings(s: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f)
    os.chmod(SETTINGS_FILE, 0o600)  # the token bundle is a secret


def _load_bundle() -> dict | None:
    return _read_settings().get("openai_oauth")


def _store_bundle(bundle: dict | None):
    s = _read_settings()
    if bundle is None:
        s.pop("openai_oauth", None)
    else:
        s["openai_oauth"] = bundle
    _write_settings(s)


# ---- the blocking browser login -----------------------------------------------

_SUCCESS_HTML = (
    "<!doctype html><meta charset='utf-8'>"
    "<title>GarminBridge — signed in</title>"
    "<style>body{font-family:-apple-system,system-ui,sans-serif;background:#faf9f7;color:#1a1a1a;"
    "display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}"
    ".card{text-align:center;max-width:420px;padding:40px}"
    "h1{font-size:20px;margin:0 0 10px}p{color:#666;font-size:14px;line-height:1.5;margin:0}</style>"
    "<div class='card'><h1>Signed in to ChatGPT ✓</h1>"
    "<p>GarminBridge can now author workouts with your ChatGPT subscription. "
    "You can close this tab and return to the app.</p></div>"
)
_ERROR_HTML = (
    "<!doctype html><meta charset='utf-8'><title>GarminBridge — sign-in failed</title>"
    "<style>body{font-family:-apple-system,system-ui,sans-serif;background:#faf9f7;color:#1a1a1a;"
    "display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}"
    ".card{text-align:center;max-width:420px;padding:40px}h1{font-size:20px;margin:0 0 10px}"
    "p{color:#666;font-size:14px}</style>"
    "<div class='card'><h1>Sign-in didn't complete</h1>"
    "<p>Return to GarminBridge and try again, or use your API key instead.</p></div>"
)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    # filled in per-login via a closure on the server object
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != REDIRECT_PATH:
            self.send_response(404)
            self.end_headers()
            return
        q = urllib.parse.parse_qs(parsed.query)
        self.server.oauth_result = {
            "code": (q.get("code") or [None])[0],
            "state": (q.get("state") or [None])[0],
            "error": (q.get("error") or [None])[0],
        }
        ok = bool(self.server.oauth_result["code"]) and not self.server.oauth_result["error"]
        body = (_SUCCESS_HTML if ok else _ERROR_HTML).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence the default stderr access log
        pass


def login(timeout: int = 180, open_browser: bool = True) -> dict:
    """Run the full PKCE browser login, blocking until the user finishes or `timeout` elapses.
    Starts the localhost:1455 callback server, opens the browser, waits for the code, validates
    state, exchanges for tokens, decodes account_id/plan, stores the bundle. Returns status()."""
    url, verifier, state = build_authorize_url()

    try:
        server = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _CallbackHandler)
    except OSError as e:
        raise RuntimeError(
            f"couldn't start the sign-in listener on {REDIRECT_HOST}:{REDIRECT_PORT} ({e}). "
            "Another sign-in may be in progress — close it and try again.")
    server.oauth_result = None
    server.timeout = 1  # so handle_request() polls rather than blocking forever

    t = threading.Thread(target=_serve_until, args=(server, timeout), daemon=True)
    t.start()

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass  # the URL is returned to the caller too; a headless env just gets the timeout

    t.join(timeout + 2)
    result = server.oauth_result
    try:
        server.server_close()
    except Exception:
        pass

    if not result:
        raise RuntimeError("sign-in timed out — no response from the browser. Try again.")
    if result.get("error"):
        raise RuntimeError(f"sign-in was refused: {result['error']}")
    if not result.get("code"):
        raise RuntimeError("sign-in didn't return an authorization code. Try again.")
    if result.get("state") != state:
        raise RuntimeError("sign-in state mismatch — aborting for safety. Try again.")

    tok = _exchange_code(result["code"], verifier)
    bundle = _bundle_from_tokens(tok)
    if not bundle.get("access_token"):
        raise RuntimeError("sign-in succeeded but no access token came back. Try again.")
    if not bundle.get("account_id"):
        raise RuntimeError("signed in, but no ChatGPT account id was found on the token. "
                           "Your account may not have subscription access.")
    _store_bundle(bundle)
    return status()


def _serve_until(server, timeout: int):
    """Handle callback requests until we have a result or the deadline passes."""
    deadline = time.time() + timeout
    while time.time() < deadline and server.oauth_result is None:
        server.handle_request()  # returns each ~1s (server.timeout) even with no request


# ---- token access / refresh ---------------------------------------------------

def valid_access_token() -> str | None:
    """Return a usable access token, refreshing it if it's within the skew window of expiry.
    Returns None if not signed in. Raises RuntimeError if a refresh was needed but failed."""
    bundle = _load_bundle()
    if not bundle or not bundle.get("access_token"):
        return None
    expires_at = int(bundle.get("expires_at") or 0)
    if expires_at and time.time() < expires_at - _REFRESH_SKEW:
        return bundle["access_token"]
    # expired or about to — refresh
    rt = bundle.get("refresh_token")
    if not rt:
        return bundle["access_token"]  # no refresh token; try what we have, caller handles 401
    tok = _refresh(rt)
    fresh = _bundle_from_tokens(tok, prev=bundle)
    _store_bundle(fresh)
    return fresh["access_token"]


def account_id() -> str | None:
    b = _load_bundle()
    return (b or {}).get("account_id")


def residency() -> str | None:
    b = _load_bundle()
    return (b or {}).get("residency")


# ---- status / logout ----------------------------------------------------------

def status() -> dict:
    """Connected-state for the UI — never leaks a token, only whether one is present + the plan."""
    b = _load_bundle()
    if not b or not b.get("access_token"):
        return {"connected": False, "plan": None, "account_id": None, "expires_at": 0}
    return {
        "connected": True,
        "plan": b.get("plan"),
        "account_id": b.get("account_id"),
        "expires_at": int(b.get("expires_at") or 0),
    }


def logout() -> dict:
    """Forget the stored token bundle."""
    _store_bundle(None)
    return status()
