use std::sync::Mutex as StdMutex;
use tauri::{Emitter, Manager};
use tauri_plugin_shell::ShellExt;

use crate::SidecarState;

const SIDECAR_NAME: &str = "scanhound-backend";
const SIDECAR_PORT: u16 = 9721;
const NONCE_PREFIX: &str = "SCANHOUND_AUTH_NONCE=";

/// Shared state for the auth nonce captured from sidecar stdout.
pub struct AuthNonce(pub StdMutex<String>);

/// Launch the Python backend sidecar process.
pub fn launch(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    let shell = app.shell();

    let command = shell.sidecar(SIDECAR_NAME)?;
    let (mut rx, child) = command
        .args(["--port", &SIDECAR_PORT.to_string(), "--host", "127.0.0.1"])
        .spawn()?;

    // Store child handle for cleanup
    let state = app.state::<SidecarState>();
    *state.child.lock().unwrap() = Some(child);

    // Log sidecar output in background
    let app_handle = app.clone();
    tauri::async_runtime::spawn(async move {
        use tauri_plugin_shell::process::CommandEvent;
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    let text = String::from_utf8_lossy(&line);
                    // Capture auth nonce from sidecar output
                    if let Some(nonce) = text.trim().strip_prefix(NONCE_PREFIX) {
                        let nonce = nonce.trim().to_string();
                        log::info!("[sidecar] captured auth nonce");
                        if let Ok(mut guard) = app_handle.state::<AuthNonce>().0.lock() {
                            *guard = nonce.clone();
                        }
                        let _ = app_handle.emit("sidecar-auth-nonce", &nonce);
                    }
                    log::info!("[sidecar] {}", text);
                }
                CommandEvent::Stderr(line) => {
                    log::warn!("[sidecar] {}", String::from_utf8_lossy(&line));
                }
                CommandEvent::Terminated(payload) => {
                    log::warn!(
                        "[sidecar] terminated with code {:?}, signal {:?}",
                        payload.code,
                        payload.signal
                    );
                    // Emit event to frontend so it knows the backend died
                    let _ = app_handle.emit("sidecar-terminated", payload.code);

                    let state = app_handle.state::<SidecarState>();
                    let mut count = state.restart_count.lock().unwrap();
                    *count += 1;

                    if *count <= 3 {
                        let attempt = *count;
                        drop(count); // release lock before async work
                        log::info!("[sidecar] restarting (attempt {}/3)", attempt);
                        let _ = app_handle.emit("sidecar-restarting", serde_json::json!({"attempt": attempt, "max": 3}));

                        // Wait 2 seconds then relaunch
                        let handle = app_handle.clone();
                        tauri::async_runtime::spawn(async move {
                            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                            if let Err(e) = launch(&handle) {
                                log::error!("[sidecar] restart failed: {}", e);
                                let _ = handle.emit("sidecar-failed", ());
                            }
                        });
                    } else {
                        drop(count);
                        log::error!("[sidecar] max restart attempts exceeded");
                        let _ = app_handle.emit("sidecar-failed", ());
                    }
                    break;
                }
                CommandEvent::Error(err) => {
                    log::error!("[sidecar] error: {}", err);
                    break;
                }
                _ => {}
            }
        }
    });

    log::info!("Sidecar launched on port {}", SIDECAR_PORT);
    Ok(())
}

/// Reset the restart counter (call after frontend successfully connects).
pub fn reset_restart_count(app: &tauri::AppHandle) {
    let state = app.state::<SidecarState>();
    *state.restart_count.lock().unwrap() = 0;
}

/// Shut down the sidecar process.
pub fn shutdown(app: &tauri::AppHandle) {
    let state = app.state::<SidecarState>();
    if let Ok(mut guard) = state.child.lock() {
        if let Some(child) = guard.take() {
            let _ = child.kill();
            log::info!("Sidecar process killed");
        }
    };
}
