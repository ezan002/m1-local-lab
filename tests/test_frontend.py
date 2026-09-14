from pathlib import Path
import shutil
import subprocess

import playwright
import pytest


def test_frontend_behavior_without_browser():
    node = shutil.which("node")
    if not node:
        bundled = Path(playwright.__file__).parent / "driver" / "node.exe"
        if bundled.is_file():
            node = str(bundled)
    if not node:
        pytest.skip("Node runtime unavailable; this is not a browser test")
    result = subprocess.run([node, "--test", str(Path(__file__).with_name("frontend_behaviors.cjs"))], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
