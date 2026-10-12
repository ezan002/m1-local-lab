"""Layer-3/4 end-to-end: real `m1-local studio` server process, real HTTP, real OTLP from a launched subprocess,
then a server restart. Deterministic provider (no network, no cost)."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, data_dir: Path, port: int) -> None:
        self.data_dir, self.port = data_dir, port
        self.proc = None

    def __enter__(self):
        log = open(self.data_dir / f"server-{time.time_ns()}.log", "wb")
        self.proc = subprocess.Popen([sys.executable, "-m", "m1_local.cli", "studio", "--port", str(self.port), "--data-dir", str(self.data_dir)],
                                     cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "PYTHONUTF8": "1"})
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", trust_env=False, timeout=30)
        for _ in range(120):
            try:
                if self.client.get("/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        else:
            raise RuntimeError("server did not start")
        self.client.headers["x-studio-csrf"] = self.client.get("/api/meta").json()["csrf_token"]
        return self

    def __exit__(self, *exc):
        self.client.close()
        self.proc.terminate()
        self.proc.wait(timeout=20)

    def wait(self, run_id, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = self.client.get(f"/api/runs/{run_id}").json()
            if run["status"] in {"completed", "failed", "paused"} and not run.get("active"):
                return run
            time.sleep(0.2)
        raise AssertionError(f"timeout waiting for {run_id}")


def test_server_process_end_to_end_with_restart(tmp_path):
    port = free_port()
    with Server(tmp_path, port) as s:
        c = s.client
        assert c.get("/").status_code == 200 and "<div id=\"root\">" in c.get("/").text  # built frontend served
        # edit + save a workflow through the API used by the editor
        wf = c.get("/api/workflows/wf_research_review").json()
        wf["definition"]["nodes"][1]["config"]["mock_response"] = "EDITED plan for {{input.topic}}"
        assert c.put("/api/workflows/wf_research_review", json={"name": wf["name"], "description": wf["description"], "definition": wf["definition"]}).status_code == 200
        done = s.wait(c.post("/api/workflows/wf_research_review/runs", json={"inputs": {"topic": "e2e", "audience": "ci"}, "provider_override": "deterministic"}).json()["id"])
        assert done["status"] == "completed"
        planner = next(x for x in c.get(f"/api/runs/{done['id']}/spans").json() if x["node_id"] == "planner" and x["kind"] == "agent")
        assert planner["output"] == "EDITED plan for e2e"
        paused = s.wait(c.post("/api/workflows/wf_research_review/runs", json={"inputs": {"topic": "restart", "audience": "ci"}, "provider_override": "deterministic", "breakpoints": ["reviewer"]}).json()["id"])
        assert paused["status"] == "paused"
        cp = [x for x in c.get(f"/api/runs/{done['id']}/checkpoints").json()["items"] if x["after_node"] == "reviewer"][-1]
        fork = s.wait(c.post(f"/api/runs/{done['id']}/fork", json={"checkpoint_id": cp["checkpoint_id"], "changes": {"nodes.reviewer.output": "APPROVED"}}).json()["id"])
        assert fork["status"] == "completed" and fork["parent"]["id"] == done["id"]
        # executable external integration: subprocess exports OTLP/protobuf over HTTP back to this server
        launch = c.post("/api/integrations/external-research-agent/launch", json={"params": {"topic": "e2e", "mode": "deterministic", "fail_tool": "true"}}).json()
        for _ in range(150):
            status = c.get(f"/api/launches/{launch['id']}").json()
            if status["status"] != "running" and status["run_id"]:
                break
            time.sleep(0.2)
        assert status["exit_code"] == 2 and status["run_id"]
        ext = c.get(f"/api/runs/{status['run_id']}").json()
        assert ext["origin"] == "external" and ext["status"] == "failed" and "word_count backend unavailable" in ext["error"]["message"]
        before = {r["id"]: r["status"] for r in c.get("/api/runs").json() + c.get("/api/runs?origin=external").json()}
        spans_before = c.get(f"/api/runs/{done['id']}/spans").json()

    with Server(tmp_path, port) as s:  # restart on the same data directory
        c = s.client
        after = {r["id"]: r["status"] for r in c.get("/api/runs").json() + c.get("/api/runs?origin=external").json()}
        assert after == before
        assert c.get(f"/api/runs/{done['id']}/spans").json() == spans_before
        assert c.get("/api/workflows/wf_research_review").json()["definition"]["nodes"][1]["config"]["mock_response"].startswith("EDITED")
        assert c.get(f"/api/runs/{fork['id']}").json()["parent"]["id"] == done["id"]
        assert c.post(f"/api/runs/{paused['id']}/resume").status_code == 202
        assert s.wait(paused["id"])["status"] == "completed"
