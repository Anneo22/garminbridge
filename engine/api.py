#!/usr/bin/env python3
"""api.py — JSON bridge between the GarminBridge desktop app and the proven content.py engine.

The app (Tauri/Rust) shells out to THIS script; everything it shows or does goes through
here. It re-implements nothing destructive: previews come from content.py's own dry-run and
APPLY shells out to the exact guarded content.py CLI with --yes. Tokens never leave the
engine dir; only JSON crosses to the UI.

Commands (JSON on stdout, human noise on stderr):
  snapshot [--refresh]                         the placement matrix + stale-route analysis
  preview  --action A --kind K [selectors]     what an action WOULD change (dry-run plan)
  apply    --action A --kind K [selectors]     run the guarded content.py action (--yes)

Selectors: --id <id> | --name <substr> | --watch-file <exact,exact> | --state <bucket>

Watch awareness (the design's hard rule — never be vague about WHERE a thing is):
  source="live"  the Fenix is on USB and was listed just now  -> watch actions ENABLED
  source="cache" last-seen snapshot (watch unplugged)         -> watch actions DISABLED (stale idx)
  source="manifest-cache" courses from the last FIT-name pull (no full snapshot yet)
  source="none"  no watch data at all -> the Watch badge shows "?" (unknown), never a false "absent"
"""
import argparse
import datetime
import json
import mimetypes
import os
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import wave

import content
import locations
import route_thumb
import route_geo
import route_course
import route_wind
import workout as W
import llm_author as L
import garmin_auth
import health_archive
import oauth_openai
import paths

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_repo():
    """Locate the checkout by looking for the CLI it must call.

    Counting parent directories breaks whenever the engine moves -- it already
    did, when the engine went from prototype/connect/ (two levels down) to
    engine/ (one). Searching upward for a marker survives the next move too.
    """
    d = HERE
    while True:
        if os.path.exists(os.path.join(d, "bin", "garminbridge")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            # Fall back to the historical layout rather than raising: the
            # Connect-only commands work fine without the CLI, and only the
            # scripts below actually need it.
            return os.path.abspath(os.path.join(HERE, ".."))
        d = parent


REPO = os.environ.get("GB_REPO_DIR") or _find_repo()
CLI = os.environ.get("GB_CLI") or os.path.join(REPO, "bin", "garminbridge")
GVE_PATHS = os.path.join(REPO, "bin", "gve-paths.sh")
EXPORT_VOICE = os.path.join(REPO, "bin", "export-voice-notes.sh")
TRANSCRIBE_MEMO = os.path.join(REPO, "bin", "transcribe-memo.sh")
INSTALL_TRANSCRIPTION = os.path.join(REPO, "bin", "install-transcription.sh")
SNAPSHOT = paths.state("watch_snapshot.json")
STALE_STATE = paths.state("stale_routes_seen.json")
WIND_CACHE = paths.state("wind_cache.json")   # forecast wind, short-TTL (changes hourly)
IMPORTED = paths.state("imported_routes.json")  # watch filenames the user added on purpose
ROUTE_TAGS = None  # overridable; defaults to <bridge_settings_dir>/route_tags.json
CONTENT = os.path.join(HERE, "content.py")
PY = sys.executable


def _route_tags_path():
    """Resolved route tags path, honouring any test override of ROUTE_TAGS."""
    return ROUTE_TAGS if ROUTE_TAGS is not None else os.path.join(
        paths.bridge_settings_dir(), "route_tags.json")

KINDS = ("workout", "course")
FOLDER_LABEL = {"workout": "GARMIN/Workouts", "course": "GARMIN/Courses"}
LOCATION = {
    "synced":       ("On watch",   "In Garmin Connect and on your Fenix"),
    "connect-only": ("Connect only", "In Garmin Connect, not on your Fenix"),
    "watch-only":   ("Watch only",  "On your Fenix, not in Garmin Connect"),
    "scheduled":    ("Scheduled",   "Training-plan file that re-syncs to the Fenix daily"),
}
LOCAL_TRANSCRIPTION = {"parakeet": "parakeet-mlx", "whisper": "mlx_whisper"}
CLOUD_TRANSCRIPTION_KEYS = {
    "openai": "GVE_OPENAI_KEY",
    "gemini": "GVE_GEMINI_KEY",
    "groq": "GVE_GROQ_KEY",
    "deepgram": "GVE_DEEPGRAM_KEY",
}
CLEANUP_KEYS = {
    "openai": "GVE_OPENAI_KEY",
    "groq": "GVE_GROQ_KEY",
    "anthropic": "GVE_ANTHROPIC_KEY",
    "gemini": "GVE_GEMINI_KEY",
}
SETTINGS_CONFIG_KEYS = {
    "GVE_TRANSCRIBE",
    "GVE_TRANSCRIBE_BACKEND",
    "GVE_TRANSCRIPT_CLEANUP",
    "GVE_TRANSCRIPT_KEEP_RAW",
    "GVE_CLEANUP_BACKEND",
    "GARMIN_VOICE_DELETE",
    "GVE_AUDIO_RETENTION_DAYS",
    "GVE_ARCHIVED_VOICE_RETENTION_DAYS",
    "GVE_ARCHIVED_VOICE_RETENTION_AUTO",
    *CLOUD_TRANSCRIPTION_KEYS.values(),
    *CLEANUP_KEYS.values(),
}
SECRET_CONFIG_KEYS = set(CLOUD_TRANSCRIPTION_KEYS.values()) | set(CLEANUP_KEYS.values())


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def log(*a):
    print(*a, file=sys.stderr)


# ---- voice memos ---------------------------------------------------------------

VOICE_TS_RE = re.compile(
    r"(20\d{2})\D?([01]\d)\D?([0-3]\d)\D?([0-2]\d)\D?([0-5]\d)(?:\D?([0-5]\d))?"
)


def _cli_env():
    env = os.environ.copy()
    # Preserve the bundle-first PATH supplied by Tauri. Homebrew is only a
    # development fallback when api.py is run directly from a checkout.
    inherited = env.get("PATH", "")
    env["PATH"] = ":".join(p for p in (inherited, "/opt/homebrew/bin", "/usr/local/bin") if p)
    return env


def _config_value(name):
    script = (
        'GVE_CONFIG="${GVE_CONFIG:-$HOME/.config/garmin-voice-export/config}"; '
        '[ -f "$GVE_CONFIG" ] && . "$GVE_CONFIG"; '
        f'printf "%s" "${{{name}:-}}"'
    )
    p = subprocess.run(["bash", "-lc", script], capture_output=True, text=True, env=_cli_env())
    return os.path.expandvars(os.path.expanduser(p.stdout.strip())) if p.returncode == 0 else ""


def _run_garminbridge(args):
    """Shell the control CLI, tolerating its absence.

    A missing CLI used to raise FileNotFoundError out of subprocess and surface
    a raw Python traceback in the UI. Callers already handle a non-zero return,
    so report it that way instead: everything that does not need the CLI keeps
    working, and the Settings pane renders rather than dying whole.
    """
    if not os.path.exists(CLI):
        return subprocess.CompletedProcess(
            args=[CLI] + list(args), returncode=127, stdout="",
            stderr=f"control CLI not found at {CLI}")
    return subprocess.run([CLI] + list(args), cwd=REPO, capture_output=True,
                          text=True, env=_cli_env())


def _cfg_get_cli(key):
    p = _run_garminbridge(["get", key])
    if p.returncode == 127:
        # The CLI is absent, which is an incomplete installation rather than a
        # bad setting. Report the value as unset so the Settings pane still
        # renders: failing the whole pane would hide every other setting too,
        # and there is nothing the reader could do with the underlying error.
        return ""
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "could not read settings").strip())
    return p.stdout.strip()


def _cfg_write_cli(key, value=None, unset=False):
    if key not in SETTINGS_CONFIG_KEYS:
        raise ValueError(f"unsupported setting: {key}")
    if unset:
        args = ["unset", key]
    else:
        if value is None:
            raise ValueError("missing setting value")
        args = ["set", key, value]
    p = _run_garminbridge(args)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "could not save settings").strip())


def _local_backend_installed(backend):
    binary = LOCAL_TRANSCRIPTION.get(backend)
    if not binary:
        return False
    venv_bin = os.path.join(REPO, ".venv", "bin", binary)
    return bool(os.path.exists(venv_bin) or shutil.which(binary, path=_cli_env().get("PATH")))


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _delete_mode(v):
    v = str(v or "").strip().lower()
    if v in ("", "0", "keep", "no", "off"):
        return "keep"
    if v in ("transcribed", "after-transcript"):
        return "transcribed"
    return "now"


def _paused_from_status():
    p = _run_garminbridge(["status"])
    if p.returncode == 127:
        # No CLI, so auto-import cannot be running and certainly is not paused.
        # Same reasoning as _cfg_get_cli: an incomplete install must not take
        # the whole Settings pane down with it.
        return False
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "could not read auto-import status").strip())
    for line in p.stdout.splitlines():
        if "auto-import" in line:
            return "PAUSED" in line
    return False


def _pause_flag_path():
    return os.path.expanduser(os.environ.get("GVE_PAUSE_FLAG", "~/.config/garmin-voice-export/paused"))


def _parse_resume_epoch(value):
    value = str(value or "").strip()
    if not value:
        return 0
    if re.fullmatch(r"\d+", value):
        return int(value)
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(dt.timestamp())


def _auto_import_resume_at(paused):
    if not paused:
        return ""
    try:
        with open(_pause_flag_path(), "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return ""
    epoch = _parse_resume_epoch(raw)
    if not epoch:
        return ""
    if time.time() >= epoch:
        try:
            os.remove(_pause_flag_path())
        except FileNotFoundError:
            pass
        return ""
    return str(epoch)


def cmd_settings_get(args):
    paused = _paused_from_status()
    vals = {k: _cfg_get_cli(k) for k in sorted(SETTINGS_CONFIG_KEYS)}
    backend = vals.get("GVE_TRANSCRIBE_BACKEND") or "parakeet"
    cleanup_backend = vals.get("GVE_CLEANUP_BACKEND") or "openai"
    key_presence = {
        "transcription": {b: bool(vals.get(k)) for b, k in CLOUD_TRANSCRIPTION_KEYS.items()},
        "cleanup": {b: bool(vals.get(k)) for b, k in CLEANUP_KEYS.items()},
    }
    public_values = {k: v for k, v in vals.items() if k not in SECRET_CONFIG_KEYS}
    return {
        "ok": True,
        "values": public_values,
        "transcription": {
            "enabled": _truthy(vals.get("GVE_TRANSCRIBE")),
            "backend": backend,
            "local_installed": {b: _local_backend_installed(b) for b in LOCAL_TRANSCRIPTION},
            "keys": key_presence["transcription"],
        },
        "cleanup": {
            "enabled": _truthy(vals.get("GVE_TRANSCRIPT_CLEANUP")),
            "backend": cleanup_backend,
            "keep_raw": _truthy(vals.get("GVE_TRANSCRIPT_KEEP_RAW")),
            "keys": key_presence["cleanup"],
        },
        "delete_mode": _delete_mode(vals.get("GARMIN_VOICE_DELETE")),
        "audio_retention_days": vals.get("GVE_AUDIO_RETENTION_DAYS") or "",
        "archived_retention": {
            "days": vals.get("GVE_ARCHIVED_VOICE_RETENTION_DAYS") or "",
            "auto": _truthy(vals.get("GVE_ARCHIVED_VOICE_RETENTION_AUTO")),
        },
        "archived_audio": archived_audio_stats(voice_archive_root(voice_root())),
        "auto_import_paused": paused,
        "auto_import_resume_at": _auto_import_resume_at(paused),
    }


def cmd_settings_set(args):
    key = args.key
    if key == "auto-import":
        wanted = (args.value or "").strip().lower()
        if wanted not in ("active", "paused"):
            raise ValueError("auto-import value must be active or paused")
        gb_args = ["pause"] if wanted == "paused" else ["resume"]
        minutes = args.minutes or 0
        if minutes < 0:
            raise ValueError("auto-import pause minutes must be 0 or more")
        if wanted == "paused" and minutes:
            gb_args.extend(["--minutes", str(minutes)])
        p = _run_garminbridge(gb_args)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "could not change auto-import").strip())
    else:
        _cfg_write_cli(key, args.value, unset=args.unset)
    out = cmd_settings_get(args)
    out["message"] = "Settings saved."
    return out


