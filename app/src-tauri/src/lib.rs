// GarminBridge Content Manager: Rust bridge + always-on tray shell.
// The native shell stays thin: content actions shell out to the gitignored Python engine
// (api.py) and quick actions shell the `garminbridge` CLI; no Garmin logic, no tokens, no
// deletes live here (every guarded action runs inside content.py / the CLI scripts).
// The tray, close-to-tray keep-alive, and launch-at-login make it the one always-on app
// (convergence track C1 — it absorbs the retired Swift menu bar's quick actions).

use std::path::PathBuf;
use std::process::{Command, Stdio};

use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, RunEvent, WindowEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};

/// `<App>.app/Contents/Resources`, when running from an installed bundle.
fn bundled_resources() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let res = exe.parent()?.parent()?.join("Resources");
    res.is_dir().then_some(res)
}

/// The checkout holding bin/garminbridge. Override with GB_REPO_DIR.
///
/// Found by searching upward from the executable rather than hardcoded to one
/// person's home directory, which is what made every build unusable on any
/// other machine. The old constant remains only as a last resort.
fn repo_root() -> PathBuf {
    if let Ok(d) = std::env::var("GB_REPO_DIR") {
        return PathBuf::from(d);
    }
    if let Ok(exe) = std::env::current_exe() {
        let mut cur = exe.as_path();
        while let Some(parent) = cur.parent() {
            if parent.join("bin/garminbridge").exists() {
                return parent.to_path_buf();
            }
            cur = parent;
        }
    }
    let home = std::env::var("HOME").unwrap_or_default();
    PathBuf::from(home).join("Second Brain/2_Code/garmin-voice-export")
}

/// Where the Python engine lives. Override with GB_ENGINE_DIR.
///
/// Prefers the copy inside the app bundle, so an installed app is self-contained
/// and does not depend on a checkout existing at all. Falls back to the tracked
/// engine/ in a development checkout, then to the pre-2026-07-21 prototype
/// location so an older working tree keeps running.
fn engine_dir() -> PathBuf {
    if let Ok(d) = std::env::var("GB_ENGINE_DIR") {
        return PathBuf::from(d);
    }
    if let Some(bundled) = bundled_resources().map(|r| r.join("engine")) {
        if bundled.is_dir() {
            return bundled;
        }
    }
    let root = repo_root();
    let shipped = root.join("engine");
    if shipped.is_dir() {
        return shipped;
    }
    root.join("prototype/connect")
}

/// The relocatable interpreter shipped with the app (or fetched into a checkout).
fn runtime_python() -> Option<PathBuf> {
    if let Some(bundled) = bundled_resources().map(|r| r.join("python/bin/python3")) {
        if bundled.is_file() {
            return Some(bundled);
        }
    }
    let checkout = repo_root().join("vendor/python/bin/python3");
    checkout.is_file().then_some(checkout)
}

/// Dependencies installed at build time beside the bundled interpreter.
fn site_packages_dir() -> Option<PathBuf> {
    if let Some(bundled) = bundled_resources().map(|r| r.join("site-packages")) {
        if bundled.is_dir() {
            return Some(bundled);
        }
    }
    let checkout = repo_root().join("vendor/site-packages");
    checkout.is_dir().then_some(checkout)
}

/// The relocated gphoto2 runtime shipped with the app (or fetched into a checkout).
fn gphoto2_runtime_dir() -> Option<PathBuf> {
    if let Some(bundled) = bundled_resources().map(|r| r.join("gphoto2")) {
        if bundled.join("bin/gphoto2").is_file() {
            return Some(bundled);
        }
    }
    let checkout = repo_root().join("vendor/gphoto2");
    checkout.join("bin/gphoto2").is_file().then_some(checkout)
}

/// The interpreter that runs the engine. Override with GB_PYTHON.
///
/// A venv beside the engine wins in a development checkout. Installed builds use
/// the interpreter and site-packages that were both prepared before bundling.
fn engine_python() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("GB_PYTHON") {
        if !p.is_empty() {
            return Some(PathBuf::from(p));
        }
    }
    let checkout = engine_dir().join(".venv/bin/python");
    if checkout.is_file() {
        return Some(checkout);
    }
    site_packages_dir()?;
    runtime_python()
}

