#!/usr/bin/env bash
# Build the Python sidecar (PyInstaller onedir) and copy it into the Tauri resources.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/src-tauri/resources/backend"

cd "$ROOT/backend"
poetry install --no-interaction
rm -rf build dist
.venv/bin/pyinstaller --noconfirm --clean dbaguy-backend.spec

find "$DEST" -mindepth 1 ! -name .gitkeep -exec rm -rf {} +
cp -R dist/dbaguy-backend/. "$DEST/"
echo "Backend bundled into $DEST"
