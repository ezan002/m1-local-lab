from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest

from m1_local.models import RuleBasedGateway
from m1_local.runtime import AgentRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", ["both", "metadata_db", "checkpoint_db"])
async def test_second_runtime_cannot_recover_a_live_owner(settings, tmp_path, shared):
    first = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    changes = {} if shared == "both" else {
        ("checkpoint_db" if shared == "metadata_db" else "metadata_db"): tmp_path / "other.sqlite"
    }
    second = AgentRuntime(settings.model_copy(update=changes), RuleBasedGateway(), enable_tracing=False)
    await first.startup()
    first.store.create_run({"run_id": "live", "thread_id": "t", "branch_id": "b", "task": "fixture", "status": "running"})
    try:
        with pytest.raises(RuntimeError, match="owned by another runtime"):
            await second.startup()
        assert first.store.get_run("live")["status"] == "running"
        assert second.graph is None
        if shared != "checkpoint_db":
            with pytest.raises(RuntimeError):
                second.request_pause("live")
            assert first.store.get_run("live")["pause_requested"] is False
    finally:
        await second.shutdown()
        await first.shutdown()
    # Normal shutdown releases ownership; orphan recovery remains available.
    await second.startup()
    try:
        if shared != "checkpoint_db":
            assert second.store.get_run("live")["status"] == "interrupted"
    finally:
        await second.shutdown()


@pytest.mark.asyncio
async def test_failed_startup_releases_ownership_and_checkpoint_connection(settings, monkeypatch):
    import m1_local.runtime as module

    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    original = module.build_graph

    def fail(*args):
        raise ValueError("controlled graph build failure")

    try:
        monkeypatch.setattr(module, "build_graph", fail)
        with pytest.raises(ValueError, match="controlled"):
            await runtime.startup()
        assert runtime.saver is None
        monkeypatch.setattr(module, "build_graph", original)
        other = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
        await other.startup()
        await other.shutdown()
    finally:
        await runtime.shutdown()


def test_process_death_releases_os_ownership(tmp_path):
    # Use a genuine second interpreter, not two objects with one in-memory lock.
    code = """
import sys
from pathlib import Path
from m1_local.runtime_ownership import RuntimeOwnership
lock = RuntimeOwnership([Path(sys.argv[1])])
try:
    lock.acquire()
except RuntimeError:
    print('busy', flush=True)
    raise SystemExit(73)
print('owned', flush=True)
if sys.argv[2] == 'hold':
    sys.stdin.readline()
lock.release()
"""
    args = [sys.executable, "-B", "-c", code, str(tmp_path / "shared.sqlite")]
    env = {**os.environ, "OPENAI_API_KEY": "", "RUN_REAL_MODEL_TESTS": "0"}
    holder = subprocess.Popen(args + ["hold"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        assert holder.stdout.readline().strip() == "owned"
        blocked = subprocess.run(args + ["once"], capture_output=True, text=True, timeout=20, env=env)
        assert blocked.returncode == 73, blocked.stderr
        assert blocked.stdout.strip() == "busy"
    finally:
        holder.kill()
        holder.communicate(timeout=10)
    recovered = subprocess.run(args + ["once"], capture_output=True, text=True, timeout=20, env=env)
    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout.strip() == "owned"


@pytest.mark.asyncio
async def test_api_lifespan_failure_releases_database_owner(settings):
    from m1_local.api import create_app

    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    app = create_app(settings, runtime)
    try:
        with pytest.raises(ValueError, match="lifespan fixture"):
            async with app.router.lifespan_context(app):
                raise ValueError("lifespan fixture")
        assert runtime.graph is None
    finally:
        await runtime.shutdown()
