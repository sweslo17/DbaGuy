#!/usr/bin/env bash
# Regenerate TypeScript API types from the backend's OpenAPI schema.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$ROOT/src/lib/api"
"$ROOT/backend/.venv/bin/python" -m dbaguy_backend.openapi_dump > "$ROOT/src/lib/api/openapi.json"
npx openapi-typescript "$ROOT/src/lib/api/openapi.json" -o "$ROOT/src/lib/api/schema.d.ts"
