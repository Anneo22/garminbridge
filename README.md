# GarminBridge

GarminBridge is the Mac-side power tool for a Garmin watch that Garmin never built.

Garmin Connect and Garmin Express handle sync. They do not give you local file control,
desktop editing, content curation, versioning, or a clean way to decide exactly what stays
on the watch. GarminBridge fills that gap: it talks to the watch over USB and manages voice
notes, routes, workouts, saved places, activities, and device backups from the Mac.

The sharpest thing it fixes is route and content management, so start there.

## The problem: Garmin's sync is one-way, and the watch never forgets

Garmin's model is push-only. You add a course or a workout in Connect, it syncs to the watch,
and from then on the two drift apart. There is no honest view of what actually lives where.

Deleting in Connect does not remove the copy already on the watch. Editing a route in Connect
can push a fresh copy while the old one stays behind, so you end up with two versions of the
same ride on the device and no way to tell which is current. Deleting on the watch can lose
the only copy you had. Over months the on-watch course list turns into an endless scroll of
stale routes, old versions, and rides you did once and never want again. The Fenix sorts that
list by proximity only: no name sort, no manual order, no folders. There are no tags, no
notes, no way to mark a course as good for a TT, a gravel day, or bad weather. Finding the
route you want mid-ride is a scroll and a prayer.

None of this is a bug. It is just what Garmin built: the watch is a terminal for the cloud,
not a library you own. GarminBridge treats it as a library you own.

It is built and verified around a Fenix 8. The lower-level paths are discovered at runtime
where possible, but other Garmin models are only as real as the testing behind them.

## What GarminBridge does that nobody else does

**It gives routes and content a real home, on the Mac.** This is the headline. GarminBridge
shows where every item lives: in Garmin Connect, on the watch, or in your Mac route library.
Once you can see that, you can act on it.

You curate what stays on the watch instead of hoarding everything Garmin ever synced. Pick the
routes you want and clear the rest. Choose a start place and trim the watch down to the courses
that begin near there, so the on-watch list is short and relevant to where you actually ride.
Deletes work on both sides: remove a route from the watch, from Connect, or from both, and
GarminBridge clears the stale and stranded copies that Garmin's one-way sync leaves behind.
Your full library stays safe on the Mac and in Connect while the watch carries only what you
need on the ride in front of you.

The pieces are already in the app: placement badges that tell you where each item sits, safe
previews before any delete, exact watch-file targeting, start-place and near-me filtering,
sorting by name, distance, recently added, or nearest start, and bulk removal from the watch.
The curation flow is still being made more intuitive, but it already does the thing Garmin
never let you do: decide, deliberately, what lives on your watch.

**It drains Garmin voice memos reliably.** Plug in the Fenix and GarminBridge pulls new voice
notes over USB, names them by recording time, transcribes them if transcription is enabled,
and files them into an organised local folder. It handles a multi-note drain instead of
forcing you to babysit one file at a time. In the desktop app the memos land in a clean list
where you can play, transcribe, send to notes, rename, archive, or delete them, one at a time
or in bulk with select-all, and a progress bar shows what is happening while a memo transcribes.

This matters because Android File Transfer and OpenMTP usually try to mount or scan the
whole watch. On a modern Garmin, that means thousands of files and a high chance of hangs
or crashes. GarminBridge reads the voice-note folder directly and gets the notes out.

**It builds custom workouts that actually animate on the watch.** You can create structured
running, cycling, swimming, and strength workouts from the Mac, then push them through the
same local engine the desktop app uses. Strength workouts are the unusual part: GarminBridge
can build custom strength sessions whose exercises show the on-watch animations. Custom
non-Garmin strength workouts normally do not do that.

You can describe a workout in plain English, attach a photo of a plan, or build it step by
step. Before anything is pushed, the engine validates the workout and shows a readable
preview.

**It backs up the watch, not just activities.** GarminBridge can copy the watch's local
folders into a Mac mirror: activities, sport profiles, data screens, device settings,
the watch's own backup blobs, Connect IQ settings and data, routes, workouts, saved
locations, gear, segments, pace bands, power guides, records, goals, schedule files,
custom maps, and more. Heavy health telemetry can be opted in separately.

Garmin gives you fragments of this through cloud sync. It does not give you a plain local
mirror you can inspect, keep, diff, or use when setting up a replacement watch.

**It gets routes in and lets you see them before they hit the watch.** GarminBridge can import
GPX or FIT routes, build Garmin-readable course files, save them to the Mac route library,
optionally add them to Garmin Connect, and copy them to `GARMIN/Courses` on the watch over USB.
Before you commit a route to the watch, the app shows the trace, distance, ascent, descent, an
elevation preview, kilometre markers, outbound direction, start coordinates, and optional
weather and wind for a chosen ride time. That is the desktop route workflow Garmin never built.

