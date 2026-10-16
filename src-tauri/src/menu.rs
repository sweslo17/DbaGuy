//! The macOS app menu (ruling R-02c-17): Tauri's default menu, except Close Window. The default binds Close Window to
//! ⌘W, and the native menu takes the shortcut before the page sees it; ⌘W belongs to the page (it closes a tab), so
//! Close Window is ⌘⇧W here. The Edit items stay the predefined ones: WKWebView needs them for the clipboard and text
//! editing.

use tauri::menu::{
    AboutMetadata, Menu, MenuEvent, MenuItem, PredefinedMenuItem, Submenu, HELP_SUBMENU_ID, WINDOW_SUBMENU_ID,
};
use tauri::{AppHandle, Manager, Runtime};

const CLOSE_WINDOW_ID: &str = "close-window";

/// Close Window, in the File and the Window menu.
fn close_window<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<MenuItem<R>> {
    MenuItem::with_id(app, CLOSE_WINDOW_ID, "Close Window", true, Some("CmdOrCtrl+Shift+W"))
}

/// The menu, item for item as `tauri::menu::Menu::default` builds it on macOS (tauri 2.12).
pub fn app_menu<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
    let package = app.package_info();
    let config = app.config();
    let about = AboutMetadata {
        name: Some(package.name.clone()),
        version: Some(package.version.to_string()),
        copyright: config.bundle.copyright.clone(),
        authors: config.bundle.publisher.clone().map(|publisher| vec![publisher]),
        ..Default::default()
    };
    Menu::with_items(
        app,
        &[
            &Submenu::with_items(
                app,
                package.name.clone(),
                true,
                &[
                    &PredefinedMenuItem::about(app, None, Some(about))?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::services(app, None)?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::hide(app, None)?,
                    &PredefinedMenuItem::hide_others(app, None)?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::quit(app, None)?,
                ],
            )?,
            &Submenu::with_items(app, "File", true, &[&close_window(app)?])?,
            &Submenu::with_items(
                app,
                "Edit",
                true,
                &[
                    &PredefinedMenuItem::undo(app, None)?,
                    &PredefinedMenuItem::redo(app, None)?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::cut(app, None)?,
                    &PredefinedMenuItem::copy(app, None)?,
                    &PredefinedMenuItem::paste(app, None)?,
                    &PredefinedMenuItem::select_all(app, None)?,
                ],
            )?,
            &Submenu::with_items(app, "View", true, &[&PredefinedMenuItem::fullscreen(app, None)?])?,
            &Submenu::with_id_and_items(
                app,
                WINDOW_SUBMENU_ID,
                "Window",
                true,
                &[
                    &PredefinedMenuItem::minimize(app, None)?,
                    &PredefinedMenuItem::maximize(app, None)?,
                    &PredefinedMenuItem::separator(app)?,
                    &close_window(app)?,
                ],
            )?,
            &Submenu::with_id(app, HELP_SUBMENU_ID, "Help", true)?,
        ],
    )
}

/// Close Window closes the focused window, as the predefined item does.
pub fn on_menu_event<R: Runtime>(app: &AppHandle<R>, event: MenuEvent) {
    if event.id() != CLOSE_WINDOW_ID {
        return;
    }
    let focused = app.webview_windows().into_values().find(|window| window.is_focused().unwrap_or(false));
    match focused {
        Some(window) => {
            if let Err(error) = window.close() {
                log::warn!("Could not close the window {}: {error}", window.label());
            }
        }
        None => log::info!("Close Window: no window is focused"),
    }
}
