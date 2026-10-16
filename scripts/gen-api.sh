#!/usr/bin/env bash
# Regenerate TypeScript API types from the backend's OpenAPI schema.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Dumping the schema builds the app: give it a throwaway data dir, never the user's real one.
DATA_DIR="$(mktemp -d)"
trap 'rm -rf "$DATA_DIR"' EXIT
mkdir -p "$ROOT/src/lib/api"
DBAGUY_DATA_DIR="$DATA_DIR" "$ROOT/backend/.venv/bin/python" -m dbaguy_backend.openapi_dump > "$ROOT/src/lib/api/openapi.json"
npx openapi-typescript "$ROOT/src/lib/api/openapi.json" -o "$ROOT/src/lib/api/schema.d.ts"
