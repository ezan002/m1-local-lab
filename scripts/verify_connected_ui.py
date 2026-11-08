"""Browser-level validation of Connected agents (route 3): real Studio server, the original M1 executor started
by the Studio as a separate process, real Chromium. Offline: the executor uses its RuleBasedGateway.

Usage:  .venv/Scripts/python.exe scripts/verify_connected_ui.py
Writes  docs/evidence/connected/ui/receipt.json and screenshots. Uses a fresh temporary data directory and free
ports; stops only the processes it started.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "evidence" / "connected" / "ui"
TASK = "Draft code for a UUID validation function"
EDIT = "Use a regex and include unit tests"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    port, executor_port = free_port(), free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="studio-connected-ui-"))
    base = f"http://127.0.0.1:{port}"
    receipt: dict = {"started_at": datetime.now(timezone.utc).isoformat(), "base": base, "data_dir": str(data_dir),
                     "executor": f"m1_local AgentRuntime in its own process on port {executor_port}",
                     "provider": "RuleBasedGateway (deterministic, offline); no model calls", "steps": [], "browser_errors": {}}
    log = open(data_dir / "server.log", "wb")
    server = subprocess.Popen([sys.executable, "-m", "m1_local.cli", "studio", "--port", str(port), "--data-dir", str(data_dir)],
                              cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "PYTHONUTF8": "1"})
    status = "FAIL"
    try:
        for _ in range(120):
            try:
                if httpx.get(base + "/api/health", trust_env=False, timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            receipt["chromium"] = browser.version
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = {"console": [], "page": [], "failed_requests": [], "http_errors": []}
            receipt["browser_errors"] = errors
            page.on("console", lambda m: errors["console"].append(m.text[:300]) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors["page"].append(str(e)[:300]))
            page.on("requestfailed", lambda r: errors["failed_requests"].append(f"{r.method} {r.url} {r.failure}"))
            page.on("response", lambda r: errors["http_errors"].append(f"{r.status} {r.request.method} {r.url}") if r.status >= 400 else None)

            def step(name: str, shot: str | None = None, **extra):
                if shot:
                    page.screenshot(path=str(OUT / f"{shot}.jpg"), type="jpeg", quality=70, full_page=True)
                receipt["steps"].append({"step": name, "screenshot": f"{shot}.jpg" if shot else None, **extra})
                print("PASS", name, flush=True)

            page.goto(base + "/#/connected")
            expect(page.get_by_text("No connected executors")).to_be_visible()
            page.get_by_role("button", name="Connect executor").click()
            dialog = page.get_by_role("dialog")
            dialog.get_by_label("Port for the executor").fill(str(executor_port))
            step("connect dialog: launch the original executor as a separate process", "01-connect-dialog")
            dialog.get_by_role("button", name="Add connector").click()
            card = page.locator(".card", has_text="M1 LangGraph executor").first
            expect(card.get_by_text("offline")).to_be_visible()
            card.get_by_role("button", name="Start executor").click()
            expect(card.get_by_text("connected", exact=True)).to_be_visible(timeout=60_000)
            expect(card.get_by_text("Pause gate", exact=True)).to_be_visible()
            step("executor process started; graph, capabilities and fork fields read from it", "02-connector-online")

            card.get_by_label(re.compile("^Task")).fill(TASK)
            card.get_by_role("button", name="Run on executor").click()
            page.wait_for_url(re.compile(r"#/runs/run_"))
            run_id = page.url.split("/runs/")[1]
            expect(page.locator("h1 .status-completed")).to_be_visible(timeout=60_000)
            expect(page.locator(".run-head .badge", has_text="connected executor")).to_be_visible()
            expect(page.locator("[data-testid=node-coder]")).to_contain_text("ok")
            expect(page.locator("[data-testid=node-web_surfer]")).to_contain_text("not run")
            step("run on the executor mirrored: graph status from its checkpoints", "03-run-graph", run_id=run_id)

            page.get_by_role("tab", name="Timeline").click()
            expect(page.locator(".tl-row", has_text="Coder").first).to_be_visible()
            page.get_by_role("tab", name=re.compile("^Spans")).click()
            page.locator(".span-row", has_text="m1.run").first.click()
            page.locator(".span-detail").get_by_role("button", name=re.compile("Attributes")).click()
            expect(page.locator(".span-detail pre", has_text="studio.correlated_by")).to_be_visible()
            step("timeline + executor's own OTLP spans attached to the run", "04-spans-otlp")

            page.get_by_role("tab", name="Checkpoints & fork").click()
            cp = page.locator(".cp", has_text="after Orchestrator").filter(has_text="→ pause_gate").last
            cp.click()
            expect(page.get_by_text("worker_instruction (written at this step · editable in a fork)")).to_be_visible()
            step("checkpoints read live from the executor, with its editable fields", "05-checkpoints")
            page.get_by_role("button", name="Fork with edits").click()
            dialog = page.get_by_role("dialog")
            box = dialog.get_by_label(re.compile("^worker_instruction"))
            box.fill(EDIT)
            step("fork dialog renders the executor's fork schema", "06-fork-dialog")
            dialog.get_by_role("button", name="Create branch and run").click()
            page.wait_for_url(lambda url: run_id not in url)
            fork_id = page.url.split("/runs/")[1]
            expect(page.locator("h1 .status-completed")).to_be_visible(timeout=60_000)
            expect(page.locator(".lineage")).to_contain_text("fork")
            expect(page.locator("[data-testid=node-planner]")).to_contain_text("inherited")
            step("fork created by the executor, run, mirrored with lineage", "07-fork-graph", fork_id=fork_id)

            page.get_by_role("tab", name="Compare").click()
            row = page.locator("tr", has=page.locator("td.mono", has_text=re.compile("^coder$")))
            expect(row.locator("td").last).to_have_text("differs")
            expect(page.locator("tr", has=page.locator("td.mono", has_text=re.compile("^planner$"))).locator("td").last).to_have_text("inherited")
            step("compare: planner inherited, coder output differs", "08-compare")

            source = httpx.get(f"{base}/api/runs/{run_id}/checkpoints", trust_env=False, timeout=10).json()["items"]
            receipt["source_checkpoints_after_fork"] = len(source)
            page.goto(base + "/#/connected")
            expect(page.locator("table").last).to_contain_text("fork of")
            step("connected runs list shows the branch", "09-connected-list")

            card = page.locator(".card", has_text="M1 LangGraph executor").first
            card.get_by_role("button", name="Stop executor").click()
            expect(card.get_by_text("offline")).to_be_visible(timeout=20_000)
            page.goto(f"{base}/#/runs/{run_id}")
            expect(page.get_by_text("The executor is not reachable right now")).to_be_visible()
            expect(page.locator("h1 .status-completed")).to_be_visible()
            step("executor stopped: mirrored history still readable", "10-offline")
            browser.close()
        expected_http_errors = [e for e in errors["http_errors"]]
        receipt["notes"] = "HTTP >= 400 responses are listed; none are expected in this flow."
        status = "PASS" if not (errors["console"] or errors["page"] or errors["failed_requests"] or expected_http_errors) else "FAIL"
    except Exception as exc:  # noqa: BLE001
        receipt["exception"] = f"{type(exc).__name__}: {exc}"
        try:
            page.screenshot(path=str(OUT / "failure.jpg"), type="jpeg", quality=70, full_page=True)
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        receipt["status"] = status
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
        (OUT / "receipt.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
        print("STATUS", status, json.dumps(receipt["browser_errors"]))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
