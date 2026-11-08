from __future__ import annotations

import time
import pytest
from pathlib import Path

from fastapi.testclient import TestClient

from m1_local.api import create_app
from m1_local.models import RuleBasedGateway
from m1_local.runtime import AgentRuntime


@pytest.mark.parametrize("task", [" ", "\t\n"])
def test_blank_task_is_validation_error_without_creating_run(settings, task):
    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    with TestClient(create_app(settings, runtime), base_url="http://127.0.0.1:8000") as client:
        token = client.get("/api/session").json()["csrf_token"]
        response = client.post("/api/runs", headers={"x-m1-csrf": token}, json={"task": task})
        assert response.status_code == 422
        assert runtime.store.list_runs() == []


@pytest.mark.parametrize("changes", [{"plan": "wrong type"}, {"plan": [1]}, {"worker_instruction": []}])
def test_fork_values_are_typed_before_checkpoint_write(settings, changes):
    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    with TestClient(create_app(settings, runtime), base_url="http://127.0.0.1:8000") as client:
        token = client.get("/api/session").json()["csrf_token"]
        response = client.post("/api/runs/missing/fork", headers={"x-m1-csrf": token}, json={"checkpoint_id": "checkpoint", "changes": changes})
        assert response.status_code == 422


@pytest.mark.parametrize("suffix,headers", [
    ("events?last_event_id=-1", {}), ("events-log?after=-1", {}),
    ("events", {"Last-Event-ID": "not-an-integer"}),
    ("events", {"Last-Event-ID": str(2**63)}),
])
def test_event_cursor_rejects_invalid_or_sqlite_overflow_values(settings, suffix, headers):
    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    with TestClient(create_app(settings, runtime), base_url="http://127.0.0.1:8000") as client:
        assert client.get(f"/api/runs/missing/{suffix}", headers=headers).status_code == 422


def test_ipv6_loopback_host_and_origin(settings):
    settings = settings.model_copy(update={"app_host": "::1"})
    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    # Starlette's TestClient URL parser cannot parse IPv6 netlocs; send the wire headers.
    with TestClient(create_app(settings, runtime), base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/session", headers={"Host": "[::1]:8000", "Origin": "http://[::1]:8000"}).status_code == 200


def wait_for_status(client: TestClient, run_id: str, wanted: set[str], timeout: float = 3) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}")
        if response.status_code == 200 and response.json()["status"] in wanted:
            return response.json()
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not reach {wanted}")


def test_local_host_origin_csrf_and_run_api(settings, monkeypatch):
    allowed = Path(settings.allowed_file_dirs[0]) / "api.csv"
    allowed.write_text("name,value\napi,1\n", encoding="utf-8")
    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    monkeypatch.setattr(runtime.observability, "check_phoenix", lambda: None)
    app = create_app(settings, runtime)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "外部轨迹" in page.text
        assert 'id="sample"' in page.text
        page = client.get("/execution")
        assert page.status_code == 200
        assert "新任务" in page.text
        assert "本地 Magentic-One 控制台" in page.text
        assert "\ufffd" not in page.text
        css = client.get("/static/style.css")
        script = client.get("/static/app.js")
        assert css.status_code == script.status_code == 200
        assert "run-view" in page.text and "screenshot-dialog" in page.text
        assert 'id="tab-attribution-main"' in page.text
        assert 'aria-controls="panel-attribution-main"' in page.text
        assert 'id="attribution-timeline"' in page.text
        assert "events-log" in script.text and "Last-Event-ID" not in script.text
        assert "innerHTML" not in script.text
        assert "outerHTML" not in script.text
        assert "insertAdjacentHTML" not in script.text
        assert script.text.count("new EventSource") == 1
        assert "loadAttributionCases" in script.text and "AbortController" in script.text

        session = client.get("/api/session")
        assert session.status_code == 200
        csrf = session.json()["csrf_token"]
        assert session.json()["capabilities"]["fork_fields"] == ["plan", "worker_instruction", "next_agent"]
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["app"]["status"] == "healthy"

        empty = client.get("/api/runs")
        assert empty.status_code == 200 and empty.json() == []
        assert client.get("/api/runs/missing").status_code == 404
        assert client.get("/api/runs/missing/events-log").status_code == 404

        rejected = client.post("/api/runs", json={"task": "Read the file"})
        assert rejected.status_code == 403
        bad_origin = client.post(
            "/api/runs",
            headers={"x-m1-csrf": csrf, "origin": "https://attacker.example"},
            json={"task": "Read the file"},
        )
        assert bad_origin.status_code == 403
        bad_host = client.get("/api/session", headers={"host": "attacker.example"})
        assert bad_host.status_code == 400

        created = client.post(
            "/api/runs",
            headers={"x-m1-csrf": csrf},
            json={"task": "Read the file", "allowed_files": [str(allowed)]},
        )
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        state = wait_for_status(client, run_id, {"completed"})
        assert set(("status", "state", "run", "trace_id", "checkpoint_id", "next")) <= set(state)
        assert state["run"]["task"] == "Read the file"
        assert "head_config" not in state["run"]
        runs = client.get("/api/runs").json()
        assert runs[0]["run_id"] == run_id
        assert "updated_at" in runs[0]
        events = client.get(f"/api/runs/{run_id}/events-log").json()
        assert events and all({"id", "event", "data", "created_at"} <= set(item) for item in events)

        runtime.store.link_execution(
            execution_id="ui-trace-test", run_id=run_id, node_name="finalizer",
            input_checkpoint_id=None, output_checkpoint_id=state["checkpoint_id"],
            trace_id="0123456789abcdef0123456789abcdef", status="committed",
        )
        assert client.get(f"/api/runs/{run_id}").json()["trace_id"] == "0123456789abcdef0123456789abcdef"


