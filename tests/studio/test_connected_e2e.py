"""End to end, three real processes: the Studio server, the original M1 executor started by the Studio as a
separate process (own port, own checkpoint database), and pytest as the HTTP client. Offline: the executor
uses its RuleBasedGateway. Covers the acceptance check from the route-3 plan: run on the external executor,
fork from a checkpoint with an edit, the branch's trace reaches the Studio, Compare shows the difference,
and the source run is unchanged."""
from __future__ import annotations

import time

import httpx

from test_studio_e2e_server import Server, free_port  # same directory; tests/studio has no __init__.py

CODE_TASK = "Draft code for a UUID validation function"


def wait_run(client: httpx.Client, run_id: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] not in {"queued", "running", "pausing"} and not run.get("active"):
            return run
        time.sleep(0.3)
    raise AssertionError(f"timeout waiting for {run_id}: {run}")


def test_studio_launches_and_debugs_the_original_executor(tmp_path):
    studio_port, executor_port = free_port(), free_port()
    with Server(tmp_path, studio_port) as s:
        c = s.client
        created = c.post("/api/connectors", json={"name": "M1 executor", "mode": "launch", "port": executor_port, "model": "rule_based"})
        assert created.status_code == 201, created.text
        cid = created.json()["id"]
        assert created.json()["reachable"] is False
        started = c.post(f"/api/connectors/{cid}/start", timeout=90)
        assert started.status_code == 202, started.text
        status = c.get(f"/api/connectors/{cid}").json()
        assert status["reachable"] and status["process"]["pid"] and status["describe"]["provider_mode"] == "deterministic"

        run = c.post(f"/api/connectors/{cid}/runs", json={"inputs": {"task": CODE_TASK}})
        assert run.status_code == 202, run.text
        done = wait_run(c, run.json()["id"])
        assert done["status"] == "completed" and done["origin"] == "connected"
        assert done["capabilities"]["fork"] and done["definition"]["nodes"]
        spans = c.get(f"/api/runs/{done['id']}/spans").json()
        assert [x["node_id"] for x in spans if x["parent_span_id"] == f"cr_{done['id']}" and x["node_id"] and x["kind"] in {"agent", "tool", "gate"}] == \
            ["planner", "orchestrator", "pause_gate", "coder", "orchestrator", "finalizer"]

        cps = c.get(f"/api/runs/{done['id']}/checkpoints").json()
        assert cps["supported"] and cps["connected"]
        after_planner = next(x for x in cps["items"] if x["after_node"] == "planner")
        after_orch = next(x for x in cps["items"] if x["after_node"] == "orchestrator" and x["next"] == ["pause_gate"])
        editable = {e["path"]: e for e in after_orch["editable"]}
        assert editable["worker_instruction"]["value"] == CODE_TASK
        before = [x["checkpoint_id"] for x in cps["items"]]
        assert c.post(f"/api/runs/{done['id']}/fork", json={"checkpoint_id": after_planner["checkpoint_id"],
                                                             "changes": {"nodes.planner.output": "x"}}).status_code == 409
        fork = c.post(f"/api/runs/{done['id']}/fork", json={"checkpoint_id": after_orch["checkpoint_id"],
                                                             "changes": {"worker_instruction": "Use a regex and include unit tests"}})
        assert fork.status_code == 202, fork.text
        branch = wait_run(c, fork.json()["id"])
        assert branch["status"] == "completed" and branch["parent"]["id"] == done["id"], branch
        assert branch["fork_changes"] == {"worker_instruction": "Use a regex and include unit tests"}
        cmp = c.get(f"/api/runs/{branch['id']}/compare/{done['id']}").json()
        verdicts = {d["node_id"]: d["verdict"] for d in cmp["diff"]}
        assert verdicts["planner"] == "inherited"
        assert verdicts["pause_gate"] == "same"
        assert verdicts["coder"] == "differs"  # the coder's prompt carries the edited instruction
        coder = cmp["a"]["nodes"]["coder"]["output"]
        assert "Use a regex" in str(coder)  # RuleBasedGateway echoes the first 200 prompt characters
        after = [x["checkpoint_id"] for x in c.get(f"/api/runs/{done['id']}/checkpoints").json()["items"]]
        assert after == before  # the source run's checkpoints and head are unchanged

        # The executor's own OTLP spans (model/node spans) were exported to the Studio and attached by run id.
        deadline = time.time() + 20
        while time.time() < deadline:
            correlated = [x for x in c.get(f"/api/runs/{done['id']}/spans").json()
                          if (x["attributes"] or {}).get("studio.correlated_by") == "application_run_id"]
            if any(x["name"] == "m1.run" for x in correlated):
                break
            time.sleep(0.5)
        assert any(x["name"] == "m1.run" for x in correlated), "executor spans did not reach the Studio"
        assert not [r for r in c.get("/api/runs?origin=external").json() if "m1" in (r.get("integration") or "")]

        stopped = c.post(f"/api/connectors/{cid}/stop")
        assert stopped.status_code == 202 and stopped.json()["status"] == "stopped"
        unreachable = c.get(f"/api/connectors/{cid}").json()
        assert unreachable["reachable"] is False
        # Mirrored history stays readable in the Studio while the executor is down.
        offline = c.get(f"/api/runs/{done['id']}").json()
        assert offline["status"] == "completed" and "connector_unavailable" in offline
