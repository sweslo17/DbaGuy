mod backend;

use backend::{BackendInfo, BackendManager};
use std::path::PathBuf;
use std::time::Duration;
use tauri::Manager;
use tauri_plugin_log::{Target, TargetKind};

const READY_TIMEOUT: Duration = Duration::from_secs(20);

#[tauri::command]
async fn backend_info(manager: tauri::State<'_, BackendManager>) -> Result<BackendInfo, String> {
    manager.wait_ready(READY_TIMEOUT).await
}

#[tauri::command]
async fn restart_backend(
    app: tauri::AppHandle,
    manager: tauri::State<'_, BackendManager>,
) -> Result<BackendInfo, String> {
    let m = manager.inner().clone();
    tauri::async_runtime::spawn_blocking(move || m.restart(&app))
        .await
        .map_err(|e| e.to_string())??;
    manager.wait_ready(READY_TIMEOUT).await
}

fn log_dir() -> PathBuf {
    PathBuf::from(std::env::var("HOME").unwrap_or_default()).join("Library/Logs/DbaGuy")
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(
            tauri_plugin_log::Builder::new()
                .targets([
                    Target::new(TargetKind::Stdout),
                    Target::new(TargetKind::Folder { path: log_dir(), file_name: Some("shell".into()) }),
                ])
                .level(log::LevelFilter::Info)
                .build(),
        )
        .manage(BackendManager::default())
        .setup(|app| {
            let _ = app.state::<BackendManager>().start(app.handle());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![backend_info, restart_backend])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                app.state::<BackendManager>().stop();
            }
        });
}