**It edits saved places from the Mac.** Saved points live in a Garmin FIT file on the watch,
not in Garmin Connect. GarminBridge reads the Mac backup, lets you rename or delete saved
places, then writes the verified change back to the watch when it is connected.

## What is included today

- Automatic voice memo import on USB connect, plus manual import on demand.
- Optional voice transcription with local or cloud backends, plus optional transcript cleanup.
- Optional Obsidian note output for imported voice memos.
- A voice memo panel with grouped per-memo actions, bulk select-all, clear local-backend install status, and progress feedback while the app loads and while a memo transcribes.
- Activity `.fit` backup, copy-only.
- Settings, sport-profile, route, workout, saved-location, and device-content backup, copy-only.
- A Tauri macOS Content Manager for workouts, routes, and saved places.
- Connect/watch placement matrix for workouts and routes.
- Add-to-watch, remove-from-watch, delete-from-Connect, rename, and stale-route cleanup flows.
- GPX/FIT route import into the Mac route library, Garmin Connect, and the watch.
- Route previews, start-place filters, nearest-route sorting, and trim-the-watch curation.
- Workout authoring from text, image, or a manual builder, with validation before push.
- A menu-bar control for voice import, backups, output folder, transcription, and freeing the
  watch for other apps.

## Install and use

Requirements:

- macOS
- [Homebrew](https://brew.sh)
- `gphoto2`, installed by the setup script if missing
- Xcode Command Line Tools for the instant-on-connect watcher

Install the command-line bridge and the on-connect importer:

```sh
git clone https://github.com/Anneo22/garminbridge.git
cd garminbridge
./install.sh
```

`install.sh` installs the USB dependencies, asks where voice memos should go, asks whether
to enable transcription, asks how deletion from the watch should work, and can install the
menu-bar app.

Common commands from the repo:

```sh
bin/garminbridge status
bin/garminbridge voice --keep
bin/garminbridge voice --delete
bin/garminbridge voice --delete-after-transcript
bin/garminbridge activities
bin/garminbridge settings
bin/garminbridge pause
bin/garminbridge resume
bin/garminbridge free
bin/garminbridge root "$HOME/Documents/Garmin Bridge"
bin/garminbridge migrate
```

Use `pause` or `free` when Garmin Express, OpenMTP, or another MTP app needs the watch's USB
connection.

Install the desktop Content Manager locally:

```sh
cd app
./install-local.sh
```

For development:

```sh
cd app
npm install
npm run tauri dev
```

The desktop app is a local, unsigned macOS app. Its data actions go through the Python engine
in `prototype/connect/`, which is intentionally account-bound and holds the local Garmin
Connect session. Without that engine and session, the UI can open but content actions will not
have Garmin data to work with. See [app/README.md](app/README.md) for the app-specific notes.

Configuration lives in `~/.config/garmin-voice-export/config`. The repo includes
[config.example](config.example) for the supported environment variables.

## Output layout

The modern layout uses one root folder:

```text
Garmin Bridge/
|-- Voice Memo/
|   `-- Archive/
`-- Backups/
    `-- Settings/
```

Set it with:

```sh
bin/garminbridge root "$HOME/Documents/Garmin Bridge"
bin/garminbridge migrate
```

Multiple devices or users can use a subfolder with `bin/garminbridge device "Fenix 8"`.

## Deletion rules

GarminBridge is conservative by default.

- Voice notes stay on the watch unless you choose `--delete` or `--delete-after-transcript`.
- Activity, settings, and profile backups are copy-only.
- Watch-content deletes are previewed before they apply.
- Garmin Connect deletes are explicit and treated as permanent where Garmin has no trash.
- Removing a route from the watch does not remove the copy in Garmin Connect or the Mac route
  library when one exists.

One Garmin caveat: deleting a voice memo `.wav` over USB removes the audio, but the watch can
keep a dead library entry until it rebuilds the voice-note index after reboot. The cleanest
watch-side deletion is still the watch's own UI.

## Roadmap, not built yet

These are planned directions, not shipped features:

- Route tags and notes, for example marking a route as good for TT, social riding, gravel,
  bad weather, or a specific training block.
- Versioning across profiles, settings, routes, and workouts, so changes can be compared and
  rolled back instead of guessed from memory.
- A data-field and data-screen editor for sport profiles.
- A sharing marketplace for routes, workouts, profiles, data screens, and other watch setups.

## License

MIT, see [LICENSE](LICENSE).
