; Inno Setup script. Per-user install (no admin rights needed).
; Build (ci.yml and release.yml do this):
;   python packaging\windows\make_version.py && pyinstaller packaging\windows\anidash.spec --noconfirm
;   curl -L -o packaging\windows\MicrosoftEdgeWebview2Setup.exe https://go.microsoft.com/fwlink/p/?LinkId=2124703
;   iscc /DAppVersion=2.0.0 /DAppNumVersion=2.0.0.0 packaging\windows\anidash.iss
; The WebView2 bootstrapper is downloaded at build time, never committed.
#ifndef AppVersion
  #define AppVersion "2.0.0"
#endif
#ifndef AppNumVersion
  #define AppNumVersion "2.0.0.0"
#endif

[Setup]
AppId={{816073A6-1557-4654-9154-9963320704BC}
AppName=anidash
AppVersion={#AppVersion}
AppPublisher=Muhammad Rafae Farrukh
AppPublisherURL=https://github.com/rafaefarrukh/anidash
; Apps & features shows "anidash" with the version in its own column
UninstallDisplayName=anidash
UninstallDisplayIcon={app}\anidash.exe
VersionInfoVersion={#AppNumVersion}
VersionInfoProductTextVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\anidash
DefaultGroupName=anidash
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=anidash-{#AppVersion}-setup
SetupIconFile=..\..\src\anidash\assets\icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; PrepareToInstall asks a running anidash to quit; Restart Manager closes whatever is left.
; We restart it ourselves after a silent upgrade (see [Run]), so Restart Manager doesn't.
CloseApplications=yes
RestartApplications=no

[Tasks]
; Only offered on a fresh install: on upgrade the Run value stays as the user left it (Settings can change it).
Name: "autostart"; Description: "Start anidash in the tray when I sign in"; GroupDescription: "Options:"; Check: not IsUpgrade
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "..\..\dist\anidash\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Evergreen WebView2 bootstrapper (~2 MB): extracted and run only when the runtime is missing
Source: "MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\anidash"; Filename: "{app}\anidash.exe"
Name: "{autodesktop}\anidash"; Filename: "{app}\anidash.exe"; Tasks: desktopicon

[Registry]
; same value anidash itself writes from Settings → Start at login; removed on uninstall in [Code] either way
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "anidash"; \
  ValueData: """{app}\anidash.exe"" service"; Tasks: autostart

[Run]
Filename: "{app}\anidash.exe"; Description: "Open anidash now"; Flags: nowait postinstall skipifsilent
; a silent upgrade (the in-app updater) brings back the instance it stopped
Filename: "{app}\anidash.exe"; Parameters: "service"; Flags: nowait; Check: RestartAfterSilentUpgrade

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
const
  (* WebView2 detection, from Microsoft's "Distribute your app and the WebView2 Runtime",
    section "Detect if a WebView2 Runtime is already installed":
    https://learn.microsoft.com/microsoft-edge/webview2/concepts/distribution#detect-if-a-webview2-runtime-is-already-installed
    Read pv (REG_SZ) under
      HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}  (per-machine, 64-bit Windows;
                                                                       HKLM\SOFTWARE\Microsoft\... on 32-bit Windows)
      HKCU\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}               (per-user)
    Neither present, or empty / 0.0.0.0 = not installed. HKLM32 is the 32-bit view, i.e. exactly those two HKLM paths.
    Missing runtime: run the Evergreen bootstrapper with "/silent /install" (same doc, "Online-only deployment"); run
    unelevated, as here, it installs per-user ("Installing the Runtime as per-machine or per-user"). The bootstrapper
    comes from the doc's "Get the Link" fwlink, https://go.microsoft.com/fwlink/p/?LinkId=2124703, which Microsoft's
    WebView2Samples (WV2DeploymentWiXCustomActionSample) also uses. *)
  WV2Key = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WV2ConsumerUrl = 'https://developer.microsoft.com/microsoft-edge/webview2/consumer/';
  UninstKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{816073A6-1557-4654-9154-9963320704BC}_is1';
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

var
  Upgrade, WasRunning, RemoveData: Boolean;

function WV2Present(Root: Integer; SubKey: String): Boolean;
var
  V: String;
begin
  Result := RegQueryStringValue(Root, SubKey, 'pv', V) and (Trim(V) <> '') and (Trim(V) <> '0.0.0.0');
end;

function WebView2Installed: Boolean;
begin
  Result := WV2Present(HKLM32, 'SOFTWARE\' + WV2Key) or WV2Present(HKCU, 'Software\' + WV2Key);
end;

function InitializeSetup: Boolean;
begin
  Upgrade := RegKeyExists(HKCU, UninstKey);
  Result := True;
end;

function IsUpgrade: Boolean;
begin
  Result := Upgrade;
end;

function RestartAfterSilentUpgrade: Boolean;
begin
  Result := WasRunning and WizardSilent;
end;

{ anidash.exe quit --wait: 0 = it was running and has stopped, 1 = it wasn't running, 2 = still running after 15 s
  (Restart Manager then closes it). }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Exe: String;
  Code: Integer;
begin
  Exe := ExpandConstant('{app}\anidash.exe');
  WasRunning := FileExists(Exe) and Exec(Exe, 'quit --wait', '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
  Result := '';
end;

procedure InstallWebView2;
var
  Code: Integer;
begin
  if WebView2Installed then begin
    Log('WebView2 Runtime present');
    Exit;
  end;
  WizardForm.StatusLabel.Caption := 'Installing Microsoft Edge WebView2 Runtime...';
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  if Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'), '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Log(Format('WebView2 bootstrapper exited with %d', [Code]))
  else
    Log('WebView2 bootstrapper did not start');
  if not WebView2Installed then  { the registry, not the exit code, decides }
    SuppressibleMsgBox('anidash could not install the Microsoft Edge WebView2 Runtime (it needs an internet connection).' + #13#10#13#10 +
      'anidash still works: the dashboard opens in your web browser instead of its own window, and AniList login ' +
      'asks you to paste a token.' + #13#10#13#10 + 'To get the window later, install WebView2 from ' + WV2ConsumerUrl,
      mbInformation, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    InstallWebView2;
end;

function HasParam(Name: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), Name) = 0 then
      Result := True;
end;

{ A process that just exited can keep its exe and DLLs locked for a moment, and the uninstaller deletes files
  right after usUninstall: wait (up to 15 s) until anidash.exe itself can be deleted. }
procedure WaitUntilDeletable(Exe: String);
var
  I: Integer;
begin
  for I := 1 to 60 do begin
    if (not FileExists(Exe)) or DeleteFile(Exe) then
      Exit;
    Sleep(250);
  end;
  Log('anidash.exe is still in use');
end;

{ "Also remove my settings, database and saved logins" (default: no; /REMOVEDATA answers yes for silent
  uninstalls). Saved logins live in Credential Manager, so `anidash forget --yes` deletes those (and stops
  anidash) before the folder goes. It returns 3 when the library is inside the data folder: then nothing is
  removed. The library folder itself is never touched. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Exe, Data: String;
  Code: Integer;
begin
  Exe := ExpandConstant('{app}\anidash.exe');
  Data := ExpandConstant('{localappdata}\anidash');
  if CurUninstallStep = usUninstall then begin
    RemoveData := HasParam('/REMOVEDATA') or (SuppressibleMsgBox('Also remove my settings, database and saved logins?' + #13#10#13#10 +
      'This deletes ' + Data + ' and anidash''s entries in Windows Credential Manager. ' +
      'Your anime library folder is not touched either way.' + #13#10#13#10 +
      'Choose No to keep them, so a reinstall picks up where you left off.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES);
    Exec(Exe, 'quit --wait', '', SW_HIDE, ewWaitUntilTerminated, Code);
    if RemoveData then begin
      if not Exec(Exe, 'forget --yes', '', SW_HIDE, ewWaitUntilTerminated, Code) then
        Code := -1;
      Log(Format('anidash forget exited with %d', [Code]));
      if Code <> 0 then begin
        RemoveData := False;
        if Code = 3 then
          SuppressibleMsgBox('Your saved logins were removed. Your anime library is inside ' + Data +
            ', so that folder was kept.', mbInformation, MB_OK, IDOK)
        else
          SuppressibleMsgBox('anidash could not remove your settings. They are kept in ' + Data +
            '; delete that folder yourself if you want them gone.', mbInformation, MB_OK, IDOK);
      end;
    end;
    WaitUntilDeletable(Exe);
  end;
  if CurUninstallStep = usPostUninstall then begin
    RegDeleteValue(HKCU, RunKey, 'anidash');  { also when Settings, not this installer, turned autostart on }
    if RemoveData then
      DelTree(Data, True, True, True);
  end;
end;
