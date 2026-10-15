#!/usr/bin/env bash
# Launch DbaGuy.app, measure time until the UI's first health check reaches the backend, report memory, quit.
set -euo pipefail
APP="${1:-src-tauri/target/release/bundle/macos/DbaGuy.app}"
LOG="$HOME/Library/Logs/DbaGuy/backend.log"
APP_PATTERN="DbaGuy.app/Contents/MacOS"
BACKEND_PATTERN="Contents/Resources/backend/dbaguy-backend"

if pgrep -f "$APP_PATTERN" >/dev/null; then
  echo "DbaGuy is already running; quit it first" >&2
  exit 1
fi

now() { python3 -c 'import time; print(time.time())'; }

# `osascript` can block on an automation permission prompt; give it a few seconds, then fall back to SIGTERM.
# The backend watches its stdin, so it exits on its own once the app is gone.
quit_app() {
  osascript -e 'quit app "DbaGuy"' >/dev/null 2>&1 &
  local osa=$!
  for _ in $(seq 1 30); do
    pgrep -f "$APP_PATTERN" >/dev/null || break
    sleep 0.1
  done
  kill "$osa" 2>/dev/null || true
  if pgrep -f "$APP_PATTERN" >/dev/null; then
    echo "app did not quit via osascript; sending SIGTERM"
    pkill -TERM -f "$APP_PATTERN" || true
  fi
}

mkdir -p "$(dirname "$LOG")" && touch "$LOG"
before=$(wc -l < "$LOG")
start=$(now)
open -n "$APP"
reached=0
for _ in $(seq 1 1200); do
  # Process substitution, not a pipe: under `pipefail` grep -q closing early would turn a match into a failure.
  if grep -q "Health check (origin=tauri://localhost)" <(tail -n +"$((before + 1))" "$LOG"); then reached=1; break; fi
  sleep 0.05
done
end=$(now)
if [[ "$reached" == 1 ]]; then
  python3 -c "print(f'cold start to first API call: {($end - $start):.2f}s')"
else
  echo "TIMEOUT: the UI never reached the backend within 60s"
fi
sleep 3
# WebKit helper processes are XPC services of launchd, so attribute them by age: keep only those started
# no earlier than the app itself. Other apps' web views started in the same window would inflate the total.
# The patterns go through the environment so this script's own awk processes do not match them in `ps`.
export APP_PATTERN BACKEND_PATTERN
app_age=$(ps -axo etime=,command= | awk '!seen && index($0, ENVIRON["APP_PATTERN"]) { print $1; seen = 1 }')
ps -axo rss=,etime=,command= | AGE="$app_age" awk '
  function secs(t,   n, a, d, p) {
    d = 0; if (index(t, "-")) { split(t, a, "-"); d = a[1]; t = a[2] }
    n = split(t, p, ":")
    return d * 86400 + (n == 3 ? p[1] * 3600 + p[2] * 60 + p[3] : p[1] * 60 + p[2])
  }
  index($0, ENVIRON["APP_PATTERN"]) || index($0, ENVIRON["BACKEND_PATTERN"]) \
    || ($0 ~ /com\.apple\.WebKit\.(WebContent|Networking|GPU)/ && secs($2) <= secs(ENVIRON["AGE"]) + 1) {
    n = split($3, b, "/"); sum += $1; printf "%7.1f MB  %s\n", $1 / 1024, b[n]
  }
  END { printf "total RSS: %.0f MB (RSS double-counts shared pages: an upper bound)\n", sum / 1024 }'
quit_app
sleep 4
if pgrep -f "$BACKEND_PATTERN" >/dev/null; then echo "LEFTOVER BACKEND PROCESS"; exit 1; fi
if pgrep -f "$APP_PATTERN" >/dev/null; then echo "APP STILL RUNNING"; exit 1; fi
echo "backend exited with the app"