def test_control_endpoints_follow_runtime_status_and_checkpoint_rules(settings):
    allowed = Path(settings.allowed_file_dirs[0]) / "control.csv"
    allowed.write_text("name,value\ncontrol,1\n", encoding="utf-8")
    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    app = create_app(settings, runtime)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        csrf = client.get("/api/session").json()["csrf_token"]
        headers = {"x-m1-csrf": csrf}
        created = client.post(
            "/api/runs", headers=headers,
            json={"task": "Read the file", "allowed_files": [str(allowed)]},
        )
        run_id = created.json()["run_id"]
        wait_for_status(client, run_id, {"completed"})
        history = client.get(f"/api/runs/{run_id}/history").json()
        terminal = next(item for item in history if not item["next"])
        nonterminal = next(item for item in history if item["next"])

        assert client.post(f"/api/runs/{run_id}/pause", headers=headers, json={}).status_code == 409
        assert client.post(f"/api/runs/{run_id}/resume", headers=headers, json={}).status_code == 409
        assert client.post(
            f"/api/runs/{run_id}/replay", headers=headers,
            json={"checkpoint_id": terminal["checkpoint_id"]},
        ).status_code == 409
        assert client.post(
            f"/api/runs/{run_id}/fork", headers=headers,
            json={"checkpoint_id": terminal["checkpoint_id"], "changes": {}},
        ).status_code == 409
        invalid = client.post(
            f"/api/runs/{run_id}/fork", headers=headers,
            json={"checkpoint_id": nonterminal["checkpoint_id"], "changes": {"task": "replace"}},
        )
        assert invalid.status_code == 422

        replayed = client.post(
            f"/api/runs/{run_id}/replay", headers=headers,
            json={"checkpoint_id": nonterminal["checkpoint_id"]},
        )
        assert replayed.status_code == 202
        replay_state = wait_for_status(client, replayed.json()["run_id"], {"completed"})
        assert replay_state["run"]["parent_run_id"] == run_id
        assert replay_state["run"]["source_checkpoint_id"] == nonterminal["checkpoint_id"]

        forked = client.post(
            f"/api/runs/{run_id}/fork", headers=headers,
            json={
                "checkpoint_id": nonterminal["checkpoint_id"],
                "changes": {"plan": ["review evidence"], "worker_instruction": "continue safely"},
            },
        )
        assert forked.status_code == 201
        fork_state = client.get(f"/api/runs/{forked.json()['run_id']}").json()
        assert fork_state["status"] == "paused"
        assert fork_state["run"]["parent_run_id"] == run_id
        assert fork_state["run"]["source_checkpoint_id"] == nonterminal["checkpoint_id"]
        resumed = client.post(f"/api/runs/{forked.json()['run_id']}/resume", headers=headers, json={})
        assert resumed.status_code == 202
        wait_for_status(client, forked.json()["run_id"], {"completed"})
