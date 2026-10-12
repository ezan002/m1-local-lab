"""Engine tests on the real LangGraph runtime + SQLite checkpointer (deterministic provider; no network)."""
import copy

import pytest

from m1_local.studio.engine import StudioEngine
from m1_local.studio.seeds import CALC_ID, RESEARCH_ID, seed
from m1_local.studio.store import StudioStore


@pytest.fixture
async def env(tmp_path):
    store = StudioStore(tmp_path / "s.sqlite")
    engine = StudioEngine(store, tmp_path / "cp.sqlite")
    await engine.startup()
    seed(store)
    yield store, engine, tmp_path
    await engine.shutdown()
    store.close()


def node_spans(store, run_id):
    spans = store.spans(run_id)
    root = next(s["id"] for s in spans if s["kind"] == "workflow")
    return [s for s in spans if s["parent_span_id"] == root and s["kind"] != "pause"]


async def run_research(store, engine, **kw):
    wf = store.get_workflow(RESEARCH_ID)
    run = await engine.start_run(wf, {"topic": "agent failures", "audience": "students"}, provider_override="deterministic", wait=True, **kw)
    return store.get_run(run["id"])


async def test_multi_agent_run_records_nested_spans_and_loop_guard(env):
    store, engine, _ = env
    run = await run_research(store, engine)
    assert run["status"] == "completed" and run["provider_mode"] == "deterministic"
    assert run["output"]["final"].startswith("# Brief: agent failures")
    path = [s["node_id"] for s in node_spans(store, run["id"])]
    # reviewer always says REVISE; the condition is forced false after max_true_visits=2
    assert path == ["start", "planner", "researcher", "reviewer", "needs_revision", "researcher", "reviewer",
                    "needs_revision", "researcher", "reviewer", "needs_revision", "final"]
    spans = store.spans(run["id"])
    researcher = next(s for s in spans if s["node_id"] == "researcher" and s["kind"] == "agent")
    children = [s for s in spans if s["parent_span_id"] == researcher["id"]]
    assert [c["kind"] for c in children] == ["llm", "tool", "llm"]
    tool = children[1]
    assert tool["input"]["text"].startswith("Plan for 'agent failures'") and tool["output"]["words"] > 0
    assert all(s["end_time"] and s["status"] == "ok" for s in spans)
    guard = [s for s in node_spans(store, run["id"]) if s["node_id"] == "needs_revision"][-1]
    assert guard["output"] == {"result": False, "branch": "false", "loop_guard": True}
    assert all(s["checkpoint_id"] for s in node_spans(store, run["id"]))


async def test_fork_changes_path_and_never_touches_source(env):
    store, engine, _ = env
    source = await run_research(store, engine)
    cps_before = await engine.checkpoints(source["id"])
    spans_before = store.spans(source["id"])
    first_review = [c for c in cps_before if c["after_node"] == "reviewer"][-1]  # history is newest-first
    fork = await engine.fork(source["id"], first_review["checkpoint_id"], {"nodes.reviewer.output": "APPROVED"}, wait=True)
    fork = store.get_run(fork["id"])
    assert fork["status"] == "completed" and fork["relation"] == "fork"
    assert fork["parent_run_id"] == source["id"] and fork["source_checkpoint_id"] == first_review["checkpoint_id"]
    assert fork["thread_id"] != source["thread_id"]
    assert fork["inherited"]["path"] == ["start", "planner", "researcher", "reviewer"]
    assert [s["node_id"] for s in node_spans(store, fork["id"])] == ["needs_revision", "final"]
    assert "Review: APPROVED" in fork["output"]["final"]
    assert "REVISE" in source["output"]["final"]
    # Source run: identical checkpoint history and spans after the fork.
    assert [c["checkpoint_id"] for c in await engine.checkpoints(source["id"])] == [c["checkpoint_id"] for c in cps_before]
    assert store.spans(source["id"]) == spans_before
    assert store.get_run(source["id"])["output"] == source["output"]


async def test_replay_restores_state_and_reproduces_deterministic_result(env):
    store, engine, _ = env
    source = await run_research(store, engine)
    cp = [c for c in await engine.checkpoints(source["id"]) if c["after_node"] == "planner"][0]
    replay = await engine.fork(source["id"], cp["checkpoint_id"], {}, relation="replay", wait=True)
    replay = store.get_run(replay["id"])
    assert replay["relation"] == "replay" and replay["status"] == "completed"
    assert replay["output"] == source["output"]
    assert node_spans(store, replay["id"])[0]["node_id"] == "researcher"  # planner was restored, not re-run


