# Garmin Voice Memos for macOS

Automatically copy the **voice notes** off your Garmin watch to your Mac — named by
when they were recorded — the moment you plug the watch in. Optionally transcribe
them locally or with your own cloud API key.

Garmin offers no supported way to get voice notes off the watch except USB, and even
that is famously flaky on a Mac. This tool makes it reliable and hands-off.

## Features

- **Instant on-connect** — a small IOKit watcher imports within seconds of plugging in (no polling).
- **Named by recording time** — e.g. `2026-06-17_12-25-40.wav`, sortable, no collisions.
- **Reliable transfer** — a `gphoto2`/MTP backend that reads only the VoiceNotes folder (the usual `libmtp` full-device scan hangs), and automatically handles Garmin Express and the macOS `PTPCamera` daemon fighting for the USB port.
- **Notifications** — a click-to-open alert when new memos arrive.
- **Keep or delete** — leave notes on the watch (default) or remove them after a verified local copy (`--delete`).
- **Optional transcription** — local (Parakeet / Whisper via Apple MLX) or bring-your-own cloud key (OpenAI, Gemini, Groq, Deepgram). Off by default.
- **Optional Obsidian output** — write each memo as a note in your vault.
- **Self-diagnosing** — a diagnostic that tells you whether a failure is the cable or the software.

## Why it works when Android File Transfer / OpenMTP don't

Those tools open the watch as a **full MTP volume** and enumerate everything — and a
modern Garmin holds thousands of map tiles. That full recursive scan is exactly what
hangs and crashes. This tool never does that:

- **Targeted reads, not a full mount.** It reads *one folder by path* with `gphoto2`
  and pulls only what changed — a handful of PTP commands, never a device-wide scan.
- **Clears contention first.** Garmin Express and the macOS `PTPCamera` daemon both
  grab the watch's single USB interface; the tool quietly steps them aside.
- **Never hard-kills a live session.** It lets MTP close cleanly, so it doesn't wedge
  the watch into the "not recognised" state.
- **Self-healing.** Transient failures are retried and recovered, not crashed on.

Targeted, contention-aware, self-healing access — not mount-and-pray. The same engine
generalizes to activities and anything else on the watch.

## Requirements

