@echo off
REM Build the Android apk. Needs Android Studio installed (for its JDK 17) and
REM the SDK path in local.properties.
cd /d "%~dp0"
if not exist local.properties (
  echo sdk.dir=%LOCALAPPDATA:\=/%/Android/Sdk> local.properties
)
if "%JAVA_HOME%"=="" set "JAVA_HOME=%ProgramFiles%\Android\Android Studio\jbr"
python ..\deployer.py --scripts || exit /b 1
call gradlew.bat :app:testDebugUnitTest :app:assembleRelease %* || exit /b 1
echo.
echo apk: %~dp0app\build\outputs\apk\release\
dir /b app\build\outputs\apk\release\*.apk
