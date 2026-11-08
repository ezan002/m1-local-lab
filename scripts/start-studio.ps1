# Start Agent Studio in the foreground (Ctrl+C to stop). No Node.js needed: the built UI is in web/studio.
param([int]$Port = 8100, [string]$DataDir = '', [switch]$Demo)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw "Missing $Python. Run scripts/install.ps1 first." }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already listening; no existing process was stopped."
}
if (-not $env:OPENAI_API_KEY) { Write-Warning 'OPENAI_API_KEY is not set: agents can only use the deterministic provider.' }
$LaunchArgs = @('-m', 'm1_local.cli', 'studio', '--port', "$Port")
if ($DataDir) { $LaunchArgs += @('--data-dir', $DataDir) }
if ($Demo) { $LaunchArgs += @('--demo') }  # bundled demo runs (demo-data/, copied to data/studio-demo/)
Write-Output "Agent Studio: http://127.0.0.1:$Port"
Set-Location $ProjectRoot
& $Python @LaunchArgs
exit $LASTEXITCODE
