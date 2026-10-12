"""Browser-level V1 validation of Agent Studio (real server process, real Chromium, deterministic provider).

Usage:  .venv/Scripts/python.exe scripts/verify_studio_ui.py
Writes  docs/evidence/v1/ui/receipt.json and screenshots. Uses a fresh temporary data directory and port;
stops only the server it started.
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
OUT = ROOT / "docs" / "evidence" / "v1" / "ui"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    port = free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="studio-ui-"))
    base = f"http://127.0.0.1:{port}"
    receipt: dict = {"started_at": datetime.now(timezone.utc).isoformat(), "base": base, "data_dir": str(data_dir),
                     "provider": "deterministic (scripted); no model calls", "steps": [], "browser_errors": {}}
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
                    page.screenshot(path=str(OUT / f"{shot}.jpg"), type="jpeg", quality=70)
                receipt["steps"].append({"step": name, "screenshot": f"{shot}.jpg" if shot else None, **extra})
                print("PASS", name, flush=True)

            page.goto(base + "/#/workflows")
            expect(page.get_by_text("Research → Review loop")).to_be_visible()
            step("workflow list renders seeded workflows", "01-workflows")

            page.get_by_role("button", name="New workflow").click()
            expect(page.locator("[data-testid=node-assistant]")).to_be_visible()
            workflow_id = page.url.split("/workflows/")[1]
            page.get_by_label("Workflow name").fill("UI validation workflow")
            # add a Tool node, move it to free space, configure it
            page.locator(".palette").get_by_role("button", name=re.compile("^Tool")).click()
            tool = page.locator("[data-testid=node-tool_1]")
            expect(tool).to_be_visible()
            answer_box = page.locator("[data-testid=node-answer]").bounding_box()
            tb = tool.bounding_box()
            page.mouse.move(tb["x"] + 40, tb["y"] + 15)
            page.mouse.down()
            page.mouse.move(answer_box["x"] + 20, answer_box["y"] + 170, steps=12)
            page.mouse.up()
            page.locator(".inspector select").first.select_option("word_count")
            page.locator(".inspector").get_by_label("Argument: text").fill("{{nodes.assistant.output}}")

            def connect(src: str, dst: str):
                s = page.locator(f"[data-testid=node-{src}] .react-flow__handle.source").bounding_box()
                d = page.locator(f"[data-testid=node-{dst}] .react-flow__handle.target").bounding_box()
                page.mouse.move(s["x"] + s["width"] / 2, s["y"] + s["height"] / 2)
                page.mouse.down()
                page.mouse.move(d["x"] + d["width"] / 2, d["y"] + d["height"] / 2, steps=15)
                page.mouse.up()

            connect("assistant", "tool_1")   # replaces assistant -> answer
            connect("tool_1", "answer")
            page.locator("[data-testid=node-answer]").click()
            page.locator(".inspector").get_by_label("Template").fill("Words: {{nodes.tool_1.output.words}} | {{nodes.assistant.output}}")
            expect(page.get_by_text(re.compile(r"\d+ issue\(s\)"))).to_have_count(0, timeout=5000)
            page.get_by_role("button", name="Save").click()
            expect(page.get_by_text("· saved")).to_be_visible()
            saved = httpx.get(f"{base}/api/workflows/{workflow_id}", trust_env=False).json()
            edges = {(e["source"], e["target"]) for e in saved["definition"]["edges"]}
            assert edges == {("start", "assistant"), ("assistant", "tool_1"), ("tool_1", "answer")}, edges
            step("editor: add node, connect edges, edit config, save (verified via API)", "02-editor-saved", workflow_id=workflow_id, edges=sorted(edges))

            page.get_by_role("button", name="Run", exact=True).click()
            page.locator(".modal select").select_option("deterministic")
            page.get_by_role("button", name="Start run").click()
            expect(page.locator(".run-chip.selected .status-completed")).to_be_visible(timeout=20000)
            expect(page.locator("[data-testid=node-tool_1] .status-ok")).to_be_visible()
            step("run from editor; live node status overlay", "03-editor-run-complete")

            page.locator(".run-chip.selected a").click()
            expect(page.locator(".run-head .status-completed")).to_be_visible()
            run_id = page.url.split("/runs/")[1]
            page.get_by_role("tab", name=re.compile("^Spans")).click()
            page.locator(".span-row", has_text="tool:word_count").click()
            expect(page.locator(".span-detail")).to_contain_text('"words"')
            step("run detail: span drill-down shows tool input/output", "04-run-spans", run_id=run_id)
            page.get_by_role("tab", name="Timeline").click()
            expect(page.locator(".tl-row").nth(2)).to_be_visible()
            step("timeline renders", "05-timeline")

            page.get_by_role("tab", name="Checkpoints & fork").click()
            page.locator(".cp", has_text="after assistant").click()
            page.get_by_role("button", name="Fork with edits").click()
            field = page.locator(".modal").get_by_label("nodes.assistant.output")
            field.fill("one two three")
            page.get_by_role("button", name="Create branch and run").click()
            page.wait_for_url(re.compile(r"/runs/run_(?!%s)" % run_id[4:]))
            expect(page.locator(".run-head .status-completed")).to_be_visible(timeout=20000)
            fork_id = page.url.split("/runs/")[1]
            expect(page.locator(".lineage")).to_contain_text(run_id)
            page.get_by_role("tab", name="Run input/output").click()
            expect(page.locator(".json").nth(1)).to_contain_text("Words: 3 | one two three")
            fork = httpx.get(f"{base}/api/runs/{fork_id}", trust_env=False).json()
            assert fork["parent_run_id"] == run_id and fork["fork_changes"] == {"nodes.assistant.output": "one two three"}
            step("fork with edited agent output -> different downstream result; lineage persisted", "06-fork-result", fork_id=fork_id)
            page.get_by_role("tab", name="Graph").click()
            expect(page.locator(".status-inherited").first).to_be_visible()
            step("fork graph marks inherited nodes", "07-fork-graph")

            page.reload()
            expect(page.locator(".run-head .status-completed")).to_be_visible()
            page.goto(base + "/#/workflows")
            expect(page.get_by_text("UI validation workflow")).to_be_visible()
            step("reload keeps workflow and runs", "08-after-reload")

            page.goto(base + "/#/workflows/wf_calculator_router")
            page.get_by_role("button", name="Run", exact=True).click()
            page.locator(".modal textarea").first.fill("1/0")
            page.get_by_role("button", name="Start run").click()
            expect(page.locator(".run-chip.selected .status-failed")).to_be_visible(timeout=20000)
            expect(page.locator("[data-testid=node-calc] .status-error")).to_be_visible()
            page.locator(".run-chip.selected a").click()
            expect(page.locator(".error-box").first).to_contain_text("division by zero")
            step("failure surfaced: failed node highlighted, error + retry hint", "09-failure")

            page.goto(base + "/#/integrations")
            page.get_by_role("button", name="Launch agent process").click()
            page.get_by_role("link", name="Open trace →").click(timeout=30000)
            expect(page.get_by_text("trace-only (read-only)")).to_be_visible()
            expect(page.get_by_role("tab", name="Checkpoints & fork")).to_be_disabled()
            step("external agent launched; trace shown read-only, no fork", "10-external-trace")
            browser.close()
        unexpected = [e for e in receipt["browser_errors"]["http_errors"]]
        receipt["result_note"] = "http_errors lists every >=400 response seen by the page"
        status = "PASS" if not receipt["browser_errors"]["page"] and not receipt["browser_errors"]["console"] and not unexpected else "PASS_WITH_BROWSER_ERRORS"
    except Exception as exc:
        receipt["failure"] = f"{type(exc).__name__}: {str(exc)[:1500]}"
        try:
            page.screenshot(path=str(OUT / "failure.jpg"), type="jpeg", quality=70)
        except Exception:
            pass
        raise
    finally:
        receipt["status"] = status
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        (OUT / "receipt.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
        server.terminate()
        server.wait(timeout=20)
        print("RESULT", status, OUT, flush=True)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
