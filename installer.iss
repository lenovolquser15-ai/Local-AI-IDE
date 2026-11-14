; LocalHarness installer — per-user, no admin rights.
; Build: LH_BUILD_DIST=dist/release build-exe.cmd, then ISCC.exe installer.iss
; Runtime data (data/, work/, config.json) is never touched on upgrade or uninstall.

#define MyAppName "LocalHarness"
#define MyAppVersion "1.0.4"
#define MyAppPublisher "LocalHarness contributors"
#define MyAppURL "https://github.com/lenovolquser15-ai/Local-AI-IDE"
#define MyAppExeName "LocalHarness.exe"

[Setup]
AppId={{7E5A3C91-B2D4-4F6A-8E1D-0C5B9A3F7E21}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=LocalHarness-Setup-v{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
VersionInfoVersion={#MyAppVersion}.0
VersionInfoDescription={#MyAppName} Setup
UninstallDisplayName={#MyAppName}
CloseApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"
Name: "startup"; Description: "Start LocalHarness with Windows (tray, no browser)"; Flags: unchecked

[Files]
Source: "dist\release\LocalHarness\*"; DestDir: "{app}"; Excludes: "__pycache__\*,*.pyc"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Same command format as harness/desktop.py _autostart_command(), so the in-app
; "Запускать с Windows" checkbox recognizes and can remove this value.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "LocalHarness"; ValueData: """{app}\{#MyAppExeName}"" serve --tray --background"; Tasks: startup; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent