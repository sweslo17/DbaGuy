mod backend;
#[cfg(target_os = "macos")]
mod menu;
mod quit;

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
    let builder = tauri::Builder::default();
    // ⌘W reaches the page (it closes a tab): the menu's Close Window is ⌘⇧W.
    #[cfg(target_os = "macos")]
    let builder = builder.menu(menu::app_menu).on_menu_event(menu::on_menu_event);
    builder
        .plugin(
            tauri_plugin_log::Builder::new()
                .targets([
                    Target::new(TargetKind::Stdout),
                    Target::new(TargetKind::Folder { path: log_dir(), file_name: Some("shell".into()) }),
                ])
                .level(log::LevelFilter::Info)
                .build(),
        )
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .manage(BackendManager::default())
        .manage(quit::QuitGate::default())
        // A window's close asks the page first (spec ⑥ CR-5): it closes through `quit::close_window`, or by itself
        // when the page does not answer in time (`quit::request_close`).
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                quit::request_close(window);
            }
        })
        .setup(|app| {
            let _ = app.state::<BackendManager>().start(app.handle());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend_info,
            restart_backend,
            quit::quit_ack,
            quit::quit_app,
            quit::close_window
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                app.state::<BackendManager>().stop();
            }
        });
}