/// The `garminbridge` control CLI (bin/garminbridge). Override with GB_CLI.
fn cli_path() -> PathBuf {
    if let Ok(d) = std::env::var("GB_CLI") {
        return PathBuf::from(d);
    }
    repo_root().join("bin/garminbridge")
}

/// A PATH where the bundled gphoto2 wins. Homebrew remains a development fallback.
fn cli_path_env() -> String {
    let inherited = std::env::var("PATH").unwrap_or_default();
    let mut entries = Vec::new();
    if let Some(runtime) = gphoto2_runtime_dir() {
        entries.push(runtime.join("bin").to_string_lossy().into_owned());
    }
    entries.push("/opt/homebrew/bin".to_string());
    entries.push("/usr/local/bin".to_string());
    if !inherited.is_empty() {
        entries.push(inherited);
    }
    entries.join(":")
}

/// Dependencies for the bundled interpreter. Explicit and checkout interpreters
/// keep their own environments instead of being polluted with CPython 3.14 wheels.
fn bundled_site_packages_for_subprocess() -> Option<PathBuf> {
    if std::env::var("GB_PYTHON").is_ok_and(|p| !p.is_empty()) {
        return None;
    }
    if engine_dir().join(".venv/bin/python").is_file() {
        return None;
    }
    site_packages_dir()
}

/// Create an engine/CLI subprocess with one inherited native runtime environment.
fn cli_command(program: impl AsRef<std::ffi::OsStr>) -> Command {
    let mut command = Command::new(program);
    command.env("PATH", cli_path_env());
    if let Some(site_packages) = bundled_site_packages_for_subprocess() {
        command.env("PYTHONPATH", site_packages);
    }
    if let Some(runtime) = gphoto2_runtime_dir() {
        command
            .env("CAMLIBS", runtime.join("camlibs"))
            .env("IOLIBS", runtime.join("iolibs"));
    }
    command
}

/// The engine interpreter, or the sentence to show the person when there isn't one.
fn engine_python_or_err() -> Result<PathBuf, String> {
    engine_python().ok_or_else(|| {
        "Garmin Bridge could not find its Python runtime and dependencies. Reinstall Garmin Bridge, or set GB_PYTHON to a prepared interpreter.".to_string()
    })
}

/// api.py prints a JSON error object even on handled failures, so stdout is the answer
/// whenever there is one; stderr only speaks when the process died with nothing to say.
fn engine_output(out: std::process::Output) -> Result<String, String> {
    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    if !stdout.trim().is_empty() {
        return Ok(stdout);
    }
    Err(format!(
        "engine produced no output (exit {}): {}",
        out.status,
        String::from_utf8_lossy(&out.stderr)
    ))
}

/// Run `api.py <args...>` in the engine dir and return stdout (always JSON).
#[tauri::command]
fn api(args: Vec<String>) -> Result<String, String> {
    let dir = engine_dir();
    let py = engine_python_or_err()?;
    // api.py shells out to gphoto2 / killall / watch_mtp.sh for every watch (USB) operation.
    // A GUI launch inherits a bare PATH, so give every engine process the bundled runtime.
    let out = cli_command(&py)
        .arg("api.py")
        .args(&args)
        .current_dir(&dir)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .output()
        .map_err(|e| format!("failed to launch engine: {e}"))?;
    engine_output(out)
}

/// Run an engine command that needs a secret, handing it over on stdin.
///
/// The secret must never appear in `args`: a command line is readable by any process on
/// the machine (`ps`). The write is followed by closing the pipe, so a command that asks
/// for nothing more sees EOF instead of waiting forever.
#[tauri::command]
fn api_with_secret(args: Vec<String>, secret: String) -> Result<String, String> {
    use std::io::Write;

    let dir = engine_dir();
    let py = engine_python_or_err()?;
    let mut child = cli_command(&py)
        .arg("api.py")
        .args(&args)
        .current_dir(&dir)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("failed to launch engine: {e}"))?;

    {
        let stdin = child
            .stdin
            .as_mut()
            .ok_or_else(|| "engine stdin unavailable".to_string())?;
        writeln!(stdin, "{secret}").map_err(|e| format!("failed to send the key: {e}"))?;
    }
    drop(child.stdin.take()); // EOF, so the engine stops reading and runs

    let out = child
        .wait_with_output()
        .map_err(|e| format!("engine did not finish: {e}"))?;
    engine_output(out)
}

