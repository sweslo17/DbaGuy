//! ⌘Q and closing the window ask the page first (spec ⑥ CR-5, A12). The predefined Quit item sends `terminate:` to
//! NSApp, which tao answers with `applicationWillTerminate` only: no `RunEvent::ExitRequested`, so nothing can keep the
//! app open. The menu's Quit is ours instead: it emits `QUIT_REQUESTED` to the page, which acks at once (`quit_ack`),
//! asks while data-tools jobs run, and quits through `quit_app`. A window's close (the red button, Close Window ⌘⇧W) is
//! held the same way (06b pre-flight M4): `lib.rs` prevents it, `request_close` emits `CLOSE_REQUESTED`, the page acks
//! (`close_ack`) and closes the window through `close_window`. A page that does not ack within `ACK_TIMEOUT` (it is
//! loading, reloading or hung) keeps neither the app nor the window open: the shell quits or closes as it did before.
//! Before it asks, the page brings its window to the front (`reveal_window`: a question in a minimized window is never
//! seen). Rust only relays: the decision is the page's.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, Runtime};

/// The event the page listens to for Quit (⌘Q); its payload is the request's number, which the page acks.
pub const QUIT_REQUESTED: &str = "app://quit-requested";
/// The event the page listens to for a window's close (the red button, Close Window ⌘⇧W); acked as Quit's.
pub const CLOSE_REQUESTED: &str = "app://close-requested";
const ACK_TIMEOUT: Duration = Duration::from_secs(2);

/// What a request asks the page.
#[derive(Clone, Copy)]
enum Ask {
    Quit,
    Close,
}

/// The last request asked (one count for both kinds), and the last one of each kind the page acked: numbers grow, an
/// ack covers every request of its kind up to it, and an ack of one kind never covers the other's request (a page
/// whose quit listener is not registered yet still quits by itself, whatever its close listener acked).
#[derive(Default)]
pub struct QuitGate {
    asked: AtomicU64,
    quit_acked: AtomicU64,
    close_acked: AtomicU64,
}

impl QuitGate {
    fn next(&self) -> u64 {
        self.asked.fetch_add(1, Ordering::SeqCst) + 1
    }

    fn acks(&self, kind: Ask) -> &AtomicU64 {
        match kind {
            Ask::Quit => &self.quit_acked,
            Ask::Close => &self.close_acked,
        }
    }

    fn ack(&self, kind: Ask, id: u64) {
        self.acks(kind).fetch_max(id, Ordering::SeqCst);
    }

    fn acked(&self, kind: Ask, id: u64) -> bool {
        self.acks(kind).load(Ordering::SeqCst) >= id
    }
}

/// Runs `fallback` after `ACK_TIMEOUT` unless the page has acked request `id` of this kind by then.
fn unless_acked<R: Runtime>(app: AppHandle<R>, kind: Ask, id: u64, fallback: impl FnOnce() + Send + 'static) {
    std::thread::spawn(move || {
        std::thread::sleep(ACK_TIMEOUT);
        if !app.state::<QuitGate>().acked(kind, id) {
            fallback();
        }
    });
}

/// The menu's Quit (⌘Q): ask the page; quit anyway when it does not answer in time.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub fn request<R: Runtime>(app: &AppHandle<R>) {
    let id = app.state::<QuitGate>().next();
    if let Err(error) = app.emit_to("main", QUIT_REQUESTED, id) {
        log::warn!("Quit: could not ask the page ({error}); quitting");
        app.exit(0);
        return;
    }
    let quitting = app.clone();
    unless_acked(app.clone(), Ask::Quit, id, move || {
        log::warn!("Quit: the page did not answer within {ACK_TIMEOUT:?}; quitting");
        quitting.exit(0);
    });
}

/// A window asked to close (its close is already prevented): the page is asked (it acks at once); a page that does not
/// ack within `ACK_TIMEOUT` (loading, reloading or hung) never keeps the window open.
pub fn request_close<R: Runtime>(window: &tauri::Window<R>) {
    let app = window.app_handle().clone();
    let id = app.state::<QuitGate>().next();
    if let Err(error) = app.emit_to(window.label(), CLOSE_REQUESTED, id) {
        log::warn!("Close: could not ask the page ({error}); closing");
        destroy(window);
        return;
    }
    let window = window.clone();
    unless_acked(app, Ask::Close, id, move || {
        log::warn!("Close: the page did not answer within {ACK_TIMEOUT:?}; closing");
        destroy(&window);
    });
}

/// Closes the window without asking again (closing the last one quits, as before: `RunEvent::ExitRequested`).
fn destroy<R: Runtime>(window: &tauri::Window<R>) {
    if let Err(error) = window.destroy() {
        log::warn!("Close: could not close the window {}: {error}", window.label());
    }
}

/// The page got Quit request `id` and decides now (it may ask the user for as long as it takes).
#[tauri::command]
pub fn quit_ack(gate: tauri::State<'_, QuitGate>, id: u64) {
    gate.ack(Ask::Quit, id);
}

/// The page got close request `id` and decides now (it may ask the user for as long as it takes).
#[tauri::command]
pub fn close_ack(gate: tauri::State<'_, QuitGate>, id: u64) {
    gate.ack(Ask::Close, id);
}

/// Brings the page's window to the front before it asks (06b review K1): a question in a minimized window is never
/// seen. tao's `set_focus` does nothing while the window is minimized, so it is unminimized and shown first.
#[tauri::command]
pub fn reveal_window(window: tauri::Window) {
    let shown = window.unminimize().and_then(|()| window.show()).and_then(|()| window.set_focus());
    if let Err(error) = shown {
        log::warn!("Ask: could not bring the window {} to the front: {error}", window.label());
    }
}

/// The page's decision to quit: the app exits as the predefined Quit would (the sidecar stops on `RunEvent::Exit`).
#[tauri::command]
pub fn quit_app(app: AppHandle) {
    log::info!("Quit: confirmed by the page");
    app.exit(0);
}

/// The page's decision to close the window it runs in.
#[tauri::command]
pub fn close_window(window: tauri::Window) {
    log::info!("Close: confirmed by the page");
    destroy(&window);
}
