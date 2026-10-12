# Dev helper: restart the Studio server on a port with a given data dir (only stops a matching m1_local studio process).
param([int]$Port = 8100, [string]$DataDir = 'data/studio')
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($c) {
  $p = Get-CimInstance Win32_Process -Filter "ProcessId = $($c.OwningProcess)"
  if ($p.CommandLine -notmatch 'm1_local.cli studio') { throw "Port $Port is used by another process; not stopping it." }
  $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($p.ParentProcessId)"
  Stop-Process -Id $p.ProcessId -Force
  if ($parent -and $parent.CommandLine -match 'm1_local.cli studio') { Stop-Process -Id $parent.ProcessId -Force }
  Start-Sleep -Milliseconds 500
}
$Python = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
$Log = Join-Path $ProjectRoot "data/logs/studio-$Port"
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot 'data/logs') | Out-Null
$proc = Start-Process -FilePath $Python -ArgumentList '-m','m1_local.cli','studio','--port',"$Port",'--data-dir',$DataDir -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput "$Log.out.log" -RedirectStandardError "$Log.err.log"
Write-Output "Studio starting as PID $($proc.Id) on http://127.0.0.1:$Port (data: $DataDir)"
