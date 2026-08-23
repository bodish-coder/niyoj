#!/usr/bin/env bash
# Build a NiYoj binary for macOS or Linux. Windows users: run build.cmd instead.
set -euo pipefail
cd "$(dirname "$0")"

python3 deployer.py --bump          # every build gets a fresh version
python3 deployer.py --scripts       # refresh the copy the android app ships

EXTRA=()
if [[ "$(uname -s)" == "Darwin" ]]; then
  EXTRA+=(--windowed)            # .app bundle, no terminal behind it
fi

python3 -m PyInstaller --onefile --name niyoj \
  --add-data "ui.html:." \
  --exclude-module tkinter --exclude-module numpy --exclude-module PIL \
  --exclude-module matplotlib --exclude-module pandas --exclude-module scipy \
  --exclude-module PySide6 --exclude-module PyQt5 --exclude-module PyQt6 \
  --exclude-module setuptools --exclude-module pkg_resources \
  "${EXTRA[@]}" \
  --distpath . --workpath build --specpath build deployer.py

echo "built ./niyoj — keep apps.json beside it"
