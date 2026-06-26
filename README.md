# GarminBridge

A reliable bridge between your Garmin watch and your Mac. Plug in and it pulls your data
across automatically. **Voice notes** are the headline: copied the moment you connect,
named by when you recorded them, optionally transcribed. It also backs up your activity
`.fit` files, and the same engine is built to reach whatever else Garmin strands on the watch.

Garmin offers no supported, reliable way to get this onto a Mac. The USB connection is
famously flaky and the official tools crash. GarminBridge makes it just work, hands-off.

## Features

- **Instant on-connect**: a small IOKit watcher imports within seconds of plugging in (no polling).
- **Named by recording time**: e.g. `2026-06-17_12-25-40.wav`, sortable, no collisions.
- **Reliable transfer**: a `gphoto2`/MTP backend that reads only the VoiceNotes folder (the usual `libmtp` full-device scan hangs), and automatically handles Garmin Express and the macOS `PTPCamera` daemon fighting for the USB port.
- **Notifications**: a click-to-open alert when new memos arrive.
- **Keep or delete, your call**: leave notes on the watch (default), remove them after a verified copy, or only once they're transcribed. Optional local retention can drop the heavy `.wav` after N days while keeping the transcript.
- **Optional transcription**: local (Parakeet / Whisper via Apple MLX) or bring-your-own cloud key (OpenAI, Gemini, Groq, Deepgram). Off by default. An optional LLM pass tidies punctuation and removes filler ("um", "uh").
- **Activity backup**: optionally copy your `.fit` activity files to the Mac on the same connect (copy-only, never deletes; they also sync to Garmin Connect). Your raw training data, readable by any FIT tool.
- **Optional Obsidian output**: write each memo as a note in your vault.
- **Self-diagnosing**: a diagnostic that tells you whether a failure is the cable or the software.

## Why it works when Android File Transfer / OpenMTP don't

Those tools open the watch as a **full MTP volume** and enumerate everything, and a
modern Garmin holds thousands of map tiles. That full recursive scan is exactly what
hangs and crashes. This tool never does that:

- **Targeted reads, not a full mount.** It reads *one folder by path* with `gphoto2`
  and pulls only what changed, a handful of PTP commands, never a device-wide scan.
- **Clears contention first.** Garmin Express and the macOS `PTPCamera` daemon both
  grab the watch's single USB interface; the tool quietly steps them aside.
- **Never hard-kills a live session.** It lets MTP close cleanly, so it doesn't wedge
  the watch into the "not recognised" state.
- **Self-healing.** Transient failures are retried and recovered, not crashed on.

Targeted, contention-aware, self-healing access, not mount-and-pray. The same engine
already backs up your activities, and reaches for whatever else Garmin strands on the watch.

## Requirements

