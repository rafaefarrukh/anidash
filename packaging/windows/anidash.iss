; Inno Setup script. Per-user install (no admin rights needed).
; Build: python packaging\windows\make_version.py && pyinstaller packaging\windows\anidash.spec --noconfirm
;        iscc /DAppVersion=2.0.0 packaging\windows\anidash.iss
#ifndef AppVersion
  #define AppVersion "2.0.0"
#endif

[Setup]
AppId={{816073A6-1557-4654-9154-9963320704BC}
AppName=anidash
AppVersion={#AppVersion}
AppPublisher=Muhammad Rafae Farrukh
AppPublisherURL=https://github.com/rafaefarrukh/anidash
DefaultDirName={localappdata}\Programs\anidash
DefaultGroupName=anidash
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=anidash-{#AppVersion}-setup
SetupIconFile=..\..\src\anidash\assets\icon.ico
UninstallDisplayIcon={app}\anidash.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "autostart"; Description: "Start anidash in the tray when I sign in"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "..\..\dist\anidash\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\anidash"; Filename: "{app}\anidash.exe"
Name: "{autodesktop}\anidash"; Filename: "{app}\anidash.exe"; Tasks: desktopicon

[Registry]
; same value anidash itself writes from Settings → Start at login
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "anidash"; \
  ValueData: """{app}\anidash.exe"" service"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\anidash.exe"; Description: "Open anidash now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\anidash.exe"; Parameters: "quit"; Flags: runhidden; RunOnceId: "QuitAnidash"

[UninstallDelete]
; settings and the database live in %LOCALAPPDATA%\anidash and are kept, so a reinstall picks up where you left off
Type: filesandordirs; Name: "{app}"
