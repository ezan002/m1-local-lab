$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
$Existing = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($Existing) { throw "Port 8000 is already listening (PID $($Existing.OwningProcess))." }
& $Python -m m1_local.cli serve

