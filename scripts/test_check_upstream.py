#!/usr/bin/env python3
"""Offline tests for the upstream early-warning check.

Runs with no network: every fetch is faked. The point of a monitor is that it is
right when it fires and silent when it should be, so these tests concentrate on
the three ways it could lie -- calling a current pin behind, calling a routine
release urgent, and turning an unreachable network into an alarm.
"""
import check_upstream as C


def test_parse_requirements_reads_only_exact_pins():
    text = (
        "# a comment\n"
        "\n"
        "garminconnect==0.3.6\n"
        "fit-tool==0.9.15  # trailing comment\n"
        "fitdecode==0.11.0\n"
        "loose>=1.0\n"          # not a pin: nothing to compare against
        "PyYAML  # optional\n"  # not a pin
    )
    pins = C.parse_requirements(text)
    assert pins == {"garminconnect": "0.3.6", "fit-tool": "0.9.15",
                    "fitdecode": "0.11.0"}, pins
    print("OK: parse_requirements reads exact pins only ->", sorted(pins))


def test_version_ordering():
    assert C.compare_versions("0.3.6", "0.3.7") == -1
    assert C.compare_versions("0.3.7", "0.3.6") == 1
    assert C.compare_versions("0.3.6", "0.3.6") == 0
    # different segment counts must not compare as unequal
    assert C.compare_versions("0.3", "0.3.0") == 0
    assert C.compare_versions("0.3.6", "0.10.0") == -1, "string compare would call this newer"
    assert C.compare_versions("1.0.0", "0.99.99") == 1
    # a prerelease is older than the same final release
    assert C.compare_versions("0.4.0rc1", "0.4.0") == -1
    assert C.compare_versions("v0.3.7", "0.3.6") == 1, "leading v (git tags) must parse"
    try:
        C.compare_versions("not-a-version", "0.3.6")
    except ValueError:
        pass
    else:
        raise AssertionError("unparseable version should raise, never guess an order")
    print("OK: version ordering, including 0.3.6 < 0.10.0 and rc < final")


def test_auth_terms_do_not_fire_on_innocent_words():
    hit = C.auth_terms_in("Fix login failure when Garmin returns 403 from Cloudflare")
    assert "login" in hit and "403" in hit and "cloudflare" in hit, hit
    # the word-boundary trap this regex exists to avoid
    miss = C.auth_terms_in("Update author metadata and authorised dealer list")
    assert miss == [], f"'author'/'authorised' must not read as auth: {miss}"
    assert C.auth_terms_in("") == []
    assert C.auth_terms_in(None) == []
    print("OK: auth terms fire on real signals, not on 'author'")


def test_releases_newer_than_skips_unparseable_tags():
    releases = [
        {"tag_name": "0.3.8", "body": "b"},
        {"tag_name": "0.3.6", "body": "pinned, not newer"},
        {"tag_name": "nightly", "body": "unparseable, must be skipped"},
        {"tag_name": "0.3.7", "body": "a"},
    ]
    got = [r["tag_name"] for r in C.releases_newer_than(releases, "0.3.6")]
    assert got == ["0.3.8", "0.3.7"], got
    print("OK: newer-release filter skips unparseable tags ->", got)


def _pins():
    return {"garminconnect": "0.3.6", "fitdecode": "0.11.0"}


def test_current_pin_is_not_behind():
    report = C.classify(_pins(), {"garminconnect": "0.3.6", "fitdecode": "0.11.0"}, {})
    assert report["behind"] is False and report["urgent"] is False
    assert C.exit_code_for(report) == 0
    print("OK: a current pin exits 0")


def test_routine_release_is_behind_but_not_urgent():
    report = C.classify(_pins(), {"garminconnect": "0.3.7", "fitdecode": "0.11.0"},
                        {"0.3.7": "Add a body-composition helper and fix a typo"})
    assert report["behind"] is True, report
    assert report["urgent"] is False, report["auth_terms"]
    assert C.exit_code_for(report) == 10
    print("OK: a routine release exits 10, not 20")


def test_auth_release_is_urgent():
    report = C.classify(_pins(), {"garminconnect": "0.3.7", "fitdecode": "0.11.0"},
                        {"0.3.7": "Fix SSO login broken by Garmin's new auth flow"})
    assert report["urgent"] is True, report
    assert "sso" in report["auth_terms"], report["auth_terms"]
    assert C.exit_code_for(report) == 20
    print("OK: an auth release exits 20 ->", report["auth_terms"])


