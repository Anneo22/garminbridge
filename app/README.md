# GarminBridge Content Manager

A macOS desktop app (Tauri) for browsing and managing what lives on your Garmin watch and in Garmin Connect, side by side: workouts, routes, and saved points. Route rows carry forecast wind (origin and degrees) and a tailwind-home verdict; hovering a route trace opens a preview with kilometre marks and the elevation profile. Delete flows spell out what is reversible and what is permanent before anything happens.

## How it works

The app is a thin native shell. Every data action shells out to a local Python engine (`prototype/connect/api.py` in the repo root) and renders its JSON. The engine holds the Garmin Connect session and the watch (MTP over USB) logic; the app itself contains no Garmin logic and no tokens.

`prototype/` is intentionally gitignored: it contains auth tokens and account-bound state. Without the engine present, the UI opens but every data action reports the engine as missing.

## Engine discovery

The shell looks for the engine at `$GB_ENGINE_DIR`, falling back to `~/Developer/garmin-voice-export/prototype/connect`. The interpreter is expected at `<dir>/.venv/bin/python`.

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
