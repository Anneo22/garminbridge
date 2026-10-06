#!/usr/bin/env python3
"""GarminBridge Phase 1 - Connect login (run this yourself; your password never leaves this terminal).

Authenticates to Garmin Connect via garminconnect's own auth engine, handles MFA,
and caches OAuth tokens into the application state directory so the other scripts
can run without your password.

Usage:  ./.venv/bin/python login.py
"""
import getpass
import os
import paths
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOKENSTORE = paths.state(".tokens")

from garminconnect import Garmin


def main() -> int:
    email = input("Garmin Connect email: ").strip()
    password = getpass.getpass("Garmin Connect password (hidden): ")

    garmin = Garmin(email=email, password=password, return_on_mfa=True)
    res1, res2 = garmin.login()
    if res1 == "needs_mfa":
        code = input("MFA code (from email/SMS/authenticator): ").strip()
        garmin.resume_login(res2, code)

    os.makedirs(TOKENSTORE, exist_ok=True)
    garmin.client.dump(TOKENSTORE)  # garminconnect 0.3.6 token client
    who = getattr(garmin, "display_name", None) or getattr(garmin, "full_name", "?")
    print(f"\nOK - logged in as {who}. Tokens cached in {TOKENSTORE}")
    print("You can now tell Claude to run probe.py / push_workout.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
