$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ExpectedPhoenix = (Resolve-Path -LiteralPath (Join-Path $ProjectRoot '.venv-phoenix/Scripts/python.exe')).Path
$ExpectedLauncher = Join-Path $ProjectRoot 'scripts/serve_phoenix.py'
$PidFile = Join-Path $ProjectRoot 'data/pids/phoenix.pid'
$OwnerFile = Join-Path $ProjectRoot 'data/pids/phoenix.owner.json'
if (-not (Test-Path -LiteralPath $PidFile)) { Write-Output 'No project Phoenix PID file found.'; exit 0 }
$PhoenixPid = [int](Get-Content -LiteralPath $PidFile -Raw)
$Process = Get-Process -Id $PhoenixPid -ErrorAction SilentlyContinue
if (-not $Process) {
  Remove-Item -LiteralPath $PidFile
  if (Test-Path -LiteralPath $OwnerFile) { Remove-Item -LiteralPath $OwnerFile }
  Write-Output 'Stale PID file removed.'
  exit 0
}
if ($Process.Path -ne $ExpectedPhoenix) { throw "PID $PhoenixPid is not the project Phoenix executable; refusing to stop it." }
if (-not (Test-Path -LiteralPath $OwnerFile)) { throw 'Phoenix ownership receipt is missing; refusing to stop an unverified process.' }
$Owner = Get-Content -LiteralPath $OwnerFile -Raw -Encoding UTF8 | ConvertFrom-Json
$Details = Get-CimInstance Win32_Process -Filter "ProcessId = $PhoenixPid"
if ($Owner.pid -ne $PhoenixPid -or $Owner.startedUtcTicks -ne $Process.StartTime.ToUniversalTime().Ticks -or $Owner.launcher -ne $ExpectedLauncher -or -not $Details.CommandLine.Contains('"' + $ExpectedLauncher + '"')) {
  throw 'Phoenix process identity does not match its ownership receipt; refusing to stop it.'
}
& taskkill.exe /PID $PhoenixPid /T /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to stop the owned Phoenix process tree.' }
Remove-Item -LiteralPath $PidFile
Remove-Item -LiteralPath $OwnerFile
Write-Output "Stopped project Phoenix PID $PhoenixPid"
