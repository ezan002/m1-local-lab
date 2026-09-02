$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SystemPython = 'C:/Users/wangy/AppData/Local/Programs/Python/Python311/python.exe'
$AppPython = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
$PhoenixPython = Join-Path $ProjectRoot '.venv-phoenix/Scripts/python.exe'

if (-not (Test-Path -LiteralPath $AppPython)) { & $SystemPython -m venv (Join-Path $ProjectRoot '.venv') }
if (-not (Test-Path -LiteralPath $PhoenixPython)) { & $SystemPython -m venv (Join-Path $ProjectRoot '.venv-phoenix') }
& $AppPython -m pip install -r (Join-Path $ProjectRoot 'requirements-app.lock.txt')
& $AppPython -m pip install -e $ProjectRoot --no-deps --no-build-isolation
& $AppPython -m playwright install chromium
& $PhoenixPython -m pip install -r (Join-Path $ProjectRoot 'requirements-phoenix.lock.txt')
& $AppPython -m pip check
& $PhoenixPython -m pip check