def cmd_transcription_install(args):
    backend = (args.backend or "").strip().lower()
    if backend not in LOCAL_TRANSCRIPTION:
        return {"ok": False, "error": "unsupported_backend",
                "message": "Only local backends need the installer."}
    if not os.path.exists(INSTALL_TRANSCRIPTION):
        return {"ok": False, "error": "installer_missing",
                "message": "The transcription installer is not available in this build."}
    p = subprocess.run([INSTALL_TRANSCRIPTION, "--backend", backend], cwd=REPO,
                       capture_output=True, text=True, env=_cli_env())
    output = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        return {"ok": False, "error": "install_failed",
                "message": output or f"Could not install {backend} transcription.",
                "rc": p.returncode, "output": output}
    out = cmd_settings_get(args)
    out["message"] = f"{backend.capitalize()} transcription is ready."
    out["output"] = output
    return out


def voice_root():
    """Resolve the voice memo destination through the same config + gve-paths.sh path logic
    that bin/garminbridge uses. Falls back to the legacy destination only if that fails."""
    try:
        subprocess.run([CLI, "root"], capture_output=True, text=True, env=_cli_env(), timeout=10)
    except Exception:
        pass
    script = (
        'GVE_CONFIG="${GVE_CONFIG:-$HOME/.config/garmin-voice-export/config}"; '
        '[ -f "$GVE_CONFIG" ] && . "$GVE_CONFIG"; '
        f'. {shlex.quote(GVE_PATHS)}; gve_resolve_paths; printf "%s" "$GVE_VOICE_DEST"'
    )
    p = subprocess.run(["bash", "-lc", script], capture_output=True, text=True, env=_cli_env())
    if p.returncode == 0 and p.stdout.strip():
        return os.path.realpath(os.path.expandvars(os.path.expanduser(p.stdout.strip())))
    return os.path.realpath(os.path.expanduser("~/Documents/Voice Memos"))


def obsidian_vault():
    return _config_value("GVE_OBSIDIAN_VAULT")


def transcript_for(audio):
    return os.path.splitext(audio)[0] + ".txt"


def raw_transcript_for(audio):
    return os.path.splitext(audio)[0] + ".raw.txt"


def voice_sidecars(audio):
    return [transcript_for(audio), raw_transcript_for(audio)]


def memo_time(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    m = VOICE_TS_RE.search(stem)
    if m:
        y, mo, d, h, mi, sec = m.groups()
        try:
            dt = datetime.datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0)).astimezone()
            return dt.isoformat(timespec="seconds"), dt.timestamp()
        except ValueError:
            pass
    st = os.stat(path)
    dt = datetime.datetime.fromtimestamp(st.st_mtime).astimezone()
    return dt.isoformat(timespec="seconds"), st.st_mtime


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read().strip()


def note_path_for(vault, audio):
    return os.path.join(vault, os.path.basename(os.path.splitext(audio)[0]) + ".md")


def _under_root(path, root):
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def voice_archive_root(root):
    return os.path.realpath(os.path.join(root, "Archive"))


def _cleanup_days(days):
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("days must be an integer >= 1")
    return days


def _archived_wavs(archive_root):
    archive_root = os.path.realpath(archive_root)
    if not os.path.isdir(archive_root):
        return
    for dirpath, _, names in os.walk(archive_root):
        for name in names:
            path = os.path.realpath(os.path.join(dirpath, name))
            if (_under_root(path, archive_root) and os.path.isfile(path)
                    and name.lower().endswith(".wav")):
                yield path