/// A Garmin sign-in that is waiting for its verification code.
///
/// The sign-in must happen inside ONE engine process: Garmin's client hands back
/// state to resume an MFA login that holds a live HTTP connection, so it cannot
/// be serialised and picked up by a second invocation. The child therefore stays
/// alive here, parked, while the user reads their code.
#[derive(Default)]
struct PendingSignIn(std::sync::Mutex<Option<std::process::Child>>);

/// Read one line of JSON from the child's stdout.
fn read_json_line(child: &mut std::process::Child) -> Result<String, String> {
    use std::io::{BufRead, BufReader};
    let stdout = child
        .stdout
        .as_mut()
        .ok_or_else(|| "engine stdout unavailable".to_string())?;
    let mut line = String::new();
    BufReader::new(stdout)
        .read_line(&mut line)
        .map_err(|e| format!("engine did not answer: {e}"))?;
    if line.trim().is_empty() {
        return Err("engine produced no output".to_string());
    }
    Ok(line)
}

/// Send the password and either finish, or park the process awaiting a code.
///
/// Secrets travel on stdin, never as arguments: a command line is readable by
/// any process on the machine (`ps`). Nothing is logged or stored here; only the
/// tokens Garmin returns are persisted, by the engine, in the state directory.
#[tauri::command]
fn garmin_sign_in(
    email: String,
    password: String,
    pending: tauri::State<'_, PendingSignIn>,
) -> Result<String, String> {
    use std::io::Write;

    let dir = engine_dir();
    let py = engine_python_or_err()?;

    // Abandon any sign-in already parked: starting over must not leak a process.
    if let Ok(mut slot) = pending.0.lock() {
        if let Some(mut old) = slot.take() {
            let _ = old.kill();
        }
    }

    let mut child = cli_command(&py)
        .arg("api.py")
        .arg("garmin-login")
        .arg("--email")
        .arg(&email)
        .current_dir(&dir)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("failed to launch engine: {e}"))?;

    {
        let stdin = child
            .stdin
            .as_mut()
            .ok_or_else(|| "engine stdin unavailable".to_string())?;
        writeln!(stdin, "{password}").map_err(|e| format!("failed to send credentials: {e}"))?;
        stdin.flush().ok();
    } // the handle is NOT dropped-with-the-process: keep the pipe for the code

    let line = read_json_line(&mut child)?;
    if line.contains("needs_code") {
        if let Ok(mut slot) = pending.0.lock() {
            *slot = Some(child);
        }
        return Ok(r#"{"ok":true,"needs_mfa":true}"#.to_string());
    }

    let _ = child.wait();
    Ok(line)
}

/// Hand the verification code to the sign-in parked by `garmin_sign_in`.
#[tauri::command]
fn garmin_sign_in_code(
    code: String,
    pending: tauri::State<'_, PendingSignIn>,
) -> Result<String, String> {
    use std::io::Write;

    let mut child = pending
        .0
        .lock()
        .map_err(|_| "sign-in state unavailable".to_string())?
        .take()
        .ok_or_else(|| {
            r#"{"ok":false,"message":"That sign-in is no longer open. Enter your email and password again."}"#
                .to_string()
        })?;

    {
        let stdin = child
            .stdin
            .as_mut()
            .ok_or_else(|| "engine stdin unavailable".to_string())?;
        writeln!(stdin, "{code}").map_err(|e| format!("failed to send the code: {e}"))?;
        stdin.flush().ok();
    }

    let line = read_json_line(&mut child);
    let _ = child.wait();
    line
}

/// Decode standard base64 (padding + newlines tolerated). Dependency-free so we don't pull a
/// crate in just to persist a picked image.
fn b64_decode(s: &str) -> Result<Vec<u8>, String> {
    fn val(c: u8) -> Option<u32> {
        match c {
            b'A'..=b'Z' => Some((c - b'A') as u32),
            b'a'..=b'z' => Some((c - b'a' + 26) as u32),
            b'0'..=b'9' => Some((c - b'0' + 52) as u32),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }
    }
    let (mut out, mut buf, mut bits) = (Vec::new(), 0u32, 0u32);
    for &c in s.as_bytes() {
        if matches!(c, b'=' | b'\n' | b'\r' | b' ') {
            continue;
        }
        buf = (buf << 6) | val(c).ok_or_else(|| format!("bad base64 char {:?}", c as char))?;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((buf >> bits) as u8);
        }
    }
    Ok(out)
}

