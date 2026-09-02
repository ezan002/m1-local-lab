from __future__ import annotations

import json
from pathlib import Path

import pytest

from m1_local.dataset import run_jsonl
from m1_local.models import RuleBasedGateway
from m1_local.runtime import AgentRuntime


@pytest.mark.asyncio
async def test_jsonl_runs_serially_with_independent_threads(settings, tmp_path: Path):
    allowed = Path(settings.allowed_file_dirs[0]) / "dataset.csv"
    allowed.write_text("name,value\nrow,1\n", encoding="utf-8")
    source = tmp_path / "cases.jsonl"
    records = [
        {"case_id": "one", "task": "Read the file", "allowed_files": [str(allowed)]},
        {"case_id": "two", "task": "Read the file again", "allowed_files": [str(allowed)]},
    ]
    source.write_text("\n".join(json.dumps(item) for item in records), encoding="utf-8")
    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    await runtime.startup()
    try:
        results = await run_jsonl(runtime, source, limit=2)
        assert [item["case_id"] for item in results] == ["one", "two"]
        assert all(item["status"] == "completed" for item in results)
        threads = {runtime.store.get_run(item["run_id"])["thread_id"] for item in results}
        assert len(threads) == 2
    finally:
        await runtime.shutdown()