def archived_cleanup_candidates(archive_root, days):
    days = _cleanup_days(days)
    now = time.time()
    items = []
    for path in _archived_wavs(archive_root):
        recorded, ts = memo_time(path)
        age_days = int((now - ts) // 86400)
        if age_days >= days:
            items.append({
                "name": os.path.basename(path),
                "path": path,
                "bytes": os.path.getsize(path),
                "time": recorded,
                "age_days": age_days,
            })
    return items


def delete_archived_wavs(candidates, archive_root, days):
    days = _cleanup_days(days)
    archive_root = os.path.realpath(archive_root)
    deleted = []
    skipped = []
    freed = 0
    now = time.time()
    for item in candidates:
        raw_path = item.get("path") if isinstance(item, dict) else item
        path = os.path.realpath(raw_path or "")
        try:
            if (not _under_root(path, archive_root) or not os.path.isfile(path)
                    or not path.lower().endswith(".wav")):
                skipped.append(path)
                continue
            _, ts = memo_time(path)
            if int((now - ts) // 86400) < days:
                skipped.append(path)
                continue
            size = os.path.getsize(path)
            os.remove(path)
            deleted.append(path)
            freed += size
        except OSError:
            skipped.append(path)
    return {"deleted": deleted, "freed_bytes": freed, "skipped": skipped}


def archived_audio_stats(archive_root):
    count = 0
    total = 0
    for path in _archived_wavs(archive_root):
        count += 1
        total += os.path.getsize(path)
    return {"count": count, "bytes": total}


def _parse_retention_days(value):
    s = str(value or "").strip()
    if not re.fullmatch(r"[1-9]\d*", s):
        raise ValueError("bad days")
    return int(s)


def _format_bytes(n):
    n = int(n or 0)
    units = ("B", "KB", "MB", "GB")
    value = float(n)
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{n} B"
    return f"{value:.1f} {unit}"


def memo_is_archived(audio, root):
    return _under_root(audio, voice_archive_root(root))


def _sidecar_target(dest_audio, src_sidecar):
    return raw_transcript_for(dest_audio) if src_sidecar.endswith(".raw.txt") else transcript_for(dest_audio)


def _unique_memo_target(dest_dir, audio, stem=None):
    stem = stem or os.path.splitext(os.path.basename(audio))[0]
    ext = os.path.splitext(audio)[1]
    for i in range(1, 1000):
        suffix = "" if i == 1 else f" {i}"
        dest_audio = os.path.join(dest_dir, stem + suffix + ext)
        targets = [dest_audio, transcript_for(dest_audio), raw_transcript_for(dest_audio)]
        if not any(os.path.exists(p) for p in targets):
            return dest_audio
    raise RuntimeError("could not choose a safe destination name")


def _checked_voice_audio(audio, root):
    audio = os.path.realpath(audio or "")
    if not _under_root(audio, root) or not os.path.isfile(audio) or not audio.lower().endswith(".wav"):
        return None
    return audio


def memo_duration_seconds(path):
    try:
        p = subprocess.run(["afinfo", path], capture_output=True, text=True, timeout=5)
        if p.returncode == 0:
            m = re.search(r"estimated duration:\s*([0-9.]+)\s*sec", p.stdout)
            if m:
                return round(float(m.group(1)))
    except Exception:
        pass
    try:
        with wave.open(path, "rb") as w:
            rate = w.getframerate()
            if rate:
                return round(w.getnframes() / rate)
    except Exception:
        pass
    return None


def _safe_memo_stem(name):
    raw = (name or "").strip()
    if raw.lower().endswith(".wav"):
        raw = os.path.splitext(raw)[0].strip()
    if not raw or raw in (".", "..") or "/" in raw or "\x00" in raw:
        return ""
    return raw


def cmd_voice_list(args):
    root = voice_root()
    vault = obsidian_vault()
    memos = []
    if os.path.isdir(root):
        for dirpath, _, names in os.walk(root):
            for name in names:
                if not name.lower().endswith(".wav"):
                    continue
                audio = os.path.realpath(os.path.join(dirpath, name))
                txt = transcript_for(audio)
                recorded, sort_ts = memo_time(audio)
                note = note_path_for(vault, audio) if vault else ""
                rel = os.path.relpath(audio, root)
                memos.append({
                    "name": os.path.splitext(name)[0],
                    "time": recorded,
                    "duration": memo_duration_seconds(audio),
                    "audio_path": audio,
                    "relative_path": rel,
                    "transcript_path": txt if os.path.exists(txt) else "",
                    "raw_transcript_path": raw_transcript_for(audio) if os.path.exists(raw_transcript_for(audio)) else "",
                    "transcript": read_text(txt) if os.path.exists(txt) else "",
                    "has_transcript": os.path.exists(txt),
                    "has_raw_transcript": os.path.exists(raw_transcript_for(audio)),
                    "archived": memo_is_archived(audio, root),
                    "note_exists": bool(note and os.path.exists(note)),
                    "note_path": note if note and os.path.exists(note) else "",
                    "_sort": sort_ts,
                })
    memos.sort(key=lambda m: (m["_sort"], m["name"]), reverse=True)
    for memo in memos:
        memo.pop("_sort", None)
    return {"ok": True, "root": root, "vault_configured": bool(vault),
            "vault": vault, "items": memos}


def _voice_summary(output):
    m = re.search(r"Done:\s*(\d+)\s+new", output)
    if not m:
        return ""
    new = int(m.group(1))
    if new == 0:
        return "No new voice memos on your watch."
    return f"Imported {new} new voice memo{'s' if new != 1 else ''}."


def cmd_voice_import(args):
    if not os.path.exists(CLI):
        return {"ok": False, "error": "cli_missing",
                "message": "Voice import is not available in this app build."}
    p = subprocess.run([CLI, "voice", "--no-notify"], cwd=REPO, capture_output=True,
                       text=True, env=_cli_env())
    output = (p.stdout + p.stderr).strip()
    summary = _voice_summary(output)
    if p.returncode == 10:
        return {"ok": False, "error": "watch_not_detected",
                "message": "Couldn't reach your Fenix. Check the cable and that the watch is unlocked, then import again.",
                "rc": p.returncode, "output": output}
    if p.returncode != 0:
        return {"ok": False, "error": "voice_import_failed",
                "message": "Voice memo import did not finish. Check the cable and try again.",
                "rc": p.returncode, "output": output}
    listed = cmd_voice_list(args)
    listed.update({"message": summary or "Voice memo import finished.",
                   "rc": p.returncode, "output": output})
    return listed


def cmd_voice_archive(args):
    root = voice_root()
    audio = _checked_voice_audio(args.audio, root)
    if not audio:
        return {"ok": False, "error": "bad_audio",
                "message": "That audio file is not in the configured Voice Memo folder."}
    dest_dir = root if args.undo else voice_archive_root(root)
    os.makedirs(dest_dir, exist_ok=True)
    if os.path.realpath(os.path.dirname(audio)) == os.path.realpath(dest_dir):
        return {"ok": True, "audio_path": audio,
                "message": "Already in active memos." if args.undo else "Already archived."}
    dest_audio = _unique_memo_target(dest_dir, audio)
    moves = [(audio, dest_audio)]
    for sidecar in voice_sidecars(audio):
        if os.path.exists(sidecar) and _under_root(sidecar, root):
            moves.append((sidecar, _sidecar_target(dest_audio, sidecar)))
    for src, dst in moves:
        shutil.move(src, dst)
    return {"ok": True, "audio_path": dest_audio,
            "message": "Moved back to active memos." if args.undo else "Archived."}


def cmd_voice_delete(args):
    root = voice_root()
    audio = _checked_voice_audio(args.audio, root)
    if not audio:
        return {"ok": False, "error": "bad_audio",
                "message": "That audio file is not in the configured Voice Memo folder."}
    deleted = []
    for path in [audio] + voice_sidecars(audio):
        if os.path.exists(path) and _under_root(path, root):
            os.remove(path)
            deleted.append(path)
    return {"ok": True, "deleted": deleted,
            "message": "Deleted the local memo copy."}


def cmd_voice_rename(args):
    root = voice_root()
    audio = _checked_voice_audio(args.audio, root)
    if not audio:
        return {"ok": False, "error": "bad_audio",
                "message": "That audio file is not in the configured Voice Memo folder."}
    stem = _safe_memo_stem(args.name)
    if not stem:
        return {"ok": False, "error": "bad_name",
                "message": "Enter a memo name without slashes."}
    old_stem = os.path.splitext(os.path.basename(audio))[0]
    if stem == old_stem:
        return {"ok": True, "audio_path": audio, "name": old_stem,
                "message": "That is already the memo name."}

    dest_audio = _unique_memo_target(os.path.dirname(audio), audio, stem=stem)
    moves = [(audio, dest_audio)]
    for sidecar in voice_sidecars(audio):
        if os.path.exists(sidecar) and _under_root(sidecar, root):
            moves.append((sidecar, _sidecar_target(dest_audio, sidecar)))
    for src, dst in moves:
        shutil.move(src, dst)
    name = os.path.splitext(os.path.basename(dest_audio))[0]
    return {"ok": True, "audio_path": dest_audio, "name": name,
            "message": "Renamed." if name == stem else f"Renamed as {name}."}


def cmd_voice_note(args):
    root = voice_root()
    audio = os.path.realpath(args.audio or "")
    if not _under_root(audio, root) or not os.path.isfile(audio) or not audio.lower().endswith(".wav"):
        return {"ok": False, "error": "bad_audio",
                "message": "That audio file is not in the configured Voice Memo folder."}
    vault = obsidian_vault()
    if not vault:
        return {"ok": False, "error": "no_vault",
                "message": "Choose a notes folder in the app, then send it again."}
    os.makedirs(vault, exist_ok=True)
    exact = note_path_for(vault, audio)
    txt = transcript_for(audio)
    env = _cli_env()
    env["GVE_LOAD_ONLY"] = "1"
    env["GVE_OBSIDIAN_NO_OVERWRITE"] = "1"
    before = {p for p in os.listdir(vault) if p.endswith(".md")} if os.path.isdir(vault) else set()
    script = f'. {shlex.quote(EXPORT_VOICE)}; write_obsidian "$1" "$2"'
    p = subprocess.run(["bash", "-lc", script, "bash", audio, txt if os.path.exists(txt) else ""],
                       capture_output=True, text=True, env=env, cwd=REPO)
    output = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        return {"ok": False, "error": "note_write_failed",
                "message": "Could not write the Obsidian note. Choose the notes folder again and try.",
                "output": output}
    match = re.search(r"obsidian note -> (.+)", output)
    note = match.group(1).strip() if match else ""
    if not note:
        after = {p for p in os.listdir(vault) if p.endswith(".md")}
        created = sorted(after - before)
        note = os.path.join(vault, created[-1]) if created else exact
    if not os.path.exists(note):
        return {"ok": False, "error": "note_write_failed",
                "message": "The Obsidian note was not created. Choose the notes folder again and try.",
                "output": output}
    return {"ok": True, "note_path": note,
            "message": "Sent to notes." if note == exact else f"Sent to notes as {os.path.basename(note)}."}


def cmd_voice_transcribe(args):
    root = voice_root()
    audio = os.path.realpath(args.audio or "")
    if not _under_root(audio, root) or not os.path.isfile(audio) or not audio.lower().endswith(".wav"):
        return {"ok": False, "error": "bad_audio",
                "message": "That audio file is not in the configured Voice Memo folder."}
    p = subprocess.run([TRANSCRIBE_MEMO, audio], cwd=REPO, capture_output=True,
                       text=True, env=_cli_env())
    output = (p.stdout + p.stderr).strip()
    if p.returncode == 3 or "not installed" in output or "not set" in output:
        return {"ok": False, "error": "transcription_unconfigured",
                "message": "Transcription is optional. Open Settings to choose a backend or save a cloud API key. You can still play, archive, and send memos to notes without it.",
                "rc": p.returncode, "output": output}
    if p.returncode != 0:
        return {"ok": False, "error": "transcription_failed",
                "message": "Transcription failed. Check the configured backend and try again.",
                "rc": p.returncode, "output": output}
    txt = transcript_for(audio)
    if not os.path.exists(txt):
        txt = output.splitlines()[-1] if output else txt
    return {"ok": True, "transcript_path": txt, "transcript": read_text(txt) if os.path.exists(txt) else "",
            "message": "Transcribed."}


def cmd_voice_cleanup_archived(args):
    try:
        days = _parse_retention_days(args.days)
    except ValueError:
        return {"ok": False, "error": "bad_days",
                "message": "Choose a whole number of days (1 or more)."}
    root = voice_root()
    archive_root = voice_archive_root(root)
    apply = bool(args.apply)
    if not os.path.isdir(archive_root):
        return {"ok": True, "apply": apply, "count": 0, "total_bytes": 0, "items": [],
                "message": "No archived memos yet."}
    candidates = archived_cleanup_candidates(archive_root, days)
    total = sum(item["bytes"] for item in candidates)
    if not apply:
        count = len(candidates)
        memo_word = "memo" if count == 1 else "memos"
        return {"ok": True, "apply": False, "count": count, "total_bytes": total,
                "items": candidates,
                "message": f"Found {count} archived {memo_word} older than {days} days using {_format_bytes(total)}."}
    res = delete_archived_wavs(candidates, archive_root, days)
    deleted_count = len(res["deleted"])
    skipped_count = len(res["skipped"])
    memo_word = "memo" if deleted_count == 1 else "memos"
    msg = f"Cleaned up {deleted_count} archived {memo_word} and freed {_format_bytes(res['freed_bytes'])}."
    if skipped_count:
        msg += f" Skipped {skipped_count} that no longer matched the safety checks."
    return {"ok": True, "apply": True, "deleted_count": deleted_count,
            "freed_bytes": res["freed_bytes"], "skipped_count": skipped_count,
            "message": msg}


# ---- watch presence + snapshot ------------------------------------------------

WATCH_PRESENCE_CACHE = paths.state(".watch_present.json")
WATCH_PRESENCE_TTL = 8   # s — one rename fires preview+apply+snapshot within this window


def _watch_present_probe():
    """The real check: macOS keeps relaunching PTPCamera, which grabs the MTP channel and
    makes a bare `gphoto2 --auto-detect` falsely report 'no device' even when the watch is
    plugged in. So suppress PTPCamera first and retry a few times before giving up."""
    import time as _t
    for attempt in range(3):
        subprocess.run(["killall", "PTPCamera"], capture_output=True)
        # Let the MTP channel actually free up before probing: macOS relaunches PTPCamera
        # fast, and running gphoto2 in the same instant loses the race (the watch then reads
        # as absent even though it's plugged in). A short settle wins it reliably.
        _t.sleep(1.2)
        try:
            p = subprocess.run(["gphoto2", "--auto-detect"], capture_output=True, text=True, timeout=15)
            if "usb:" in p.stdout:
                return True
        except FileNotFoundError:
            # A missing bundled runtime is an installation failure, not "watch absent".
            log("gphoto2 is missing from the bundled runtime and PATH; reinstall Garmin Bridge")
            return False
        except Exception:
            pass
    return False


def watch_present(force=False):
    """Is the Fenix on USB? The probe is expensive (killall + up to 3x gphoto2 with settle
    sleeps, ~3s when the watch is absent), and a single rename fires several presence checks
    within a couple of seconds — so cache the result briefly and let them share one probe.
    Any path about to WRITE to the watch passes force=True, so a write is never allowed or
    blocked on a stale reading; read-only hints (the snapshot pill, a dialog note) use cache."""
    if not force:
        try:
            c = json.load(open(WATCH_PRESENCE_CACHE))
            if time.time() - float(c["at"]) <= WATCH_PRESENCE_TTL:
                return bool(c["present"])
        except Exception:
            pass
    present = _watch_present_probe()
    try:
        json.dump({"present": present, "at": time.time()}, open(WATCH_PRESENCE_CACHE, "w"))
    except Exception:
        pass
    return present


def capture_live():
    """List ALL content folders over USB in ONE MTP session (fast scan) and persist the
    snapshot. Returns the kinds dict."""
    leaves = [content.WATCH_FOLDER[k] for k in KINDS]
    scanned = content.watch_scan(leaves)
    kinds = {k: [[i, f] for i, f in scanned.get(content.WATCH_FOLDER[k], [])] for k in KINDS}
    snap = {"captured_at": _now(), "kinds": kinds}
    json.dump(snap, open(SNAPSHOT, "w"), ensure_ascii=False, indent=2)
    return snap


def resolve_watch(refresh):
    """Decide where the watch picture comes from. Live MTP listing is slow (settle + wait),
    so default to the cached snapshot and only re-list the watch on --refresh or first run.
    Connectivity is reported separately (cheap auto-detect). Returns
    (kinds_dict, source, captured_at, kinds_present_set, connected)."""
    # An explicit Refresh must re-probe live, never trust the short cache — otherwise a
    # watch plugged in AFTER a cached "absent" reading stays invisible until the TTL lapses.
    connected = watch_present(force=refresh)
    if connected and (refresh or not os.path.exists(SNAPSHOT)):
        try:
            snap = capture_live()
            return snap["kinds"], "live", snap["captured_at"], set(KINDS), connected
        except SystemExit as e:
            log(f"live watch list failed ({e}); falling back to cache")
        except Exception as e:
            log(f"live watch list error ({e!r}); falling back to cache")
    if os.path.exists(SNAPSHOT):
        snap = json.load(open(SNAPSHOT))
        return snap["kinds"], "cache", snap.get("captured_at"), set(snap["kinds"].keys()), connected
    # last resort: the course FIT-name manifest IS a record of what was on the watch
    manifest = content.load_manifest()
    if manifest.get("course"):
        files = list(manifest["course"].keys())
        return ({"course": [[i, f] for i, f in enumerate(files)]},
                "manifest-cache", None, {"course"}, connected)
    return {}, "none", None, set(), connected


# ---- imported routes ----------------------------------------------------------
# A route the user IMPORTED (GPX/FIT -> USB add) is legitimately watch-only (it isn't in
# Connect by design), so the stale detector must NOT flag it as an orphaned old version.
# We remember the on-watch filenames we added so enrich() can tell "you added this" apart
# from "Garmin's one-way sync stranded this".

def load_imported():
    if os.path.exists(IMPORTED):
        try:
            return set(json.load(open(IMPORTED)))
        except Exception:
            return set()
    return set()


def add_imported(fname):
    s = load_imported()
    s.add(fname)
    json.dump(sorted(s), open(IMPORTED, "w"))


# ---- route tags ---------------------------------------------------------------

def _clean_tags(values):
    if isinstance(values, str):
        values = values.split(",")
    out, seen = [], set()
    for value in values or []:
        if not isinstance(value, str):
            continue
        tag = value.strip()
        key = tag.casefold()
        if tag and key not in seen:
            out.append(tag)
            seen.add(key)
    return sorted(out, key=str.casefold)


def _clean_tag_key(key):
    key = (key or "").strip() if isinstance(key, str) else ""
    if not key or key != os.path.basename(key) or "/" in key or "\\" in key:
        return ""
    return key


def tags_entry_for(fit_basename, store):
    raw = (store or {}).get(fit_basename) if fit_basename else None
    if not isinstance(raw, dict):
        return {"tags": [], "folder": ""}
    folder = raw.get("folder") if isinstance(raw.get("folder"), str) else ""
    return {"tags": _clean_tags(raw.get("tags", [])), "folder": folder.strip()}


def load_tags(path=None):
    path = path or _route_tags_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_tags(store, path=None):
    path = path or _route_tags_path()
    clean = {}
    for key, raw in (store or {}).items():
        key = _clean_tag_key(key)
        if not key or not isinstance(raw, dict):
            continue
        entry = tags_entry_for(key, {key: raw})
        if entry["tags"] or entry["folder"]:
            clean[key] = entry
    root = os.path.dirname(path) or "."
    os.makedirs(root, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".route_tags.", suffix=".json", dir=root)
    os.close(fd)
    try:
        with open(tmp, "w") as f:
            json.dump(clean, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def cmd_route_tags_set(args):
    key = _clean_tag_key(args.key)
    if not key:
        return {"ok": False, "error": "invalid_key",
                "message": "Route tag key must be a FIT basename."}
    tags = _clean_tags(getattr(args, "tags", "") or "")
    folder = (getattr(args, "folder", "") or "").strip()
    store = load_tags()
    if tags or folder:
        store[key] = {"tags": tags, "folder": folder}
    else:
        store.pop(key, None)
    save_tags(store)
    return {"ok": True, "message": "Tags saved."}


# ---- row enrichment -----------------------------------------------------------

def stale_kind(r, imported=()):
    """For a course row, classify on-watch staleness using ONLY the internal-name
    manifest (never the lossy filename), so a current route is never mislabelled. A route
    the user imported themselves is watch-only on purpose, so it is never stale."""
    if r["kind"] != "course" or content.state(r) != "watch-only":
        return None
    if r.get("watch_file") in imported:
        return None
    return "orphan" if r.get("manifest_verified") else "needs_manifest"


def match_backup(r, name_lookup):
    key = content._norm(r["name"] or "")
    for entry in name_lookup:
        nkey, fit_basename, thumb = entry if len(entry) == 3 else (entry[0], "", entry[1])
        if content.keys_match(key, nkey):
            return fit_basename, thumb
    return None


def match_thumb(r, name_lookup, id_thumbs):
    """The route's line-art trace. Prefer the Mac-backup FIT (offline, matched the
    same truncation-tolerant way add-to-watch matches), else the Connect geoPoints
    trace cached by course id (fills routes with no local FIT)."""
    match = match_backup(r, name_lookup)
    if match and match[1] and match[1].get("d"):
        return match[1]
    return id_thumbs.get(str(r["id"])) if r.get("id") else None


def enrich(r, source, kinds_present, connected, thumb_lookup=(), id_thumbs=None, imported=(), tags_store=None):
    st = content.state(r)
    label, detail = LOCATION[st]
    stale = stale_kind(r, imported)
    backup = match_backup(r, thumb_lookup) if r["kind"] == "course" else None
    tag_key = backup[0] if backup else ""
    tag_entry = tags_entry_for(tag_key, tags_store or {}) if tag_key else {"tags": [], "folder": ""}
    thumb = None
    if r["kind"] == "course":
        thumb = backup[1] if backup and backup[1] and backup[1].get("d") else None
        if not thumb:
            thumb = (id_thumbs or {}).get(str(r["id"])) if r.get("id") else None
    is_imported = r["kind"] == "course" and st == "watch-only" and r.get("watch_file") in imported
    if is_imported:
        label, detail = "On watch", "Route you imported onto your Fenix (not in Garmin Connect)"
    elif stale == "orphan":
        label, detail = "Stale route", "Old or orphaned route on your Fenix (no current Connect match)"
    elif stale == "needs_manifest":
        detail = "On your Fenix; can't confirm it's current until the FIT names are refreshed"
    watch_known = r["kind"] in kinds_present
    uid = f"{r['kind']}:{r['id']}" if r["id"] else f"{r['kind']}:w:{r['watch_file']}"
    return {
        "uid": uid,
        "kind": r["kind"],
        "name": r["name"],
        "sport": r.get("sport"),
        "id": r["id"],
        "in_connect": r["in_connect"],
        "on_watch": r["on_watch"],
        "watch_known": watch_known,
        "state": st,
        "location_label": label,
        "location_detail": detail,
        "folder": FOLDER_LABEL[r["kind"]],
        "watch_file": r.get("watch_file"),
        "watch_idx": r.get("watch_idx"),
        "scheduled": r.get("scheduled", False),
        "manifest_verified": r.get("manifest_verified", False),
        "stale": stale,
        "imported": is_imported,
        "thumb": thumb,
        "tags": tag_entry["tags"],
        "route_folder": tag_entry["folder"],
        "tag_key": tag_key,
        "actions": {
            # Watch-write actions are offered whenever we know the watch picture for this kind.
            # They stay SAFE off a stale cache because apply re-lists the watch live and targets
            # by exact --watch-file, and the apply guard refuses if the Fenix is unplugged.
            "can_add_to_watch": bool(watch_known and r["in_connect"] and not r["on_watch"]),
            "can_rm_watch": bool(watch_known and r["on_watch"] and not r.get("scheduled")),
            # Connect delete works any time (no watch needed); permanent for courses
            "can_rm_connect": bool(r["in_connect"] and r["id"]),
            "rm_connect_permanent": r["kind"] == "course",
        },
    }


def build_rows(g, refresh):
    connect_items = content.connect_items(g)
    # content.build() drops the sport field; join it back by id so the UI can filter by sport
    sport_by_id = {c["id"]: c.get("sport") for c in connect_items}
    manifest = content.load_manifest()
    kinds, source, captured_at, present, connected = resolve_watch(refresh)
    thumb_lookup = route_thumb.name_lookup()   # local-FIT traces, built once; cache-backed
    id_thumbs = route_thumb.connect_lookup()   # Connect geoPoints traces cached by course id
    imported = load_imported()                 # routes the user added directly (never "stale")
    tags_store = load_tags()
    rows = []
    counts = {}
    for kind in KINDS:
        witems = [tuple(x) for x in kinds.get(kind, [])]
        mrows = content.build(connect_items, witems, kind, manifest)
        for r in mrows:
            r["sport"] = sport_by_id.get(r["id"])
        enriched = [enrich(r, source, present, connected, thumb_lookup, id_thumbs, imported, tags_store)
                    for r in mrows]
        rows.extend(enriched)
        c = {"synced": 0, "connect-only": 0, "watch-only": 0, "scheduled": 0}
        for e in enriched:
            c[e["state"]] += 1
        counts[kind] = c
    return rows, source, captured_at, counts, connected


def stale_block(rows):
    """The forum-confirmed stale-route reminder: verified orphans (safe to surface/clean)
    and the unverified bucket (need a FIT-name refresh before any verdict)."""
    orphans = [r for r in rows if r["kind"] == "course" and r["stale"] == "orphan"]
    needs = [r for r in rows if r["kind"] == "course" and r["stale"] == "needs_manifest"]
    seen = {}
    if os.path.exists(STALE_STATE):
        try:
            seen = json.load(open(STALE_STATE))
        except Exception:
            seen = {}
    return {
        "orphans": [r["uid"] for r in orphans],
        "orphan_count": len(orphans),
        "needs_manifest_count": len(needs),
        "last_checked": seen.get("last_checked"),
        "last_orphan_count": seen.get("orphan_count"),
    }


# ---- commands -----------------------------------------------------------------

def locations_block(connected):
    """Saved points (Lctns.fit), the third content type. They live only on the watch + the
    Mac backup (never Connect), so this reads the fast offline backup; editing pulls the live
    file. source='mac-backup' when a backup exists, else 'none'. `editable` gates the UI: an
    edit re-lists the watch live and requires the Fenix on USB."""
    pts = locations.points_from_backup()
    return {
        "source": "mac-backup" if os.path.exists(locations.mac_backup_path()) else "none",
        "count": len(pts),
        "watch_connected": connected,
        "editable": bool(connected),
        "points": pts,
    }


def cmd_snapshot(args):
    g = content.login()
    rows, source, captured_at, counts, connected = build_rows(g, args.refresh)
    stale = stale_block(rows)
    # record that we surfaced the orphans now (the "remind" half of detect+remind)
    json.dump({"last_checked": _now(), "orphan_count": stale["orphan_count"]},
              open(STALE_STATE, "w"))
    sports = sorted({r["sport"] for r in rows if r["sport"]})
    tags = sorted({t for r in rows for t in (r.get("tags") or [])}, key=str.casefold)
    folders = sorted({r["route_folder"] for r in rows if r.get("route_folder")}, key=str.casefold)
    return {
        "ok": True,
        "generated_at": _now(),
        "watch": {"source": source, "captured_at": captured_at, "connected": connected},
        "counts": counts,
        "sports": sports,
        "tags": tags,
        "folders": folders,
        "stale_routes": stale,
        "locations": locations_block(connected),
        "items": rows,
    }


def _content_argv(args, yes):
    argv = [PY, CONTENT, args.action, "--kind", args.kind]
    for flag, val in (("--id", args.id), ("--name", args.name),
                      ("--watch-file", args.watch_file), ("--state", args.state),
                      ("--to", getattr(args, "to", None)),
                      # caller-known placement (from the UI's snapshot): lets content.py rename
                      # skip its inventory fetch + USB watch scan entirely.
                      ("--on-watch", getattr(args, "on_watch", None)),
                      ("--watch-idx", getattr(args, "watch_idx", None))):
        if val is not None and val != "":
            argv += [flag, val]
    if getattr(args, "scheduled", False):
        argv += ["--scheduled"]
    if yes:
        argv += ["--yes"]
    return argv


def _run_content(argv):
    p = subprocess.run(argv, capture_output=True, text=True, cwd=HERE)
    return {"rc": p.returncode, "stdout": p.stdout, "stderr": p.stderr}


def _loc_preview(args):
    """Saved-point rename/delete plan, from the offline Mac backup (no Connect, no live watch
    list). Shapes to the same fields the UI's rename/delete dialogs already read. Editing
    always needs the Fenix on USB (the apply path pulls the live file), so needs_live_watch is
    always true here; connectivity is a quick auto-detect so the dialog is honest."""
    op = "delete" if args.action == "rm-location" else "rename"
    idx = int(args.index) if args.index is not None else None
    p = locations.preview(op, idx, getattr(args, "to", None))
    if not p.get("ok"):
        return {"ok": False, "error": p.get("reason", "error"),
                "message": p.get("message", "saved point not found")}
    connected = watch_present()
    if op == "rename":
        return {"ok": True, "action": "rename", "kind": "location", "verb": "Rename",
                "index": idx, "old_name": p["old_name"], "new_name": p["new_name"],
                "on_watch": True, "is_course": False,
                "watch_fits": p["fits"], "watch_name_max": p["name_max"],
                "propagates_to_watch": p["fits"], "needs_live_watch": True,
                "watch_connected": connected, "reversible": True, "permanent": False}
    return {"ok": True, "action": "rm-location", "kind": "location", "verb": "Delete saved point",
            "index": idx, "count": 1, "permanent": True, "reversible": False,
            "needs_live_watch": True, "watch_connected": connected,
            "changes": [{"uid": f"location:{idx}", "name": p["name"], "state": "on-watch",
                         "from_label": "On watch",
                         "effect": "permanently deleted from your Fenix's saved points "
                                   f"({p['remaining']} would remain)"}]}


def cmd_preview(args):
    """Structured 'what will change' + the engine's own authoritative dry-run text."""
    if args.kind == "location":
        return _loc_preview(args)

    # Rename preview needs only the single item's current name (and, for an on-watch course,
    # the on-watch file's byte ceiling) — never the full inventory or a watch scan. It runs in
    # the background while the dialog is already open, so keep it a single Connect GET. The UI
    # passes --on-watch (it knows placement); without it we assume off-watch (generic note).
    if args.action == "rename":
        g = content.login()
        name = content.connect_name(g, args.kind, args.id)
        if name is None:
            return {"ok": False, "error": "not_found", "message": "item not found"}
        new_name = (args.to or "").strip()
        on_watch = getattr(args, "on_watch", None) == "1" and not getattr(args, "scheduled", False)
        # Course watch-rename is length-constrained by the on-watch FIT's actual name-field
        # size — which Garmin often TRUNCATES below the Connect name (verified 2026-07-01).
        # So read the real ceiling, not the Connect name length. Workouts are unconstrained
        # (re-downloaded). content.py's patch is the authoritative gate; this mirrors it.
        watch_fits = True
        watch_name_max = None
        if on_watch and args.kind == "course":
            watch_name_max = content.course_watch_name_max(name)
            if watch_name_max is not None:
                watch_fits = len(new_name.encode("utf-8")) <= watch_name_max
        return {
            "ok": True, "action": "rename", "kind": args.kind, "verb": "Rename",
            "id": args.id, "old_name": name, "new_name": new_name,
            "on_watch": on_watch, "is_course": args.kind == "course",
            "watch_fits": bool(watch_fits), "watch_name_max": watch_name_max,
            "propagates_to_watch": bool(on_watch and watch_fits),
            "needs_live_watch": on_watch, "watch_connected": watch_present(),
            "reversible": True, "permanent": False,
        }

    g = content.login()
    rows, source, captured_at, _, connected = build_rows(g, False)
    sel = [r for r in rows if r["kind"] == args.kind]
    if args.id:
        sel = [r for r in sel if str(r["id"]) == str(args.id)]
    if args.watch_file:
        wanted = set(args.watch_file.split(","))
        sel = [r for r in sel if r["watch_file"] in wanted]
    if args.name:
        q = args.name.lower()
        sel = [r for r in sel if q in (r["name"] or "").lower()]
    if args.state:
        sel = [r for r in sel if r["state"] == args.state]

    verb = {"add-to-watch": "Add to your Fenix",
            "rm-watch": "Remove from your Fenix",
            "rm-connect": "Delete from Garmin Connect",
            "clean-watch": "Remove stale items from your Fenix"}.get(args.action, args.action)
    if args.action == "clean-watch":
        sel = [r for r in sel if r["state"] == "watch-only"
               and (args.kind != "course" or r["manifest_verified"])]

    permanent = args.action == "rm-connect" and args.kind == "course"
    reversible = args.action in ("add-to-watch", "rm-watch")
    changes = []
    for r in sel:
        if args.action == "add-to-watch":
            where = f"copied into {r['folder']} on your Fenix"
        elif args.action in ("rm-watch", "clean-watch"):
            where = f"deleted from {r['folder']} on your Fenix (stays in Garmin Connect)"
        elif args.action == "rm-connect":
            where = "permanently deleted from Garmin Connect (does NOT remove the on-watch copy)"
        else:
            where = ""
        changes.append({"uid": r["uid"], "name": r["name"], "state": r["state"],
                        "from_label": r["location_label"], "effect": where})

    # Note: the authoritative guards (synced/scheduled protection, manifest-verified course
    # gate, explicit-id rm-connect) run at APPLY time inside content.py --yes. Preview stays
    # fast and never re-lists the watch live just to describe the change.
    return {
        "ok": True,
        "action": args.action, "kind": args.kind, "verb": verb,
        "count": len(changes), "permanent": permanent, "reversible": reversible,
        "needs_live_watch": args.action in ("add-to-watch", "rm-watch", "clean-watch"),
        "watch_connected": connected,
        "changes": changes,
    }


def _loc_apply(args):
    """Saved-point edit: always a watch write (pull live -> patch -> verify -> delete+upload ->
    sync backup), so require the Fenix on USB, then shell the guarded locations.py --yes and
    surface its JSON result."""
    if not watch_present(force=True):
        return {"ok": False, "error": "watch_not_connected",
                "message": "Plug in your Fenix over USB to change its saved points."}
    op = "delete" if args.action == "rm-location" else "rename"
    argv = [PY, os.path.join(HERE, "locations.py"), op, "--index", str(args.index), "--yes"]
    if op == "rename":
        argv += ["--to", args.to or ""]
    if getattr(args, "expect", None):
        argv += ["--expect", args.expect]
    res = _run_content(argv)
    for line in reversed(res["stdout"].splitlines()):   # locations.py prints a JSON result line
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except Exception:
                continue
            parsed.setdefault("action", args.action)
            parsed.setdefault("kind", "location")
            if not parsed.get("ok") and "message" not in parsed:
                parsed["message"] = res["stderr"].strip() or "the engine reported an error"
            return parsed
    return {"ok": res["rc"] == 0, "action": args.action, "kind": "location", "rc": res["rc"],
            "output": (res["stdout"] + res["stderr"]).strip(),
            "message": (res["stdout"] + res["stderr"]).strip() or "the engine reported an error"}


def cmd_apply(args):
    if args.kind == "location":
        return _loc_apply(args)
    if args.action in ("add-to-watch", "rm-watch", "clean-watch") and not watch_present(force=True):
        return {"ok": False, "error": "watch_not_connected",
                "message": "Plug in your Fenix over USB to change what's on the watch."}
    # Rename touches the watch only if the item is currently ON it. The UI already knows the
    # placement (from its snapshot) and passes it, so we DON'T re-login or rebuild the whole
    # matrix just to look up one row — that was the bulk of the old rename latency, and it also
    # made a Connect-only rename fail whenever the watch was unplugged. A Connect-only rename
    # needs no watch at all; only a synced item requires the Fenix plugged in (to keep the two
    # names in step), so we guard just that case.
    if args.action == "rename":
        on_watch = getattr(args, "on_watch", None) == "1" and not getattr(args, "scheduled", False)
        if on_watch and not watch_present(force=True):
            return {"ok": False, "error": "watch_not_connected",
                    "message": "This item is on your Fenix; plug it in over USB so the name updates in both places."}
    res = _run_content(_content_argv(args, yes=True))
    out = {"ok": res["rc"] == 0, "action": args.action, "kind": args.kind,
           "rc": res["rc"], "output": (res["stdout"] + res["stderr"]).strip()}
    if res["rc"] != 0:
        out["message"] = out["output"] or "the engine reported an error"
    return out


def cmd_thumbs(args):
    """Fill route thumbnails that have no local-FIT trace by fetching each course's
    geoPoints from Connect's course-service, tracing them, and caching by id. Called
    once by the UI (after the snapshot renders) for the ids still missing a shape, so
    the fast offline snapshot stays fast and every route eventually shows its line.
    --ids <id,id,...>. Returns {id: {vb,d}} for the ones that produced a trace."""
    ids = [i for i in (args.ids or "").split(",") if i.strip()]
    cache = route_thumb.connect_load()
    todo = [i for i in ids if not route_thumb.connect_fresh(cache.get(i))]
    out = {}
    if todo:
        g = content.login()
        for cid in todo:
            try:
                p = g.client.request("GET", "connectapi",
                                     f"/course-service/course/{cid}", api=True).json()
                route_thumb.connect_store(cache, cid, p)
            except Exception as e:
                sys.stderr.write(f"  thumb fetch failed id={cid}: {e!r}\n")
                cache[str(cid)] = {"vb": None, "d": None, "pts": 0}  # don't retry a bad id
        route_thumb.connect_save(cache)
    for cid in ids:
        e = cache.get(str(cid))
        if e and e.get("d"):
            # the whole public trace (shape + metrics + bearing + start lat/lon), so a
            # freshly-filled route shows its data line and can be wind-checked right away.
            out[str(cid)] = route_thumb._public(e)
    return {"ok": True, "thumbs": out}


# ---- start-place geocoding + present position ---------------------------------

def cmd_geocode(args):
    """A typed place name -> candidate anchor points (keyless open-meteo geocoding)."""
    return {"ok": True, "query": args.q, "results": route_geo.geocode(args.q)}


def cmd_here(args):
    """Present position (APPROXIMATE, from the network IP). The precise anchors are a searched
    place or a saved Place; this is the convenience 'near me' button."""
    return route_geo.here()


# ---- import a route (GPX/FIT -> course FIT -> Mac library [+ watch]) -----------

def _unique_backup_path(filename):
    """A non-colliding path in the Mac-backup Courses dir for `filename` (adds _2, _3, ...)."""
    cbd = content.course_backup_dir()
    os.makedirs(cbd, exist_ok=True)
    base, ext = os.path.splitext(filename)
    cand = os.path.join(cbd, filename)
    i = 2
    while os.path.exists(cand):
        cand = os.path.join(cbd, f"{base}_{i}{ext}")
        i += 1
    return cand


CONNECT_COURSE_ACTIVITY = {
    "cycling": 10,   # road_biking
    "running": 1,
    "walking": 9,
    "hiking": 3,
    "swimming": 26,
}


def _connect_course_payload(imported, name, sport):
    payload = json.loads(json.dumps(imported))
    payload["courseName"] = name
    payload["activityTypePk"] = CONNECT_COURSE_ACTIVITY.get(sport, CONNECT_COURSE_ACTIVITY["cycling"])
    payload["rulePK"] = 2          # private, same as the web UI default
    payload["sourceTypeId"] = 3    # user-created/imported course
    payload["coordinateSystem"] = "WGS84"
    payload["targetCoordinateSystem"] = "WGS84"
    payload["originalCoordinateSystem"] = "WGS84"
    payload["hasTurnDetectionDisabled"] = False
    payload["includeLaps"] = False
    payload["favorite"] = False
    pts = payload.get("geoPoints") or []
    if not pts:
        raise ValueError("Garmin Connect returned no route points")
    payload["startPoint"] = pts[0]
    payload["coursePoints"] = [
        p for p in (payload.get("coursePoints") or [])
        if p.get("coursePointType") not in ("start", "finish")
    ]
    for point in payload["coursePoints"]:
        point.pop("lapByCoursePointValue", None)
    for key in list(payload.keys()):
        if payload[key] is None:
            del payload[key]
    return payload


def create_connect_course(src, name, sport):
    g = content.login()
    ctype = mimetypes.guess_type(src)[0] or "application/octet-stream"
    with open(src, "rb") as f:
        imported = g.client.request(
            "POST", "connectapi", "/course-service/course/import", api=True,
            files={"file": (os.path.basename(src), f, ctype)},
        ).json()
    created = g.client.request(
        "POST", "connectapi", "/course-service/course", api=True,
        json=_connect_course_payload(imported, name, sport),
    ).json()
    cid = created.get("courseId") or created.get("id")
    if not cid:
        raise RuntimeError("Garmin Connect create returned no course id")
    return {"id": cid, "name": created.get("courseName") or name}


def remember_watch_course_name(fname, name):
    manifest = content.load_manifest()
    manifest.setdefault("course", {})[fname] = name
    json.dump(manifest, open(paths.state("watch_manifest.json"), "w"),
              ensure_ascii=False, indent=2)


def short_error(e):
    return " ".join(str(e).split())[:240]


def cmd_import_route(args):
    """Build a Garmin course FIT from a GPX/FIT file, save it to the Mac route library, and
    (when --to-watch and the Fenix is on USB) upload it into GARMIN/Courses so it navigates on
    the watch. Returns {ok, imported, name, sport, dist_m, ..., saved_to_library, in_connect,
    on_watch, connect_message, watch_message}. The library copy makes the route thumbnailed +
    re-addable even if the watch is unplugged now."""
    src = args.file
    if not src or not os.path.exists(src):
        return {"ok": False, "error": "no_file", "message": "route file not found"}
    import shutil
    try:
        tmp = os.path.join(tempfile.gettempdir(), "gb_import_route.fit")
        res = route_course.import_route(src, name=args.name or None,
                                        sport=args.sport or None, dst=tmp)
    except Exception as e:
        return {"ok": False, "error": "parse", "message": f"could not read the route: {e}"}

    # --dry: parse + build to temp only (to compute the real distance), report what WE detected,
    # and touch nothing. The app calls this first so the Add-route dialog shows the route's real
    # name/sport/distance before the user commits.
    if getattr(args, "dry", False):
        return {"ok": True, "preview": True, "imported": False, "name": res["name"],
                "sport": res["sport"], "dist_m": res["dist_m"], "asc_m": res["asc_m"],
                "n_points": res["n_points"]}

    # save into the Mac route library (COURSE_BACKUP) under a unique, watch-safe name
    lib_path = _unique_backup_path(route_course._safe_filename(res["name"]))
    saved = False
    try:
        shutil.copyfile(tmp, lib_path)
        saved = True
    except Exception as e:
        log(f"could not save to route library: {e!r}")
        lib_path = tmp   # fall back to the temp copy for the upload

    out = {"ok": True, "imported": True, "name": res["name"], "sport": res["sport"],
           "dist_m": res["dist_m"], "asc_m": res["asc_m"], "n_points": res["n_points"],
           "filename": os.path.basename(lib_path), "saved_to_library": saved,
           "in_connect": False, "connect_id": None, "connect_message": "",
           "on_watch": False, "watch_message": ""}

    if args.to_connect:
        try:
            cc = create_connect_course(src, res["name"], res["sport"])
            out["in_connect"] = True
            out["connect_id"] = cc["id"]
            out["connect_message"] = "Added to Garmin Connect."
        except Exception as e:
            log(f"Connect course create failed: {short_error(e)}")
            out["connect_error"] = short_error(e)
            out["connect_message"] = "Garmin Connect upload failed; saved to your Mac route library."

    if not args.to_watch:
        out["watch_message"] = "Saved to your route library. Plug in your Fenix and add it to the watch."
        return out
    if not watch_present(force=True):   # about to WRITE to the watch — never gate on a stale reading
        out["watch_message"] = "Saved to your route library. Plug in your Fenix over USB to add it to the watch."
        return out

    fname = os.path.basename(lib_path)
    p = subprocess.run(["bash", content.MTP, "upload", lib_path, content.WATCH_FOLDER["course"]],
                       capture_output=True, text=True)
    log(p.stderr)
    if p.returncode == 0:
        add_imported(fname)                 # remember it so it's never flagged "stale"
        remember_watch_course_name(fname, res["name"])
        if os.path.exists(SNAPSHOT):
            try:
                os.remove(SNAPSHOT)         # force a live re-list so the new route shows
            except OSError:
                pass
        out["on_watch"] = True
        out["watch_message"] = "Added to your Fenix. It navigates from Courses on the watch."
    else:
        out["watch_message"] = "Saved to your library, but the watch upload failed — try Refresh and add it again."
    return out


# ---- forecast wind (open-meteo, no key) ---------------------------------------

WIND_TTL = 1800          # seconds a cached hour stays fresh (forecasts refresh ~hourly)
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


def _wind_load():
    if os.path.exists(WIND_CACHE):
        try:
            return json.load(open(WIND_CACHE))
        except Exception:
            pass
    return {}


RAIN_LOOKAHEAD = 8       # hours after the chosen slot to scan for the next rain onset
RAIN_PROB, RAIN_MM = 50, 0.2   # "rain likely" = >=50% chance or >=0.2 mm in an hour


def _wind_fetch(coords, hour):
    """Fetch the conditions at `hour` (UTC 'YYYY-MM-DDTHH:00') for a batch of unique
    (lat, lon) pairs in ONE open-meteo call. Returns per point
    {deg, speed, gust, temp, feels, code, rain_in, rain_prob}: wind direction (compass
    bearing the wind blows FROM) + mean speed + gust (km/h), air temp + apparent 'feels
    like' (deg C), the WMO weather code (-> a sun/cloud/rain glyph so you know how to
    dress), and a rain outlook: rain_in = hours from the slot to the next likely rain
    (0 = at the slot, None = dry for the next few hours), rain_prob = its % chance."""
    if not coords:
        return {}
    q = urllib.parse.urlencode({
        "latitude": ",".join(f"{la:.4f}" for la, _ in coords),
        "longitude": ",".join(f"{lo:.4f}" for _, lo in coords),
        "hourly": ("temperature_2m,apparent_temperature,weather_code,uv_index,precipitation_probability,"
                   "precipitation,wind_speed_10m,wind_direction_10m,wind_gusts_10m"),
        "daily": "sunset",       # so the UI can say if you'll have light / need bike lights
        "wind_speed_unit": "kmh",
        "timezone": "GMT",       # UTC times, so one hour string matches every location
        "forecast_days": 4,      # covers now .. +2 days (the UI's furthest plan slot)
    })
    req = urllib.request.Request(f"{OPEN_METEO}?{q}", headers={"User-Agent": "GarminBridge"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    locs = data if isinstance(data, list) else [data]   # multi-point -> list, single -> dict
    hh, hd = hour[:13], hour[:10]                        # 'YYYY-MM-DDTHH' and the date
    out = {}
    for (la, lo), loc in zip(coords, locs):
        hr = (loc or {}).get("hourly") or {}
        times = hr.get("time") or []
        idx = next((i for i, t in enumerate(times) if t[:13] == hh), None)
        if idx is None:
            continue
        col = lambda k: hr.get(k) or []
        at = lambda k: col(k)[idx] if idx < len(col(k)) else None
        deg, spd = at("wind_direction_10m"), at("wind_speed_10m")
        if deg is None or spd is None:
            continue
        temp, feels, code = at("temperature_2m"), at("apparent_temperature"), at("weather_code")
        gust, uv = at("wind_gusts_10m"), at("uv_index")
        # rain outlook: first hour at/after the slot where rain is likely + how hard then
        probs, precs = col("precipitation_probability"), col("precipitation")
        rain_in = rain_prob = rain_mm = None
        for j in range(idx, min(idx + RAIN_LOOKAHEAD + 1, len(times))):
            p = probs[j] if j < len(probs) else None
            mm = precs[j] if j < len(precs) else None
            if (p is not None and p >= RAIN_PROB) or (mm is not None and mm >= RAIN_MM):
                rain_in, rain_prob = j - idx, (None if p is None else int(p))
                rain_mm = None if mm is None else round(mm, 1)
                break
        # sunset for the slot's day (UTC ISO; the UI localises it + works out light left)
        daily = (loc or {}).get("daily") or {}
        dtimes, sunsets = daily.get("time") or [], daily.get("sunset") or []
        di = next((i for i, t in enumerate(dtimes) if t[:10] == hd), None)
        sunset = sunsets[di] if di is not None and di < len(sunsets) else None
        out[(la, lo)] = {
            "deg": round(deg), "speed": round(spd),
            "gust": None if gust is None else round(gust),
            "temp": None if temp is None else round(temp),
            "feels": None if feels is None else round(feels),
            "code": None if code is None else int(code),
            "uv": None if uv is None else round(uv),
            "rain_in": rain_in, "rain_prob": rain_prob, "rain_mm": rain_mm,
            "sunset": sunset,
        }
    return out


AIR_API = "https://air-quality-api.open-meteo.com/v1/air-quality"


def _air_fetch(coords, hour):
    """Air quality + pollen at `hour` for the batch, from open-meteo's separate (keyless)
    air-quality API. Returns {(lat,lon): {aqi, pollen}}: European AQI + the peak of the
    common pollens (grains/m3; Europe-only via CAMS, None elsewhere)."""
    if not coords:
        return {}
    q = urllib.parse.urlencode({
        "latitude": ",".join(f"{la:.4f}" for la, _ in coords),
        "longitude": ",".join(f"{lo:.4f}" for _, lo in coords),
        "hourly": "european_aqi,grass_pollen,birch_pollen,ragweed_pollen",
        "timezone": "GMT",
        "forecast_days": 4,
    })
    req = urllib.request.Request(f"{AIR_API}?{q}", headers={"User-Agent": "GarminBridge"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    locs = data if isinstance(data, list) else [data]
    hh = hour[:13]
    out = {}
    for (la, lo), loc in zip(coords, locs):
        hr = (loc or {}).get("hourly") or {}
        times = hr.get("time") or []
        idx = next((i for i, t in enumerate(times) if t[:13] == hh), None)
        if idx is None:
            continue
        at = lambda k: (hr.get(k) or [])[idx] if idx < len(hr.get(k) or []) else None
        aqi = at("european_aqi")
        pol = [p for p in (at("grass_pollen"), at("birch_pollen"), at("ragweed_pollen")) if p is not None]
        out[(la, lo)] = {"aqi": None if aqi is None else round(aqi),
                         "pollen": None if not pol else round(max(pol))}
    return out


FIELDS = ("deg", "speed", "gust", "temp", "feels", "code", "uv",
          "rain_in", "rain_prob", "rain_mm", "sunset", "aqi", "pollen")  # per-route bundle


def cmd_wind(args):
    """Per-route forecast conditions for a chosen hour. --q is 'uid,lat,lon;...' (the UI
    already knows each route's start from its trace); --hour is a UTC 'YYYY-MM-DDTHH:00'.
    Conditions change hourly and depend on WHEN you ride, so this is fetched on demand
    (never in the snapshot) and cached only briefly. Returns {uid: {deg, speed, temp,
    feels, code}}: wind (deg = the direction it blows FROM, for the tailwind-home verdict)
    plus temperature, feels-like, and the WMO weather code. No Garmin login: open-meteo
    is public and keyless."""
    hour = (args.hour or "").strip()
    reqs = []                                            # [(uid, rlat, rlon)]
    for part in (args.q or "").split(";"):
        bits = part.rsplit(",", 2)
        if len(bits) != 3:
            continue
        uid, la, lo = bits
        try:
            reqs.append((uid, round(float(la), 2), round(float(lo), 2)))
        except ValueError:
            continue
    if not reqs or not hour:
        return {"ok": True, "hour": hour, "wind": {}}

    cache = _wind_load()
    now = time.time()
    key = lambda la, lo: f"{la},{lo},{hour}"
    fresh = lambda e: bool(e) and now - e.get("at", 0) <= WIND_TTL and "sunset" in e  # "sunset" -> latest bundle
    need = {(la, lo) for _, la, lo in reqs if not fresh(cache.get(key(la, lo)))}
    if need:
        batch = sorted(need)
        try:
            fetched = _wind_fetch(batch, hour)
        except Exception as e:
            sys.stderr.write(f"  wind fetch failed: {e!r}\n")   # best-effort, serve what we have
            fetched = {}
        air = {}
        try:
            air = _air_fetch(batch, hour)                        # separate endpoint, own failure domain
        except Exception as e:
            sys.stderr.write(f"  air fetch failed: {e!r}\n")     # weather still shows without it
        for coord, vals in fetched.items():
            a = air.get(coord, {})
            cache[key(*coord)] = {**vals, "aqi": a.get("aqi"), "pollen": a.get("pollen"), "at": now}
        if fetched:
            json.dump(cache, open(WIND_CACHE, "w"))

    out = {}
    for uid, la, lo in reqs:
        e = cache.get(key(la, lo))
        if e and "deg" in e:
            out[uid] = {f: e.get(f) for f in FIELDS}
    return {"ok": True, "hour": hour, "wind": out}


def _track_from_backup_file(name):
    if not name:
        return None, None
    path = os.path.join(route_thumb.course_backup_dir(), os.path.basename(name))
    if not os.path.isfile(path):
        return None, None
    track, meta = route_thumb._u_track_fit(path)
    return track, {"source": "mac-backup", "name": os.path.basename(path), "meta": meta}


def _track_from_backup_name(name):
    if not name:
        return None, None
    path, internal = content.backup_course_fit(name)
    if not path:
        return None, None
    track, meta = route_thumb._u_track_fit(path)
    return track, {"source": "mac-backup", "name": internal or os.path.basename(path), "meta": meta}


def _track_from_connect_id(cid):
    if not cid:
        return None, None
    g = content.login()
    payload = g.client.get("connectapi", f"/course-service/course/{cid}", api=True).json()
    track, meta = route_thumb._u_track_geopoints(payload or {})
    return track, {"source": "garmin-connect", "name": (payload or {}).get("courseName"), "meta": meta}


def cmd_route_wind(args):
    """Directional wind along one route, timed from the selected start time.

    Route selectors are read-only. Prefer the exact Connect id when present; local
    backup selectors let watch-only/library routes work without a Connect course id.
    """
    tried = []
    track = meta = None
    for label, fn, value in (
        ("id", _track_from_connect_id, args.id),
        ("watch-file", _track_from_backup_file, args.watch_file),
        ("name", _track_from_backup_name, args.name),
    ):
        if not value:
            continue
        tried.append(label)
        try:
            track, meta = fn(value)
        except Exception as e:
            sys.stderr.write(f"  route-wind {label} lookup failed: {e!r}\n")
            track = meta = None
        if track:
            break
    if not track:
        return {"ok": False, "error": "route_not_found",
                "message": "Could not find GPS points for that route."}
    try:
        wind = route_wind.compute_route_wind(track, start_iso=args.start_iso,
                                             reverse=args.reverse, speed_kmh=args.speed_kmh)
    except Exception as e:
        return {"ok": False, "error": "wind_unavailable",
                "message": f"Could not compute route wind: {e}"}
    return {"ok": True, "selector": {k: v for k, v in {
        "id": args.id, "watch_file": args.watch_file, "name": args.name,
        "tried": tried, "source": (meta or {}).get("source"),
        "route_name": (meta or {}).get("name"),
    }.items() if v}, "wind": wind}


# ---- workout authoring (shared engine, in-app) --------------------------------
# The app authors workouts two ways — a no-LLM structured form, and a BYO-key
# natural-language/image input — and BOTH end at the same engine (workout.py):
# validate → build → push. This bridge re-implements none of that; it calls it.

def _spec_arg(args):
    return json.loads(args.spec)


def cmd_workout_catalog(args):
    return {"ok": True, "hits": W.catalog.search(args.search, limit=25)}


def cmd_workout_preview(args):
    """Validate a spec + return a human-readable read-back. No account touched.
    ok=command ran; `valid`=the spec itself passed (errors don't fail the command)."""
    spec = _spec_arg(args)
    res = W.validate(spec)
    return {"ok": True, "valid": not res["errors"], "name": spec.get("name"),
            "errors": res["errors"], "warnings": res["warnings"],
            "summary": W.summarize(spec)}


def cmd_workout_push(args):
    """Validate → push to the live account via the app's own Connect session.
    ok=command ran; `pushed`=it reached Connect (a bad spec sets pushed=false, not ok=false).
    --schedule DATE also schedules it so it auto-syncs to the watch (no manual Send-to-device)."""
    spec = _spec_arg(args)
    res = W.validate(spec)
    if res["errors"]:
        return {"ok": True, "pushed": False, "errors": res["errors"],
                "warnings": res["warnings"]}
    g = content.login()
    result = W.push(spec, g=g, schedule_date=(args.schedule or None))
    return {"ok": True, "pushed": True, "warnings": res["warnings"], **result}


def cmd_workout_get(args):
    wid = str(args.id or "").strip()
    if not wid.isdigit():
        return {"ok": False, "error": "bad-id",
                "message": "Could not load this workout from Garmin Connect."}
    try:
        g = content.login()
        detail = g.connectapi(f"/workout-service/workout/{wid}")
        spec = W.dto_to_spec(detail)
        segs = detail.get("workoutSegments") or []
        sport = ((detail.get("sportType") or {}).get("sportTypeKey")
                 or (((segs[0] or {}).get("sportType") or {}).get("sportTypeKey") if segs else None)
                 or "")
        return {"ok": True, "spec": spec, "name": detail.get("workoutName") or "",
                "sport": sport}
    except Exception as e:
        return {"ok": False, "error": repr(e),
                "message": "Could not load this workout from Garmin Connect."}


def cmd_workout_settings_get(args):
    return {"ok": True, **L.public_settings()}


def cmd_workout_settings_set(args):
    # No --key-stdin means the caller is not changing the key, so pass None and leave the
    # stored one alone. Reading stdin unconditionally would hang every settings change that
    # only touches the provider or model, since nothing would ever be written to the pipe.
    key = _secret_from_stdin() if getattr(args, "key_stdin", False) else None
    pub = L.save_settings(provider=args.provider, model=args.model, key=key,
                          openai_auth=getattr(args, "openai_auth", None),
                          openai_oauth_model=getattr(args, "openai_oauth_model", None))
    return {"ok": True, **pub}


def cmd_workout_models(args):
    return {"ok": True, **L.model_options(provider=args.provider, auth=getattr(args, "auth", None))}


def cmd_workout_llm(args):
    """Author path: user text/image → spec (via their model) → validate + preview.
    Uses the ChatGPT subscription (OAuth) when OpenAI+oauth is configured, else the BYO key;
    on OAuth failure with a key set it falls back and reports a note. Returns the spec so the
    UI can show it and let the user push or edit."""
    notes = []
    try:
        spec = L.author(args.text or "", image_path=args.image or None, notes=notes)
    except Exception as e:
        return {"ok": False, "error": "llm", "message": str(e)}
    res = W.validate(spec)
    return {"ok": True, "spec": spec, "name": spec.get("name"),
            "errors": res["errors"], "warnings": res["warnings"],
            "summary": W.summarize(spec), "notes": notes}


# ---- Sign in with ChatGPT (OpenAI OAuth) --------------------------------------

def cmd_workout_oauth_login(args):
    """Blocking: start the localhost:1455 callback server, open the browser, wait for the user to
    finish the ChatGPT sign-in, exchange the code for tokens, store them. Returns oauth status.
    This intentionally blocks while the browser login happens (the app awaits it)."""
    try:
        st = oauth_openai.login()
    except Exception as e:
        return {"ok": False, "error": "oauth", "message": str(e)}
    return {"ok": True, **st, **L.public_settings()}


def cmd_workout_oauth_status(args):
    return {"ok": True, **oauth_openai.status()}


def cmd_workout_oauth_logout(args):
    oauth_openai.logout()
    return {"ok": True, **oauth_openai.status(), **L.public_settings()}


def _secret_from_stdin() -> str:
    """Read one secret from stdin.

    Secrets are passed this way rather than as arguments because any process on
    the machine can read another's command line via `ps`.
    """
    data = sys.stdin.readline()
    return data.rstrip("\n")


def cmd_garmin_status(args):
    return garmin_auth.status()


def cmd_garmin_login(args):
    """Sign in, staying alive across the verification-code wait.

    Protocol on stdin/stdout, one line each, because the MFA state cannot be
    serialised and so cannot be split across two invocations:
      < password              (first line of stdin)
      > {"needs_code": true}  (only if Garmin asks for one; flushed immediately)
      < code                  (second line of stdin)
      > the final JSON        (printed by main(), as every other command)
    A caller that never sees `needs_code` simply never sends a second line.
    """
    def read_code():
        print(json.dumps({"needs_code": True}), flush=True)
        return _secret_from_stdin()

    return garmin_auth.sign_in(args.email, _secret_from_stdin(), read_code)


def cmd_garmin_logout(args):
    return garmin_auth.logout()


def cmd_health_query(args):
    if args.date:
        if args.start or args.end or args.bucket is not None:
            raise ValueError("use --date or --from/--to, not both")
        with health_archive.open_ro() as db:
            result = health_archive.query_day(db, args.date, args.series, args.points)
    else:
        if not args.start or not args.end:
            raise ValueError("health-query needs --date or both --from and --to")
        with health_archive.open_ro() as db:
            result = health_archive.query_range(
                db, args.start, args.end, args.series, args.bucket)
        if args.points:
            result["points_omitted"] = "points are only available for a single --date"
    return {"ok": True, **result}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--refresh", action="store_true")
    s = sub.add_parser("thumbs")
    s.add_argument("--ids", required=True)
    s = sub.add_parser("wind")
    s.add_argument("--q", required=True, help="uid,lat,lon;uid,lat,lon;...")
    s.add_argument("--hour", required=True, help="UTC hour, YYYY-MM-DDTHH:00")
    s = sub.add_parser("route-wind")
    s.add_argument("--id", help="exact Garmin Connect course id")
    s.add_argument("--name", help="route name, matched against the Mac backup FIT's internal course name")
    s.add_argument("--watch-file", dest="watch_file", help="exact route FIT basename from the watch/Mac backup")
    s.add_argument("--start-iso", dest="start_iso", help="ride start time; ISO-8601, UTC if no zone is supplied")
    s.add_argument("--reverse", action="store_true", help="analyse the route in reverse")
    s.add_argument("--speed-kmh", dest="speed_kmh", type=float, default=route_wind.DEFAULT_SPEED_KMH,
                   help="assumed average ride speed for route-timed forecast sampling")
    s = sub.add_parser("route-tags-set")
    s.add_argument("--key", required=True, help="backup FIT basename from the Mac route library")
    s.add_argument("--tags", default="", help="comma-separated route tags; empty clears tags")
    s.add_argument("--folder", default="", help="route folder name; empty clears folder")
    s = sub.add_parser("geocode")
    s.add_argument("--q", required=True, help="a place name to look up")
    sub.add_parser("here")
    s = sub.add_parser("import-route")
    s.add_argument("--file", required=True, help="path to a .gpx or .fit route")
    s.add_argument("--name", help="override the route name")
    s.add_argument("--sport", help="cycling|running|walking|hiking|swimming")
    s.add_argument("--to-watch", dest="to_watch", action="store_true",
                   help="also upload it into GARMIN/Courses (needs the Fenix on USB)")
    s.add_argument("--to-connect", dest="to_connect", action="store_true",
                   help="also create it in Garmin Connect")
    s.add_argument("--dry", action="store_true",
                   help="parse + report the detected route only; save/upload nothing")
    sub.add_parser("voice-list")
    sub.add_parser("voice-import")
    s = sub.add_parser("voice-archive")
    s.add_argument("--audio", required=True, help="path to a local .wav voice memo")
    s.add_argument("--undo", action="store_true", help="move the memo out of Archive")
    s = sub.add_parser("voice-delete")
    s.add_argument("--audio", required=True, help="path to a local .wav voice memo")
    s = sub.add_parser("voice-rename")
    s.add_argument("--audio", required=True, help="path to a local .wav voice memo")
    s.add_argument("--name", required=True, help="new memo filename stem")
    s = sub.add_parser("voice-note")
    s.add_argument("--audio", required=True, help="path to a local .wav voice memo")
    s = sub.add_parser("voice-transcribe")
    s.add_argument("--audio", required=True, help="path to a local .wav voice memo")
    s = sub.add_parser("voice-cleanup-archived")
    s.add_argument("--days", required=True)
    s.add_argument("--apply", action="store_true")
    sub.add_parser("settings-get")
    s = sub.add_parser("settings-set")
    s.add_argument("key", help="config key, or auto-import")
    s.add_argument("value", nargs="?", help="value to save")
    s.add_argument("--unset", action="store_true", help="remove this config key")
    s.add_argument("--minutes", type=int, default=0, help="auto-import pause duration")
    s = sub.add_parser("transcription-install")
    s.add_argument("--backend", required=True, choices=sorted(LOCAL_TRANSCRIPTION),
                   help="local transcription backend to install")
    for name in ("preview", "apply"):
        s = sub.add_parser(name)
        s.add_argument("--action", required=True,
                       choices=["add-to-watch", "rm-watch", "rm-connect", "clean-watch",
                                "rename", "rm-location"])
        s.add_argument("--kind", required=True, choices=["workout", "course", "location"])
        s.add_argument("--id")
        s.add_argument("--name")
        s.add_argument("--to", dest="to")
        s.add_argument("--index")
        s.add_argument("--expect")
        s.add_argument("--watch-file", dest="watch_file")
        s.add_argument("--state")
        # caller-known placement for rename (from the UI's snapshot): lets the engine skip its
        # inventory fetch + USB watch scan, and gates the watch-required check without a login.
        s.add_argument("--on-watch", dest="on_watch", choices=["0", "1"])
        s.add_argument("--watch-idx", dest="watch_idx")
        s.add_argument("--scheduled", action="store_true")
    # workout authoring
    s = sub.add_parser("workout-catalog")
    s.add_argument("--search", required=True)
    for name in ("workout-preview", "workout-push"):
        s = sub.add_parser(name)
        s.add_argument("--spec", required=True, help="the workout spec as a JSON string")
        if name == "workout-push":
            s.add_argument("--schedule", help="YYYY-MM-DD to schedule → auto-syncs to the watch")
    s = sub.add_parser("workout-get")
    s.add_argument("--id", required=True)
    sub.add_parser("workout-settings-get")
    s = sub.add_parser("workout-settings-set")
    s.add_argument("--provider")
    s.add_argument("--model")
    s.add_argument("--key-stdin", dest="key_stdin", action="store_true",
                   help="read the API key from the first line of stdin. It is never taken "
                        "as an argument: any process on the machine can read a command "
                        "line via `ps`. Omit to leave the stored key untouched")
    s.add_argument("--openai-auth", dest="openai_auth", choices=["key", "oauth"],
                   help="OpenAI auth mode: BYO API key, or Sign in with ChatGPT")
    s.add_argument("--openai-oauth-model", dest="openai_oauth_model",
                   help="model to use on the ChatGPT subscription surface (e.g. gpt-5.5)")
    s = sub.add_parser("workout-models")
    s.add_argument("--provider", choices=list(L.PROVIDERS))
    s.add_argument("--auth", choices=["key", "oauth"], default="key")
    s = sub.add_parser("workout-llm")
    s.add_argument("--text", default="")
    s.add_argument("--image", help="path to a screenshot/photo of a plan")
    # Sign in with ChatGPT (OpenAI OAuth) — login blocks while the browser flow runs
    sub.add_parser("workout-oauth-login")
    sub.add_parser("workout-oauth-status")
    sub.add_parser("workout-oauth-logout")
    sub.add_parser("garmin-status")
    s = sub.add_parser("garmin-login"); s.add_argument("--email", required=True)
    sub.add_parser("garmin-logout")
    s = sub.add_parser("health-query")
    s.add_argument("--date", help="YYYY-MM-DD")
    s.add_argument("--from", dest="start", help="YYYY-MM-DD")
    s.add_argument("--to", dest="end", help="YYYY-MM-DD")
    s.add_argument("--series", help="comma-separated series keys")
    s.add_argument("--points", action="store_true",
                   help="include raw points; only allowed with --date")
    s.add_argument("--bucket", type=int, help="range chart bucket count")
    args = ap.parse_args()
    try:
        out = {"snapshot": cmd_snapshot, "preview": cmd_preview, "apply": cmd_apply,
               "thumbs": cmd_thumbs, "wind": cmd_wind,
               "route-wind": cmd_route_wind,
               "route-tags-set": cmd_route_tags_set,
               "geocode": cmd_geocode, "here": cmd_here, "import-route": cmd_import_route,
               "voice-list": cmd_voice_list,
               "voice-import": cmd_voice_import,
               "voice-archive": cmd_voice_archive,
               "voice-delete": cmd_voice_delete,
               "voice-rename": cmd_voice_rename,
               "voice-note": cmd_voice_note,
               "voice-transcribe": cmd_voice_transcribe,
               "voice-cleanup-archived": cmd_voice_cleanup_archived,
               "settings-get": cmd_settings_get,
               "settings-set": cmd_settings_set,
               "transcription-install": cmd_transcription_install,
               "workout-catalog": cmd_workout_catalog,
               "workout-get": cmd_workout_get,
               "workout-preview": cmd_workout_preview, "workout-push": cmd_workout_push,
               "workout-settings-get": cmd_workout_settings_get,
               "workout-settings-set": cmd_workout_settings_set,
               "workout-models": cmd_workout_models,
               "workout-llm": cmd_workout_llm,
               "workout-oauth-login": cmd_workout_oauth_login,
               "workout-oauth-status": cmd_workout_oauth_status,
               "workout-oauth-logout": cmd_workout_oauth_logout,
               "garmin-status": cmd_garmin_status,
               "garmin-login": cmd_garmin_login,
               "garmin-logout": cmd_garmin_logout,
               "health-query": cmd_health_query}[args.cmd](args)
    except SystemExit as e:
        out = {"ok": False, "error": "engine", "message": str(e)}
    except Exception as e:
        import traceback
        traceback.print_exc()
        out = {"ok": False, "error": "exception", "message": repr(e)}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