/// Persist a picked image (base64, no data: prefix) to a temp file and return its path, so the
/// Python engine's `workout-llm` can read it by path. One fixed filename per extension, overwritten
/// on each pick — an authoring image is transient.
#[tauri::command]
fn save_temp_image(b64: String, ext: String) -> Result<String, String> {
    use std::io::Write;
    let bytes = b64_decode(b64.trim())?;
    let safe: String = ext.chars().filter(|c| c.is_ascii_alphanumeric()).take(5).collect();
    let safe = if safe.is_empty() { "png".into() } else { safe.to_lowercase() };
    let mut path = std::env::temp_dir();
    path.push(format!("garminbridge-author-image.{safe}"));
    std::fs::File::create(&path)
        .and_then(|mut f| f.write_all(&bytes))
        .map_err(|e| e.to_string())?;
    Ok(path.to_string_lossy().into_owned())
}

/// Persist a picked route file (base64, no data: prefix) to a temp file and return its path, so
/// the engine's `import-route` can read the GPX/FIT by path. One fixed filename per extension,
/// overwritten each pick — an import file is transient. Same pattern as save_temp_image, kept
/// separate so a picked image and a picked route never clobber each other.
#[tauri::command]
fn save_temp_file(b64: String, ext: String) -> Result<String, String> {
    use std::io::Write;
    let bytes = b64_decode(b64.trim())?;
    let safe: String = ext.chars().filter(|c| c.is_ascii_alphanumeric()).take(5).collect();
    let safe = if safe.is_empty() { "dat".into() } else { safe.to_lowercase() };
    let mut path = std::env::temp_dir();
    path.push(format!("garminbridge-import.{safe}"));
    std::fs::File::create(&path)
        .and_then(|mut f| f.write_all(&bytes))
        .map_err(|e| e.to_string())?;
    Ok(path.to_string_lossy().into_owned())
}

/// Fire a `garminbridge <subcommand>` detached. These touch the watch (freeing the USB
/// device first) and emit their own desktop notification on completion, so we don't block the
/// tray or capture output — fire and forget.
fn run_cli(args: &[&str]) {
    let _ = cli_command(cli_path())
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn();
}

/// Run a named quick action (exposed so the in-app UI can reuse it in C2).
#[tauri::command]
fn run_action(name: String) -> Result<(), String> {
    match name.as_str() {
        "import" => run_cli(&["voice"]),
        "backup-activities" => run_cli(&["activities"]),
        "backup-settings" => run_cli(&["settings"]),
        other => return Err(format!("unknown action: {other}")),
    }
    Ok(())
}

/// The configured output root ("Garmin Bridge" folder). Asks the CLI (`garminbridge root`);
/// falls back to the known default, then ~/Documents, so "Open folder" always opens something.
fn output_folder() -> PathBuf {
    if let Ok(out) = cli_command(cli_path())
        .arg("root")
        .output()
    {
        let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
        let p = PathBuf::from(&s);
        if p.is_dir() {
            return p;
        }
    }
    let home = std::env::var("HOME").unwrap_or_default();
    let default = PathBuf::from(&home).join("Documents/Garmin Bridge");
    if default.is_dir() {
        default
    } else {
        PathBuf::from(home).join("Documents")
    }
}

