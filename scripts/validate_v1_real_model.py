"""Model-backed V1 validation (makes real, billable OpenAI-compatible calls; ~10 small requests).

Requires OPENAI_API_KEY in the environment. Starts its own server on a free port with a temp data dir.
Writes docs/evidence/v1/real-model/receipt.json. Never prints or stores the key.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "evidence" / "v1" / "real-model"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("BLOCKED: OPENAI_API_KEY not set")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    port, data_dir = free_port(), Path(tempfile.mkdtemp(prefix="studio-real-"))
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen([sys.executable, "-m", "m1_local.cli", "studio", "--port", str(port), "--data-dir", str(data_dir)],
                              cwd=ROOT, stdout=open(data_dir / "server.log", "wb"), stderr=subprocess.STDOUT)
    receipt: dict = {"started_at": datetime.now(timezone.utc).isoformat(), "checks": []}
    c = httpx.Client(base_url=base, trust_env=False, timeout=60)
    try:
        for _ in range(120):
            try:
                if c.get("/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        meta = c.get("/api/meta").json()
        c.headers["x-studio-csrf"] = meta["csrf_token"]
        receipt["provider"] = {k: v for k, v in meta["provider"].items()}

        def wait(run_id: str) -> dict:
            for _ in range(600):
                run = c.get(f"/api/runs/{run_id}").json()
                if run["status"] in {"completed", "failed", "paused"} and not run["active"]:
                    return run
                time.sleep(0.5)
            raise TimeoutError(run_id)

        def summary(run: dict) -> dict:
            spans = c.get(f"/api/runs/{run['id']}/spans").json()
            root = {s["id"] for s in spans if s["kind"] == "workflow"}
            llm = [s for s in spans if s["kind"] == "llm"]
            return {"id": run["id"], "status": run["status"], "provider_mode": run["provider_mode"], "error": run["error"],
                    "relation": run.get("relation"), "parent_run_id": run.get("parent_run_id"),
                    "executed_path": [s["node_id"] for s in spans if s["parent_span_id"] in root and s["kind"] not in {"pause"}],
                    "llm_calls": [{"node": s["node_id"], "model": s["attributes"].get("response_model"), "usage": s["attributes"].get("usage"),
                                   "latency_ms": s["attributes"].get("latency_ms"), "status": s["status"]} for s in llm],
                    "tool_calls": [{"node": s["node_id"], "name": s["name"], "status": s["status"]} for s in spans if s["kind"] == "tool"],
                    "reviewer_outputs": [s["output"] for s in spans if s["node_id"] == "reviewer" and s["kind"] == "agent"],
                    "output_preview": json.dumps(run["output"], ensure_ascii=False)[:600] if run["output"] else None}

        src = wait(c.post("/api/workflows/wf_research_review/runs", json={
            "inputs": {"topic": "How do checkpoints enable debugging of LLM agent workflows?", "audience": "graduate students"},
            "provider_override": "openai"}).json()["id"])
        s1 = summary(src)
        receipt["checks"].append({"name": "real model multi-agent run", "pass": src["status"] == "completed" and all(x["model"] for x in s1["llm_calls"]), "run": s1})

        cps = c.get(f"/api/runs/{src['id']}/checkpoints").json()["items"]
        source_spans_before = c.get(f"/api/runs/{src['id']}/spans").json()
        first_review = [x for x in cps if x["after_node"] == "reviewer"][-1]
        original_verdict = first_review["state"]["nodes"]["reviewer"]["output"]
        new_verdict = "APPROVED" if "REVISE" in original_verdict.upper() else "REVISE: add one concrete numeric example from a real system."
        fork = wait(c.post(f"/api/runs/{src['id']}/fork", json={"checkpoint_id": first_review["checkpoint_id"],
                                                               "changes": {"nodes.reviewer.output": new_verdict}}).json()["id"])
        s2 = summary(fork)
        diverged = s2["executed_path"] != s1["executed_path"][len(first_review["state"]["path"]):]
        receipt["checks"].append({"name": "fork of real-model run with edited reviewer verdict changes the path",
                                  "pass": fork["status"] == "completed" and fork["parent_run_id"] == src["id"] and diverged
                                          and c.get(f"/api/runs/{src['id']}/spans").json() == source_spans_before,
                                  "original_verdict": original_verdict, "edited_verdict": new_verdict, "run": s2,
                                  "source_spans_unchanged": c.get(f"/api/runs/{src['id']}/spans").json() == source_spans_before,
                                  "source_checkpoints_unchanged": [x["checkpoint_id"] for x in c.get(f"/api/runs/{src['id']}/checkpoints").json()["items"]] == [x["checkpoint_id"] for x in cps]})

        planner_cp = [x for x in cps if x["after_node"] == "planner"][0]
        replay = wait(c.post(f"/api/runs/{src['id']}/replay", json={"checkpoint_id": planner_cp["checkpoint_id"]}).json()["id"])
        s3 = summary(replay)
        receipt["checks"].append({"name": "replay from planner checkpoint (state restored, model re-called)",
                                  "pass": replay["status"] == "completed" and s3["executed_path"][0] == "researcher",
                                  "output_identical_to_source": replay["output"] == src["output"],
                                  "note": "Non-identical output is expected: replay restores state, it does not make model calls deterministic.",
                                  "run": s3})

        tool_wf = {"default_provider": "openai", "nodes": [
            {"id": "start", "type": "start", "config": {"inputs": [{"name": "a", "default": "48271"}, {"name": "b", "default": "9137"}]}},
            {"id": "calc_agent", "type": "agent", "config": {"system_prompt": "You must use the calculator tool for arithmetic. Reply with only the number.",
                                                              "prompt": "Compute {{input.a}} * {{input.b}} - 17.", "tools": ["calculator"], "max_tool_rounds": 2}},
            {"id": "out", "type": "output", "config": {"template": "{{nodes.calc_agent.output}}"}}],
            "edges": [{"source": "start", "target": "calc_agent"}, {"source": "calc_agent", "target": "out"}]}
        wf = c.post("/api/workflows", json={"name": "Real tool-calling check", "definition": tool_wf}).json()
        trun = wait(c.post(f"/api/workflows/{wf['id']}/runs", json={"inputs": {}}).json()["id"])
        tspans = c.get(f"/api/runs/{trun['id']}/spans").json()
        tools = [s for s in tspans if s["kind"] == "tool"]
        expected = 48271 * 9137 - 17
        receipt["checks"].append({"name": "real model calls a tool (OpenAI function calling) and uses its result",
                                  "pass": trun["status"] == "completed" and any(t["status"] == "ok" for t in tools) and str(expected) in json.dumps(trun["output"]).replace(",", ""),
                                  "expected": expected, "tool_spans": [{"input": t["input"], "output": t["output"], "status": t["status"], "tool_call_id": t["attributes"].get("tool_call_id")} for t in tools],
                                  "run": summary(trun)})

        launch = c.post("/api/integrations/external-research-agent/launch", json={"params": {"topic": "agent tool failures", "mode": "openai", "fail_tool": "false"}}).json()
        for _ in range(240):
            st = c.get(f"/api/launches/{launch['id']}").json()
            if st["status"] != "running" and st["run_id"]:
                break
            time.sleep(0.5)
        ext = c.get(f"/api/runs/{st['run_id']}").json()
        ext_spans = c.get(f"/api/runs/{st['run_id']}/spans").json()
        llm_models = [s["attributes"].get("llm.model_name") for s in ext_spans if s["kind"] == "llm"]
        receipt["checks"].append({"name": "external OTel agent with real model -> read-only trace",
                                  "pass": st["exit_code"] == 0 and ext["status"] == "completed" and not ext["capabilities"]["fork"],
                                  "run_id": ext["id"], "span_count": len(ext_spans), "llm_models": llm_models,
                                  "token_attributes": [{k: v for k, v in s["attributes"].items() if k.startswith("llm.token_count")} for s in ext_spans if s["kind"] == "llm"]})
    finally:
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        receipt["status"] = "PASS" if receipt["checks"] and all(x["pass"] for x in receipt["checks"]) else "FAIL"
        (OUT / "receipt.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
        c.close()
        server.terminate()
        server.wait(timeout=20)
    for check in receipt["checks"]:
        print("PASS" if check["pass"] else "FAIL", check["name"])
    print("RESULT", receipt["status"])
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
