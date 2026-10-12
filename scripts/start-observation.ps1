param(
    [string]$PhoenixUrl = 'http://127.0.0.1:6006',
    [int]$Port = 8000,
    [string]$Store = '',
    [string]$Catalog = ''
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already listening; no existing process was stopped."
}
$LaunchArgs = @('-m', 'm1_local.cli', 'observe', '--phoenix-url', $PhoenixUrl, '--port', "$Port")
if ($Store) { $LaunchArgs += @('--store', $Store) }
if ($Catalog) { $LaunchArgs += @('--catalog', $Catalog) }
& $Python @LaunchArgs
exit $LASTEXITCODE
