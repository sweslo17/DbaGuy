#!/usr/bin/env bash
# Launch a built DbaGuy.app, measure time until the UI's first health check reaches the backend, report memory, quit.
#
# Only the instance this script starts is touched: its executable is run directly (PID from `$!`), its shell log is read
# from its own stdout, and only that PID is stopped. An installed DbaGuy the user is running is left alone: processes
# are never matched by pattern, only by PID or by this build's exact executable path.
set -euo pipefail
APP="$(cd "${1:-src-tauri/target/release/bundle/macos/DbaGuy.app}" && pwd -P)"
EXE="$APP/Contents/MacOS/$(/usr/libexec/PlistBuddy -c "Print :CFBundleExecutable" "$APP/Contents/Info.plist")"
DATA_DIR="${DBAGUY_DATA_DIR:-$(mktemp -d)}"
REAL_DATA_DIR="$HOME/Library/Application Support/DbaGuy"
if [[ "$(cd "$DATA_DIR" && pwd -P)" == "$(cd "$REAL_DATA_DIR" 2>/dev/null && pwd -P)" ]]; then
  echo "refusing to measure with the real data dir ($REAL_DATA_DIR)" >&2
  exit 1
fi
echo "data dir: $DATA_DIR"
OUT="$(mktemp -d)/shell.out" # the app's stdout: tauri-plugin-log mirrors the shell log, sidecar lines included

# `ps -o comm` is the executable's full path: equal to EXE means this very build is already running. Process
# substitution, not a pipe: under `pipefail` grep -q closing early would turn a match into a failure.
if grep -qxF "$EXE" <(ps -axo comm=); then
  echo "this build ($EXE) is already running; quit it first" >&2
  exit 1
fi

now() { python3 -c 'import time; print(time.time())'; }
# Whether PID is still the process running EXECUTABLE (not exited, not a zombie, not a reused PID).
running() {
  local stat comm
  read -r stat comm <<<"$(ps -p "$1" -o stat=,comm= 2>/dev/null)" || return 1
  [[ -n "$stat" && "$stat" != Z* && "$comm" == "$2" ]]
}

start=$(now)
# Run directly, not through `open`: the app inherits this environment (the temp DBAGUY_DATA_DIR, so neither the real
# data dir nor the real Keychain service is used) and `$!` is its PID. No dev backend: that would skip the sidecar.
env -u DBAGUY_DEV_BACKEND_URL -u DBAGUY_DEV_BACKEND_TOKEN DBAGUY_DATA_DIR="$DATA_DIR" "$EXE" >"$OUT" 2>&1 &
APP_PID=$!
disown "$APP_PID" # no "Terminated" job report when it is stopped
echo "app pid: $APP_PID"
for _ in $(seq 1 100); do # `env` execs the app in the same process: wait until the PID runs EXE
  running "$APP_PID" "$EXE" && break
  sleep 0.02
done

# SIGTERM, then SIGKILL after 5 s; the backend watches its stdin, so it exits once the app is gone.
stop_app() {
  running "$APP_PID" "$EXE" || return 0
  kill -TERM "$APP_PID" 2>/dev/null || true
  for _ in $(seq 1 50); do
    running "$APP_PID" "$EXE" || return 0
    sleep 0.1
  done
  echo "app did not exit on SIGTERM; sending SIGKILL"
  kill -KILL "$APP_PID" 2>/dev/null || true
}
trap stop_app EXIT

reached=0
for _ in $(seq 1 1200); do
  running "$APP_PID" "$EXE" || break
  if grep -q "Health check (origin=tauri://localhost)" "$OUT"; then reached=1; break; fi
  sleep 0.05
done
end=$(now)
if [[ "$reached" == 1 ]]; then
  python3 -c "print(f'cold start to first API call: {($end - $start):.2f}s')"
else
  echo "TIMEOUT: the UI never reached the backend within 60s (output: $OUT)"
fi
sleep 3
# The sidecar is a child of the app: noted now (PID and executable), since once the app is gone its children belong to
# launchd.
CHILDREN=$(pgrep -P "$APP_PID" | tr '\n' ' ' || true)
CHILD_EXES=$(for pid in $CHILDREN; do printf '%s %s\n' "$pid" "$(ps -p "$pid" -o comm= 2>/dev/null || true)"; done)
# WebKit helper processes are XPC services of launchd, so attribute them by age: keep only those started no earlier
# than the app itself. Other apps' web views started in the same window would inflate the total.
app_age=$(ps -p "$APP_PID" -o etime= | tr -d ' ')
PIDS_FILE="$(mktemp)" # the PIDs listed below, for `footprint`
ps -axo pid=,rss=,etime=,command= | AGE="$app_age" OURS=" $APP_PID $CHILDREN" PIDS_FILE="$PIDS_FILE" awk '
  function secs(t,   n, a, d, p) {
    d = 0; if (index(t, "-")) { split(t, a, "-"); d = a[1]; t = a[2] }
    n = split(t, p, ":")
    return d * 86400 + (n == 3 ? p[1] * 3600 + p[2] * 60 + p[3] : p[1] * 60 + p[2])
  }
  index(ENVIRON["OURS"] " ", " " $1 " ") \
    || ($0 ~ /com\.apple\.WebKit\.(WebContent|Networking|GPU)/ && secs($3) <= secs(ENVIRON["AGE"]) + 1) {
    n = split($4, b, "/"); sum += $2; printf "%7.1f MB  %s\n", $2 / 1024, b[n]; print $1 > (ENVIRON["PIDS_FILE"])
  }
  END { printf "total RSS: %.0f MB (RSS double-counts shared pages: an upper bound)\n", sum / 1024 }'
# phys_footprint of the same processes: what Activity Monitor calls Memory (RSS above double-counts shared pages).
# Before stop_app: the processes must still be running.
if command -v footprint >/dev/null && [[ -s "$PIDS_FILE" ]]; then
  # shellcheck disable=SC2046  # one PID per word
  footprint --noCategories $(<"$PIDS_FILE") 2>/dev/null | grep -E 'Footprint:' | sed -E 's/^ +//' || true
fi
rm -f "$PIDS_FILE"
stop_app
trap - EXIT
left=""
while read -r pid exe; do
  [[ -n "$pid" ]] || continue
  for _ in $(seq 1 40); do
    running "$pid" "$exe" || break
    sleep 0.1
  done
  if running "$pid" "$exe"; then left="$left $pid"; fi
done <<<"$CHILD_EXES"
if [[ -n "$left" ]]; then
  echo "LEFTOVER BACKEND PROCESS:$left (stopping it)"
  kill -TERM $left 2>/dev/null || true
  exit 1
fi
if running "$APP_PID" "$EXE"; then echo "APP STILL RUNNING"; exit 1; fi
echo "backend exited with the app"
