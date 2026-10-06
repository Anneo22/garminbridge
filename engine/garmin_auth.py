"""Garmin Connect sign-in for the app, including the two-step MFA case.

Until now the only way to authenticate was login.py, a terminal script that
prompted for a password. Nothing in the app reached it, so a new install had no
way to sign in at all: every Connect feature failed against a token file the
user had no means of creating.

Two constraints shape this module.

The password never appears in a command line. api.py reads it from stdin and
passes it here as a value; it is never an argv entry (any process on the machine
can read those via `ps`), never logged, and never written to disk. Only the
OAuth tokens Garmin returns are persisted.

MFA runs inside a single invocation. Garmin's client returns a state object to
resume the sign-in, and that object holds a live HTTP client, so it cannot be
serialised and parked between two separate engine calls. The process therefore
stays alive across the wait for the code, reading it from stdin when asked.
"""
import os

import paths

from garminconnect import Garmin

TOKENSTORE = paths.state(".tokens")
def status() -> dict:
    """Who is signed in, if anyone. Never raises: a failure here just means no."""
    try:
        g = Garmin()
        g.login(TOKENSTORE)
        name = getattr(g, "full_name", None) or getattr(g, "display_name", None)
        return {"ok": True, "signed_in": True, "name": name}
    except Exception:
        return {"ok": True, "signed_in": False, "name": None}


def sign_in(email: str, password: str, read_code) -> dict:
    """Sign in, asking for a verification code only if Garmin demands one.

    The whole exchange happens in ONE process, deliberately. Garmin's client
    hands back a state object to resume an MFA sign-in, and that object holds a
    live HTTP client -- it cannot be serialised, so it cannot be parked on disk
    and picked up by a second invocation. An earlier attempt to split the two
    steps across processes could never have worked for that reason.

    `read_code` is called only when a code is actually required. It blocks until
    the caller supplies one, which is what keeps this process, and the live
    client inside it, alive across the wait.
    """
    if not email or not password:
        return {"ok": False, "message": "Enter your Garmin Connect email and password."}

    try:
        g = Garmin(email=email, password=password, return_on_mfa=True)
        result, client_state = g.login()
    except Exception as e:
        _trace(f"login raised {type(e).__name__}: {str(e)[:160]}")
        return {"ok": False, "message": _humanise(e)}

    _trace(f"login -> result={result!r} state_type={type(client_state).__name__}")

    if result != "needs_mfa":
        return _persist(g)

    # client_state is ALWAYS None here, and that is correct rather than a
    # failure: this client keeps the half-finished sign-in on itself, and its
    # resume_login ignores the argument entirely (the parameter is named
    # `_client_state`). What must survive is THIS object, which is why the
    # whole exchange stays inside one process.
    code = read_code()
    if not code:
        return {"ok": False, "message": "No code entered. Start the sign-in again."}

    try:
        g.resume_login(client_state, code.strip())
    except Exception as e:
        _trace(f"resume raised {type(e).__name__}: {str(e)[:160]}")
        return {"ok": False, "message": _humanise(e)}
    return _persist(g)


def _persist(g) -> dict:
    os.makedirs(TOKENSTORE, exist_ok=True)
    g.client.dump(TOKENSTORE)
    name = getattr(g, "full_name", None) or getattr(g, "display_name", None)
    return {"ok": True, "needs_mfa": False, "signed_in": True, "name": name}


def logout() -> dict:
    """Forget the stored session. Only tokens are removed; nothing else is touched."""
    removed = 0
    if os.path.isdir(TOKENSTORE):
        for name in os.listdir(TOKENSTORE):
            try:
                os.remove(os.path.join(TOKENSTORE, name))
                removed += 1
            except OSError:
                pass
    elif os.path.exists(TOKENSTORE):
        os.remove(TOKENSTORE)
        removed = 1
    return {"ok": True, "signed_in": False, "removed": removed}


def _trace(line: str) -> None:
    """Append one redacted line about a sign-in attempt.

    Sign-in is the one flow that cannot be reproduced without a real account, so
    when it misbehaves there is otherwise nothing to go on. This records the
    SHAPE of what Garmin returned and never its content: no password, no code,
    no token, no response body.
    """
    try:
        import datetime
        with open(paths.state("signin-debug.log"), "a") as f:
            f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def _describe_last_response(g) -> str:
    """The HTTP status and Garmin's own responseStatus type. Nothing else."""
    try:
        resp = getattr(g.client, "last_resp", None)
        if resp is None:
            return "last_resp=None"
        bits = [f"http={getattr(resp, 'status_code', '?')}"]
        try:
            body = resp.json()
            bits.append("type=" + str((body.get("responseStatus") or {}).get("type")))
            bits.append("keys=" + ",".join(sorted(body.keys())[:8]))
        except Exception:
            bits.append("body=unparsed")
        return " ".join(bits)
    except Exception as e:
        return f"describe failed: {type(e).__name__}"


def _humanise(e: Exception) -> str:
    """Turn a library exception into something worth showing a person.

    Order matters. A rejected verification code raises errors that also contain
    the word "invalid", so the code case is tested BEFORE the credential case --
    otherwise a correct password gets blamed for a mistyped code, and the reader
    goes looking for the wrong problem.
    """
    text = f"{type(e).__name__}: {e}".lower()
    if "mfa" in text or "verification code" in text or "code" in text:
        return "That code was not accepted. Check it and try again."
    if "429" in text or "too many" in text or "rate" in text:
        return "Garmin is rate-limiting sign-ins. Wait a few minutes and try again."
    if "401" in text or "unauthor" in text or "invalid" in text or "credential" in text:
        return "Garmin did not accept that email and password."
    if "timed out" in text or "timeout" in text or "connection" in text or "network" in text:
        return "Could not reach Garmin. Check your internet connection."
    return "Sign-in failed. Please try again."
