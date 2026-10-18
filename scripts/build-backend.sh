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

# One OpenSSL for the whole bundle: PyInstaller links Python's _ssl and _hashlib (and psycopg's libpq) to the
# libssl/libcrypto it took from pymssql (pymssql/.dylibs). That is safe only while it is the OpenSSL the venv's Python
# was built against; fail when the versions differ (e.g. a pymssql wheel built with another OpenSSL).
crypto="$(find dist/dbaguy-backend/_internal -maxdepth 1 -name 'libcrypto*.dylib' | head -n 1)"
if [[ -z "$crypto" ]]; then
  echo "error: no libcrypto in the bundle's _internal; the OpenSSL check cannot run" >&2
  exit 1
fi
# A fresh interpreter that never imports ssl, so the bundled library is the only libcrypto it loads.
bundled_ssl="$(.venv/bin/python -I -c 'import ctypes, sys
lib = ctypes.CDLL(sys.argv[1]); lib.OpenSSL_version.restype = ctypes.c_char_p
print(" ".join(lib.OpenSSL_version(0).decode().split()[:2]))' "$crypto")"
venv_ssl="$(.venv/bin/python -I -c 'import ssl; print(" ".join(ssl.OPENSSL_VERSION.split()[:2]))')"
if [[ "$bundled_ssl" != "$venv_ssl" ]]; then
  echo "error: the bundle's OpenSSL ($bundled_ssl, $crypto) is not the one the venv's Python uses ($venv_ssl)" >&2
  exit 1
fi
echo "OpenSSL in the bundle: $bundled_ssl (same as the venv's Python)"

find "$DEST" -mindepth 1 ! -name .gitkeep -exec rm -rf {} +
cp -R dist/dbaguy-backend/. "$DEST/"
echo "Backend bundled into $DEST"
