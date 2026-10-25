#!/usr/bin/env bash
# Package the built DbaGuy.app into a drag-to-install disk image (the app and an Applications link), check the app's
# signature inside the mounted image, and write the image's SHA-256 beside it. Run after `npm run build:app`
# (finalize-app.sh has put the sidecar back); the release workflow and a local release use this same script.
# Arguments, each optional ("" for the default): the app, the output folder, and the label the image is named after
# (default the app's version; a beta passes its tag's, e.g. 0.2.0-beta.1). Prints the image's path on its last line.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${1:-$ROOT/src-tauri/target/release/bundle/macos/DbaGuy.app}"
OUT="${2:-$ROOT/src-tauri/target/release/bundle/dmg}"

VERSION="${3:-$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")}"
ARCH="$(lipo -archs "$APP/Contents/MacOS/dbaguy")"
[[ "$ARCH" == "arm64" ]] && ARCH="aarch64"  # Tauri's name for it
NAME="DbaGuy_${VERSION}_${ARCH// /-}.dmg"

STAGE="$(mktemp -d)"
MOUNT="$(mktemp -d)"
cleanup() {
  hdiutil detach -quiet "$MOUNT" 2>/dev/null || true
  rm -rf "$STAGE" "$MOUNT"
}
trap cleanup EXIT

ditto "$APP" "$STAGE/DbaGuy.app"  # ditto keeps the sidecar's symlinks and the signature
ln -s /Applications "$STAGE/Applications"
mkdir -p "$OUT"
rm -f "$OUT/$NAME" "$OUT/$NAME.sha256"
hdiutil create -quiet -volname "DbaGuy $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$OUT/$NAME"

hdiutil attach -quiet -nobrowse -readonly -noverify -mountpoint "$MOUNT" "$OUT/$NAME"
codesign --verify --deep --strict "$MOUNT/DbaGuy.app"
hdiutil detach -quiet "$MOUNT"

(cd "$OUT" && shasum -a 256 "$NAME" > "$NAME.sha256")
echo "signature ok inside the image; $(cut -d' ' -f1 "$OUT/$NAME.sha256")"
echo "$OUT/$NAME"
