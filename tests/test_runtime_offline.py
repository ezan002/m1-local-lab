from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from m1_local.models import PlanDecision, RouteDecision, RuleBasedGateway
from m1_local.runtime import AgentRuntime


@pytest.mark.asyncio
async def test_file_task_persists_history_and_does_not_duplicate(settings):
    allowed = Path(settings.allowed_file_dirs[0]) / "facts.csv"
    allowed.write_text("name,value\nalpha,1\n", encoding="utf-8")
    gateway = RuleBasedGateway()
    runtime = AgentRuntime(settings, gateway, enable_tracing=False)
    await runtime.startup()
    try:
        run_id = await runtime.create_run("Summarize the allowed file", allowed_files=[str(allowed)])
        current = await runtime.current_state(run_id)
        assert current["status"] == "completed"
        assert current["state"]["progress_ledger"][0]["agent"] == "file_reader"
        message_ids = [item["id"] for item in current["state"]["messages"]]
        assert len(message_ids) == len(set(message_ids))
        calls_before_history = gateway.calls
        history = await runtime.history(run_id)
        assert len(history) >= 4
        assert all(item["checkpoint_id"] for item in history)
        assert gateway.calls == calls_before_history
        stored_events = runtime.store.events_after(run_id)
        replayed_events = [chunk async for chunk in runtime.sse(run_id, stored_events[0]["id"])]
        assert f"id: {stored_events[0]['id']}\n" not in "".join(replayed_events)
        assert f"id: {stored_events[1]['id']}\n" in "".join(replayed_events)
        assert '"_created_at"' in "".join(replayed_events)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_coder_artifact_is_explicitly_unexecuted(settings):
    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    await runtime.startup()
    try:
        run_id = await runtime.create_run("Draft code for a UUID validation function")
        current = await runtime.current_state(run_id)
        artifact = current["state"]["artifacts"][0]
        assert artifact["type"] == "unexecuted_code"
        assert runtime.artifacts.resolve_id_path(artifact["path"]).is_file()
        assert settings.enable_code_execution is False
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_fork_preserves_old_head_and_whitelists_changes(settings):
    allowed = Path(settings.allowed_file_dirs[0]) / "facts.csv"
    allowed.write_text("x,y\n1,2\n", encoding="utf-8")
    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    await runtime.startup()
    try:
        original = await runtime.create_run("Read the file", allowed_files=[str(allowed)])
        original_head = runtime.store.get_run(original)["head_config"]
        history = await runtime.history(original)
        source = next(item for item in history if item["next"])
        forked = await runtime.fork(original, source["checkpoint_id"], {"worker_instruction": "changed"})
        assert runtime.store.get_run(original)["head_config"] == original_head
        fork_record = runtime.store.get_run(forked)
        assert fork_record["parent_run_id"] == original
        assert fork_record["source_checkpoint_id"] == source["checkpoint_id"]
        with pytest.raises(ValueError, match="not allowed"):
            await runtime.fork(original, source["checkpoint_id"], {"task": "replace"})
    finally:
        await runtime.shutdown()


class StallingGateway(RuleBasedGateway):
    async def route(self, state):
        self.calls += 1
        return RouteDecision(
            complete=False, made_progress=False, loop_detected=True,
            next_agent="coder", instruction="retry", reason="no progress",
        )


class FailingGateway(RuleBasedGateway):
    async def plan(self, state):
        raise RuntimeError("controlled model failure")


@pytest.mark.asyncio
async def test_stall_replans_then_hits_controlled_limit(settings):
    limited = settings.model_copy(update={"max_graph_steps": 3, "max_replans": 1})
    gateway = StallingGateway()
    runtime = AgentRuntime(limited, gateway, enable_tracing=False)
    await runtime.startup()
    try:
        run_id = await runtime.create_run("Draft code")
        current = await runtime.current_state(run_id)
        assert current["state"]["replan_count"] == 1
        assert current["status"] == "failed"
        assert "not complete successfully" in current["state"]["final_answer"]
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_model_failure_is_persisted_as_failure(settings):
    runtime = AgentRuntime(settings, FailingGateway(), enable_tracing=False)
    await runtime.startup()
    try:
        run_id = await runtime.create_run("Fail in a controlled way")
        record = runtime.store.get_run(run_id)
        assert record["status"] == "failed"
        assert "controlled model failure" in record["error"]
        assert any(event["event"] == "error" for event in runtime.store.events_after(run_id))
    finally:
        await runtime.shutdown()


class GatePlanningGateway(RuleBasedGateway):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def plan(self, state):
        self.entered.set()
        await self.release.wait()
        return await super().plan(state)


@pytest.mark.asyncio
async def test_background_admission_is_atomic_and_pause_survives_restart(settings):
    allowed = Path(settings.allowed_file_dirs[0]) / "pause.csv"
    allowed.write_text("name,value\npaused,1\n", encoding="utf-8")
    gateway = GatePlanningGateway()
    runtime = AgentRuntime(settings, gateway, enable_tracing=False)
    await runtime.startup()
    run_id = await runtime.create_run(
        "Read the file", allowed_files=[str(allowed)], start_background=True
    )
    task = runtime._tasks[run_id]
    await asyncio.wait_for(gateway.entered.wait(), timeout=2)
    with pytest.raises(RuntimeError, match="another run is active"):
        await runtime.create_run("This must be rejected", start_background=True)
    runtime.request_pause(run_id)
    gateway.release.set()
    await asyncio.wait_for(task, timeout=5)
    assert runtime.store.get_run(run_id)["status"] == "paused"
    await runtime.shutdown()

    recovered = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    await recovered.startup()
    try:
        assert recovered.store.get_run(run_id)["status"] == "paused"
        await recovered.resume(run_id)
        current = await recovered.current_state(run_id)
        assert current["status"] == "completed"
        assert current["state"]["progress_ledger"][0]["agent"] == "file_reader"
    finally:
        await recovered.shutdown()


@pytest.mark.asyncio
async def test_replay_creates_lineage_without_moving_original_head(settings):
    allowed = Path(settings.allowed_file_dirs[0]) / "replay.csv"
    allowed.write_text("x,y\n1,2\n", encoding="utf-8")
    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    await runtime.startup()
    try:
        original = await runtime.create_run("Read the file", allowed_files=[str(allowed)])
        original_head = runtime.store.get_run(original)["head_config"]
        source = next(item for item in await runtime.history(original) if item["next"])
        replayed = await runtime.replay(original, source["checkpoint_id"])
        replay_record = runtime.store.get_run(replayed)
        assert replay_record["status"] == "completed"
        assert replay_record["parent_run_id"] == original
        assert replay_record["source_checkpoint_id"] == source["checkpoint_id"]
        assert runtime.store.get_run(original)["head_config"] == original_head
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_completed_run_cannot_be_resumed(settings):
    allowed = Path(settings.allowed_file_dirs[0]) / "done.csv"
    allowed.write_text("x,y\n1,2\n", encoding="utf-8")
    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    await runtime.startup()
    try:
        run_id = await runtime.create_run("Read the file", allowed_files=[str(allowed)])
        with pytest.raises(ValueError, match="only paused, failed, or interrupted"):
            await runtime.resume(run_id)
    finally:
        await runtime.shutdown()
