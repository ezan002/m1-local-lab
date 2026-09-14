[CmdletBinding()]
param([string]$PythonExe)

$ErrorActionPreference = 'Stop'
if ($env:PIP_NO_INDEX -and $env:PIP_NO_INDEX.Trim().ToLowerInvariant() -notin @('0', 'false', 'no', 'off')) {
    throw 'PIP_NO_INDEX disables dependency index access. This installer needs the locked packages; explicitly enable index access for this process or use a separately prepared offline wheelhouse. Your environment has not been changed.'
}
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$AppPython = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
$PhoenixPython = Join-Path $ProjectRoot '.venv-phoenix/Scripts/python.exe'

function Invoke-CheckedPython {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed with exit code $LASTEXITCODE" }
}

if (-not $PythonExe) {
    $Launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($Launcher) {
        $PythonExe = Invoke-CheckedPython $Launcher.Source @('-3.11', '-c', 'import sys; print(sys.executable)')
    } else {
        $Candidate = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $Candidate) { throw 'Install Python 3.11 x64 or pass -PythonExe with its full path.' }
        $PythonExe = $Candidate.Source
    }
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw 'Python executable not found; pass -PythonExe with its full path.' }
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
$Version = Invoke-CheckedPython $PythonExe @('-c', 'import sys; print(*sys.version_info[:2], 64 if sys.maxsize > 2**32 else 32, sep=chr(46))')
if ($Version -ne '3.11.64') { throw 'Python 3.11 x64 is required.' }

foreach ($EnvironmentPython in @($AppPython, $PhoenixPython)) {
    if (Test-Path -LiteralPath $EnvironmentPython) {
        $Version = Invoke-CheckedPython $EnvironmentPython @('-c', 'import sys; print(*sys.version_info[:2], 64 if sys.maxsize > 2**32 else 32, sep=chr(46))')
        if ($Version -ne '3.11.64') { throw 'Existing virtual environment must use Python 3.11 x64; it has not been replaced.' }
    }
}
if (-not (Test-Path -LiteralPath $AppPython)) { Invoke-CheckedPython $PythonExe @('-m', 'venv', (Join-Path $ProjectRoot '.venv')) }
if (-not (Test-Path -LiteralPath $PhoenixPython)) { Invoke-CheckedPython $PythonExe @('-m', 'venv', (Join-Path $ProjectRoot '.venv-phoenix')) }
Invoke-CheckedPython $AppPython @('-m', 'pip', 'install', '-r', (Join-Path $ProjectRoot 'requirements-app.lock.txt'))
Invoke-CheckedPython $AppPython @('-m', 'pip', 'install', '-e', $ProjectRoot, '--no-deps', '--no-build-isolation')
Invoke-CheckedPython $AppPython @('-m', 'playwright', 'install', 'chromium')
Invoke-CheckedPython $PhoenixPython @('-m', 'pip', 'install', '-r', (Join-Path $ProjectRoot 'requirements-phoenix.lock.txt'))
Invoke-CheckedPython $AppPython @('-m', 'pip', 'check')
Invoke-CheckedPython $PhoenixPython @('-m', 'pip', 'check')
