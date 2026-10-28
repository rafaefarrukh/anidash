# CI only (ci.yml, windows-build): install the built setup.exe silently, selftest the installed app, upgrade
# over a running copy, then uninstall keeping and removing user data. Needs `pip install .` (for keyring).
param([Parameter(Mandatory)] [string] $Setup, [Parameter(Mandatory)] [string] $Version)
$ErrorActionPreference = "Stop"
$app = Join-Path $env:LOCALAPPDATA "Programs\anidash"
$exe = Join-Path $app "anidash.exe"
$data = Join-Path $env:LOCALAPPDATA "anidash"
$uninstKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{816073A6-1557-4654-9154-9963320704BC}_is1"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$shortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\anidash.lnk"

# Start-Process -Wait also waits for children (the service setup starts); WaitForExit waits for the process only.
function Run([string] $file, [string[]] $argv) {
  $p = Start-Process $file -ArgumentList $argv -PassThru
  $null = $p.Handle  # keeps ExitCode readable
  if (-not $p.WaitForExit(180000)) { throw "$file $argv timed out" }
  return $p.ExitCode
}
function Install([string] $name) {
  $log = Join-Path $env:RUNNER_TEMP "$name.log"
  $rc = Run $Setup @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=`"$log`"")
  Get-Content $log | Select-String -Pattern "WebView2|anidash forget|Exception|Error" | ForEach-Object { "  log: $_" }
  if ($rc -ne 0) { Get-Content $log; throw "setup exited $rc" }
  if (-not (Test-Path $exe)) { throw "anidash.exe not installed" }
}
function Procs { Get-CimInstance Win32_Process -Filter "Name='anidash.exe'" | Format-Table ProcessId, ParentProcessId, CommandLine -AutoSize | Out-String -Width 400 }
function Uninstall([string[]] $extra) {
  "   anidash processes before uninstall:"; Procs
  $log = Join-Path $env:RUNNER_TEMP "uninstall.log"
  $rc = Run (Join-Path $app "unins000.exe") (@("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=`"$log`"") + $extra)
  if ($rc -ne 0) { throw "uninstaller exited $rc" }
  # the uninstaller re-launches itself from %TEMP% and returns at once: wait for the real one
  for ($i = 0; $i -lt 120 -and ((Test-Path $app) -or (Test-Path $uninstKey)); $i++) { Start-Sleep -Milliseconds 500 }
  if (Test-Path $log) { Get-Content $log | Select-String "forget" | ForEach-Object { "  log: $_" } }
  if (Test-Path $app) {
    "   still running:"; Procs
    if (Test-Path $log) { Get-Content $log }
    Get-Content (Join-Path $data "Logs\anidash.log") -Tail 40 -ErrorAction SilentlyContinue
    throw "app folder still there"
  }
  if (Test-Path $uninstKey) { throw "uninstall entry still registered" }
  if ((Get-ItemProperty $runKey -ErrorAction SilentlyContinue).anidash) { throw "autostart value left behind" }
}
function Health { try { return Invoke-RestMethod http://127.0.0.1:8787/api/health -TimeoutSec 2 } catch { return $null } }
$runtimeJson = Join-Path $data "run\runtime.json"
# health can answer just before runtime.json is written
function WaitUp { for ($i = 0; $i -lt 60; $i++) { if ((Health).app -eq "anidash" -and (Test-Path $runtimeJson)) { return } Start-Sleep -Milliseconds 500 }; throw "anidash did not come up" }
function RunningPid { (Get-Content $runtimeJson -Raw | ConvertFrom-Json).pid }
function Secret { python -c "import keyring; print(keyring.get_password('anidash', 'qbit_password') or '')" }

Remove-Item $runtimeJson -ErrorAction SilentlyContinue  # stale: the smoke test force-killed its instance

"== fresh install"
Install "install1"
$u = Get-ItemProperty $uninstKey
if ($u.DisplayName -ne "anidash" -or $u.DisplayVersion -ne $Version) { throw "Apps & features shows '$($u.DisplayName)' '$($u.DisplayVersion)'" }
if (-not (Test-Path $shortcut)) { throw "no Start-menu shortcut" }
if ((Get-ItemProperty $runKey).anidash -ne "`"$exe`" service") { throw "autostart task not applied" }
$out = Join-Path $env:RUNNER_TEMP "selftest-installed.json"
$rc = Run $exe @("selftest", $out)
Get-Content $out
if ($rc -ne 0) { throw "selftest failed from the installed location" }
$r = Get-Content $out -Raw | ConvertFrom-Json
if ($r.checks.window.detail -ne "winforms/edgechromium") { throw "window backend is not WebView2" }
if (-not $r.checks.webview2.ok) { throw "WebView2 registry detection failed on a runner that has it" }

"== upgrade over a running copy keeps the autostart choice"
Start-Process $exe -ArgumentList "service", "--no-tray"
WaitUp
$before = RunningPid
$null = Run $exe @("autostart", "off")
Install "install2"
if ((Get-ItemProperty $runKey -ErrorAction SilentlyContinue).anidash) { throw "upgrade turned autostart back on" }
WaitUp  # the silent upgrade restarts the instance it stopped
$after = RunningPid
if ($after -eq $before) { throw "the old instance was not stopped" }
"   restarted: pid $before -> $after"

"== uninstall keeps user data by default"
Uninstall @()
if (Health) { throw "anidash still running after uninstall" }
if (-not (Test-Path (Join-Path $data "anidash.db"))) { throw "user data was removed without asking" }

"== uninstall /REMOVEDATA removes it and the saved logins"
Install "install3"
python -c "import keyring; keyring.set_password('anidash', 'qbit_password', 'ci-secret')"
if ((Secret) -ne "ci-secret") { throw "could not plant a test secret" }
Uninstall @("/REMOVEDATA")
if (Test-Path $data) { throw "$data still there" }
if (Secret) { throw "saved login left in Credential Manager" }
"installer test passed"
