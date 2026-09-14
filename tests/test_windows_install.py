from __future__ import annotations

import shutil
import re
import subprocess
import sys
import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell installer")
ROOT = Path(__file__).resolve().parents[1]


def test_version_probe_works_with_real_python_and_windows_argument_passing(tmp_path):
    source = (ROOT / "scripts/install.ps1").read_text(encoding="utf-8-sig")
    function = re.search(r"function Invoke-CheckedPython \{.*?^\}", source, re.S | re.M).group()
    probe = next(line for line in source.splitlines() if line.startswith("$Version ="))
    script = tmp_path / "probe.ps1"
    script.write_text(function + "\n$PythonExe = '" + sys.executable.replace("'", "''") + "'\n" + probe + "\nWrite-Output $Version\n", encoding="utf-8")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "3.11.64"


@pytest.mark.parametrize("version,expected", [("3.11.64", "exit code 17"), ("3.12.64", "Python 3.11 x64")])
def test_installer_uses_supplied_python_and_stops_before_later_commands(tmp_path, version, expected):
    project = tmp_path / "checkout with spaces"
    (project / "scripts").mkdir(parents=True)
    # Refuse to execute legacy installers that would ignore the test interpreter.
    assert "[string]$PythonExe" in (ROOT / "scripts/install.ps1").read_text(encoding="utf-8-sig")
    shutil.copyfile(ROOT / "scripts/install.ps1", project / "scripts/install.ps1")
    interpreter = project / "python fixture.cmd"
    interpreter.write_text(
        '@echo off\nif "%~1"=="-c" (\n'
        f'  echo {version}\n  exit /b 0\n)\n'
        'echo attempted>>"%~dp0calls.txt"\nexit /b 17\n', encoding="ascii",
    )
    result = subprocess.run([
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(project / "scripts/install.ps1"), "-PythonExe", str(interpreter),
    ], cwd=tmp_path, capture_output=True, text=True, timeout=30, env={**os.environ, "PIP_NO_INDEX": "0"})
    assert result.returncode != 0
    assert expected in result.stdout + result.stderr
    calls = project / "calls.txt"
    assert (len(calls.read_text().splitlines()) if calls.exists() else 0) == (1 if version == "3.11.64" else 0)


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_installer_reports_no_index_without_changing_it_or_running_python(tmp_path, value):
    project = tmp_path / "checkout with spaces"
    (project / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/install.ps1", project / "scripts/install.ps1")
    interpreter = project / "python fixture.cmd"
    interpreter.write_text('@echo off\necho unexpected>"%~dp0called.txt"\nexit /b 17\n', encoding="ascii")
    command = "try { & '" + str(project / "scripts/install.ps1").replace("'", "''") + "' -PythonExe '" + str(interpreter).replace("'", "''") + "' } catch { Write-Output $_.Exception.Message }; Write-Output ('preserved=' + $env:PIP_NO_INDEX)"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=20, env={**os.environ, "PIP_NO_INDEX": value})
    assert "PIP_NO_INDEX disables" in result.stdout
    assert "preserved=" + value in result.stdout
    assert not (project / "called.txt").exists()


def test_powershell_scripts_parse_without_errors():
    script = """$errorsFound = @()
Get-ChildItem -LiteralPath 'scripts' -Filter '*.ps1' | ForEach-Object {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$parseErrors) | Out-Null
    $errorsFound += $parseErrors
}
if ($errorsFound.Count) { $errorsFound | ForEach-Object { Write-Output $_.Message }; exit 1 }
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
