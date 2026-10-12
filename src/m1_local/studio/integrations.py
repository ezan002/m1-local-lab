"""Executable external integrations: launch an independently defined agent as a subprocess.

The launched program reports back only through OTLP; the Studio has no control channel into it,
so the resulting run is trace-only (no pause/resume/fork).
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from .store import StudioStore, now

ROOT = Path(__file__).resolve().parents[3]

INTEGRATIONS: dict[str, dict[str, Any]] = {
    "external-research-agent": {
        "id": "external-research-agent",
        "name": "External research agent (OpenTelemetry)",
        "description": "Independent Analyst → Critic program in examples/external_agent/ that exports OTLP spans. "
                       "Launchable from here; observable only (no checkpoints).",
        "script": "examples/external_agent/research_agent.py",
        "capabilities": {"trace": True, "execute": True, "checkpoint": False, "pause_resume": False, "fork": False, "replay": False},
        "parameters": [{"name": "topic", "label": "Topic", "default": "why tool calls fail in agent systems"},
                       {"name": "mode", "label": "Mode (deterministic | openai)", "default": "deterministic"},
                       {"name": "fail_tool", "label": "Simulate tool failure (true/false)", "default": "false"}],
    },
}


class Launcher:
    def __init__(self, store: StudioStore, ingest_endpoint: str) -> None:
        self.store = store
        self.endpoint = ingest_endpoint
        self.launches: dict[str, dict[str, Any]] = {}

    async def launch(self, integration_id: str, params: dict[str, str]) -> dict[str, Any]:
        spec = INTEGRATIONS.get(integration_id)
        if spec is None:
            raise KeyError(f"unknown integration {integration_id}")
        mode = params.get("mode", "deterministic")
        if mode not in {"deterministic", "openai"}:
            raise ValueError("mode must be deterministic or openai")
        if mode == "openai" and not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is not set; use mode=deterministic")
        launch_id = "launch_" + uuid.uuid4().hex[:10]
        cmd = [sys.executable, str(ROOT / spec["script"]), "--topic", params.get("topic") or "agents", "--mode", mode,
               "--endpoint", self.endpoint]
        if str(params.get("fail_tool", "")).lower() in {"1", "true", "yes"}:
            cmd.append("--fail-tool")
        env = {**os.environ, "STUDIO_LAUNCH_ID": launch_id, "PYTHONIOENCODING": "utf-8"}
        process = await asyncio.create_subprocess_exec(*cmd, cwd=str(ROOT), env=env, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE)
        record = {"id": launch_id, "integration": integration_id, "params": params, "status": "running",
                  "pid": process.pid, "started_at": now(), "exit_code": None, "stdout": "", "stderr": ""}
        self.launches[launch_id] = record
        asyncio.create_task(self._watch(record, process))
        return record

    async def _watch(self, record: dict[str, Any], process: asyncio.subprocess.Process) -> None:
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            record["stderr_note"] = "killed after 300s timeout"
        record.update(exit_code=process.returncode, stdout=stdout.decode("utf-8", "replace")[-4000:],
                      stderr=stderr.decode("utf-8", "replace")[-4000:], ended_at=now(),
                      status="exited" if process.returncode == 0 else "failed")

    def get(self, launch_id: str) -> dict[str, Any]:
        record = self.launches.get(launch_id)
        if record is None:
            raise KeyError(launch_id)
        rows = self.store._query("SELECT id FROM runs WHERE origin='external' AND json_extract(capabilities, '$.launch_id')=?", (launch_id,))
        return {**record, "run_id": rows[0]["id"] if rows else None}
