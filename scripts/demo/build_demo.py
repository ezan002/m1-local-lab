"""One command: record the live demo, then build the video, preview and coverage report.

    python scripts/demo/build_demo.py [--pace 1.0] [--out docs/demo] [--headed] [--keep]

Exit code 0 = every step passed; 2 = the video was built but some steps failed (they are shown in the video and
listed in COVERAGE.md); 1 = the recording could not be made.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pace", type=float, default=1.0)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "demo")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep intermediate clips")
    args = parser.parse_args()
    rec = ROOT / "data" / "demo" / datetime.now().strftime("%Y%m%d-%H%M%S")
    cmd = [sys.executable, str(HERE / "record_demo.py"), "--out", str(rec), "--pace", str(args.pace)]
    if args.headed:
        cmd.append("--headed")
    recorded = subprocess.run(cmd, cwd=ROOT).returncode
    if recorded not in (0, 2):
        print("recording failed; see", rec / "manifest.json", "and", rec / "processes.log")
        return 1
    cmd = [sys.executable, str(HERE / "make_video.py"), str(rec), "--out", str(args.out)] + (["--keep"] if args.keep else [])
    built = subprocess.run(cmd, cwd=ROOT).returncode
    return 1 if built not in (0, 2) else max(recorded, built)


if __name__ == "__main__":
    raise SystemExit(main())
