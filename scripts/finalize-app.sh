#!/usr/bin/env bash
# Post-process the Tauri bundle: put the PyInstaller sidecar back verbatim and re-sign the app ad hoc.
# Tauri copies resources with symlinks dereferenced, which duplicates the shared dylibs and drops the
# Python.framework Resources/Versions links in `_internal`.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${1:-$ROOT/src-tauri/target/release/bundle/macos/DbaGuy.app}"
SRC="$ROOT/src-tauri/resources/backend"
DEST="$APP/Contents/Resources/backend"

rm -rf "$DEST"
ditto "$SRC" "$DEST"
rm -f "$DEST/.gitkeep"
codesign --force --deep -s - "$APP"
codesign --verify --deep --strict "$APP"
echo "signature ok: $APP"
