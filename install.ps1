<#
.SYNOPSIS
  Per-user ChatBridge install for Windows (no administrator rights needed).

.DESCRIPTION
  Creates a private virtual environment, installs ChatBridge into it, adds the `chatbridge` and `chatbridge-gui`
  commands to your PATH, a Start Menu shortcut, and (optionally) a hidden background auto-sync that starts at logon.
  Your chats, Claude data and settings are never touched by installing or uninstalling.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1              # install
  powershell -ExecutionPolicy Bypass -File install.ps1 -Service     # install + auto-sync at logon
  powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall   # remove everything this script created
#>
[CmdletBinding()]
param(
  [switch]$Uninstall,
  [switch]$Service,               # also start `chatbridge watch` hidden at every logon
  [string]$Prefix = (Join-Path $env:LOCALAPPDATA 'Programs\ChatBridge'),
  [string]$StartMenuDir = (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'),
  [string]$TaskName = 'ChatBridge Auto-Sync',
  [switch]$NoPath,                # do not touch the PATH
  [switch]$NoShortcuts,           # no Start Menu entry
  [string]$Python                 # python.exe to use (default: the newest Python 3.11+ found)
)

$ErrorActionPreference = 'Stop'
$Prefix = [IO.Path]::GetFullPath($Prefix)
$StartMenuDir = [IO.Path]::GetFullPath($StartMenuDir)
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Prefix 'venv'
$Bin = Join-Path $Prefix 'bin'
$Icon = Join-Path $Prefix 'chatbridge.ico'
$Shortcut = Join-Path $StartMenuDir 'ChatBridge.lnk'
$StartupLink = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\ChatBridge Auto-Sync.lnk'
$LogDir = Join-Path $env:LOCALAPPDATA 'ChatBridge'

function Remove-FromUserPath([string]$dir) {
  $current = [Environment]::GetEnvironmentVariable('Path', 'User')
  if (-not $current) { return }
  $kept = ($current -split ';' | Where-Object { $_ -and ($_.TrimEnd('\') -ne $dir.TrimEnd('\')) }) -join ';'
  if ($kept -ne $current) { [Environment]::SetEnvironmentVariable('Path', $kept, 'User') }
}

function Stop-AutoSync {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  }
  Remove-Item -LiteralPath $StartupLink -Force -ErrorAction SilentlyContinue
}

function New-Shortcut([string]$path, [string]$target, [string]$arguments, [string]$description) {
  $shell = New-Object -ComObject WScript.Shell
  $link = $shell.CreateShortcut($path)
  $link.TargetPath = $target
  $link.Arguments = $arguments
  $link.Description = $description
  $link.WorkingDirectory = $env:USERPROFILE
  if (Test-Path $Icon) { $link.IconLocation = $Icon }
  $link.Save()
}

if ($Uninstall) {
  # Removes only what this script created; chats, Claude data, settings and the link database are untouched.
  Stop-AutoSync
  Get-Process -Name 'pythonw', 'python' -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($Venv, [StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force
  Remove-Item -LiteralPath $Shortcut -Force -ErrorAction SilentlyContinue
  Remove-FromUserPath $Bin
  if (Test-Path $Prefix) { Remove-Item -LiteralPath $Prefix -Recurse -Force }
  Write-Host "Uninstalled ChatBridge from $Prefix (your data in $LogDir was left alone)."
  exit 0
}

# --- find Python 3.11+ with Tk ----------------------------------------------------------------------------------
# Native commands that fail print to stderr, which Windows PowerShell 5.1 turns into errors under -ErrorAction Stop;
# probing for interpreters must be quiet, so it runs with errors silenced.
function Invoke-Probe([string]$exe, [string[]]$arguments) {
  $saved = $ErrorActionPreference
  $ErrorActionPreference = 'SilentlyContinue'
  try {
    $out = & $exe @arguments 2>$null
    if ($LASTEXITCODE -eq 0) { return ($out | Select-Object -First 1) }
    return $null
  } finally { $ErrorActionPreference = $saved }
}

function Test-Python([string]$exe) {
  $out = Invoke-Probe $exe @('-c', "import sys, tkinter; print('%d.%d' % sys.version_info[:2])")
  if (-not $out) { return $false }
  $v = [version]$out
  return ($v.Major -eq 3 -and $v.Minor -ge 11)
}

$candidates = @()
if ($Python) { $candidates += $Python }
if (Get-Command py.exe -ErrorAction SilentlyContinue) {
  foreach ($minor in 14, 13, 12, 11) {
    $found = Invoke-Probe 'py.exe' @("-3.$minor", '-c', 'import sys; print(sys.executable)')
    if ($found) { $candidates += $found }
  }
}
$onPath = Get-Command python.exe -ErrorAction SilentlyContinue
if ($onPath) { $candidates += $onPath.Source }
$PythonExe = $candidates | Where-Object { $_ -and (Test-Python $_) } | Select-Object -First 1
if (-not $PythonExe) {
  Write-Error "Python 3.11 or newer (with Tk, the default in the python.org installer) was not found. Install it from https://www.python.org/downloads/windows/ or run: winget install Python.Python.3.12"
}
Write-Host "Using $PythonExe"

# --- venv + package ----------------------------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $Prefix, $Bin | Out-Null
if (Test-Path $Venv) { Stop-AutoSync; Remove-Item -LiteralPath $Venv -Recurse -Force }
& $PythonExe -m venv $Venv
if ($LASTEXITCODE -ne 0) { Write-Error 'Could not create the virtual environment.' }
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
$VenvPythonW = Join-Path $Venv 'Scripts\pythonw.exe'
& $VenvPython -m pip install --quiet --disable-pip-version-check --no-warn-script-location $Here
if ($LASTEXITCODE -ne 0) { Write-Error 'pip could not install ChatBridge.' }

# Command shims that stay valid wherever the venv lives.
Set-Content -LiteralPath (Join-Path $Bin 'chatbridge.cmd') -Encoding ASCII -Value "@echo off`r`n`"$VenvPython`" -m chatbridge %*"
Set-Content -LiteralPath (Join-Path $Bin 'chatbridge-gui.cmd') -Encoding ASCII -Value "@echo off`r`nstart `"`" `"$VenvPythonW`" -m chatbridge.tkgui %*"
$packaged = & $VenvPython -c "import importlib.resources as r; print(r.files('chatbridge').joinpath('data/chatbridge.ico'))"
Copy-Item -LiteralPath $packaged -Destination $Icon -Force

if (-not $NoPath) {
  $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  if (-not (($userPath -split ';') -contains $Bin)) {
    [Environment]::SetEnvironmentVariable('Path', (($userPath, $Bin | Where-Object { $_ }) -join ';'), 'User')
  }
}

if (-not $NoShortcuts) {
  New-Item -ItemType Directory -Force -Path $StartMenuDir | Out-Null
  New-Shortcut $Shortcut $VenvPythonW '-m chatbridge.tkgui' 'Two-way chat history sync between Cursor and Claude'
}

# --- optional auto-sync at logon ---------------------------------------------------------------------------------
if ($Service) {
  Stop-AutoSync
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  $log = Join-Path $LogDir 'watch.log'
  $arguments = "-m chatbridge watch --interval 20 --log-file `"$log`""
  try {
    $action = New-ScheduledTaskAction -Execute $VenvPythonW -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
      -MultipleInstances IgnoreNew -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -User $env:USERNAME `
      -Description 'Keeps linked Cursor and Claude conversations up to date (ChatBridge)' | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Auto-sync enabled (scheduled task '$TaskName', log: $log)."
  } catch {
    Write-Warning "Could not create a scheduled task ($($_.Exception.Message)); using the Startup folder instead."
    New-Shortcut $StartupLink $VenvPythonW $arguments 'ChatBridge auto-sync'
    Start-Process -FilePath $VenvPythonW -ArgumentList $arguments -WindowStyle Hidden
    Write-Host "Auto-sync enabled (Startup shortcut, log: $log)."
  }
}

Write-Host ''
Write-Host 'Installed ChatBridge.'
if (-not $NoShortcuts) { Write-Host "  Start Menu: ChatBridge" }
Write-Host "  Commands : chatbridge doctor | chatbridge list | chatbridge-gui   (open a NEW terminal so PATH is refreshed)"
Write-Host "  Remove   : powershell -ExecutionPolicy Bypass -File `"$Here\install.ps1`" -Uninstall"
