#!/usr/bin/env bash
# Build the Python sidecar (PyInstaller onedir) and copy it into the Tauri resources.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/src-tauri/resources/backend"

cd "$ROOT/backend"
poetry install --no-interaction
rm -rf build dist
.venv/bin/pyinstaller --noconfirm --clean dbaguy-backend.spec

# The sidecar runs mysql-connector in pure mode (use_pure=True). PyInstaller follows any import that reaches the C
# extension and bundles it with its own libssl/libcrypto (~13 MB, unused), so the spec excludes it; fail if it is back.
if [[ -n "$(find dist/dbaguy-backend -name '_mysql_connector*' | head -n 1)" ]]; then
  echo "error: the mysql-connector C extension (_mysql_connector) was bundled; check the excludes in dbaguy-backend.spec" >&2
  exit 1
fi

# SQL Server: pymssql's extension (FreeTDS linked in) must be in the bundle. PyInstaller collects the OpenSSL it loads
# (pymssql/.dylibs) as a binary dependency; this check does not look at that.
if [[ -z "$(find dist/dbaguy-backend -name '_mssql*.so' | head -n 1)" ]]; then
  echo "error: pymssql's _mssql extension is missing from the bundle; check hiddenimports in dbaguy-backend.spec" >&2
  exit 1
fi

find "$DEST" -mindepth 1 ! -name .gitkeep -exec rm -rf {} +
cp -R dist/dbaguy-backend/. "$DEST/"
echo "Backend bundled into $DEST"
