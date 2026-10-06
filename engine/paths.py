"""Where the engine keeps state, as opposed to where its code lives.

Every module used to resolve its runtime state next to its own source
(`os.path.join(HERE, ".tokens")`). That is what made the app host-bound: the
code could not be installed anywhere without carrying one user's credentials,
caches and personal route data along with it, and a shipped copy would write
those files back into the application directory.

State now lives in the macOS-conventional per-user location:

    ~/Library/Application Support/Garmin Bridge/

Note the distinction this module does NOT cross. Files that ship *with* the
engine -- `watch_mtp.sh`, `workout_authoring.md`, `workout_spec.schema.json` --
stay resolved next to the code, because they are part of the distribution, not
user state. Only things the engine *writes* belong here.

Existing installs are migrated on first use, so an upgrade does not silently
log the user out.
"""
import os
import shutil
import shlex
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))

# Override for tests and for running two engines side by side.
_ENV = "GB_STATE_DIR"
_DEFAULT = os.path.expanduser("~/Library/Application Support/Garmin Bridge")

# -- bridge root / settings resolution (honours GARMIN_BRIDGE_ROOT) ------------
# The control CLI and bash side already honour GARMIN_BRIDGE_ROOT; these
# helpers expose the same resolution to the Python engine so readers do not
# hardcode ~/Documents/Garmin Bridge/.... Resolution is lazy and cached.

_GVE_PATHS = os.path.join(HERE, "..", "bin", "gve-paths.sh")

# Historical Python reader defaults. The bash side's no-root default for
# GVE_SET_DEST is ~/Documents/Garmin Settings, but the engine has always
# read from ~/Documents/Garmin Bridge/Settings; preserve that exactly.
_LEGACY_BRIDGE_ROOT = os.path.expanduser("~/Documents/Garmin Bridge")
_LEGACY_SETTINGS_DIR = os.path.join(_LEGACY_BRIDGE_ROOT, "Settings")

# Bash-side default when no root is configured. Matching this means the user
# has not set a custom root/settings dest via config or environment.
_BASH_DEFAULT_SETTINGS_DIR = os.path.expanduser("~/Documents/Garmin Settings")

_GVE_PATHS_CACHE = {}


def _cli_env():
    env = os.environ.copy()
    inherited = env.get("PATH", "")
    env["PATH"] = ":".join(p for p in (inherited, "/opt/homebrew/bin", "/usr/local/bin") if p)
    return env


def _resolve_gve_paths():
    """Resolve GVE_SET_DEST through gve-paths.sh + config.

    Caches the result. Falls back to the legacy Python defaults when no custom
    root is configured or the resolve fails.
    """
    global _GVE_PATHS_CACHE
    if _GVE_PATHS_CACHE:
        return _GVE_PATHS_CACHE
    try:
        script = (
            'GVE_CONFIG="${GVE_CONFIG:-$HOME/.config/garmin-voice-export/config}"; '
            '[ -f "$GVE_CONFIG" ] && . "$GVE_CONFIG"; '
            f'. {shlex.quote(_GVE_PATHS)}; gve_resolve_paths; '
            'printf "%s\\t%s\\n" "$GVE_BRIDGE_BASE" "$GVE_SET_DEST"'
        )
        p = subprocess.run(["bash", "-lc", script], capture_output=True,
                           text=True, env=_cli_env())
        if p.returncode == 0 and p.stdout.strip():
            base, settings = p.stdout.strip().split("\t", 1)
            base = (os.path.realpath(os.path.expandvars(os.path.expanduser(base)))
                    if base else "")
            settings = os.path.realpath(os.path.expandvars(os.path.expanduser(settings)))
            if base or settings != os.path.realpath(_BASH_DEFAULT_SETTINGS_DIR):
                _GVE_PATHS_CACHE = {"base": base, "settings": settings}
                return _GVE_PATHS_CACHE
    except Exception:
        pass
    _GVE_PATHS_CACHE = {
        "base": os.path.realpath(_LEGACY_BRIDGE_ROOT),
        "settings": os.path.realpath(_LEGACY_SETTINGS_DIR),
    }
    return _GVE_PATHS_CACHE


def bridge_settings_dir():
    """The bridge settings directory (GVE_SET_DEST).

    Returns the configured GARMIN_BRIDGE_ROOT/Settings or GARMIN_SETTINGS_DEST
    when set, otherwise the legacy ~/Documents/Garmin Bridge/Settings.
    """
    return _resolve_gve_paths()["settings"]

# State filenames that earlier versions wrote beside the code. Anything listed
# here is migrated out of a legacy directory the first time it is asked for.
_MIGRATABLE = (
    ".tokens",
    ".app_settings.json",
    ".watch_present.json",
    "watch_snapshot.json",
    "watch_manifest.json",
    "stale_routes_seen.json",
    "wind_cache.json",
    "imported_routes.json",
    "route_traces.json",
    "route_traces_connect.json",
)

# Where a pre-migration install kept its state. HERE covers an in-place upgrade;
# the prototype directory covers the original development location, which is
# where this Mac's live Garmin session actually lives.
_LEGACY_DIRS = (
    HERE,
    os.path.abspath(os.path.join(HERE, "..", "prototype", "connect")),
)


def state_dir() -> str:
    """The directory holding this user's engine state. Created on demand."""
    d = os.environ.get(_ENV) or _DEFAULT
    os.makedirs(d, exist_ok=True)
    return d


def state(name: str) -> str:
    """Absolute path to a state file, migrating a legacy copy if one exists.

    Migration copies rather than moves: the legacy directory is gitignored and
    local, so leaving it intact costs nothing and keeps the change reversible
    if a user needs to roll back to an older build.
    """
    target = os.path.join(state_dir(), name)
    if not os.path.exists(target) and name in _MIGRATABLE:
        for legacy_dir in _LEGACY_DIRS:
            src = os.path.join(legacy_dir, name)
            if src == target or not os.path.exists(src):
                continue
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, target)
                else:
                    shutil.copy2(src, target)
            except OSError:
                # A failed migration must not be fatal: the caller will simply
                # find no state and behave as a fresh install would.
                pass
            break
    return target


def asset(name: str) -> str:
    """Absolute path to a file that ships alongside the code, never user state."""
    return os.path.join(HERE, name)
