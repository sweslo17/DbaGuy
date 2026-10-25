#!/usr/bin/env bash
# Start the app's bundled sidecar on its own and check it before a release: it must print its ready line, answer
# /system/health with the app's version and list the five engines. A throwaway data dir, no log file and a random
# token: it never touches the real app data, logs or Keychain items. Stops only the process it started.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${1:-$ROOT/src-tauri/target/release/bundle/macos/DbaGuy.app}"
BIN="$APP/Contents/Resources/backend/dbaguy-backend"
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")"

DATA="$(mktemp -d)"
TOKEN="$(openssl rand -hex 16)"
DBAGUY_DATA_DIR="$DATA/data" DBAGUY_LOG_TO_FILE=false DBAGUY_TOKEN="$TOKEN" DBAGUY_PORT=0 \
  "$BIN" >"$DATA/stdout" 2>"$DATA/stderr" &
PID=$!
cleanup() {
  kill "$PID" 2>/dev/null || true
  wait "$PID" 2>/dev/null || true
  rm -rf "$DATA"
}
trap cleanup EXIT

ready_port() {
  python3 -c '
import json, sys
for line in open(sys.argv[1]):
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if event.get("event") == "ready":
        print(event["port"])
        break
' "$DATA/stdout"
}

port=""
for _ in $(seq 1 600); do  # up to 60 s: a fresh bundle's first start is slow
  port="$(ready_port)"
  [[ -n "$port" ]] && break
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "error: the sidecar exited before it was ready" >&2
    cat "$DATA/stderr" >&2
    exit 1
  fi
  sleep 0.1
done
if [[ -z "$port" ]]; then
  echo "error: the sidecar printed no ready line within 60 s" >&2
  cat "$DATA/stderr" >&2
  exit 1
fi

get() { curl -fsS --max-time 10 -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$port$1"; }
get /system/health > "$DATA/health"
get /system/engines > "$DATA/engines"
python3 - "$DATA/health" "$DATA/engines" "$VERSION" <<'EOF'
import json, sys
health = json.load(open(sys.argv[1]))
engines = sorted(e["engine"] for e in json.load(open(sys.argv[2])))
expected = sorted(["couchbase", "mssql", "mysql", "postgres", "sqlite"])
if health.get("status") != "ok" or health.get("version") != sys.argv[3]:
    sys.exit(f"error: /system/health answered {health}, expected ok and version {sys.argv[3]}")
if engines != expected:
    sys.exit(f"error: /system/engines lists {engines}, expected {expected}")
print(f"sidecar ok: version {health['version']}, engines {', '.join(engines)}")
EOF
