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

/// Canonical repo checkout. Override with GB_REPO_DIR; default is the locked 2_Code home
/// (moved there 2026-07; the old ~/Developer path is retired). The engine and CLI both hang
/// off this, so one env var relocates the whole host-bound toolchain.
fn repo_root() -> PathBuf {
    if let Ok(d) = std::env::var("GB_REPO_DIR") {
        return PathBuf::from(d);
    }
    let home = std::env::var("HOME").unwrap_or_default();
    PathBuf::from(home).join("Second Brain/2_Code/garmin-voice-export")
}

/// Where the Python engine lives. Override with GB_ENGINE_DIR; default is
/// <repo>/prototype/connect (gitignored, holds tokens — only exists on the machine that runs
/// the engine, so the app is host-bound by design until the engine ships in the bundle; see
/// README). The venv interpreter is <dir>/.venv/bin/python (macOS-only for now).
fn engine_dir() -> PathBuf {
    if let Ok(d) = std::env::var("GB_ENGINE_DIR") {
        return PathBuf::from(d);
    }
    repo_root().join("prototype/connect")
}

/// The `garminbridge` control CLI (bin/garminbridge). Override with GB_CLI.
fn cli_path() -> PathBuf {
    if let Ok(d) = std::env::var("GB_CLI") {
        return PathBuf::from(d);
    }
    repo_root().join("bin/garminbridge")
}

/// A PATH that includes Homebrew, because the CLI scripts call gphoto2 etc. and a GUI-launched
/// process inherits a bare PATH that misses /opt/homebrew/bin.
fn cli_path_env() -> String {
    let inherited = std::env::var("PATH").unwrap_or_default();
    format!("/opt/homebrew/bin:/usr/local/bin:{inherited}")
}

/// Run `api.py <args...>` in the engine dir and return stdout (always JSON). api.py prints a
/// JSON error object even on handled failures, so we surface stdout when present.
#[tauri::command]
fn api(args: Vec<String>) -> Result<String, String> {
    let dir = engine_dir();
    let py = dir.join(".venv/bin/python");
    if !py.exists() {
        return Err(format!(
            "engine python not found at {}: set GB_ENGINE_DIR to the prototype/connect dir",
            py.display()
        ));
    }
    // api.py shells out to gphoto2 / killall / watch_mtp.sh for every watch (USB) operation, and
    // gphoto2 lives in /opt/homebrew/bin. A GUI-launched .app inherits a BARE PATH that misses it,
    // so without this the watch is never detected and no USB action works (it worked in `tauri dev`
    // only because a terminal launch inherits the full PATH). Same fix run_cli already applies.
    let out = Command::new(&py)
        .arg("api.py")
        .args(&args)
        .current_dir(&dir)
        .env("PATH", cli_path_env())
        .output()
        .map_err(|e| format!("failed to launch engine: {e}"))?;
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
    let _ = Command::new(cli_path())
        .args(args)
        .env("PATH", cli_path_env())
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
    if let Ok(out) = Command::new(cli_path())
        .arg("root")
        .env("PATH", cli_path_env())
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
    let r = Command::new(cli_path())
        .arg("root").arg(&root_str)
        .env("PATH", cli_path_env())
        .output()
        .map_err(|e| e.to_string())?;
    if !r.status.success() {
        return Err(format!("could not set the folder: {}", String::from_utf8_lossy(&r.stderr)));
    }
    // offer to move existing files in (the old menu bar's behaviour; migrate never overwrites)
    let ask = r#"button returned of (display dialog "Move your current voice memos and activity backups into this folder? Existing files are never overwritten." buttons {"Not now", "Move"} default button "Move" with title "Garmin Bridge")"#;
    if let Ok(a) = Command::new("osascript").arg("-e").arg(ask).output() {
        if String::from_utf8_lossy(&a.stdout).trim() == "Move" {
            let _ = Command::new(cli_path())
                .arg("migrate")
                .env("PATH", cli_path_env())
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
        .invoke_handler(tauri::generate_handler![
            api,
            save_temp_image,
            save_temp_file,
            change_output_folder,
            run_action,
            open_output_folder,
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
