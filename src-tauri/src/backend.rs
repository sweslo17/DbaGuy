//! Spawns and supervises the Python sidecar.

use serde::Serialize;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager};

/// How long `stop` waits for the backend to exit after closing its stdin before killing it. Longer than the
/// backend's own forced-exit deadline (5 s, `backend/dbaguy_backend/__main__.py`), which closes SSH tunnels first.
const STOP_TIMEOUT: Duration = Duration::from_secs(8);

#[derive(Clone, Debug, Serialize)]
pub struct BackendInfo {
    pub base_url: String,
    pub token: String,
}

#[derive(Default)]
struct State {
    child: Option<Child>,
    /// Kept open for the backend's stdin watchdog; dropping it tells the backend to exit.
    stdin: Option<ChildStdin>,
    info: Option<BackendInfo>,
    last_error: Option<String>,
    generation: u64,
    stopping: bool,
}

#[derive(Clone, Default)]
pub struct BackendManager {
    state: Arc<Mutex<State>>,
}

impl BackendManager {
    pub fn start(&self, app: &AppHandle) -> Result<(), String> {
        let result = self.start_inner(app);
        if let Err(e) = &result {
            log::error!("Failed to start backend: {e}");
            self.state.lock().unwrap().last_error = Some(e.clone());
        }
        result
    }

    fn start_inner(&self, app: &AppHandle) -> Result<(), String> {
        if let Ok(url) = std::env::var("DBAGUY_DEV_BACKEND_URL") {
            // scripts/dev.sh sets both; there is no fixed dev token the backend would accept.
            let token = std::env::var("DBAGUY_DEV_BACKEND_TOKEN")
                .map_err(|_| "DBAGUY_DEV_BACKEND_URL is set without DBAGUY_DEV_BACKEND_TOKEN".to_string())?;
            log::info!("Using external dev backend at {url}");
            self.state.lock().unwrap().info = Some(BackendInfo { base_url: url, token });
            return Ok(());
        }
        let token = new_token()?;
        let mut cmd = backend_command(app)?;
        cmd.env("DBAGUY_TOKEN", &token)
            .env("DBAGUY_PORT", "0")
            .env("DBAGUY_WATCH_STDIN", "true")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = cmd.spawn().map_err(|e| format!("failed to spawn backend: {e}"))?;
        let stdout = child.stdout.take().ok_or("backend stdout missing")?;
        let stderr = child.stderr.take().ok_or("backend stderr missing")?;
        let stdin = child.stdin.take();
        let generation = {
            let mut st = self.state.lock().unwrap();
            st.generation += 1;
            st.child = Some(child);
            st.stdin = stdin;
            st.info = None;
            st.last_error = None;
            st.stopping = false;
            st.generation
        };
        log::info!("Backend spawned (generation {generation})");

        let state = self.state.clone();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                match parse_ready(&line) {
                    Some(port) => {
                        let mut st = state.lock().unwrap();
                        if st.generation == generation {
                            st.info = Some(BackendInfo {
                                base_url: format!("http://127.0.0.1:{port}"),
                                token: token.clone(),
                            });
                        }
                        log::info!("Backend ready on port {port}");
                    }
                    None => log::info!("[backend stdout] {line}"),
                }
            }
        });

        std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                log::info!("[backend] {line}");
            }
        });

        let state = self.state.clone();
        let app = app.clone();
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_millis(500));
            let mut st = state.lock().unwrap();
            if st.generation != generation {
                return;
            }
            let Some(child) = st.child.as_mut() else { return };
            match child.try_wait() {
                Ok(Some(status)) => {
                    let stopping = st.stopping;
                    st.child = None;
                    st.stdin = None;
                    st.info = None;
                    st.last_error = Some(format!("backend exited: {status}"));
                    drop(st);
                    if !stopping {
                        log::error!("Backend exited unexpectedly: {status}");
                        let _ = app.emit("backend://exited", status.code());
                    }
                    return;
                }
                Ok(None) => {}
                Err(e) => {
                    log::error!("Failed to poll backend process: {e}");
                    return;
                }
            }
        });
        Ok(())
    }

    pub async fn wait_ready(&self, timeout: Duration) -> Result<BackendInfo, String> {
        let deadline = Instant::now() + timeout;
        loop {
            {
                let st = self.state.lock().unwrap();
                if let Some(info) = &st.info {
                    return Ok(info.clone());
                }
                if st.child.is_none() {
                    if let Some(err) = &st.last_error {
                        return Err(err.clone());
                    }
                }
            }
            if Instant::now() >= deadline {
                return Err("backend did not become ready in time".into());
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }

    pub fn stop(&self) {
        let (child, stdin) = {
            let mut st = self.state.lock().unwrap();
            st.stopping = true;
            st.info = None;
            (st.child.take(), st.stdin.take())
        };
        drop(stdin);
        if let Some(mut child) = child {
            let deadline = Instant::now() + STOP_TIMEOUT;
            while Instant::now() < deadline {
                if let Ok(Some(_)) = child.try_wait() {
                    log::info!("Backend stopped");
                    return;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            log::warn!("Backend did not exit in time; killing it");
            let _ = child.kill();
            let _ = child.wait();
        }
    }

    pub fn restart(&self, app: &AppHandle) -> Result<(), String> {
        self.stop();
        self.start(app)
    }
}

fn backend_command(app: &AppHandle) -> Result<Command, String> {
    if cfg!(debug_assertions) {
        let backend_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../backend");
        let mut cmd = Command::new(backend_dir.join(".venv/bin/python"));
        cmd.args(["-m", "dbaguy_backend"]).current_dir(&backend_dir);
        Ok(cmd)
    } else {
        let exe = app
            .path()
            .resource_dir()
            .map_err(|e| format!("resource dir unavailable: {e}"))?
            .join("backend")
            .join("dbaguy-backend");
        Ok(Command::new(exe))
    }
}

fn new_token() -> Result<String, String> {
    let mut buf = [0u8; 32];
    getrandom::fill(&mut buf).map_err(|e| format!("token generation failed: {e}"))?;
    Ok(buf.iter().map(|b| format!("{b:02x}")).collect())
}

fn parse_ready(line: &str) -> Option<u16> {
    let value: serde_json::Value = serde_json::from_str(line).ok()?;
    if value.get("event")?.as_str()? != "ready" {
        return None;
    }
    value.get("port")?.as_u64().and_then(|p| u16::try_from(p).ok())
}
