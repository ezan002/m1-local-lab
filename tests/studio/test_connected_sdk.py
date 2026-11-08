"""Agent Studio driving an independently written LangGraph agent through the studio_executor SDK.

In-process: the real SDK HTTP app (ASGITransport) + the real ConnectedService/store.
Three processes: the example agent (examples/langgraph_agent/support_agent.py), the Studio server, pytest."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from m1_local.studio.connected import ConnectedService
from m1_local.studio.connectors import StudioExecutorConnector
from m1_local.studio.store import StudioStore
from studio_executor import ForkField, StudioExecutor, create_asgi_app
from test_studio_e2e_server import Server, free_port  # same directory; tests/studio has no __init__.py

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "s" * 24
QUESTION = "I was charged twice, how do I get a refund?"


def example_module():
    spec = importlib.util.spec_from_file_location("support_agent", ROOT / "examples" / "langgraph_agent" / "support_agent.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["support_agent"] = module
    spec.loader.exec_module(module)
    return module


@contextlib.asynccontextmanager
async def studio_with_sdk_agent(tmp_path: Path):
    example = example_module()
    executor = StudioExecutor(
        example.build(example.Model("deterministic"), InMemorySaver()), name="Support answer agent",
        input_fields=[{"name": "question", "label": "Customer question", "default": ""}],
        make_input=lambda i: {"question": i["question"], "trail": []}, provider_mode="deterministic",
        fork_fields=[ForkField("category"), ForkField("facts", "string_list"), ForkField("draft"), ForkField("review")],
        node_types={"classify": "agent", "lookup": "tool", "draft": "agent", "review": "agent", "finalize": "node"})
    store = StudioStore(tmp_path / "studio.sqlite")
    service = ConnectedService(store, studio_port=8100, data_root=tmp_path / "connected", poll_interval=0.05)
    record = service.create_connector(kind="studio_executor", name="Support agent", mode="attach",
                                      base_url="http://127.0.0.1:8020", token=TOKEN)
    service._clients[record["id"]] = StudioExecutorConnector(
        "http://127.0.0.1:8020", TOKEN, transport=httpx.ASGITransport(app=create_asgi_app(executor, token=TOKEN, port=8020)))
    try:
        yield service, record["id"], store
    finally:
        await service.shutdown()
        await executor.aclose()
        store.close()


async def settle(service, run_id, timeout=20):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = await service.sync(run_id)
        if run["status"] not in {"queued", "running", "pausing"}:
            return run
        await asyncio.sleep(0.05)
    raise AssertionError("did not settle")


def path_of(store, run_id):
    return [s["node_id"] for s in store.spans(run_id) if s["parent_span_id"] == f"cr_{run_id}" and s["node_id"] and s["kind"] != "pause"]


def test_connector_validation_and_token_masking(tmp_path):
    store = StudioStore(tmp_path / "s.sqlite")
    service = ConnectedService(store, studio_port=8100)
    with pytest.raises(ValueError, match="token is required"):
        service.create_connector(kind="studio_executor", name="x", mode="attach", base_url="http://127.0.0.1:8020")
    with pytest.raises(ValueError, match="attach"):
        service.create_connector(kind="studio_executor", name="x", mode="launch", token=TOKEN)
    with pytest.raises(ValueError, match="loopback"):
        service.create_connector(kind="studio_executor", name="x", mode="attach", base_url="http://10.0.0.5:8020", token=TOKEN)
    record = service.create_connector(kind="studio_executor", name="x", mode="attach", base_url="http://127.0.0.1:8020", token=TOKEN)
    status = asyncio.run(service.status(record["id"]))
    assert status["config"]["token"] == "•••" and TOKEN not in str(status)
    store.close()


@pytest.mark.asyncio
async def test_sdk_agent_run_fork_replay_through_the_studio(tmp_path):
    async with studio_with_sdk_agent(tmp_path) as (service, cid, store):
        describe = await service.describe(cid)
        assert describe["protocol"] == "studio-executor/1" and describe["provider_mode"] == "deterministic"
        run = await service.start_run(cid, {"question": QUESTION})
        done = await settle(service, run["id"])
        assert done["status"] == "completed" and done["output"]["category"] == "billing"
        assert done["input"] == {"question": QUESTION}
        assert path_of(store, run["id"]) == ["classify", "lookup", "draft", "review", "draft", "review", "finalize"]

        cps = await service.checkpoints(run["id"])
        first_review = next(c for c in reversed(cps) if c["after_node"] == "review")
        assert first_review["forkable"] and first_review["next"] == ["draft"]
        assert {e["path"]: e["value"] for e in first_review["editable"]}["review"].startswith("REVISE")
        before = [c["checkpoint_id"] for c in cps]

        with pytest.raises(ValueError, match="not editable"):
            await service.fork(run["id"], first_review["checkpoint_id"], {"trail": []}, relation="fork")
        # plan A: only the fields the review step wrote are editable here; earlier steps are forked from their own
        # checkpoints. The executor reports the writes per checkpoint, so this does not depend on values changing.
        assert [e["path"] for e in first_review["editable"]] == ["review"]
        assert {c["after_node"]: [e["path"] for e in c["editable"]] for c in cps if c["after_node"] in ("classify", "lookup")} == \
            {"classify": ["category"], "lookup": ["facts"]}
        with pytest.raises(ValueError, match="fork from the checkpoint after that step"):
            await service.fork(run["id"], first_review["checkpoint_id"], {"category": "technical"}, relation="fork")
        with pytest.raises(ValueError, match="cannot be edited"):
            await service.fork(run["id"], first_review["checkpoint_id"], {"review": "APPROVED", "draft": "x"}, relation="fork")
        second_review = next(c for c in cps if c["after_node"] == "review")      # wrote "APPROVED"
        assert [e["path"] for e in second_review["editable"]] == ["review"]
        fork = await settle(service, (await service.fork(run["id"], first_review["checkpoint_id"], {"review": "APPROVED"}, relation="fork"))["id"])
        assert fork["status"] == "completed" and fork["relation"] == "fork" and fork["parent_run_id"] == run["id"]
        assert path_of(store, fork["id"]) == ["finalize"]
        assert sorted(fork["inherited"]["nodes"]) == ["classify", "draft", "lookup", "review"]
        assert [s["name"] for s in store.spans(fork["id"]) if s["kind"] == "edit"] == ["fork edit"]
        assert [c["checkpoint_id"] for c in await service.checkpoints(run["id"])] == before  # source untouched

        after_classify = next(c for c in cps if c["after_node"] == "classify")
        branch = await settle(service, (await service.fork(run["id"], after_classify["checkpoint_id"], {"category": "technical"}, relation="fork"))["id"])
        assert branch["output"]["category"] == "technical" and branch["status"] == "completed"

        replay = await settle(service, (await service.fork(run["id"], after_classify["checkpoint_id"], {}, relation="replay"))["id"])
        assert replay["relation"] == "replay" and replay["output"] == done["output"]
        assert path_of(store, replay["id"]) == ["lookup", "draft", "review", "draft", "review", "finalize"]

        listed = await service.remote_runs(cid)
        assert {r["studio_run_id"] for r in listed} >= {run["id"], fork["id"], branch["id"], replay["id"]}


def test_three_processes_independent_agent_and_studio(tmp_path):
    agent_port, studio_port = free_port(), free_port()
    env = {**os.environ, "PYTHONUTF8": "1", "STUDIO_EXECUTOR_TOKEN": TOKEN,
           "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")]))}
    log = open(tmp_path / "agent.log", "wb")
    agent = subprocess.Popen([sys.executable, str(ROOT / "examples" / "langgraph_agent" / "support_agent.py"), "--port", str(agent_port),
                              "--data-dir", str(tmp_path / "agent-data")], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        with Server(tmp_path, studio_port) as s:
            c = s.client
            created = c.post("/api/connectors", json={"kind": "studio_executor", "name": "Support agent", "mode": "attach",
                                                      "base_url": f"http://127.0.0.1:{agent_port}", "token": TOKEN})
            assert created.status_code == 201, created.text
            cid = created.json()["id"]
            for _ in range(80):
                status = c.get(f"/api/connectors/{cid}").json()
                if status["reachable"]:
                    break
                time.sleep(0.25)
            assert status["reachable"], (tmp_path / "agent.log").read_text(errors="replace")
            assert status["config"]["token"] == "•••"
            run = c.post(f"/api/connectors/{cid}/runs", json={"inputs": {"question": QUESTION}}).json()
            done = wait(c, run["id"])
            assert done["status"] == "completed" and done["output"]["revisions"] == 1
            cps = c.get(f"/api/runs/{done['id']}/checkpoints").json()["items"]
            first_review = next(x for x in reversed(cps) if x["after_node"] == "review")
            fork = c.post(f"/api/runs/{done['id']}/fork", json={"checkpoint_id": first_review["checkpoint_id"], "changes": {"review": "APPROVED"}})
            assert fork.status_code == 202, fork.text
            branch = wait(c, fork.json()["id"])
            assert branch["status"] == "completed" and branch["output"]["revisions"] == 0
            cmp = c.get(f"/api/runs/{branch['id']}/compare/{done['id']}").json()
            verdicts = {d["node_id"]: d["verdict"] for d in cmp["diff"]}
            assert verdicts["classify"] == "inherited" and verdicts["finalize"] == "differs"
            after = [x["checkpoint_id"] for x in c.get(f"/api/runs/{done['id']}/checkpoints").json()["items"]]
            assert after == [x["checkpoint_id"] for x in cps]  # the source run's checkpoints are unchanged
            # Restart the agent process: runs and checkpoints persist in its own SQLite files.
            agent.terminate()
            agent.wait(timeout=20)
            offline = c.get(f"/api/runs/{done['id']}").json()
            assert "connector_unavailable" in offline
    finally:
        if agent.poll() is None:
            agent.terminate()
            agent.wait(timeout=20)
    agent2 = subprocess.Popen([sys.executable, str(ROOT / "examples" / "langgraph_agent" / "support_agent.py"), "--port", str(agent_port),
                               "--data-dir", str(tmp_path / "agent-data")], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        client = httpx.Client(base_url=f"http://127.0.0.1:{agent_port}/studio/v1", headers={"x-studio-executor-token": TOKEN}, trust_env=False)
        for _ in range(80):
            try:
                runs = client.get("/runs").json()
                break
            except httpx.HTTPError:
                time.sleep(0.25)
        assert len(runs) == 2 and {r["status"] for r in runs} == {"completed"}
        assert len(client.get(f"/runs/{runs[-1]['run_id']}/history").json()) >= 8
        client.close()
    finally:
        agent2.terminate()
        agent2.wait(timeout=20)


def wait(c: httpx.Client, run_id: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = c.get(f"/api/runs/{run_id}").json()
        if run["status"] not in {"queued", "running", "pausing"} and not run.get("active"):
            return run
        time.sleep(0.3)
    raise AssertionError(f"timeout waiting for {run_id}")
