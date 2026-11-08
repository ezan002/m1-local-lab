"""Build the shareable demo data set in demo-data/ from a local Studio data directory.

    python scripts/build_demo_data.py                       # source: data/studio
    python scripts/build_demo_data.py --source D:/other/studio

What goes in:
- Studio workflow runs (with their LangGraph checkpoints, so Checkpoints / Replay / Fork work offline);
- connected-agent runs whose connector card still exists (graph, spans, steps and compare are stored in the Studio;
  checkpoints and forks of these runs need the agent itself to be running);
- external traces from the example research agent;
- a Codex session converted from the synthetic fixture in tests/fixtures (no real session is published).

What stays out: connector tokens, Studio settings, external traces from real Codex sessions (they can hold private
prompts and file contents) and the stray "unknown_service" fragments.

Start the Studio on a copy of it with `python -m m1_local.cli studio --demo` (or scripts/start-studio.ps1 -Demo).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from m1_local.studio.otlp import ingest_file  # noqa: E402
from m1_local.studio.store import StudioStore  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "codex_session_sample.jsonl"


def snapshot(path: Path, into: Path) -> Path:
    """Copy a live SQLite database (with its WAL) and fold the WAL in, without touching the original."""
    target = into / path.name
    for suffix in ("", "-wal", "-shm"):
        src = path.with_name(path.name + suffix)
        if src.exists():
            shutil.copy2(src, target.with_name(target.name + suffix))
    con = sqlite3.connect(target)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    return target


def keep_run(run: sqlite3.Row, connectors: set[str]) -> bool:
    if run["origin"] == "studio":
        return True
    if run["origin"] == "connected":
        return run["integration"] in connectors
    name = (run["workflow_name"] or "").lower()
    return run["origin"] == "external" and not name.startswith(("codex", "unknown_service"))


def build(source: Path, out: Path) -> dict[str, int]:
    with tempfile.TemporaryDirectory() as tmp:
        src = sqlite3.connect(snapshot(source / "studio.sqlite", Path(tmp)))
        src.row_factory = sqlite3.Row
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        StudioStore(out / "studio.sqlite").close()  # creates the schema
        dst = sqlite3.connect(out / "studio.sqlite")

        def copy(table: str, rows: list[sqlite3.Row]) -> None:
            if rows:
                cols = rows[0].keys()
                dst.executemany(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                [tuple(r) for r in rows])

        connectors = src.execute("SELECT * FROM connectors").fetchall()
        scrubbed = []
        for c in connectors:
            item = dict(c)
            config = json.loads(item["config"] or "{}")
            config.pop("token", None)  # the access token stays on the machine that made it
            item["config"] = json.dumps(config)
            scrubbed.append(item)
        if scrubbed:
            cols = list(scrubbed[0])
            dst.executemany(f"INSERT OR REPLACE INTO connectors ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [tuple(r[k] for k in cols) for r in scrubbed])
        ids = {c["id"] for c in connectors}
        copy("workflows", src.execute("SELECT * FROM workflows").fetchall())
        runs = [r for r in src.execute("SELECT * FROM runs ORDER BY created_at").fetchall() if keep_run(r, ids)]
        kept = {r["id"] for r in runs}
        # A branch whose source run was left out keeps working as a run of its own.
        rows = []
        for r in runs:
            item = dict(r)
            if item["parent_run_id"] and item["parent_run_id"] not in kept:
                item["parent_run_id"] = None
            rows.append(item)
        if rows:
            cols = list(rows[0])
            dst.executemany(f"INSERT OR REPLACE INTO runs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [tuple(r[k] for k in cols) for r in rows])
        spans = [s for s in src.execute("SELECT * FROM spans ORDER BY run_id, seq").fetchall() if s["run_id"] in kept]
        copy("spans", spans)
        dst.commit()
        dst.close()

        studio_threads = [r["thread_id"] for r in runs if r["origin"] == "studio" and r["thread_id"]]
        if (source / "checkpoints.sqlite").exists() and studio_threads:
            cp = snapshot(source / "checkpoints.sqlite", Path(tmp))
            con = sqlite3.connect(cp)
            for table in [t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
                cols = [c[1] for c in con.execute(f"PRAGMA table_info({table})")]
                if "thread_id" in cols:
                    marks = ",".join("?" * len(studio_threads))
                    con.execute(f"DELETE FROM {table} WHERE thread_id NOT IN ({marks})", studio_threads)
            con.commit()
            con.execute("VACUUM")
            con.close()
            shutil.copy2(cp, out / "checkpoints.sqlite")

    store = StudioStore(out / "studio.sqlite")
    ingest_file(store, FIXTURE.read_text(encoding="utf-8"), FIXTURE.name)
    store.close()
    con = sqlite3.connect(out / "studio.sqlite")
    con.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal/-shm next to it
    counts = {o: n for o, n in con.execute("SELECT origin, COUNT(*) FROM runs GROUP BY origin")}
    counts["spans"] = con.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
    con.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=ROOT / "data" / "studio")
    parser.add_argument("--out", type=Path, default=ROOT / "demo-data")
    args = parser.parse_args()
    counts = build(args.source, args.out)
    print(f"demo data written to {args.out}: {json.dumps(counts)}")


if __name__ == "__main__":
    main()