async def test_fork_rejects_invalid_requests(env):
    store, engine, _ = env
    source = await run_research(store, engine)
    cps = await engine.checkpoints(source["id"])
    terminal = cps[0]
    with pytest.raises(ValueError, match="terminal"):
        await engine.fork(source["id"], terminal["checkpoint_id"], {})
    mid = [c for c in cps if c["after_node"] == "reviewer"][0]
    for changes, msg in (({"visits.needs_revision": 0}, "not editable"), ({"nodes.final.output": "x"}, "has not executed"),
                         ({"nodes.needs_revision.output": {"result": True}}, "recomputed"), ({"input.nope": "x"}, "unknown input")):
        with pytest.raises(ValueError, match=msg):
            await engine.fork(source["id"], mid["checkpoint_id"], changes)
    with pytest.raises(KeyError):
        await engine.fork(source["id"], "1f00000-not-a-checkpoint", {})
    with pytest.raises(ValueError, match="replay does not accept changes"):
        await engine.fork(source["id"], mid["checkpoint_id"], {"input.topic": "x"}, relation="replay")
    initial = [c for c in cps if not c["after_node"] and c["next"]][0]
    with pytest.raises(ValueError, match="precedes the first node"):
        await engine.fork(source["id"], initial["checkpoint_id"], {})
    # no failed fork created a run
    assert len(store.list_runs()) == 1


async def test_breakpoint_pause_and_resume(env):
    store, engine, _ = env
    run = await run_research(store, engine, breakpoints=["reviewer"])
    assert run["status"] == "paused"
    assert [s["node_id"] for s in node_spans(store, run["id"])] == ["start", "planner", "researcher"]
    paused = [s for s in store.spans(run["id"]) if s["kind"] == "pause"]
    assert paused and paused[0]["status"] == "paused" and paused[0]["node_id"] == "reviewer"
    await engine.resume(run["id"], wait=True)
    run = store.get_run(run["id"])
    assert run["status"] == "completed"
    assert [s for s in store.spans(run["id"]) if s["kind"] == "pause"][0]["status"] == "resumed"
    with pytest.raises(ValueError, match="cannot resume"):
        await engine.resume(run["id"])


async def test_pause_request_takes_effect_at_next_node(env):
    store, engine, _ = env
    wf = store.get_workflow(RESEARCH_ID)
    run = store.create_run(workflow_id=wf["id"], workflow_version=1, workflow_name="x", origin="studio", thread_id="th_manual",
                           definition=wf["definition"], input={"topic": "t", "audience": "a"}, provider_mode="deterministic",
                           capabilities={"provider_override": "deterministic"}, breakpoints=[])
    store.update_run(run["id"], pause_requested=1)  # requested before the first node starts
    await engine._launch(run["id"], {"run_id": run["id"], "input": {"topic": "t", "audience": "a"}, "nodes": {}, "visits": {}, "path": []},
                         {"configurable": {"thread_id": "th_manual", "checkpoint_ns": ""}}, wait=True)
    assert store.get_run(run["id"])["status"] == "paused"
    assert node_spans(store, run["id"]) == []
    await engine.resume(run["id"], wait=True)
    assert store.get_run(run["id"])["status"] == "completed"


async def test_tool_failure_is_reported_and_retry_reexecutes_node(env):
    store, engine, _ = env
    wf = store.get_workflow(CALC_ID)
    run = await engine.start_run(wf, {"expression": "1/0"}, wait=True)
    run = store.get_run(run["id"])
    assert run["status"] == "failed"
    assert run["error"]["type"] == "ToolError" and run["error"]["node_id"] == "calc" and "division by zero" in run["error"]["message"]
    calc = [s for s in store.spans(run["id"]) if s["node_id"] == "calc"]
    assert {s["status"] for s in calc} == {"error"} and calc[0]["error"]["traceback"]
    assert run["output"] is None
    await engine.resume(run["id"], wait=True)  # retry: deterministic failure happens again, as a new attempt
    calc_after = [s for s in store.spans(run["id"]) if s["node_id"] == "calc" and s["kind"] == "tool" and s["parent_span_id"] is not None]
    assert len(calc_after) > len([s for s in calc if s["kind"] == "tool"])
    assert store.get_run(run["id"])["status"] == "failed"


async def test_condition_routes_both_ways(env):
    store, engine, _ = env
    wf = store.get_workflow(CALC_ID)
    big = store.get_run((await engine.start_run(wf, {"expression": "12 * (7 + 5)"}, wait=True))["id"])
    small = store.get_run((await engine.start_run(wf, {"expression": "2 + 2"}, wait=True))["id"])
    assert big["output"] == {"large": "LARGE: 12 * (7 + 5) = 144"}
    assert small["output"] == {"small": "small: 2 + 2 = 4"}


async def test_missing_credentials_fail_explicitly(env, monkeypatch):
    store, engine, _ = env
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    wf = store.get_workflow(RESEARCH_ID)
    run = await engine.start_run(wf, {"topic": "t", "audience": "a"}, provider_override="openai", wait=True)
    run = store.get_run(run["id"])
    assert run["status"] == "failed" and run["error"]["type"] == "MissingCredentials" and run["error"]["node_id"] == "planner"
    assert run["output"] is None
    llm = [s for s in store.spans(run["id"]) if s["kind"] == "llm"]
    assert llm[0]["status"] == "error" and "OPENAI_API_KEY" in llm[0]["error"]["message"]


