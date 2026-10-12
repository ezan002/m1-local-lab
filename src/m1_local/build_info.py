"""Version facts captured once when a service process starts.

Never re-read later: a newer HEAD on disk must not be reported as loaded code.
Only a commit ID, a dirty flag, the start time and the mode are exposed.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNKNOWN = "unknown"


def _git(*args: str, root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            encoding="utf-8", timeout=5, check=True, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout


def capture_build_info(mode: str, root: Path = ROOT) -> dict:
    commit = (_git("rev-parse", "HEAD", root=root) or "").strip()
    status = _git("status", "--porcelain", root=root)
    known = len(commit) == 40 and all(c in "0123456789abcdef" for c in commit)
    return {
        "commit": commit if known else UNKNOWN,
        # True/False only when Git answered; untracked files count as uncommitted changes.
        "uncommitted_changes": (bool(status.strip()) if status is not None else UNKNOWN) if known else UNKNOWN,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        "captured": "at process start; not refreshed while running",
    }
