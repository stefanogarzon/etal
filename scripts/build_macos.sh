#!/bin/bash
# Build the macOS .app and zip it for release. Run ON A MAC:
#   bash scripts/build_macos.sh
# Output: dist/Et al..app  and  dist/EtAl-macos.app.zip
#   (asset name must end in .app.zip for the self-updater)
set -e
cd "$(dirname "$0")/.."

python3 -m venv .venv-build
./.venv-build/bin/pip install --upgrade pip
# pyobjc is needed for the native share sheet (app.py) and the WebKit backend.
./.venv-build/bin/pip install -r requirements.txt pyinstaller \
  pyobjc-framework-Cocoa pyobjc-framework-WebKit

./.venv-build/bin/pyinstaller --noconfirm --clean "Et al..spec"

cd dist
# ditto preserves macOS metadata; matches the extractor used by the self-updater.
ditto -c -k --sequesterRsrc --keepParent "Et al..app" "EtAl-macos.app.zip"
echo ""
echo "Built: dist/Et al..app  and  dist/EtAl-macos.app.zip"
echo "For distribution, codesign + notarize; for personal use the unsigned .app runs."
