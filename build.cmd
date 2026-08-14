@echo off
REM Rebuild niyoj.exe after editing deployer.py or ui.html.
cd /d "%~dp0"
taskkill /IM niyoj.exe /F >nul 2>&1
python -m PyInstaller --onefile --noconsole --name niyoj ^
  --icon "%~dp0niyoj.ico" ^
  --add-data "%~dp0ui.html;." ^
  --exclude-module pygame --exclude-module numpy --exclude-module PIL ^
  --exclude-module tkinter --exclude-module matplotlib --exclude-module pandas ^
  --exclude-module scipy --exclude-module PySide6 --exclude-module PyQt5 ^
  --exclude-module PyQt6 --exclude-module qtpy --exclude-module cryptography ^
  --exclude-module setuptools --exclude-module pkg_resources ^
  --distpath . --workpath build --specpath build deployer.py

REM Installer (optional): only if Inno Setup is installed.
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" ("%ISCC%" /Q "%~dp0installer.iss" && echo Installer: dist\) else (
  echo Skipped installer ^(Inno Setup 6 not found^).
)
