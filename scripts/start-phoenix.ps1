[CmdletBinding()]
param([string]$DataDirectory)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Phoenix = Join-Path $ProjectRoot '.venv-phoenix/Scripts/python.exe'
$Launcher = Join-Path $ProjectRoot 'scripts/serve_phoenix.py'
$PidDir = Join-Path $ProjectRoot 'data/pids'
$PidFile = Join-Path $PidDir 'phoenix.pid'
$OwnerFile = Join-Path $PidDir 'phoenix.owner.json'
$WorkingDir = if ($DataDirectory) { [IO.Path]::GetFullPath($DataDirectory) } else { Join-Path $ProjectRoot 'data/phoenix' }
$LogDir = Join-Path $ProjectRoot 'data/logs'
$StdoutLog = Join-Path $LogDir 'phoenix.stdout.log'
$StderrLog = Join-Path $LogDir 'phoenix.stderr.log'
New-Item -ItemType Directory -Force -Path $PidDir, $WorkingDir, $LogDir | Out-Null

$Existing = Get-NetTCPConnection -LocalPort 6006 -State Listen -ErrorAction SilentlyContinue
if ($Existing) { throw "Port 6006 is already listening (PID $($Existing.OwningProcess)); refusing to start another Phoenix." }
if (-not (Test-Path -LiteralPath $Phoenix)) { throw "Phoenix executable not found: $Phoenix" }

$PhoenixEnv = @{
  PHOENIX_HOST = '127.0.0.1'
  PHOENIX_PORT = '6006'
  PHOENIX_WORKING_DIR = $WorkingDir
  PHOENIX_ALLOW_EXTERNAL_RESOURCES = 'false'
}
$OldValues = @{}
foreach ($Entry in $PhoenixEnv.GetEnumerator()) {
  $OldValues[$Entry.Key] = [Environment]::GetEnvironmentVariable($Entry.Key, 'Process')
  [Environment]::SetEnvironmentVariable($Entry.Key, $Entry.Value, 'Process')
}
try {
  $Process = Start-Process -FilePath $Phoenix -ArgumentList ('"' + $Launcher + '"') -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog
  @{ pid = $Process.Id; startedUtcTicks = $Process.StartTime.ToUniversalTime().Ticks; launcher = $Launcher } | ConvertTo-Json | Set-Content -LiteralPath $OwnerFile -Encoding UTF8
  Set-Content -LiteralPath $PidFile -Value $Process.Id -Encoding ascii
  Write-Output "Phoenix starting as PID $($Process.Id) at http://127.0.0.1:6006 (HTTP OTLP enabled; gRPC disabled)"
} finally {
  foreach ($Entry in $OldValues.GetEnumerator()) { [Environment]::SetEnvironmentVariable($Entry.Key, $Entry.Value, 'Process') }
}
