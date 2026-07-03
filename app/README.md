---
Modified: 2026-07-02T09:48
---
# GarminBridge Content Manager

A macOS desktop app (Tauri) for browsing and managing what lives on your Garmin watch and in Garmin Connect, side by side: workouts, routes, and saved points. Route rows carry forecast wind (origin and degrees) and a tailwind-home verdict; hovering a route trace opens a preview with kilometre marks and the elevation profile. Delete flows spell out what is reversible and what is permanent before anything happens.

## How it works

The app is a thin native shell. Every data action shells out to a local Python engine (`prototype/connect/api.py` in the repo root) and renders its JSON. The engine holds the Garmin Connect session and the watch (MTP over USB) logic; the app itself contains no Garmin logic and no tokens.

`prototype/` is intentionally gitignored: it contains auth tokens and account-bound state. Without the engine present, the UI opens but every data action reports the engine as missing.

## Always-on tray (convergence C1)

The app runs as the one always-on GarminBridge app. It installs a menu-bar **tray** with the quick actions the retired Swift menu bar carried: import voice notes, back up activities, back up settings & profiles, open the Garmin Bridge folder, open the Content Manager window, and **Open at login** (launch-at-login, off by default). Quick actions shell the `garminbridge` CLI. Closing the window **drops it to the tray** instead of quitting (the app keeps running); the tray "Open Content Manager" or a dock click brings it back. A login-launch starts hidden in the tray (the LaunchAgent passes `--hidden`).

## Engine and CLI discovery

The shell derives everything from the repo root: `$GB_REPO_DIR`, falling back to `~/Second Brain/2_Code/garmin-voice-export`. From there the Python engine is `<repo>/prototype/connect` (override with `$GB_ENGINE_DIR`; interpreter at `<dir>/.venv/bin/python`) and the CLI is `<repo>/bin/garminbridge` (override with `$GB_CLI`).

## Run and build

```sh
cd app
npm install
npm run tauri dev     # development window
npm run tauri build   # unsigned .app and .dmg (macOS)
```

## Status

- macOS-only, matching the rest of the GarminBridge toolchain (the bundle targets are `app` and `dmg` on purpose).
- Builds are local and unsigned; packaging and signing are on hold.
- Bundling the engine into the app (so it is not host-bound) is the known blocker before any distribution.
