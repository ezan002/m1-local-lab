$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ExpectedPhoenix = (Resolve-Path -LiteralPath (Join-Path $ProjectRoot '.venv-phoenix/Scripts/phoenix.exe')).Path
$PidFile = Join-Path $ProjectRoot 'data/pids/phoenix.pid'
if (-not (Test-Path -LiteralPath $PidFile)) { Write-Output 'No project Phoenix PID file found.'; exit 0 }
$PhoenixPid = [int](Get-Content -LiteralPath $PidFile -Raw)
$Process = Get-Process -Id $PhoenixPid -ErrorAction SilentlyContinue
if (-not $Process) { Remove-Item -LiteralPath $PidFile; Write-Output 'Stale PID file removed.'; exit 0 }
if ($Process.Path -ne $ExpectedPhoenix) { throw "PID $PhoenixPid is not the project Phoenix executable; refusing to stop it." }
Stop-Process -Id $PhoenixPid
Remove-Item -LiteralPath $PidFile
Write-Output "Stopped project Phoenix PID $PhoenixPid"
