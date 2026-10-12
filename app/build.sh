#!/bin/bash
# build.sh — build Filmstamp.app (SwiftUI front end for the filmstamp CLI)
#
# One Sources/main.swift, swiftc, a hand-assembled bundle, ad-hoc
# signature. No Xcode project. The app holds no stamping logic and no Python: it runs the
# repo's own `filmstamp` launcher, so the CLI and the app cannot disagree.
#
# Usage:   ./app/build.sh        (from anywhere)
# Result:  /Applications/Filmstamp.app — the only copy. Building happens in a throwaway
#          temp folder, so no second app is left in the repo.
set -euo pipefail

APP_NAME="Filmstamp"
HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT
BUNDLE="${BUILD}/${APP_NAME}.app"
DEST="/Applications/${APP_NAME}.app"

echo "Building ${APP_NAME} ..."
mkdir -p "${BUILD}"
# -swift-version 5: the sources are written for Swift 5 concurrency rules.
swiftc -parse-as-library -O -swift-version 5 -o "${BUILD}/${APP_NAME}" "${HERE}/Sources/main.swift"

rm -rf "${BUNDLE}"
mkdir -p "${BUNDLE}/Contents/MacOS" "${BUNDLE}/Contents/Resources"
cp "${BUILD}/${APP_NAME}" "${BUNDLE}/Contents/MacOS/${APP_NAME}"
cp "${HERE}/Info.plist"   "${BUNDLE}/Contents/Info.plist"
cp "${HERE}/Filmstamp.icns" "${BUNDLE}/Contents/Resources/Filmstamp.icns"
codesign --force --deep --sign - "${BUNDLE}" >/dev/null 2>&1 || echo "note: ad-hoc codesign skipped"

# Quit a running copy, replace it, and drop the stale repo build folder from earlier versions.
osascript -e "tell application \"${APP_NAME}\" to quit" >/dev/null 2>&1 || true
rm -rf "${DEST}" "${HERE}/build"
cp -R "${BUNDLE}" "${DEST}"
echo "Done: ${DEST}"
echo "Run:  open \"${DEST}\""
