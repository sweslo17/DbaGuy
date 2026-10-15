#!/usr/bin/env bash
# Start the backend (auto-reload) plus the Tauri dev app, or the web UI with --web.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${DBAGUY_DEV_PORT:-18765}"
TOKEN=dev-token

cleanup() {
  if [[ -n "${BACKEND_PID:-}" ]]; then kill "$BACKEND_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM

(
  cd "$ROOT/backend"
  DBAGUY_TOKEN=$TOKEN DBAGUY_LOG_LEVEL=DEBUG exec .venv/bin/uvicorn \
    dbaguy_backend.app:create_app_from_env --factory --reload \
    --host 127.0.0.1 --port "$PORT" --log-level warning
) &
BACKEND_PID=$!

cd "$ROOT"
if [[ "${1:-}" == "--web" ]]; then
  VITE_BACKEND_URL="http://127.0.0.1:$PORT" VITE_BACKEND_TOKEN=$TOKEN npm run dev
else
  DBAGUY_DEV_BACKEND_URL="http://127.0.0.1:$PORT" DBAGUY_DEV_BACKEND_TOKEN=$TOKEN npm run tauri dev
fi
