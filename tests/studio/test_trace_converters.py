"""Raw (non-OpenTelemetry) trajectories converted to OTLP and imported through the External traces import."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from m1_local.studio.app import create_app
from m1_local.studio.converters import convert

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "codex_session_sample.jsonl"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, port=8100), base_url="http://127.0.0.1:8100") as c:
        c.headers["x-studio-csrf"] = c.get("/api/meta").json()["csrf_token"]
        yield c


def _spans(payload):
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    attrs = lambda s: {a["key"]: next(iter(a["value"].values())) for a in s["attributes"]}  # noqa: E731
    return [{**s, "a": attrs(s)} for s in spans]


def test_codex_session_maps_to_session_turn_model_and_tool_spans():
    fmt, payloads = convert(FIXTURE.read_text(encoding="utf-8"), "codex_session_sample.jsonl")
    assert fmt == "codex" and len(payloads) == 1
    spans = _spans(payloads[0])
    by_name = {s["name"]: s for s in spans}
    root, turn = by_name["codex session"], by_name["turn 1"]
    assert "parentSpanId" not in root and turn["parentSpanId"] == root["spanId"]
    assert root["traceId"] == "0190aaaabbbb7ccc8dddeeeeffff0001"
    assert root["a"]["openinference.span.kind"] == "CHAIN" and root["a"]["llm.model_name"] == "demo-model"
    assert turn["a"]["openinference.span.kind"] == "AGENT"
    assert turn["a"]["input.value"] == "Write is_even and test it"            # not the <environment_context> message
    assert turn["a"]["output.value"] == "Done: even.py, tests pass."
    llms = [s for s in spans if s["a"]["openinference.span.kind"] == "LLM"]
    assert [s["name"] for s in llms] == ["model call 1", "model call 2", "model call 3"]
    assert [s["a"]["llm.token_count.total"] for s in llms] == ["110", "250", "320"]
    assert llms[0]["a"]["output.value"].startswith("I will look around first.") and "[tool call] exec" in llms[0]["a"]["output.value"]
    # model call 2 starts when the first tool result was available, ends when its response was recorded
    assert int(llms[1]["startTimeUnixNano"]) == int(by_name["exec: ls missing_dir"]["endTimeUnixNano"])
    tools = [s for s in spans if s.get("parentSpanId") == turn["spanId"] and s["a"]["openinference.span.kind"] == "TOOL"]
    assert [s["name"] for s in tools] == ["exec: ls missing_dir", "exec: python -m unittest"]
    failed, ok = tools
    assert failed["status"]["code"] == 2 and "exited with code 1" in failed["status"]["message"]
    assert "status" not in ok and "status" not in root and "status" not in turn   # a failed command is not a failed session
    # a tool starts when the model response that requested it was recorded
    assert int(failed["startTimeUnixNano"]) == int(llms[0]["endTimeUnixNano"])
    files = [s for s in spans if s["name"].startswith("file change")]
    assert len(files) == 1 and files[0]["parentSpanId"] == ok["spanId"] and "even.py" in files[0]["name"]
    assert "is_even" in files[0]["a"]["output.value"]
    text = json.dumps(payloads)
    for private in ("SECRET SYSTEM PROMPT", "DEVELOPER INSTRUCTIONS", "user-PRIVATE", "environment_context"):
        assert private not in text


def test_otlp_and_unknown_files_are_not_treated_as_codex():
    assert convert(json.dumps({"resourceSpans": []})) is None
    assert convert('{"type": "something", "payload": {}}\n') is None
    assert convert("not json") is None


def test_codex_file_imports_as_an_external_trace(client):
    r = client.post("/api/traces/import", json={"filename": "rollout-x.jsonl", "content": FIXTURE.read_text(encoding="utf-8")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["format"] == "codex" and body["accepted"] == 8 and body["runs"] == ["ext_0190aaaabbbb7ccc8dddeeeeffff0001"]
    run = client.get(f"/api/runs/{body['runs'][0]}").json()
    assert run["origin"] == "external" and run["status"] == "completed" and run["error"]["error_span_count"] == 1
    names = [s["name"] for s in client.get(f"/api/runs/{body['runs'][0]}/spans").json()]
    # tree order: session, turn, then the turn's children by start time (model and tool calls interleaved)
    assert names[:2] == ["codex session", "turn 1"]
    assert names[2:] == ["model call 1", "exec: ls missing_dir", "model call 2", "exec: python -m unittest",
                         "file change: even.py", "model call 3"]
    # importing the same session again only reports duplicates
    again = client.post("/api/traces/import", json={"content": FIXTURE.read_text(encoding="utf-8")}).json()
    assert again["accepted"] == 0 and again["duplicates"] == 8
