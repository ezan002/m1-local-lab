"""HTTP API tests (FastAPI TestClient, real engine + SQLite, deterministic provider)."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from m1_local.studio.app import create_app

BASE = "http://127.0.0.1:8100"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, port=8100), base_url=BASE) as c:
        c.headers["x-studio-csrf"] = c.get("/api/meta").json()["csrf_token"]
        yield c


def wait(client, run_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in {"completed", "failed", "paused", "cancelled"} and not run["active"]:
            return run
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not finish: {run['status']}")


def test_boundary_checks(tmp_path):
    with TestClient(create_app(tmp_path, port=8100), base_url=BASE) as c:
        assert c.get("/api/workflows", headers={"host": "evil.test"}).status_code == 400
        assert c.post("/api/workflows", json={}).status_code == 403  # no CSRF token
        token = c.get("/api/meta").json()["csrf_token"]
        assert c.post("/api/workflows", json={}, headers={"x-studio-csrf": token, "origin": "http://evil.test"}).status_code == 403
        meta = c.get("/api/meta").json()
        assert "api_key" not in json.dumps(meta).lower().replace("openai_key_configured", "")


def test_workflow_crud_validation_and_persistence(tmp_path):
    with TestClient(create_app(tmp_path, port=8100), base_url=BASE) as c:
        c.headers["x-studio-csrf"] = c.get("/api/meta").json()["csrf_token"]
        seeded = {w["id"] for w in c.get("/api/workflows").json()}
        assert seeded == {"wf_research_review", "wf_calculator_router"}
        wf = c.get("/api/workflows/wf_calculator_router").json()
        bad = json.loads(json.dumps(wf["definition"]))
        bad["edges"] = bad["edges"][:-1]
        r = c.put("/api/workflows/wf_calculator_router", json={"name": wf["name"], "definition": bad})
        assert r.status_code == 422 and any("true" in e for e in r.json()["detail"]["errors"])
        assert c.post("/api/workflows/validate", json=bad).json()["valid"] is False
        wf["definition"]["nodes"][2]["config"]["right"] = "1000"
        r = c.put("/api/workflows/wf_calculator_router", json={"name": "Renamed", "description": "d", "definition": wf["definition"]})
        assert r.status_code == 200 and r.json()["version"] == 2
        created = c.post("/api/workflows", json={"name": "New", "definition": wf["definition"]}).json()
    # Restart the app on the same data directory: edits and new workflow persist.
    with TestClient(create_app(tmp_path, port=8100), base_url=BASE) as c:
        wf = c.get("/api/workflows/wf_calculator_router").json()
        assert wf["name"] == "Renamed" and wf["version"] == 2 and wf["definition"]["nodes"][2]["config"]["right"] == "1000"
        assert c.get(f"/api/workflows/{created['id']}").status_code == 200


def test_run_inspect_fork_compare_via_api(client):
    r = client.post("/api/workflows/wf_research_review/runs", json={"inputs": {"topic": "loops", "audience": "devs"}, "provider_override": "deterministic"})
    assert r.status_code == 202
    run = wait(client, r.json()["id"])
    assert run["status"] == "completed" and run["capabilities"]["fork"] is True
    spans = client.get(f"/api/runs/{run['id']}/spans").json()
    assert {s["kind"] for s in spans} >= {"workflow", "agent", "llm", "tool", "condition", "output"}
    cps = client.get(f"/api/runs/{run['id']}/checkpoints").json()
    assert cps["supported"] and len(cps["items"]) > 5
    reviewer_cp = [c for c in cps["items"] if c["after_node"] == "reviewer"][-1]
    terminal = cps["items"][0]
    assert client.post(f"/api/runs/{run['id']}/fork", json={"checkpoint_id": terminal["checkpoint_id"], "changes": {}}).status_code == 409
    assert client.post(f"/api/runs/{run['id']}/fork", json={"checkpoint_id": "nope", "changes": {}}).status_code == 404
    r = client.post(f"/api/runs/{run['id']}/fork", json={"checkpoint_id": reviewer_cp["checkpoint_id"], "changes": {"nodes.reviewer.output": "APPROVED"}})
    assert r.status_code == 202
    fork = wait(client, r.json()["id"])
    assert fork["parent"]["id"] == run["id"] and fork["relation"] == "fork"
    assert client.get(f"/api/runs/{run['id']}").json()["children"][0]["id"] == fork["id"]
    cmp = client.get(f"/api/runs/{fork['id']}/compare/{run['id']}").json()
    assert cmp["a"]["executed_path"] == ["needs_revision", "final"]
    assert cmp["b"]["executed_path"].count("researcher") == 3
    verdict = {d["node_id"]: d["verdict"] for d in cmp["diff"]}
    assert verdict["planner"] == "inherited" and verdict["final"] == "differs"
    assert client.post(f"/api/runs/{run['id']}/resume").status_code == 409
    assert client.post(f"/api/runs/{run['id']}/pause").status_code == 409


def test_run_errors_surface_through_api(client):
    r = client.post("/api/workflows/wf_calculator_router/runs", json={"inputs": {"expression": "1/0"}})
    run = wait(client, r.json()["id"])
    assert run["status"] == "failed" and run["error"]["node_id"] == "calc"
    assert client.post("/api/workflows/wf_calculator_router/runs", json={"breakpoints": ["ghost"]}).status_code == 422
    assert client.post("/api/workflows/missing/runs", json={}).status_code == 404


def _otlp_protobuf(fail: bool = False) -> bytes:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "sdk-test-agent"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("session") as root:
        root.set_attribute("openinference.span.kind", "CHAIN")
        root.set_attribute("input.value", json.dumps({"q": "hi"}))
        with tracer.start_as_current_span("writer") as agent:
            agent.set_attribute("openinference.span.kind", "AGENT")
            agent.set_attribute("agent.name", "writer")
            agent.set_attribute("output.value", "draft")
            if fail:
                try:
                    raise RuntimeError("tool exploded")
                except RuntimeError as exc:
                    agent.record_exception(exc)
                    agent.set_status(Status(StatusCode.ERROR, str(exc)))
                    root.set_status(Status(StatusCode.ERROR, "child failed"))
    provider.shutdown()
    return encode_spans(exporter.get_finished_spans()).SerializeToString()


def test_otlp_protobuf_ingest_is_read_only_and_idempotent(client):
    body = _otlp_protobuf()
    r = client.post("/v1/traces", content=body, headers={"content-type": "application/x-protobuf"})
    assert r.status_code == 200 and r.json()["accepted"] == 2
    run_id = r.json()["runs"][0]
    again = client.post("/v1/traces", content=body, headers={"content-type": "application/x-protobuf"}).json()
    assert again["accepted"] == 0 and again["duplicates"] == 2
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["origin"] == "external" and run["status"] == "completed" and run["capabilities"]["fork"] is False
    spans = client.get(f"/api/runs/{run_id}/spans").json()
    writer = next(s for s in spans if s["name"] == "writer")
    session = next(s for s in spans if s["name"] == "session")
    assert writer["parent_span_id"] == session["id"] and writer["kind"] == "agent" and writer["node_id"] == "writer"
    assert session["input"] == {"q": "hi"}
    assert client.get(f"/api/runs/{run_id}/checkpoints").json()["supported"] is False
    for action in ("resume", "pause"):
        assert client.post(f"/api/runs/{run_id}/{action}").status_code == 409
    assert client.post(f"/api/runs/{run_id}/fork", json={"checkpoint_id": "x", "changes": {}}).status_code == 409


def test_otlp_failures_and_malformed_payloads(client):
    r = client.post("/v1/traces", content=_otlp_protobuf(fail=True), headers={"content-type": "application/x-protobuf"})
    run = client.get(f"/api/runs/{r.json()['runs'][0]}").json()
    assert run["status"] == "failed" and run["error"]["type"] == "RuntimeError" and "tool exploded" in run["error"]["message"]
    writer = next(s for s in client.get(f"/api/runs/{run['id']}/spans").json() if s["name"] == "writer")
    assert writer["status"] == "error" and writer["error"]["traceback"]
    assert client.post("/v1/traces", content=b"\x00\xffnot-protobuf", headers={"content-type": "application/x-protobuf"}).status_code == 400
    assert client.post("/v1/traces", content=b"{bad", headers={"content-type": "application/json"}).status_code == 400
    incomplete = {"resourceSpans": [{"scopeSpans": [{"spans": [{"name": "no ids"}]}]}]}
    r = client.post("/v1/traces", json=incomplete)
    assert r.status_code == 400 and "traceId" in r.json()["rejected"][0]
    ok_json = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "json-agent"}}]},
                                  "scopeSpans": [{"spans": [{"traceId": "0af7651916cd43dd8448eb211c80319c", "spanId": "b7ad6b7169203331",
                                                             "name": "root", "startTimeUnixNano": "1700000000000000000", "endTimeUnixNano": "1700000001000000000"}]}]}]}
    r = client.post("/v1/traces", json=ok_json)
    assert r.status_code == 200 and r.json()["runs"] == ["ext_0af7651916cd43dd8448eb211c80319c"]