async def test_missing_input_and_template_errors_fail(env):
    store, engine, _ = env
    wf = store.get_workflow(CALC_ID)
    run = store.get_run((await engine.start_run(wf, {"expression": "   "}, wait=True))["id"])
    assert run["status"] == "failed" and run["error"]["node_id"] == "start" and "missing required inputs" in run["error"]["message"]
    broken = copy.deepcopy(wf)
    broken["definition"]["nodes"][1]["config"]["arguments"]["expression"] = "{{nodes.nope.output}}"
    run = store.get_run((await engine.start_run(broken, {"expression": "1"}, wait=True))["id"])
    assert run["error"]["type"] == "TemplateError" and run["error"]["node_id"] == "calc"


async def test_restart_recovery_paused_and_interrupted_runs(tmp_path):
    store = StudioStore(tmp_path / "s.sqlite")
    engine = StudioEngine(store, tmp_path / "cp.sqlite")
    await engine.startup()
    seed(store)
    paused = await run_research(store, engine, breakpoints=["reviewer"])
    crashed = await run_research(store, engine, breakpoints=["final"])
    store.update_run(crashed["id"], status="running")  # simulate a process that died mid-run
    cps = await engine.checkpoints(paused["id"])
    await engine.shutdown()
    store.close()

    store2 = StudioStore(tmp_path / "s.sqlite")
    engine2 = StudioEngine(store2, tmp_path / "cp.sqlite")
    recovered = await engine2.startup()
    try:
        assert recovered == [crashed["id"]]
        assert store2.get_run(crashed["id"])["status"] == "interrupted"
        assert store2.get_run(paused["id"])["status"] == "paused"
        assert [c["checkpoint_id"] for c in await engine2.checkpoints(paused["id"])] == [c["checkpoint_id"] for c in cps]
        await engine2.resume(paused["id"], wait=True)
        await engine2.resume(crashed["id"], wait=True)
        assert store2.get_run(paused["id"])["status"] == "completed"
        assert store2.get_run(crashed["id"])["status"] == "completed"
        assert store2.get_workflow(RESEARCH_ID)["name"] == "Research → Review loop"
    finally:
        await engine2.shutdown()
        store2.close()


async def test_start_defaults_are_resolved_into_inputs(env):
    store, engine, _ = env
    wf = store.get_workflow(CALC_ID)
    run = store.get_run((await engine.start_run(wf, {}, wait=True))["id"])
    assert run["input"] == {"expression": "12 * (7 + 5)"}
    assert run["output"] == {"large": "LARGE: 12 * (7 + 5) = 144"}  # template reads input.expression
    with pytest.raises(ValueError, match="unknown inputs"):
        await engine.start_run(wf, {"nope": "1"})


async def test_cancel_running_run_then_resume(env, monkeypatch):
    import asyncio

    store, engine, _ = env
    original = engine.deterministic.chat
    gate = {"slow": True}

    async def slow_chat(*args, **kwargs):
        if gate["slow"]:
            await asyncio.sleep(30)
        return await original(*args, **kwargs)

    monkeypatch.setattr(engine.deterministic, "chat", slow_chat)
    wf = store.get_workflow(RESEARCH_ID)
    run = await engine.start_run(wf, {"topic": "t", "audience": "a"}, provider_override="deterministic")
    for _ in range(100):
        if any(s["kind"] == "llm" for s in store.spans(run["id"])):
            break
        await asyncio.sleep(0.02)
    await engine.cancel(run["id"])
    cancelled = store.get_run(run["id"])
    assert cancelled["status"] == "cancelled" and not engine.is_active(run["id"])
    planner = next(s for s in store.spans(run["id"]) if s["node_id"] == "planner" and s["kind"] == "agent")
    assert planner["status"] == "cancelled"
    assert not [s for s in store.spans(run["id"]) if s["status"] == "running"]  # no span left open
    gate["slow"] = False
    await engine.resume(run["id"], wait=True)
    assert store.get_run(run["id"])["status"] == "completed"


async def test_agent_tool_error_is_returned_to_model_and_recorded(env):
    store, engine, _ = env
    wf = store.get_workflow(RESEARCH_ID)
    researcher = next(n for n in wf["definition"]["nodes"] if n["id"] == "researcher")
    researcher["config"]["mock_tool_calls"] = [{"name": "calculator", "arguments": {"expression": "1/0"}}]
    run = store.get_run((await engine.start_run(wf, {"topic": "t", "audience": "a"}, provider_override="deterministic", wait=True))["id"])
    assert run["status"] == "completed"  # a failing tool call inside an agent loop is fed back, not fatal
    tool = next(s for s in store.spans(run["id"]) if s["kind"] == "tool" and s["node_id"] == "researcher")
    assert tool["status"] == "error" and "division by zero" in tool["error"]["message"]
    second_llm = [s for s in store.spans(run["id"]) if s["kind"] == "llm" and s["node_id"] == "researcher"][1]
    assert any(m["role"] == "tool" and "division by zero" in m["content"] for m in second_llm["input"]["messages"])