def test_unreadable_notes_warn_without_claiming_routine():
    """The version gap must still surface when the changelog cannot be read."""
    report = C.classify(_pins(), {"garminconnect": "0.3.7", "fitdecode": "0.11.0"}, {})
    assert report["behind"] is True and report["urgent"] is False
    assert report["notes_available"] is False
    assert "unclassified" in C.format_report(report), C.format_report(report)
    print("OK: missing release notes report as unclassified, not as routine")


def test_unknown_latest_is_never_reported_as_behind():
    report = C.classify(_pins(), {"garminconnect": None, "fitdecode": None}, {})
    assert report["behind"] is False, "unknown must not mean behind"
    assert C.exit_code_for(report) == 0
    print("OK: an unknown latest version never reads as behind")


def test_non_auth_package_behind_does_not_raise_the_alarm():
    report = C.classify(_pins(), {"garminconnect": "0.3.6", "fitdecode": "0.99.0"}, {})
    assert report["behind"] is False and report["urgent"] is False
    assert report["also_behind"] == ["fitdecode"], report["also_behind"]
    assert C.exit_code_for(report) == 0
    print("OK: a stale file-format library is reported, not alarmed ->", report["also_behind"])


def test_network_failure_exits_2_not_an_alarm(tmp_requirements):
    """An unreachable PyPI is 'unknown', never 'urgent'. A monitor that cries
    wolf when the wifi drops gets muted, which defeats the whole point."""
    original = C.latest_on_pypi
    C.latest_on_pypi = lambda pkg, fetch=None: (_ for _ in ()).throw(
        C.CheckFailed("simulated network failure"))
    try:
        code = C.main(["--requirements", tmp_requirements])
        assert code == 2, f"offline check must exit 2, got {code}"
    finally:
        C.latest_on_pypi = original
    print("OK: an unreachable network exits 2 (unknown), not 10 or 20")


def test_missing_auth_package_is_a_failed_check(tmp_requirements_without_garminconnect):
    """If the pin this exists to watch disappears, fail loudly rather than
    reporting 'all current' -- silence would be indistinguishable from health."""
    try:
        C.run_check(tmp_requirements_without_garminconnect)
    except C.CheckFailed as e:
        assert C.AUTH_CRITICAL in str(e)
    else:
        raise AssertionError("a missing garminconnect pin must fail the check")
    print("OK: losing the watched pin fails the check instead of reporting health")


def test_real_requirements_file_still_pins_the_watched_package():
    """Guards the check against the repo moving underneath it."""
    with open(C.REQUIREMENTS) as f:
        pins = C.parse_requirements(f.read())
    assert C.AUTH_CRITICAL in pins, (
        f"{C.AUTH_CRITICAL} is no longer pinned in {C.REQUIREMENTS}; "
        "the check is watching the wrong file")
    C.parse_version(pins[C.AUTH_CRITICAL])
    print(f"OK: real requirements.txt pins {C.AUTH_CRITICAL}=={pins[C.AUTH_CRITICAL]}")


def main():
    import os
    import tempfile

    root = tempfile.mkdtemp(prefix="upstream-check-test-")
    full = os.path.join(root, "requirements.txt")
    with open(full, "w") as f:
        f.write("garminconnect==0.3.6\nfitdecode==0.11.0\n")
    partial = os.path.join(root, "no-garminconnect.txt")
    with open(partial, "w") as f:
        f.write("fitdecode==0.11.0\n")

    try:
        test_parse_requirements_reads_only_exact_pins()
        test_version_ordering()
        test_auth_terms_do_not_fire_on_innocent_words()
        test_releases_newer_than_skips_unparseable_tags()
        test_current_pin_is_not_behind()
        test_routine_release_is_behind_but_not_urgent()
        test_auth_release_is_urgent()
        test_unreadable_notes_warn_without_claiming_routine()
        test_unknown_latest_is_never_reported_as_behind()
        test_non_auth_package_behind_does_not_raise_the_alarm()
        test_network_failure_exits_2_not_an_alarm(full)
        test_missing_auth_package_is_a_failed_check(partial)
        test_real_requirements_file_still_pins_the_watched_package()
        print("\nALL OK")
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
