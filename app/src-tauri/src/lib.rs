// GarminBridge Content Manager: Rust bridge.
// The native shell stays thin: it shells out to the gitignored Python engine (api.py) and
// returns its JSON verbatim to the web UI. No Garmin logic, no tokens, no deletes live here;
// every guarded action runs inside content.py (see prototype/connect/).

use std::path::PathBuf;
use std::process::Command;

/// Where the Python engine lives. Override with GB_ENGINE_DIR; the default derives the
/// canonical checkout location from the home directory (prototype/ is gitignored and only
/// exists on the machine that runs the engine, so the app is host-bound by design until
/// the engine ships inside the bundle; see README). The venv interpreter is
/// <dir>/.venv/bin/python (macOS-only for now, like the rest of the toolchain).
fn engine_dir() -> PathBuf {
    if let Ok(d) = std::env::var("GB_ENGINE_DIR") {
        return PathBuf::from(d);
    }
    let home = std::env::var("HOME").unwrap_or_default();
    PathBuf::from(home).join("Developer/garmin-voice-export/prototype/connect")
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
    let out = Command::new(&py)
        .arg("api.py")
        .args(&args)
        .current_dir(&dir)
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![api])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