- macOS (Apple Silicon or Intel)
- [Homebrew](https://brew.sh)
- `gphoto2` (`brew install gphoto2`)
- Xcode Command Line Tools (for the on-connect watcher; `xcode-select --install`)

## Install

```sh
git clone https://github.com/Anneo22/garminbridge.git
cd garminbridge
./install.sh
```

Or with Homebrew: `brew install anneo22/garmin/garminbridge`, then `garminbridge-setup`.

`install.sh` installs dependencies (`gphoto2`, `terminal-notifier`), asks where to
save memos and whether to delete them from the watch, and sets up the on-connect
importer. It can also set up transcription and the menu-bar app.

That's it. Plug in your watch; new memos appear in your folder and you get a
clickable notification. To remove the agent: `bin/uninstall-autorun.sh`.

### Menu-bar app (optional)

```sh
bin/install-menubar.sh
```
Adds a menu-bar item that is the control center: live status, **import voice notes**,
**back up activities**, **open the folder**, choose when to **remove notes from the watch**,
set **local audio** retention, **change the folder**, and **Pause** to free the watch for
other apps. Transcription is fully controllable here too: turn it on with an on-device model
or a cloud key (entered in a native dialog), and toggle transcript cleanup, with no Terminal.

## Usage

Run a one-off import manually (no agent needed):

```sh
bin/export-voice-notes.sh                          # import; leave notes on the watch
bin/export-voice-notes.sh --delete                 # import, then remove from the watch
bin/export-voice-notes.sh --delete-after-transcript # remove from the watch only once transcribed
bin/export-voice-notes.sh --keep                   # explicit: never delete (default)
```

Diagnose a connection that isn't working:

```sh
bin/garmin-diag.sh                   # reports whether it's the cable or the software
```

## Configuration

Set these as environment variables (e.g. in the install command, or a config file,
see `config.example`). Sensible defaults mean you usually need none.

| Variable | Default | Purpose |
|---|---|---|
| `GARMIN_BRIDGE_ROOT` | unset | One root folder for everything: voice notes go to `<root>/Voice Memo`, activity backups to `<root>/Backups`. Recommended; set it from the menu bar or `garminbridge root PATH` |
| `GARMIN_BRIDGE_DEVICE` | unset | Optional sub-level under the root for several watches / an Edge / users (e.g. `Fenix 8`) |
| `GARMIN_VOICE_DEST` | `~/Documents/Voice Memos` | Where memos are saved (overrides the root layout if set) |
| `GARMIN_VOICE_DELETE` | `keep` | Delete from the watch: `keep` \| `now` (after a verified copy) \| `transcribed` (after a transcript too) |
| `GVE_AUDIO_RETENTION_DAYS` | unset | Drop the local `.wav` this many days after recording, keeping the `.txt` (`0` = as soon as transcribed). Never deletes un-transcribed audio while transcription is on |
| `GARMIN_VOICE_SUBPATH` | `GARMIN/Audio/VoiceNotes` | On-watch folder (override if a model differs) |
| `GARMIN_VOICE_REGEX` | `VoiceNotes[0-9]+\.wav` | Which files count as voice notes (`[0-9]+` matches any number, 6, 12, 100) |
| `GVE_NAME_FORMAT` | `%Y-%m-%d_%H-%M-%S` | Filename format, a `date` format string (see Naming) |
| `GVE_TRANSCRIBE` | `0` | `1` to transcribe each new memo |
| `GVE_TRANSCRIBE_BACKEND` | `parakeet` | `parakeet` \| `whisper` \| `openai` \| `gemini` \| `groq` \| `deepgram` |
| `GVE_TRANSCRIPT_CLEANUP` | `0` | `1` to clean each transcript with an LLM (punctuation, remove filler) |
| `GVE_CLEANUP_BACKEND` | `openai` | Cleanup provider: `openai` \| `groq` \| `anthropic` \| `gemini` |
| `GVE_OBSIDIAN_VAULT` | unset | Path to a vault folder to also write each memo as a note |
| `GARMIN_ACTIVITY_BACKUP` | `0` | `1` to also copy activity `.fit` files on connect (copy-only) |
| `GARMIN_ACTIVITY_DEST` | `~/Documents/Garmin Activities` | Where activity files are saved |
| `GARMIN_ACTIVITY_MAX` | `0` | Max activity files to fetch per run (`0` = all) |
| `GARMIN_SETTINGS_BACKUP` | `0` | `1` to also back up settings, sport profiles, routes, and more on connect (copy-only) |
| `GARMIN_SETTINGS_DEST` | `<root>/Settings` | Where the settings backup goes (else `~/Documents/Garmin Settings`) |
| `GARMIN_BACKUP_HEALTH` | `0` | `1` to also mirror heavy health telemetry (Monitor/Metrics/Sleep) |

> Writing to `~/Documents` requires Full Disk Access for the agent's interpreter
> (`/bin/bash`) under macOS privacy rules. Point `GARMIN_VOICE_DEST` at a home-root
> folder (e.g. `~/Voice Memos`) to avoid that.

## Output folder

By default voice notes land in `~/Documents/Voice Memos` and activity backups in
`~/Documents/Garmin Activities`. For one tidy home instead, set an **output root**
(from the menu bar's "Change output folder…", or the CLI):

```
garminbridge root "$HOME/Documents/Garmin Bridge"   # pick the location
garminbridge migrate                                # move existing files in (never overwrites)
```

That gives you:

```
Garmin Bridge/
├── Voice Memo/        new notes  (Archive/ holds ones already transcribed/handled)
└── Backups/           activity .fit files
```

Bridging more than one device or person? Add a sub-level with
`garminbridge device "Fenix 8"` (a watch + an Edge, several watches, several users) →
`Garmin Bridge/Fenix 8/Voice Memo`, and so on.

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

### Transcript cleanup (optional)

Raw speech-to-text keeps every "um" and has rough punctuation. Turn on `GVE_TRANSCRIPT_CLEANUP=1`
(with `GVE_CLEANUP_BACKEND` = `openai` \| `groq` \| `anthropic` \| `gemini` and the matching key)
to run each transcript through an LLM that fixes punctuation, paragraphs, and filler, and is
told not to change meaning, summarise, or translate. The audio stays the source of truth; if a
cleanup call fails the raw transcript is kept. Set `GVE_TRANSCRIPT_KEEP_RAW=1` to keep both.

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

Deletion is opt-in. There are two independent things you can clean up: the **watch**
and your **Mac**.

**On the watch** (`GARMIN_VOICE_DELETE`): `keep` (default), `now` (remove after a
verified local copy), or `transcribed` (remove only once a transcript also exists, a
safety gate so the source audio leaves the watch only when you have both a copy and the
text). A note is always removed from the watch only after a verified local copy exists.

**On your Mac** (`GVE_AUDIO_RETENTION_DAYS`): voice memos pile up and the audio is the
heavy part. Set this to drop the local `.wav` N days after it was recorded while keeping
the `.txt` transcript (`0` = as soon as it's transcribed). While transcription is on it
never deletes audio that hasn't been transcribed yet, so you don't lose anything silently.

One caveat about the watch: removing a `.wav` over USB frees the audio immediately, but
the watch's voice-note **library index** isn't accessible over USB, so the watch keeps
showing a now-empty entry (it won't play) **until it rebuilds its library on reboot.**
The fully clean alternative is to delete notes from the watch's own UI.

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

## Activity backup

Turn on `GARMIN_ACTIVITY_BACKUP=1` (in `install.sh`, the menu bar, or the config) to also
copy your activity `.fit` files to the Mac whenever you plug in. It runs right after the
voice import on the same connect, sharing one USB session so the two never collide.

It is **copy-only and never deletes** anything: your activities still sync to Garmin Connect
and the watch manages its own storage. This is just a local archive of the raw `.fit` files,
readable by any FIT tool, for your own analysis or another platform.

```sh
bin/garmin-voice activities          # back up new activities now
```

Files keep their original timestamped names (e.g. `2026-06-17-07-23-16.fit`), are deduped by
recording time + size, and a big first backup is resumable, it picks up where it left off if
the connection drops. Bound it per run with `GARMIN_ACTIVITY_MAX=N`. Default destination:
`~/Documents/Garmin Activities`.

## Settings & sport-profile backup

Turn on `GARMIN_SETTINGS_BACKUP=1` to also copy your **settings and sport profiles** to the
Mac on connect, so a new or upgraded watch can be set back up the way you had it. Also
copy-only, never deletes. Run it now with `garminbridge settings`.

It mirrors (into `<root>/Settings/`): the per-sport profiles (`Sports/` — the data-screen
"faces"), device settings (`Device/`), the watch's own restorable backup blobs
(`Device Backup/`, incl. `settings_backup.bak`), Connect IQ app + watch-face settings and
data (`Connect IQ/`, `Connect IQ Data/`), **routes** (`Courses/`), workouts, saved locations,
gear, segments, pace bands, power guides, records, goals, and schedule.

Each folder is fingerprinted, so unchanged folders are skipped on later connects. Heavy health
telemetry is excluded by default (it syncs to Garmin Connect); opt in with
`GARMIN_BACKUP_HEALTH=1`. (Restoring these back onto a watch is a separate, careful step —
this is the backup half.)

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

MIT, see [LICENSE](LICENSE).
