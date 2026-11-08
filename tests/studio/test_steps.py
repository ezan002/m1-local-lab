"""Step view: runs flattened into numbered atomic actions (studio/steps.py, GET /api/runs/{id}/steps)."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from m1_local.studio.app import create_app
from m1_local.studio.steps import build_steps

BASE = "http://127.0.0.1:8100"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "codex_session_sample.jsonl"


def span(id, parent, kind, name, start, end, node_id=None, **extra):
    t = lambda s: f"2026-11-08T10:00:{s:06.3f}+00:00"  # noqa: E731
    return {"id": id, "parent_span_id": parent, "kind": kind, "name": name, "node_id": node_id, "status": "ok",
            "start_time": t(start), "end_time": t(end), "input": extra.pop("input", None),
            "output": extra.pop("output", None), "error": None, "attributes": extra.pop("attributes", {}), **extra}


def test_workflow_run_numbers_actions_and_keeps_control_nodes():
    spans = [
        span("root", None, "workflow", "wf", 0, 9),
        span("n1", "root", "start", "Brief", 0.0, 0.01, "start"),
        span("n2", "root", "agent", "Researcher", 1, 5, "researcher"),
        span("l1", "n2", "llm", "llm:openai", 1, 2, "researcher", attributes={"usage": {"prompt_tokens": 10, "completion_tokens": 5}, "response_model": "m"}),
        span("t1", "n2", "tool", "tool:word_count", 2, 2.5, "researcher"),
        span("l2", "n2", "llm", "llm:openai", 2.5, 5, "researcher"),
        span("n3", "root", "condition", "Needs revision?", 5, 5.01, "check"),
        span("p1", "root", "pause", "paused before x", 5.5, 5.6, "x"),
    ]
    steps = build_steps(spans)
    assert [s["step"] for s in steps] == [1, 2, 3, 4, 5]
    assert [(s["kind"], s["span_id"], s["agent"]) for s in steps] == [
        ("start", "n1", "Brief"), ("llm", "l1", "Researcher"), ("tool", "t1", "Researcher"),
        ("llm", "l2", "Researcher"), ("condition", "n3", "Needs revision?")]
    assert steps[1]["tokens"] == {"prompt": 10, "completion": 5, "total": 15} and steps[1]["model"] == "m"
    assert steps[1]["duration_ms"] == 1000


def test_duplicate_instrumentation_is_listed_once_and_covers_the_mirrored_node():
    # Connected executor: Studio mirrors graph nodes from checkpoints, the executor exports its own spans, and
    # LangChain's instrumentor records the same model call a second time.
    spans = [
        span("cr", None, "workflow", "M1 run", 0, 60),
        span("cn_p", "cr", "agent", "Planner", 0, 30, "planner"),
        span("run", "cr", "agent", "m1.run", 0, 60),
        span("lc", "run", "chain", "planner", 0.1, 29.9),
        span("chat", "lc", "llm", "ChatOpenAI", 0.2, 29.8, input="x", attributes={"langgraph_node": "planner"}),
        span("own", "run", "llm", "m1.model.planner", 0.21, 29.79, input={"task": "a long prompt"}, output="plan",
             attributes={"langgraph_node": "planner", "llm.model_name": "qwen3:8b"}),
        span("commit", "run", "chain", "m1.checkpoint.commit", 30, 30.01),
    ]
    steps = build_steps(spans)
    assert len(steps) == 1
    assert steps[0]["span_id"] == "own" and steps[0]["agent"] == "Planner" and steps[0]["model"] == "qwen3:8b"
    assert steps[0]["duplicate_span_ids"] == ["chat"]
    assert all("_duplicates" not in s for s in spans)


def test_nested_calls_fold_into_the_outer_step_and_uninstrumented_nodes_are_steps():
    spans = [
        span("root", None, "chain", "session", 0, 10),
        span("turn", "root", "agent", "turn 1", 0, 10, attributes={"agent.name": "codex"}),
        span("tool", "turn", "tool", "apply_patch", 1, 2, "apply_patch"),
        span("file", "tool", "tool", "file change: a.py", 1.5, 1.5),
        span("loose", "turn", "tool", "file change: b.py", 3, 3),
    ]
    steps = build_steps(spans)
    assert [(s["span_id"], s["agent"], s["nested_span_ids"]) for s in steps] == [
        ("tool", "codex · turn 1", ["file"]), ("loose", "codex · turn 1", [])]
    # A graph whose nodes export no spans of their own (an uninstrumented connected agent).
    plain = [span("cr", None, "workflow", "run", 0, 3), span("a", "cr", "agent", "generate_query", 0, 1, "generate_query"),
             span("b", "cr", "tool", "web_research", 1, 2, "web_research")]
    assert [(s["step"], s["kind"], s["name"]) for s in build_steps(plain)] == [(1, "agent", "generate_query"), (2, "tool", "web_research")]


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, port=8100), base_url=BASE) as c:
        c.headers["x-studio-csrf"] = c.get("/api/meta").json()["csrf_token"]
        yield c


def test_steps_endpoint_for_an_imported_codex_session(client):
    r = client.post("/api/traces/import", json={"filename": FIXTURE.name, "content": FIXTURE.read_text(encoding="utf-8")})
    assert r.status_code == 200, r.text
    run_id = r.json()["runs"][0]
    steps = client.get(f"/api/runs/{run_id}/steps").json()
    spans = client.get(f"/api/runs/{run_id}/spans").json()
    assert steps and [s["step"] for s in steps] == list(range(1, len(steps) + 1))
    assert {s["kind"] for s in steps} <= {"llm", "tool"} and "llm" in {s["kind"] for s in steps}
    assert len(steps) < len(spans)  # the session and turn spans are containers, not steps
    by_id = {s["id"]: s for s in spans}
    assert all(s["span_id"] in by_id for s in steps)
    assert json.dumps(steps)  # serialisable
    assert client.get("/api/runs/missing/steps").status_code == 404


def test_steps_endpoint_for_a_studio_workflow_run(client):
    import time
    r = client.post("/api/workflows/wf_research_review/runs", json={"inputs": {"topic": "loops", "audience": "devs"}, "provider_override": "deterministic"})
    run_id = r.json()["id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] == "completed" and not run["active"]:
            break
        time.sleep(0.1)
    steps = client.get(f"/api/runs/{run_id}/steps").json()
    kinds = [s["kind"] for s in steps]
    assert kinds[0] == "start" and kinds[-1] == "output"
    assert {"llm", "tool", "condition"} <= set(kinds) and "agent" not in kinds  # agent nodes are covered by their calls
    assert all(s["agent"] for s in steps)


def test_routing_decision_is_a_step_and_every_span_is_explained():
    from m1_local.studio.steps import span_roles
    spans = [
        span("cr", None, "workflow", "M1 run", 0, 60),
        span("cn_o", "cr", "agent", "Orchestrator", 0, 30, "orchestrator"),
        span("cn_f", "cr", "agent", "Finalizer", 31, 50, "finalizer"),
        span("run", "cr", "agent", "m1.run", 0, 60),
        span("lg", "run", "chain", "LangGraph", 0, 59),
        span("o", "lg", "chain", "orchestrator", 0, 30),
        span("seq", "o", "chain", "RunnableSequence", 0.1, 29),
        span("chat", "seq", "llm", "ChatOpenAI", 0.2, 28.9, attributes={"langgraph_node": "orchestrator"}),
        span("parse", "seq", "chain", "RunnableLambda", 28.9, 29),
        span("route", "o", "chain", "after_orchestrator", 29.5, 29.6, output="__REDACTED__"),
        span("f", "lg", "chain", "finalizer", 31, 50),
        span("chat2", "f", "llm", "ChatOpenAI", 31.1, 49.9, attributes={"langgraph_node": "finalizer"}),
        span("commit", "run", "chain", "m1.checkpoint.commit", 30, 30.01, attributes={"langgraph_node": "orchestrator"}),
    ]
    steps = build_steps(spans)
    assert [(s["kind"], s["agent"]) for s in steps] == [("llm", "Orchestrator"), ("route", "Orchestrator"), ("llm", "Finalizer")]
    assert steps[1]["output"]["next"] == "Finalizer"
    roles = {r["span_id"]: (r["role"], r["steps"]) for r in span_roles(spans, steps)}
    assert len(roles) == len(spans)
    assert roles["chat"] == ("step", [1]) and roles["route"] == ("step", [2])
    assert roles["o"] == ("container", [1, 2]) and roles["cn_o"] == ("container", [1]) and roles["cr"][0] == "root"
    assert roles["parse"][0] == "plumbing" and roles["commit"][0] == "plumbing"
