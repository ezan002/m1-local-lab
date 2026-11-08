"""Browser-level validation of an SDK agent in Connected agents: real Studio server, the independent example
agent (examples/langgraph_agent/support_agent.py, studio_executor SDK) in its own process, real Chromium.
Offline: the agent runs in deterministic mode.

Usage:  .venv/Scripts/python.exe scripts/verify_sdk_ui.py
Writes  docs/evidence/connected/sdk-ui/receipt.json and screenshots. Fresh temporary data directories and free
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
OUT = ROOT / "docs" / "evidence" / "connected" / "sdk-ui"
QUESTION = "I was charged twice, how do I get a refund?"
TOKEN = "ui-check-" + "x" * 20


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    port, agent_port = free_port(), free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="studio-sdk-ui-"))
    base = f"http://127.0.0.1:{port}"
    receipt: dict = {"started_at": datetime.now(timezone.utc).isoformat(), "base": base, "data_dir": str(data_dir),
                     "agent": f"examples/langgraph_agent/support_agent.py on port {agent_port} (deterministic)",
                     "steps": [], "browser_errors": {}}
    env = {**os.environ, "PYTHONUTF8": "1", "STUDIO_EXECUTOR_TOKEN": TOKEN,
           "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")]))}
    log = open(data_dir / "processes.log", "wb")
    agent = subprocess.Popen([sys.executable, str(ROOT / "examples" / "langgraph_agent" / "support_agent.py"), "--port", str(agent_port),
                              "--data-dir", str(data_dir / "agent")], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    server = subprocess.Popen([sys.executable, "-m", "m1_local.cli", "studio", "--port", str(port), "--data-dir", str(data_dir / "studio")],
                              cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "PYTHONUTF8": "1"})
    status = "FAIL"
    page = None
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
            page.get_by_role("button", name="Connect executor").click()
            dialog = page.get_by_role("dialog")
            dialog.get_by_label("Agent type").select_option("studio_executor")
            dialog.get_by_label(re.compile("^Name")).fill("Support answer agent")
            dialog.get_by_label(re.compile("^Agent URL")).fill(f"http://127.0.0.1:{agent_port}")
            dialog.get_by_label(re.compile("^Token")).fill(TOKEN)
            step("connect dialog: attach an SDK agent by URL + token", "01-connect-sdk")
            dialog.get_by_role("button", name="Add connector").click()
            card = page.locator(".card", has_text="Support answer agent").first
            expect(card.get_by_text("connected", exact=True)).to_be_visible(timeout=30_000)
            expect(card.get_by_text("Lookup", exact=False).first).to_be_visible()
            expect(card.locator("code", has_text="review")).to_be_visible()
            step("agent online: its graph, fork fields and inputs are read from the SDK", "02-sdk-online")

            card.get_by_label(re.compile("^Customer question")).fill(QUESTION)
            card.get_by_role("button", name="Run on executor").click()
            page.wait_for_url(re.compile(r"#/runs/run_"))
            run_id = page.url.split("/runs/")[1]
            expect(page.locator("h1 .status-completed")).to_be_visible(timeout=60_000)
            expect(page.locator("[data-testid=node-draft]")).to_contain_text("×2")
            step("run mirrored: draft/review loop visible (×2)", "03-sdk-run-graph", run_id=run_id)

            page.get_by_role("tab", name="Checkpoints & fork").click()
            page.locator(".cp", has_text="after review").filter(has_text="→ draft").last.click()
            expect(page.get_by_text("review (written at this step · editable in a fork)")).to_be_visible()
            page.get_by_role("button", name="Fork with edits").click()
            dialog = page.get_by_role("dialog")
            dialog.get_by_label(re.compile("^review")).fill("APPROVED")
            step("fork the first review to APPROVED", "04-sdk-fork-dialog")
            dialog.get_by_role("button", name="Create branch and run").click()
            page.wait_for_url(lambda url: run_id not in url)
            fork_id = page.url.split("/runs/")[1]
            expect(page.locator("h1 .status-completed")).to_be_visible(timeout=60_000)
            expect(page.locator("[data-testid=node-classify]")).to_contain_text("inherited")
            step("branch skips the revision and finalizes", "05-sdk-fork-graph", fork_id=fork_id)

            page.get_by_role("tab", name="Compare").click()
            row = page.locator("tr", has=page.locator("td.mono", has_text=re.compile("^finalize$")))
            expect(row.locator("td").last).to_have_text("differs")
            step("compare: finalize differs (answer without the revision)", "06-sdk-compare")
            browser.close()
        status = "PASS" if not any(errors.values()) else "FAIL"
    except Exception as exc:  # noqa: BLE001
        receipt["exception"] = f"{type(exc).__name__}: {exc}"
        if page is not None:
            try:
                page.screenshot(path=str(OUT / "failure.jpg"), type="jpeg", quality=70, full_page=True)
            except Exception:  # noqa: BLE001
                pass
        raise
    finally:
        receipt["status"] = status
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        for proc in (server, agent):
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
        (OUT / "receipt.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
        print("STATUS", status, json.dumps(receipt["browser_errors"]))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
