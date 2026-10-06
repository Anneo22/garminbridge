#!/usr/bin/env python3
"""Early warning that the Garmin authentication stack moved upstream.

Why this exists: the app pins
`garminconnect`, vendors it into an offline wheelhouse, and ships with no
updater. `garminconnect` authenticates by impersonating the Garmin mobile app at
the TLS layer, which Garmin periodically breaks. When it does, a fix appears
upstream within days, but every installed copy of this app stays broken until
somebody rebuilds. This is the cheap signal that a rebuild is needed.

It takes no credentials and makes no authenticated call: it reads the pins out of
engine/requirements.txt and asks PyPI what the current versions are. That is the
whole mechanism, and it is deliberately dumber than a login smoke test, which
would need a stored password to be useful.

A new release is not by itself evidence that Garmin moved. So when `garminconnect`
is behind, this also reads the upstream release notes and reports whether they
mention authentication. That is the difference between "worth a look this week"
and "rebuild now", and it is the same distinction ADR 0002's review trigger asks
for.

Usage:
    ./scripts/check_upstream.py              # human summary
    ./scripts/check_upstream.py --json       # machine-readable
    ./scripts/check_upstream.py --notify     # macOS notification when it matters

Exit codes:
    0   pins are current, nothing to do
    10  a newer garminconnect exists, no authentication signal in its notes
    20  a newer garminconnect exists AND its notes mention authentication
    2   could not complete the check (offline, PyPI unreachable, bad response)

Exit 2 is deliberately not an alarm. A failed check means "unknown", and a
monitor that shouts when the network is down gets muted, which is worse than one
that stays quiet.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

# The one dependency that owns authentication. The others are file-format
# libraries: a stale fitdecode cannot lock anyone out of their account.
AUTH_CRITICAL = "garminconnect"

# garminconnect's own repository. Hardcoded because it is the package's identity,
# not configuration -- if this moves, the pin itself needs revisiting anyway.
AUTH_CRITICAL_REPO = "cyberjunky/python-garminconnect"

# Terms that mark a release as touching the auth path. Chosen to over-trigger
# rather than under-trigger: a false positive costs one glance at a changelog,
# a false negative costs a silently broken app.
#
# Deliberately NOT `auth\w*`: that matches "author" and "authorised", which are
# ordinary changelog words, and a monitor that fires on package metadata is one
# nobody reads. "authorization" is left out for the same reason -- the cases that
# matter are already covered by oauth, token, credential, 401 and 403.
AUTH_TERMS = [
    r"auth", r"authn", r"authenticat\w*",
    r"sso", r"login", r"log[- ]?in", r"sign[- ]?in", r"signin",
    r"mfa", r"2fa", r"oauth", r"token\w*", r"credential\w*",
    r"cloudflare", r"captcha", r"403", r"401", r"rate[- ]?limit\w*",
    r"impersonat\w*", r"user[- ]?agent", r"tls", r"fingerprint\w*",
]
AUTH_RE = re.compile(r"\b(" + "|".join(AUTH_TERMS) + r")\b", re.IGNORECASE)

PYPI_JSON = "https://pypi.org/pypi/{package}/json"
GITHUB_RELEASES = "https://api.github.com/repos/{repo}/releases?per_page=50"
USER_AGENT = "garminbridge-upstream-check (+https://github.com/Anneo22)"
TIMEOUT = 15

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIREMENTS = os.path.join(REPO_ROOT, "engine", "requirements.txt")

# State lives with the app's other state rather than in the repo, so a checkout
# stays clean and a rebuild does not re-notify. The path is duplicated from
# engine/paths.py rather than imported: this script must keep working when the
# engine cannot import, which is exactly the situation it warns about.
STATE_DIR = os.path.expanduser("~/Library/Application Support/Garmin Bridge")
STATE_FILE = os.path.join(STATE_DIR, "upstream-check.json")


class CheckFailed(Exception):
    """The check could not be completed. Not the same as 'something is wrong'."""


# --- pure logic (no network, no disk) ---------------------------------------

def parse_requirements(text):
    """Return {package: pinned_version} for exact `==` pins.

    Only `==` pins are read. A loose requirement has no single version to
    compare against, and this project pins everything on purpose.
    """
    pins = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)\s*==\s*([^\s;]+)$", line)
        if m:
            pins[m.group(1).lower().replace("_", "-")] = m.group(2)
    return pins


def parse_version(text):
    """(release_tuple, is_prerelease), or None if unparseable.

    Enough for PyPI final releases like "0.3.6". Not a full PEP 440
    implementation: this compares release segments and treats any alpha/beta/rc
    suffix as older than the same release without one. That covers every version
    this project has ever pinned, and a version it cannot parse is reported as
    unknown rather than silently mis-ordered.
    """
    if not isinstance(text, str):
        return None
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)\s*(.*)$", text.strip())
    if not m:
        return None
    nums = tuple(int(p) for p in m.group(1).split("."))
    suffix = m.group(2)
    is_pre = bool(re.match(r"^[._-]?(a|b|c|rc|alpha|beta|dev|pre)", suffix, re.I))
    return (nums, is_pre)


def compare_versions(a, b):
    """-1 if a < b, 0 if equal, 1 if a > b. Raises ValueError if either is unparseable."""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        raise ValueError(f"cannot compare versions {a!r} and {b!r}")
    na, prea = pa
    nb, preb = pb
    width = max(len(na), len(nb))
    na = na + (0,) * (width - len(na))
    nb = nb + (0,) * (width - len(nb))
    if na != nb:
        return -1 if na < nb else 1
    if prea != preb:
        # same release numbers: a prerelease is older than the final
        return -1 if prea else 1
    return 0


def auth_terms_in(text):
    """Sorted, de-duplicated auth-related terms found in release notes."""
    if not text:
        return []
    return sorted({m.group(0).lower() for m in AUTH_RE.finditer(text)})


def releases_newer_than(releases, pinned):
    """Release entries strictly newer than `pinned`, newest first.

    Entries whose tag cannot be parsed as a version are skipped rather than
    guessed at; a draft or oddly-tagged release should not silently become the
    thing we compare against.
    """
    out = []
    for rel in releases:
        tag = (rel.get("tag_name") or rel.get("name") or "").strip()
        try:
            if compare_versions(tag, pinned) > 0:
                out.append(rel)
        except ValueError:
            continue
    out.sort(key=lambda r: parse_version(r.get("tag_name") or r.get("name") or "")[0],
             reverse=True)
    return out


def classify(pins, latest, notes_by_version):
    """Build the report. Pure: every input is already fetched.

    `latest` maps package -> latest version string (or None when unknown).
    `notes_by_version` maps version -> release-note text for AUTH_CRITICAL.
    """
    report = {"packages": [], "auth_terms": [], "urgent": False, "behind": False}

    for package in sorted(pins):
        pinned = pins[package]
        current = latest.get(package)
        entry = {"package": package, "pinned": pinned, "latest": current,
                 "behind": False, "auth_critical": package == AUTH_CRITICAL}
        if current is not None:
            try:
                entry["behind"] = compare_versions(pinned, current) < 0
            except ValueError:
                entry["latest"] = None  # unparseable: report as unknown, never as behind
        report["packages"].append(entry)

    critical = next((p for p in report["packages"] if p["auth_critical"]), None)
    if critical and critical["behind"]:
        report["behind"] = True
        terms = auth_terms_in("\n".join(notes_by_version.values()))
        report["auth_terms"] = terms
        report["urgent"] = bool(terms)
        report["notes_available"] = bool(notes_by_version)

    report["also_behind"] = [p["package"] for p in report["packages"]
                             if p["behind"] and not p["auth_critical"]]
    return report


def exit_code_for(report):
    if report["urgent"]:
        return 20
    if report["behind"]:
        return 10
    return 0


def format_report(report):
    """Human summary. Leads with the verdict, then the evidence."""
    critical = next((p for p in report["packages"] if p["auth_critical"]), None)
    lines = []

    if report["urgent"]:
        lines.append(
            f"REBUILD LIKELY NEEDED: {AUTH_CRITICAL} {critical['pinned']} -> "
            f"{critical['latest']}, and the release notes mention authentication.")
        lines.append("Terms found: " + ", ".join(report["auth_terms"]))
        lines.append("Garmin may have changed its auth flow. Installed copies of "
                     "the app cannot receive this fix without a rebuild.")
    elif report["behind"]:
        lines.append(
            f"Behind upstream: {AUTH_CRITICAL} {critical['pinned']} -> {critical['latest']}.")
        if report.get("notes_available"):
            lines.append("The release notes do not mention authentication, so this "
                         "looks routine rather than urgent.")
        else:
            lines.append("Release notes could not be read, so this is unclassified. "
                         "Check the changelog before deciding.")
    else:
        if critical and critical["latest"] is None:
            lines.append(f"{AUTH_CRITICAL}: pinned {critical['pinned']}, latest unknown.")
        else:
            lines.append(f"Up to date: {AUTH_CRITICAL} {critical['pinned']} is current.")

    if report["also_behind"]:
        lines.append("Also behind (not auth-critical): " + ", ".join(report["also_behind"]))

    return "\n".join(lines)


# --- I/O --------------------------------------------------------------------

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        raise CheckFailed(f"could not reach {url}: {e}") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise CheckFailed(f"unreadable response from {url}: {e}") from e


def latest_on_pypi(package, fetch=fetch_json):
    """Latest version string, or None when the response has no usable version.

    PyPI's `info.version` is the latest non-prerelease unless only prereleases
    exist, which is the behaviour we want.
    """
    data = fetch(PYPI_JSON.format(package=package))
    version = (data.get("info") or {}).get("version")
    return version if isinstance(version, str) and version.strip() else None


def release_notes_since(pinned, fetch=fetch_json):
    """{version: notes} for releases newer than `pinned`.

    Returns {} rather than raising when the notes cannot be read: an unreadable
    changelog must not suppress the version warning itself, which is the part
    that always works.
    """
    try:
        releases = fetch(GITHUB_RELEASES.format(repo=AUTH_CRITICAL_REPO))
    except CheckFailed:
        return {}
    if not isinstance(releases, list):
        return {}
    out = {}
    for rel in releases_newer_than(releases, pinned):
        tag = (rel.get("tag_name") or rel.get("name") or "").strip()
        out[tag] = "\n".join(filter(None, [rel.get("name"), rel.get("body")]))
    return out


def read_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, STATE_FILE)
    except OSError:
        # Losing the state file costs a duplicate notification, nothing more.
        pass


def notify(title, message):
    """macOS notification. Best effort: a missing osascript must not fail the check."""
    script = ('display notification {msg} with title {title}'
              .format(msg=json.dumps(message), title=json.dumps(title)))
    try:
        subprocess.run(["osascript", "-e", script], check=False,
                       capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


# --- entry point ------------------------------------------------------------

def run_check(requirements_path=REQUIREMENTS):
    with open(requirements_path) as f:
        pins = parse_requirements(f.read())
    if AUTH_CRITICAL not in pins:
        raise CheckFailed(
            f"{AUTH_CRITICAL} is not pinned in {requirements_path}. Either the "
            f"dependency was renamed or this check is watching the wrong file.")

    latest = {}
    for package in pins:
        try:
            latest[package] = latest_on_pypi(package)
        except CheckFailed:
            if package == AUTH_CRITICAL:
                raise  # the one package we exist to watch: failing to read it is a failed check
            latest[package] = None

    notes = {}
    critical_latest = latest.get(AUTH_CRITICAL)
    if critical_latest and compare_versions(pins[AUTH_CRITICAL], critical_latest) < 0:
        notes = release_notes_since(pins[AUTH_CRITICAL])

    return classify(pins, latest, notes)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--notify", action="store_true",
                    help="send a macOS notification when action may be needed")
    ap.add_argument("--force-notify", action="store_true",
                    help="notify even if this version was already reported")
    ap.add_argument("--requirements", default=REQUIREMENTS,
                    help="requirements file to read pins from")
    args = ap.parse_args(argv)

    try:
        report = run_check(args.requirements)
    except (CheckFailed, OSError) as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            print(f"Check did not complete: {e}", file=sys.stderr)
        return 2

    code = exit_code_for(report)

    if args.json:
        print(json.dumps({"ok": True, "exit_code": code, **report}, indent=2))
    else:
        print(format_report(report))

    if args.notify and report["behind"]:
        critical = next(p for p in report["packages"] if p["auth_critical"])
        state = read_state()
        already = state.get("notified_version")
        if already != critical["latest"] or args.force_notify:
            title = ("Garmin Bridge: rebuild likely needed" if report["urgent"]
                     else "Garmin Bridge: dependency behind")
            notify(title, format_report(report).split("\n")[0])
            state["notified_version"] = critical["latest"]
            state["notified_urgent"] = report["urgent"]
            write_state(state)

    return code


if __name__ == "__main__":
    sys.exit(main())
