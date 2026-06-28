#!/bin/bash
# Create a GitHub Release and upload the built assets.
#   bash scripts/release.sh 0.2.0
# Requires: gh CLI authenticated (`gh auth login`), and the assets already built
# in dist/. The tag MUST equal __version__ in server.py (the self-updater compares
# the running version against the latest release tag).
set -e
VERSION="${1:?usage: release.sh <version, e.g. 0.2.0>}"
TAG="v$VERSION"
cd "$(dirname "$0")/.."

ASSETS=()
[ -f "dist/EtAl.exe" ]            && ASSETS+=("dist/EtAl.exe")            # Windows
[ -f "dist/EtAl-macos.app.zip" ] && ASSETS+=("dist/EtAl-macos.app.zip")  # macOS
if [ ${#ASSETS[@]} -eq 0 ]; then
  echo "No built assets in dist/. Run scripts/build_windows.ps1 and/or scripts/build_macos.sh first."
  exit 1
fi

echo "Releasing $TAG with: ${ASSETS[*]}"
gh release create "$TAG" "${ASSETS[@]}" \
  --repo stefanogarzon/etal \
  --title "Et al. $TAG" \
  --notes "Et al. $TAG"
echo "Done. Installed apps on an older version will now offer this update."
