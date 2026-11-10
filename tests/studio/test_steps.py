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


def test_workflow_run_numbers_messages_and_leaves_control_unnumbered():
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
    # agent message (decides on the tool) -> tool result -> agent message (writes the conclusion)
    assert [(s["step"], s["kind"], s["span_id"], s["agent"]) for s in steps] == [
        (1, "llm", "l1", "Researcher"), (2, "tool", "t1", "Researcher"), (3, "llm", "l2", "Researcher")]
    assert steps[0]["tokens"] == {"prompt": 10, "completion": 5, "total": 15} and steps[0]["model"] == "m"
    assert steps[0]["duration_ms"] == 1000
    from m1_local.studio.steps import span_roles
    roles = {r["span_id"]: r["role"] for r in span_roles(spans, steps)}
    assert roles["n1"] == roles["n3"] == "control" and roles["n2"] == "container"


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
    assert set(kinds) <= {"llm", "tool"} and {"llm", "tool"} <= set(kinds)  # messages only; control is not numbered
    assert [s["step"] for s in steps] == list(range(1, len(steps) + 1))
    assert all(s["labels"] == [] and s["marks"] == [] for s in steps)
    assert all(s["agent"] for s in steps)


def test_routing_is_control_not_a_step_and_every_span_is_explained():
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
    assert [(s["kind"], s["agent"]) for s in steps] == [("llm", "Orchestrator"), ("llm", "Finalizer")]
    roles = {r["span_id"]: (r["role"], r["steps"]) for r in span_roles(spans, steps)}
    assert len(roles) == len(spans)
    assert roles["chat"] == ("step", [1]) and roles["route"] == ("control", [])
    assert roles["o"] == ("container", [1]) and roles["cn_o"] == ("container", [1]) and roles["cr"][0] == "root"
    assert roles["parse"][0] == "plumbing" and roles["commit"][0] == "plumbing"


LONGRCA = {
    "question_ID": "swe_bench_pro__001",
    "history": [
        {"step": 0, "role": "user", "content": "You are working on the repository foo."},
        {"step": 1, "role": "assistant", "name": "DiagnostAgent", "content": "<think>look at tests</think>run pytest"},
        {"step": 2, "role": "user", "name": "Computer_terminal", "content": "2 failed"},
        {"step": 3, "role": "assistant", "name": "ActionAgent", "content": "<think>patch</think>edit file"},
    ],
    "mistake_agent": "DiagnostAgent (-> ActionAgent)", "mistake_step": 3, "mistake_reason": "wrong patch",
}


def test_longrca_trajectory_keeps_the_dataset_step_numbers(client):
    r = client.post("/api/traces/import", json={"filename": "swe_bench_pro__001.json", "content": json.dumps(LONGRCA)})
    assert r.status_code == 200, r.text
    assert r.json()["format"] == "longrca"
    run_id = r.json()["runs"][0]
    from m1_local.studio.converters import trajectory_run_id
    assert run_id == trajectory_run_id("swe_bench_pro__001")
    steps = client.get(f"/api/runs/{run_id}/steps").json()
    assert [(s["step"], s["kind"], s["agent"]) for s in steps] == [
        (0, "chain", "user"), (1, "agent", "DiagnostAgent"), (2, "tool", "Computer_terminal"), (3, "agent", "ActionAgent")]
    assert steps[1]["think"] == "look at tests" and steps[2]["output"] == "2 failed"
    assert steps[3]["labels"][0]["agent"] == "DiagnostAgent (-> ActionAgent)" and not steps[1]["labels"]
    # a localisation result, then many at once by question id
    assert client.put(f"/api/runs/{run_id}/marks", json={"step": 1, "note": "model says step 1"}).status_code == 200
    assert client.put(f"/api/runs/{run_id}/marks", json={"step": 99}).status_code == 400
    bulk = client.post("/api/marks/import", json={"source": "method-b", "items": [
        {"question_ID": "swe_bench_pro__001", "step": 2}, {"question_ID": "missing", "step": 1}]}).json()
    assert len(bulk["applied"]) == 1 and bulk["missing"][0]["reason"] == "trajectory not imported"
    steps = client.get(f"/api/runs/{run_id}/steps").json()
    assert [m["source"] for m in steps[1]["marks"]] == ["localization"] and steps[2]["marks"][0]["source"] == "method-b"
    assert client.delete(f"/api/runs/{run_id}/marks", params={"step": 1}).status_code == 200
    assert client.get(f"/api/runs/{run_id}/marks").json()[0]["step"] == 2


def test_trajectory_files_in_json_lines_and_lists():
    from m1_local.studio.converters import convert
    rows = [dict(LONGRCA, question_ID=f"q{i}") for i in range(3)]
    fmt, payloads = convert("\n".join(json.dumps(r) for r in rows), "mini.jsonl")
    assert fmt == "longrca" and len(payloads) == 3
    fmt, payloads = convert(json.dumps(rows), "mini.json")
    assert len(payloads) == 3
    history_as_string = dict(LONGRCA, history=json.dumps(LONGRCA["history"]))
    fmt, payloads = convert(json.dumps(history_as_string), "x.json")
    spans = payloads[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 5  # 4 messages + the trajectory root


def test_reimporting_a_revised_trajectory_updates_its_annotation(client):
    first = client.post("/api/traces/import", json={"filename": "a.json", "content": json.dumps(LONGRCA)}).json()
    revised = dict(LONGRCA, mistake_step=1, mistake_agent="DiagnostAgent", mistake_reason="revised upstream")
    second = client.post("/api/traces/import", json={"filename": "a.json", "content": json.dumps(revised)}).json()
    assert second["runs"] == first["runs"] and second["updated"] == 1 and second["accepted"] == 0
    steps = client.get(f"/api/runs/{first['runs'][0]}/steps").json()
    assert [s["step"] for s in steps if s["labels"]] == [1] and steps[1]["labels"][0]["reason"] == "revised upstream"