- macOS (Apple Silicon or Intel)
- [Homebrew](https://brew.sh)
- `gphoto2` (`brew install gphoto2`)
- Xcode Command Line Tools (for the on-connect watcher; `xcode-select --install`)

## Install

```sh
git clone https://github.com/Anneo22/garmin-voice-export.git
cd garmin-voice-export
./install.sh
```

Or with Homebrew: `brew install anneo22/garmin/garmin-voice-export`, then `garmin-voice-setup`.

`install.sh` installs dependencies (`gphoto2`, `terminal-notifier`), asks where to
save memos and whether to delete them from the watch, and sets up the on-connect
importer. It can also set up transcription and the menu-bar app.

That's it. Plug in your watch; new memos appear in your folder and you get a
clickable notification. To remove the agent: `bin/uninstall-autorun.sh`.

### Menu-bar app (optional)

```sh
bin/install-menubar.sh
```
Adds a menu-bar item that is the control center: live status, **Sync now**, **Open
folder**, **toggle delete-from-watch**, **change destination**, set up transcription,
and **Pause** (free the watch for other apps). No Terminal needed for daily use.

## Usage

Run a one-off import manually (no agent needed):

```sh
bin/export-voice-notes.sh            # import; leave notes on the watch
bin/export-voice-notes.sh --delete   # import, then remove from the watch
bin/export-voice-notes.sh --keep     # explicit: never delete (default)
```

Diagnose a connection that isn't working:

```sh
bin/garmin-diag.sh                   # reports whether it's the cable or the software
```

## Configuration

Set these as environment variables (e.g. in the install command, or a config file —
see `config.example`). Sensible defaults mean you usually need none.

| Variable | Default | Purpose |
|---|---|---|
| `GARMIN_VOICE_DEST` | `~/Documents/Voice Memos` | Where memos are saved |
| `GARMIN_VOICE_DELETE` | unset | Set to `--delete` to remove from the watch (agent) |
| `GARMIN_VOICE_SUBPATH` | `GARMIN/Audio/VoiceNotes` | On-watch folder (override if a model differs) |
| `GARMIN_VOICE_REGEX` | `VoiceNotes[0-9]+\.wav` | Which files count as voice notes (`[0-9]+` matches any number — 6, 12, 100) |
| `GVE_NAME_FORMAT` | `%Y-%m-%d_%H-%M-%S` | Filename format — a `date` format string (see Naming) |
| `GVE_TRANSCRIBE` | `0` | `1` to transcribe each new memo |
| `GVE_TRANSCRIBE_BACKEND` | `parakeet` | `parakeet` \| `whisper` \| `openai` \| `gemini` \| `groq` \| `deepgram` |
| `GVE_OBSIDIAN_VAULT` | unset | Path to a vault folder to also write each memo as a note |

> Writing to `~/Documents` requires Full Disk Access for the agent's interpreter
> (`/bin/bash`) under macOS privacy rules. Point `GARMIN_VOICE_DEST` at a home-root
> folder (e.g. `~/Voice Memos`) to avoid that.

## Transcription (optional)

Off by default. To enable, install a backend and turn it on:

```sh
bin/install-transcription.sh         # interactive: pick local (MLX) or a cloud key
```

- **Local, offline, free:** Parakeet (NVIDIA Parakeet TDT 0.6B v3 via Apple MLX) or
  Whisper (`mlx-whisper`). Best on Apple Silicon. The installer sets up an isolated
  Python environment; nothing else on your system is touched.
- **Cloud, bring-your-own-key:** OpenAI, Gemini, Groq (`whisper-large-v3-turbo`),
  or Deepgram. Your key is stored locally in the config file, never committed.

Each memo gets a `.txt` next to its `.wav`. With `GVE_OBSIDIAN_VAULT` set, it also
becomes a note (transcript + recording date + linked audio).

## Naming

Files are named from each note's recording time via `GVE_NAME_FORMAT`, a `date`
format string. Examples:

| `GVE_NAME_FORMAT` | Result |
|---|---|
| `%Y-%m-%d_%H-%M-%S` (default) | `2026-06-17_12-25-40.wav` |
| `%Y%m%d-%H%M` | `20260617-1225.wav` |
| `Memo %Y-%m-%d %H.%M` | `Memo 2026-06-17 12.25.wav` |
| `%Y/%m/%Y-%m-%d_%H-%M-%S` | `2026/06/2026-06-17_12-25-40.wav` (organised into year/month subfolders) |

## Deleting from the watch

Deletion is opt-in (`--delete`). One caveat to understand: removing a `.wav` over USB
frees the audio immediately, but the watch's voice-note **library index** isn't
accessible over USB, so the watch keeps showing a now-empty entry (it won't play)
**until the watch rebuilds its library, which it does on reboot.** In practice:
delete via this tool, and the entries disappear after the next watch restart. The
fully clean alternative is to delete notes from the watch's own UI. When `--delete`
is used, a note is removed from the watch only after a verified local copy exists.

## Pausing / freeing the watch for other apps

This tool grabs the watch's USB connection to import. To use Garmin Express, OpenMTP,
or any other MTP app, free the watch with `garmin-voice`:

```sh
bin/garmin-voice pause     # turn auto-import OFF and release the watch (survives reconnects)
bin/garmin-voice resume    # turn it back ON
bin/garmin-voice free      # one-shot: release the watch right now (e.g. "the Mac won't see it")
bin/garmin-voice status    # show current state
bin/garmin-voice sync      # run an import now (accepts --delete etc.)
```

`free` is handy for Garmin's notoriously finicky USB connection: it kills every
process holding the device (this tool, leftover `gphoto2`, the macOS `PTPCamera`
daemon) so the next app you open connects cleanly.

## How it works

1. A Swift/IOKit daemon (`src/garmin-usb-watcher.swift`) fires the moment a Garmin
   USB device attaches.
2. `gphoto2` opens the watch over MTP, reads only the VoiceNotes folder, and downloads
   new notes (deduped by name + size).
3. Each note is saved using its recording timestamp; transcription/Obsidian run if
   enabled; the note is optionally deleted from the watch after verification.

Garmin Express and the macOS `PTPCamera` daemon both try to hold the watch's single
USB interface; the importer quietly stops them for the duration of a sync.

## Compatibility

Developed and verified on a Fenix 8 (firmware 22.35). Storage paths and device IDs
are discovered at runtime, not hardcoded, so it should work on any Garmin watch with
the voice-note feature. Reports for other models are welcome via issues.

## License

MIT — see [LICENSE](LICENSE).
