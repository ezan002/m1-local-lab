"""studio_executor SDK on its own (no Agent Studio): a real LangGraph graph with a real checkpointer, driven
through the SDK API and through its HTTP app."""
from __future__ import annotations

import asyncio
import importlib.util
import operator
import sys
from pathlib import Path
from typing import Annotated, TypedDict

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from studio_executor import ExecutorConflict, ForkField, StudioExecutor, create_asgi_app

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 24


def load_example():
    spec = importlib.util.spec_from_file_location("support_agent", ROOT / "examples" / "langgraph_agent" / "support_agent.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["support_agent"] = module  # LangGraph resolves the state's type hints through sys.modules
    spec.loader.exec_module(module)
    return module


def support_executor(tmp_path: Path | None = None, model=None) -> StudioExecutor:
    example = load_example()
    graph = example.build(model or example.Model("deterministic"), InMemorySaver())
    return StudioExecutor(graph, name="support", input_fields=[{"name": "question", "label": "Q", "default": ""}],
                          make_input=lambda i: {"question": i["question"], "trail": []},
                          fork_fields=[ForkField("category"), ForkField("facts", "string_list"), ForkField("draft"), ForkField("review")],
                          registry_path=(tmp_path / "runs.sqlite") if tmp_path else None)


async def settle(executor: StudioExecutor, run_id: str, timeout: float = 10) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = await executor.run(run_id)
        if run["status"] not in {"queued", "running", "pausing"}:
            return run
        await asyncio.sleep(0.02)
    raise AssertionError("run did not settle")


def test_fork_fields_with_reducers_are_rejected():
    example = load_example()
    graph = example.build(example.Model("deterministic"), InMemorySaver())
    with pytest.raises(ValueError, match="reducer"):
        StudioExecutor(graph, name="x", fork_fields=[ForkField("trail", "string_list")])
    with pytest.raises(ValueError, match="not a state key"):
        StudioExecutor(graph, name="x", fork_fields=[ForkField("nope")])
    with pytest.raises(ValueError, match="checkpointer"):
        StudioExecutor(StateGraph(example.SupportState).add_node("a", lambda s: {}).add_edge(START, "a").compile(), name="x")


@pytest.mark.asyncio
async def test_run_history_fork_replay(tmp_path):
    ex = support_executor(tmp_path)
    run_id = await ex.start({"question": "I need a refund for a double charge"})
    run = await settle(ex, run_id)
    assert run["status"] == "completed" and run["output"]["answer"].startswith("Thanks")
    history = await ex.history(run_id)
    assert [h["source"] for h in history][-1] == "input" and history[0]["next"] == []
    nodes = [p["next"][0] for p in reversed(history[1:]) if p["next"] and p["next"] != ["__start__"]]
    assert nodes == ["classify", "lookup", "draft", "review", "draft", "review", "finalize"]  # one revision loop
    describe = ex.describe()
    assert {n["id"] for n in describe["graph"]["nodes"]} == {"classify", "lookup", "draft", "review", "finalize"}

    after_first_review = next(h for h in reversed(history) if h["next"] == ["draft"] and h["values"].get("review", "").startswith("REVISE"))
    before = [h["checkpoint_id"] for h in history]
    with pytest.raises(ValueError, match="not editable"):
        await ex.fork(run_id, after_first_review["checkpoint_id"], {"trail": ["x"]})
    fork_id = await ex.fork(run_id, after_first_review["checkpoint_id"], {"review": "APPROVED"})
    assert (await ex.run(fork_id))["status"] == "paused"
    await ex.resume(fork_id)
    fork = await settle(ex, fork_id)
    assert fork["status"] == "completed" and fork["relation"] == "fork" and fork["parent_run_id"] == run_id
    fork_history = await ex.history(fork_id)
    # Branch from the edited checkpoint goes straight to finalize; the source run is untouched.
    assert [h["checkpoint_id"] for h in await ex.history(run_id)] == before
    assert fork_history[-len(before):] != before and any(h["checkpoint_id"] == after_first_review["checkpoint_id"] for h in fork_history)
    assert fork["output"]["revisions"] == 0

    replay_id = await ex.replay(run_id, after_first_review["checkpoint_id"])
    replay = await settle(ex, replay_id)
    assert replay["status"] == "completed" and replay["output"] == run["output"]
    with pytest.raises(ExecutorConflict, match="terminal"):
        await ex.replay(run_id, history[0]["checkpoint_id"])
    await ex.aclose()


@pytest.mark.asyncio
async def test_pause_at_boundary_failure_retry_and_interrupt():
    example = load_example()

    class Slow(example.Model):
        async def classify(self, question):
            await asyncio.sleep(0.3)
            return await super().classify(question)

    ex = support_executor(model=Slow("deterministic"))
    run_id = await ex.start({"question": "refund please"})
    await asyncio.sleep(0.05)
    await ex.pause(run_id)
    paused = await settle(ex, run_id)
    assert paused["status"] == "paused"
    assert (await ex.history(run_id))[0]["next"] == ["lookup"]  # stopped after classify committed
    await ex.resume(run_id)
    assert (await settle(ex, run_id))["status"] == "completed"
    await ex.aclose()

    class FailOnce(example.Model):
        failed = False

        async def review(self, *a):
            if not FailOnce.failed:
                FailOnce.failed = True
                raise RuntimeError("reviewer service unavailable")
            return await super().review(*a)

    ex = support_executor(model=FailOnce("deterministic"))
    run_id = await ex.start({"question": "refund please"})
    failed = await settle(ex, run_id)
    assert failed["status"] == "failed" and "reviewer service unavailable" in failed["error"]["message"]
    assert (await ex.history(run_id))[0]["next"] == ["review"]
    await ex.resume(run_id)
    assert (await settle(ex, run_id))["status"] == "completed"
    await ex.aclose()

    class S(TypedDict, total=False):
        ask: str
        approved: str
        log: Annotated[list, operator.add]

    def gate(state):
        return {"approved": interrupt({"question": "approve?"}), "log": ["gate"]}

    builder = StateGraph(S)
    builder.add_node("gate", gate)
    builder.add_edge(START, "gate")
    builder.add_edge("gate", END)
    ex = StudioExecutor(builder.compile(checkpointer=InMemorySaver()), name="hitl",
                        input_fields=[{"name": "ask", "label": "ask", "default": ""}])
    run_id = await ex.start({"ask": "x"})
    waiting = await settle(ex, run_id)
    assert waiting["status"] == "paused" and (await ex.history(run_id))[0]["interrupted"]
    await ex.resume(run_id, "yes")
    done = await settle(ex, run_id)
    assert done["status"] == "completed" and done["output"]["approved"] == "yes"
    await ex.aclose()


@pytest.mark.asyncio
async def test_registry_survives_restart_and_marks_interrupted(tmp_path):
    ex = support_executor(tmp_path)
    run_id = await ex.start({"question": "export error"})
    await settle(ex, run_id)
    ex.registry.update(run_id, status="running")  # simulate a crash mid-run
    ex.registry.close()
    again = support_executor(tmp_path)
    assert again.registry.get(run_id)["status"] == "interrupted"
    assert any(r["run_id"] == run_id for r in again.list_runs())


@pytest.mark.asyncio
async def test_http_app_requires_token_and_rejects_browsers():
    ex = support_executor()
    app = create_asgi_app(ex, token=TOKEN, port=8020)
    base = "http://127.0.0.1:8020/studio/v1"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base) as c:
        assert (await c.get("/describe")).status_code == 401
        assert (await c.get("/describe", headers={"x-studio-executor-token": "wrong" * 5})).status_code == 401
        good = {"x-studio-executor-token": TOKEN}
        assert (await c.get("/describe", headers={**good, "origin": "http://evil.example"})).status_code == 403
        assert (await c.get("/describe", headers={**good, "host": "evil.example"})).status_code == 400
        assert (await c.get("/describe", headers=good)).json()["protocol"] == "studio-executor/1"
        started = await c.post("/runs", json={"inputs": {"question": "refund"}}, headers=good)
        assert started.status_code == 202
        assert (await c.post("/runs", json={"inputs": {"bad": 1}}, headers=good)).status_code in {409, 422}
        assert (await c.get("/runs/missing", headers=good)).status_code == 404
        assert (await c.post("/runs/x/fork", content=b"not json", headers=good)).status_code == 400
    await ex.aclose()
