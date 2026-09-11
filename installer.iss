; NiYoj installer - ships the built app folder, no source.
; Build:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
; Output: dist\niyoj-setup-<version>.exe
;
; Per-user install (no admin prompt); apps.json then sits next to the exe.

#define AppName    "NiYoj"
#define AppVersion "1.1.19"
#define AppExe     "niyoj.exe"

[Setup]
AppId={{7C1B4E2A-9F3D-4A6B-8E15-3D9C2F0A5B71}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=bodish-coder
AppPublisherURL=https://github.com/bodish-coder/niyoj
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist
OutputBaseFilename=niyoj-setup-{#AppVersion}
SetupIconFile=niyoj.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
VersionInfoVersion={#AppVersion}
; Update in place: shut the running app, keep apps.json / logs already in {app}.
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes

[Tasks]
Name: desktopicon; Description: "Create a &desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\niyoj\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md";  DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExe}"
Name: "{userdesktop}\{#AppName}";     Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

; apps.json (your servers and apps) is left behind on uninstall on purpose -
; reinstalling keeps your setup. Delete {app} by hand to be rid of it.