/// Reveal the output folder in Finder.
#[tauri::command]
fn open_output_folder() -> Result<(), String> {
    Command::new("open")
        .arg(output_folder())
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// Open a local file in its default app, or reveal it in Finder. Used by the voice memo
/// panel for reliable playback/reveal without threading local file URLs through the webview CSP.
#[tauri::command]
fn open_path(path: String, reveal: bool) -> Result<(), String> {
    let p = PathBuf::from(path);
    if !p.exists() {
        return Err(format!("file not found: {}", p.display()));
    }
    let mut cmd = Command::new("open");
    if reveal {
        cmd.arg("-R");
    }
    cmd.arg(p)
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// Play a local audio file in a known macOS audio player. Plain `open <wav>` depends on the
/// user's file associations; QuickTime is the reliable default on macOS.
#[tauri::command]
fn play_audio(path: String) -> Result<(), String> {
    let p = PathBuf::from(path);
    if !p.exists() {
        return Err(format!("file not found: {}", p.display()));
    }
    let script = r#"
on run argv
  set audioPath to POSIX file (item 1 of argv)
  tell application "QuickTime Player"
    set docRef to open audioPath
    activate
    play docRef
  end tell
end run
"#;
    let out = Command::new("osascript")
        .arg("-e")
        .arg(script)
        .arg(&p)
        .output()
        .map_err(|_| "Could not start playback in QuickTime Player.".to_string())?;
    if out.status.success() {
        Ok(())
    } else {
        Err("Could not start playback in QuickTime Player.".to_string())
    }
}

/// Choose the folder where voice memo notes are written and persist it through the existing CLI
/// config setter (`garminbridge set GVE_OBSIDIAN_VAULT PATH`).
#[tauri::command]
fn set_notes_folder() -> Result<String, String> {
    let pick = r#"try
    POSIX path of (choose folder with prompt "Choose the folder for voice memo notes")
on error number -128
    ""
end try"#;
    let out = Command::new("osascript")
        .arg("-e").arg(pick)
        .output()
        .map_err(|e| e.to_string())?;
    let chosen = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if chosen.is_empty() {
        return Ok(String::new());
    }
    let r = cli_command(cli_path())
        .arg("set").arg("GVE_OBSIDIAN_VAULT").arg(&chosen)
        .output()
        .map_err(|e| e.to_string())?;
    if !r.status.success() {
        return Err(format!("could not set the notes folder: {}", String::from_utf8_lossy(&r.stderr)));
    }
    Ok(chosen)
}

/// Change the output ("Garmin Bridge") folder — the retired Swift menu bar's "Change output
/// folder…", which the Tauri tray lost in the convergence. Uses an AppleScript folder picker (no
/// extra crate), nests a "Garmin Bridge" folder unless the user already picked one, points the CLI
/// at it (`garminbridge root PATH`), then offers to migrate existing files (never overwrites).
/// Returns the new root, or "" if the user cancelled. Blocks on the picker, so the tray fires it
/// on a background thread.
fn do_change_output_folder() -> Result<String, String> {
    let pick = r#"try
    POSIX path of (choose folder with prompt "Choose your Garmin Bridge folder")
on error number -128
    ""
end try"#;
    let out = Command::new("osascript")
        .arg("-e").arg(pick)
        .output()
        .map_err(|e| e.to_string())?;
    let chosen = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if chosen.is_empty() {
        return Ok(String::new()); // cancelled
    }
    let mut root = PathBuf::from(&chosen);
    if root.file_name().map(|n| n != "Garmin Bridge").unwrap_or(true) {
        root = root.join("Garmin Bridge");
    }
    let root_str = root.to_string_lossy().into_owned();
    let r = cli_command(cli_path())
        .arg("root").arg(&root_str)
        .output()
        .map_err(|e| e.to_string())?;
    if !r.status.success() {
        return Err(format!("could not set the folder: {}", String::from_utf8_lossy(&r.stderr)));
    }
    // offer to move existing files in (the old menu bar's behaviour; migrate never overwrites)
    let ask = r#"button returned of (display dialog "Move your current voice memos and activity backups into this folder? Existing files are never overwritten." buttons {"Not now", "Move"} default button "Move" with title "Garmin Bridge")"#;
    if let Ok(a) = Command::new("osascript").arg("-e").arg(ask).output() {
        if String::from_utf8_lossy(&a.stdout).trim() == "Move" {
            let _ = cli_command(cli_path())
                .arg("migrate")
                .spawn();
        }
    }
    Ok(root_str)
}

/// Command wrapper so the in-app UI can also trigger the folder change later.
#[tauri::command]
fn change_output_folder() -> Result<String, String> {
    do_change_output_folder()
}

/// Toggle launch-at-login (used by the tray and, later, the in-app settings pane).
#[tauri::command]
fn set_autostart(app: tauri::AppHandle, enabled: bool) -> Result<(), String> {
    let manager = app.autolaunch();
    if enabled {
        manager.enable().map_err(|e| e.to_string())
    } else {
        manager.disable().map_err(|e| e.to_string())
    }
}

/// Whether launch-at-login is currently registered.
#[tauri::command]
fn autostart_enabled(app: tauri::AppHandle) -> Result<bool, String> {
    app.autolaunch().is_enabled().map_err(|e| e.to_string())
}

/// Bring the main window to the front (from the tray, a dock re-open, or a second launch).
fn show_main(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        // Must be the first plugin: a second launch focuses the already-running app instead of
        // spawning a duplicate (the "two apps, tray disappeared" bug).
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            show_main(app);
        }))
        .plugin(tauri_plugin_opener::init())
        // --hidden is passed by the login LaunchAgent so a login-launch starts in the tray,
        // while a manual launch shows the window.
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec!["--hidden"]),
        ))
        .manage(PendingSignIn::default())
        .invoke_handler(tauri::generate_handler![
            api,
            api_with_secret,
            garmin_sign_in,
            garmin_sign_in_code,
            save_temp_image,
            save_temp_file,
            change_output_folder,
            run_action,
            open_output_folder,
            open_path,
            play_audio,
            set_notes_folder,
            set_autostart,
            autostart_enabled
        ])
        .setup(|app| {
            // Tray menu — the retired menu bar's quick actions, plus window + login controls.
            let import =
                MenuItem::with_id(app, "import", "Import voice notes", true, None::<&str>)?;
            let backup_act =
                MenuItem::with_id(app, "backup-activities", "Back up activities", true, None::<&str>)?;
            let backup_set = MenuItem::with_id(
                app,
                "backup-settings",
                "Back up settings & profiles",
                true,
                None::<&str>,
            )?;
            let open_folder = MenuItem::with_id(
                app,
                "open-folder",
                "Open Garmin Bridge folder",
                true,
                None::<&str>,
            )?;
            let change_folder = MenuItem::with_id(
                app,
                "change-folder",
                "Change output folder…",
                true,
                None::<&str>,
            )?;
            let show =
                MenuItem::with_id(app, "show", "Open Garmin Bridge", true, None::<&str>)?;
            let login_on = app.autolaunch().is_enabled().unwrap_or(false);
            let login = CheckMenuItem::with_id(
                app,
                "login",
                "Open at login",
                true,
                login_on,
                None::<&str>,
            )?;
            let quit = PredefinedMenuItem::quit(app, Some("Quit Garmin Bridge"))?;

            let menu = Menu::with_items(
                app,
                &[
                    &import,
                    &backup_act,
                    &backup_set,
                    &PredefinedMenuItem::separator(app)?,
                    &open_folder,
                    &change_folder,
                    &PredefinedMenuItem::separator(app)?,
                    &show,
                    &login,
                    &PredefinedMenuItem::separator(app)?,
                    &quit,
                ],
            )?;

            let login_item = login.clone(); // Arc-backed; shares state with the menu entry
            let tray = TrayIconBuilder::with_id("gb-tray")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Garmin Bridge")
                .menu(&menu)
                .show_menu_on_left_click(true)
                .on_menu_event(move |app, event| match event.id.as_ref() {
                    "import" => run_cli(&["voice"]),
                    "backup-activities" => run_cli(&["activities"]),
                    "backup-settings" => run_cli(&["settings"]),
                    "open-folder" => {
                        let _ = open_output_folder();
                    }
                    "change-folder" => {
                        // blocks on the AppleScript picker → run off the tray/main thread
                        std::thread::spawn(|| {
                            let _ = do_change_output_folder();
                        });
                    }
                    "show" => show_main(app),
                    "login" => {
                        let manager = app.autolaunch();
                        let now = manager.is_enabled().unwrap_or(false);
                        let _ = if now {
                            manager.disable()
                        } else {
                            manager.enable()
                        };
                        let _ = login_item.set_checked(manager.is_enabled().unwrap_or(!now));
                    }
                    _ => {}
                })
                .build(app)?;
            // Keep the tray alive for the app's lifetime. The TrayIcon is ref-counted and its
            // menu-bar icon is removed when the last handle drops — without this it would drop
            // at the end of setup and the menu bar would vanish while the window kept working.
            app.manage(tray);

            // Launched at login → start with the window hidden (dock + tray still present).
            if std::env::args().any(|a| a == "--hidden") {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.hide();
                }
            }
            Ok(())
        })
        // Closing the window drops it to the tray (keep-alive) instead of quitting.
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while running tauri application");

    app.run(|app_handle, event| {
        // Clicking the dock icon while hidden re-opens the window (macOS reopen).
        if let RunEvent::Reopen { .. } = event {
            show_main(app_handle);
        }
    });
}
