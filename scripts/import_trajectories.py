"""Bulk-import trajectory dataset files (LongRCA etc.) and step marks into a running Agent Studio.

    python scripts/import_trajectories.py data/longrca                          # every .json/.jsonl/.parquet in it
    python scripts/import_trajectories.py data/longrca --marks results.json --source localization

The files go through the same endpoint as the upload on External traces (POST /api/traces/import), so each
trajectory becomes one run whose steps keep the dataset's own `step` numbers. `--marks` then attaches results such
as a localisation method's chosen step: a JSON list [{"question_ID": "...", "step": 21, "note"?: "..."}]
(POST /api/marks/import). Re-running is safe: trajectories already imported are reported as duplicates and marks
are replaced.

Download a whole benchmark first, e.g. all SWE-bench Pro trajectories of LongRCA (needs `pip install huggingface_hub`):

    python -c "from huggingface_hub import snapshot_download; snapshot_download('CLoud5-real/longrca-bench', repo_type='dataset', allow_patterns='data/full/*swe_bench_pro__*', local_dir='data/longrca')"
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

SUFFIXES = {".json", ".jsonl", ".ndjson", ".parquet"}


def call(base: str, path: str, body: dict | None, token: str | None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    if token:
        headers["x-studio-csrf"] = token
    request = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if body is not None else "GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # the Studio is local: never use a proxy
    try:
        with opener.open(request, timeout=300) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{path}: HTTP {exc.code} {exc.read().decode(errors='replace')[:300]}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path, help="files or folders (searched recursively)")
    parser.add_argument("--studio", default="http://127.0.0.1:8100")
    parser.add_argument("--marks", type=Path, help="JSON list of {question_ID, step} to mark after importing")
    parser.add_argument("--source", default="localization", help="name shown on the marks")
    parser.add_argument("--dataset", default="longrca")
    args = parser.parse_args()

    base = args.studio.rstrip("/")
    try:
        token = call(base, "/api/meta", None, None)["csrf_token"]
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Agent Studio is not reachable at {base} ({exc}); start it first")

    files = []
    for p in args.paths:
        files += sorted(f for f in p.rglob("*") if f.suffix.lower() in SUFFIXES) if p.is_dir() else [p]
    ok = dup = 0
    for f in files:
        if f.suffix.lower() == ".parquet":
            body = {"filename": f.name, "content": base64.b64encode(f.read_bytes()).decode(), "encoding": "base64"}
        else:
            body = {"filename": f.name, "content": f.read_text(encoding="utf-8")}
        try:
            r = call(base, "/api/traces/import", body, token)
        except RuntimeError as exc:
            print(f"  ! {f.name}: {exc}")
            continue
        ok += len(r.get("runs", []))
        dup += 1 if r.get("duplicates") and not r.get("accepted") else 0
        print(f"  {f.name}: {r.get('format')} · {len(r.get('runs', []))} trajectory(ies) · {r.get('accepted')} spans")
    print(f"imported {len(files)} file(s), {ok} trajectory run(s){f', {dup} already present' if dup else ''}")

    if args.marks:
        items = json.loads(args.marks.read_text(encoding="utf-8"))
        if isinstance(items, dict):
            items = items.get("items") or [{"question_ID": k, "step": v} for k, v in items.items()]
        r = call(base, "/api/marks/import", {"items": items, "source": args.source, "dataset": args.dataset}, token)
        print(f"marked {len(r['applied'])} step(s) as '{args.source}'; {len(r['missing'])} skipped "
              f"(trajectory not imported or bad step)")


if __name__ == "__main__":
    main()
