"""Connected executor (route 3): the Studio drives the original M1 LangGraph executor through its own HTTP
control API. The executor here is the real AgentRuntime + m1_local.api app (real LangGraph, real SQLite
checkpointer) with the offline RuleBasedGateway; the Studio side is the real ConnectedService and store.
Only the transport is in-process (httpx ASGITransport); the subprocess variant is in test_connected_e2e.py."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import httpx
import pytest

from m1_local.api import create_app as create_legacy_app
from m1_local.models import RuleBasedGateway
from m1_local.runtime import AgentRuntime
from m1_local.studio.connected import ConnectedService, layout, state_delta, strip_volatile
from m1_local.studio.connectors import ConnectorConflict, M1LegacyConnector
from m1_local.studio.otlp import ingest
from m1_local.studio.store import StudioStore

CODE_TASK = "Draft code for a UUID validation function"


class SlowGateway(RuleBasedGateway):
    """Gives a pause request time to land before the pause gate."""

    async def route(self, state):
        await asyncio.sleep(0.4)
        return await super().route(state)


class FailsOnceGateway(RuleBasedGateway):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def complete(self, prompt: str) -> str:
        if not self.failed and "Generate or analyze code" in prompt:
            self.failed = True
            raise RuntimeError("simulated coder outage")
        return await super().complete(prompt)


@contextlib.asynccontextmanager
async def connected_pair(settings, tmp_path: Path, gateway=None):
    runtime = AgentRuntime(settings, gateway or RuleBasedGateway(), enable_tracing=False)
    legacy = create_legacy_app(settings, runtime)
    await runtime.startup()
    store = StudioStore(tmp_path / "studio" / "studio.sqlite")
    service = ConnectedService(store, studio_port=8100, data_root=tmp_path / "connected", poll_interval=0.05)
    record = service.create_connector(kind="m1_legacy", name="Legacy executor", mode="attach",
                                      base_url="http://127.0.0.1:8000", provider_mode="deterministic")
    service._clients[record["id"]] = M1LegacyConnector("http://127.0.0.1:8000", provider_mode="deterministic",
                                                       transport=httpx.ASGITransport(app=legacy))
    try:
        yield service, record["id"], runtime, store
    finally:
        await service.shutdown()
        await runtime.shutdown()
        store.close()


async def settle(service: ConnectedService, run_id: str, timeout: float = 30) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = await service.sync(run_id)
        if run["status"] not in {"queued", "running", "pausing"}:
            return run
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} did not settle")


def node_spans(store: StudioStore, run_id: str) -> list[dict]:
    # Node executions: children of the run root that carry a node id (correlated OTLP spans have none).
    return [s for s in store.spans(run_id) if s["parent_span_id"] == f"cr_{run_id}" and s["node_id"] and s["kind"] in {"agent", "tool", "gate"}]


def test_helpers_are_bookkeeping_aware():
    assert state_delta({"a": 1, "log": [1]}, {"a": 2, "log": [1, 2], "same": None}) == {"a": 2, "log": [2], "same": None}
    assert strip_volatile({"execution_id": "x", "progress": [{"id": "1", "summary": "s"}]}) == {"progress": [{"summary": "s"}]}
    pos = layout([{"id": "a"}, {"id": "b"}, {"id": "c"}],
                 [{"source": "__start__", "target": "a"}, {"source": "a", "target": "b"}, {"source": "b", "target": "a"}, {"source": "a", "target": "c"}])
    assert pos["a"]["x"] == 0 and pos["b"]["x"] == pos["c"]["x"] == 270


@pytest.mark.asyncio
async def test_describe_reads_the_executors_own_graph_and_fork_schema(settings, tmp_path):
    async with connected_pair(settings, tmp_path) as (service, cid, _runtime, _store):
        describe = await service.describe(cid)
        ids = {n["id"] for n in describe["graph"]["nodes"]}
        assert ids == {"planner", "orchestrator", "pause_gate", "web_surfer", "file_reader", "coder", "finalizer"}
        assert describe["graph"]["source"] == "executor"  # from GET /api/graph, not the connector fallback
        assert {f["path"] for f in describe["fork_fields"]} == {"plan", "worker_instruction", "next_agent"}
        assert describe["capabilities"]["fork"] and not describe["capabilities"]["cancel"]


@pytest.mark.asyncio
async def test_run_is_mirrored_from_checkpoints_and_fork_branches_without_touching_the_source(settings, tmp_path):
    async with connected_pair(settings, tmp_path) as (service, cid, runtime, store):
        run = await service.start_run(cid, {"task": CODE_TASK})
        assert run["origin"] == "connected" and run["remote_run_id"]
        done = await settle(service, run["id"])
        assert done["status"] == "completed", done
        assert "Offline deterministic result" in done["output"]["final_answer"]
        path = [s["node_id"] for s in node_spans(store, run["id"])]
        assert path == ["planner", "orchestrator", "pause_gate", "coder", "orchestrator", "finalizer"]
        assert all(s["checkpoint_id"] for s in node_spans(store, run["id"]))
        planner = node_spans(store, run["id"])[0]
        assert planner["output"]["plan"] == ["create a code artifact", "review without execution"]

        checkpoints = await service.checkpoints(run["id"])
        after_planner = next(c for c in checkpoints if c["after_node"] == "planner")
        assert after_planner["forkable"] and after_planner["next"] == ["orchestrator"]
        assert next(e for e in after_planner["editable"] if e["path"] == "plan")["value"] == planner["output"]["plan"]
        source_history = [h["checkpoint_id"] for h in await runtime.history(run["remote_run_id"])]

        with pytest.raises(ValueError, match="not editable"):
            await service.fork(run["id"], after_planner["checkpoint_id"], {"task": "x"}, relation="fork")
        with pytest.raises(ValueError, match="list of strings"):
            await service.fork(run["id"], after_planner["checkpoint_id"], {"plan": "not a list"}, relation="fork")

        fork = await service.fork(run["id"], after_planner["checkpoint_id"],
                                  {"plan": ["read the explicitly allowed file", "summarize evidence"]}, relation="fork")
        fork = await settle(service, fork["id"])
        assert fork["relation"] == "fork" and fork["parent_run_id"] == run["id"]
        assert fork["source_checkpoint_id"] == after_planner["checkpoint_id"]
        assert fork["inherited"]["nodes"] == ["planner"]
        # The edited plan makes the orchestrator pick file_reader, which fails without an allowed file.
        assert fork["status"] == "failed" and "allowed file" in fork["error"]["message"]
        fork_spans = store.spans(fork["id"])
        assert [s["name"] for s in fork_spans if s["kind"] == "edit"] == ["fork edit"]
        assert any(s["node_id"] == "file_reader" and s["status"] == "error" for s in fork_spans)
        assert not any(s["node_id"] == "planner" for s in fork_spans)  # inherited, not re-executed
        # Source branch in the executor is unchanged: same head, same checkpoints.
        assert [h["checkpoint_id"] for h in await runtime.history(run["remote_run_id"])] == source_history
        assert (await service.sync(run["id"]))["status"] == "completed"

        after_orch = next(c for c in checkpoints if c["after_node"] == "orchestrator" and c["next"] == ["pause_gate"])
        replay = await settle(service, (await service.fork(run["id"], after_orch["checkpoint_id"], {}, relation="replay"))["id"])
        assert replay["status"] == "completed" and replay["relation"] == "replay"
        assert [s["node_id"] for s in node_spans(store, replay["id"])] == ["pause_gate", "coder", "orchestrator", "finalizer"]
        assert [s["name"] for s in store.spans(replay["id"]) if s["kind"] == "edit"] == ["replay seed"]

        # Route edit: after the orchestrator (which chose coder) send the run straight to the finalizer instead.
        assert sorted(e["path"] for e in after_orch["editable"]) == ["next_agent", "worker_instruction"]
        route = next(e for e in after_orch["editable"] if e["path"] == "next_agent")
        assert route["value"] == "coder" and "finalizer" in route["choices"]
        with pytest.raises(ValueError, match="must be one of"):
            await service.fork(run["id"], after_orch["checkpoint_id"], {"next_agent": "planner_x"}, relation="fork")
        with pytest.raises(ValueError, match="fork from the checkpoint after that step"):
            await service.fork(run["id"], after_orch["checkpoint_id"], {"plan": ["x"]}, relation="fork")   # planner wrote it
        rerouted = await settle(service, (await service.fork(run["id"], after_orch["checkpoint_id"],
                                                             {"next_agent": "finalizer"}, relation="fork"))["id"])
        assert rerouted["status"] == "completed"
        assert [s["node_id"] for s in node_spans(store, rerouted["id"])] == ["finalizer"]   # coder skipped


@pytest.mark.asyncio
async def test_pause_resume_and_retry_are_forwarded(settings, tmp_path):
    async with connected_pair(settings, tmp_path, SlowGateway()) as (service, cid, _runtime, store):
        run = await service.start_run(cid, {"task": CODE_TASK})
        await service.pause(run["id"])
        paused = await settle(service, run["id"])
        assert paused["status"] == "paused"
        pauses = [s for s in store.spans(run["id"]) if s["kind"] == "pause"]
        assert pauses and pauses[-1]["status"] == "paused" and pauses[-1]["node_id"] == "pause_gate"
        await service.resume(run["id"])
        done = await settle(service, run["id"])
        assert done["status"] == "completed"
        assert all(s["status"] == "resumed" for s in store.spans(run["id"]) if s["kind"] == "pause")

    async with connected_pair(settings, tmp_path / "retry", FailsOnceGateway()) as (service, cid, _runtime, store):
        run = await service.start_run(cid, {"task": CODE_TASK})
        failed = await settle(service, run["id"])
        assert failed["status"] == "failed" and failed["error"]["node_id"] == "coder"
        assert any(s["node_id"] == "coder" and s["status"] == "error" for s in store.spans(run["id"]))
        await service.resume(run["id"])  # retry from the last committed checkpoint
        done = await settle(service, run["id"])
        assert done["status"] == "completed" and done["error"] is None
        coder = [s for s in node_spans(store, run["id"]) if s["node_id"] == "coder"]
        assert [s["status"] for s in coder] == ["error", "ok"]  # the failed attempt stays visible next to the retry


@pytest.mark.asyncio
async def test_existing_executor_runs_can_be_imported_with_lineage(settings, tmp_path):
    async with connected_pair(settings, tmp_path) as (service, cid, runtime, store):
        source = await runtime.create_run(CODE_TASK)  # started outside the Studio
        history = await runtime.history(source)
        after_planner = next(h for h in history if h["next"] == ["orchestrator"])
        branch = await runtime.fork(source, after_planner["checkpoint_id"], {"plan": ["create a code artifact"]})
        await runtime.resume(branch)
        listed = await service.remote_runs(cid)
        assert {r["run_id"] for r in listed} >= {source, branch} and all(r["studio_run_id"] is None for r in listed)
        imported = await service.import_run(cid, branch)
        assert imported["relation"] == "fork" and imported["parent_run_id"]
        parent = store.get_run(imported["parent_run_id"])
        assert parent["remote_run_id"] == source and parent["status"] == "completed"
        assert (await service.import_run(cid, branch))["id"] == imported["id"]  # idempotent


@pytest.mark.asyncio
async def test_conflicts_from_the_executor_are_reported(settings, tmp_path):
    async with connected_pair(settings, tmp_path, SlowGateway()) as (service, cid, _runtime, _store):
        first = await service.start_run(cid, {"task": CODE_TASK})
        with pytest.raises(ConnectorConflict, match="another run is active"):
            await service.start_run(cid, {"task": CODE_TASK})
        await settle(service, first["id"])
        with pytest.raises(ValueError, match="unknown inputs"):
            await service.start_run(cid, {"prompt": "x"})


def test_otlp_spans_from_the_executor_attach_to_the_mirrored_run(tmp_path):
    store = StudioStore(tmp_path / "s.sqlite")
    run = store.create_run(origin="connected", integration="conn_x", remote_run_id="remote-1", status="running")
    store.insert_span(run["id"], {"id": f"cr_{run['id']}", "name": "root", "kind": "workflow", "status": "running"})

    def span(span_id: str, parent: str | None, run_attr: str) -> dict:
        attrs = [{"key": "application_run_id", "value": {"stringValue": run_attr}},
                 {"key": "openinference.span.kind", "value": {"stringValue": "LLM"}},
                 {"key": "agent.name", "value": {"stringValue": "planner"}}]
        return {"traceId": "ab" * 16, "spanId": span_id, "parentSpanId": parent or "", "name": "m1.model.planner",
                "attributes": attrs, "startTimeUnixNano": "1", "endTimeUnixNano": "2"}

    payload = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "m1"}}]},
                                  "scopeSpans": [{"spans": [span("11" * 8, None, "remote-1"), span("22" * 8, "11" * 8, "remote-1"),
                                                            span("33" * 8, None, "someone-else")]}]}]}
    result = ingest(store, payload)
    assert result["accepted"] == 3
    attached = {s["id"]: s for s in store.spans(run["id"])}
    assert attached["11" * 8]["parent_span_id"] == f"cr_{run['id']}" and attached["11" * 8]["node_id"] is None
    assert attached["22" * 8]["parent_span_id"] == "11" * 8
    assert store.get_run("ext_" + "ab" * 16) is not None  # unrelated span still becomes an external trace
    store.close()
