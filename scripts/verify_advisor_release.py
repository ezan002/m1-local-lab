"""Zero-fee release acceptance; run from an isolated checkout with both environments.

Creates a NEW output directory and synthetic Phoenix data. Never loads research
datasets or uses OpenAIModelGateway / WebSurfer. See --help for invocation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx


ROOT = Path(__file__).resolve().parents[1]
PHOENIX = "http://127.0.0.1:6006"
APP = "http://127.0.0.1:8000"


def listeners() -> list[dict]:
    result = subprocess.run(["netstat.exe", "-ano", "-p", "tcp"], capture_output=True, text=True, check=True)
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[0] == "TCP" and fields[3] == "LISTENING":
            if fields[1].rsplit(":", 1)[-1] in {"4317", "6006", "8000", "8001"}:
                rows.append({"address": fields[1], "pid": int(fields[4])})
    return rows


def wait_http(url: str, seconds: int = 180) -> dict:
    deadline = time.monotonic() + seconds
    with httpx.Client(trust_env=False, timeout=3) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(url)
                if response.status_code == 200:
                    return response.json() if "json" in response.headers.get("content-type", "") else {}
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
    raise RuntimeError(f"health check timed out: {url}")


def fixture_records():
    from m1_local.external_datasets import AttributionGroundTruth, ExternalTraceRecord

    result = []
    for index in range(3):
        trace_source = index == 2
        trace = ([{"step_id": 5, "agent_name": "Planner", "output": "plan"}, {"step_id": 6, "agent_name": "WebSurfer", "output": "timeout error"}]
                 if trace_source else [{"trace_index": 0, "role": "Planner", "content": "plan"}, {"trace_index": 1, "role": "WebSurfer", "content": "timeout error" if index == 0 else "ordinary response"}])
        result.append(ExternalTraceRecord(
            dataset_case_id=f"advisor-fixture-{index}", source_dataset="TraceElephant" if trace_source else "Who&When",
            source_version="synthetic-release-fixture", source_split="fixture", source_case_id=str(index),
            source_license="locally authored synthetic fixture", task="Synthetic attribution fixture <script>throw Error('must remain text')</script>",
            expected_answer="synthetic expected answer", original_system="synthetic", trace=trace,
            ground_truth=AttributionGroundTruth(responsible_agent="WebSurfer", decisive_step="99" if trace_source else "1", failure_reason="synthetic failure"),
            metadata={"truncated": trace_source, "original_trace_items": 31 if trace_source else 2},
        ))
    return result


def serve_offline(output: Path) -> None:
    import uvicorn
    from m1_local.api import create_app
    from m1_local.config import Settings
    from m1_local.models import RuleBasedGateway
    from m1_local.runtime import AgentRuntime

    settings = Settings(_env_file=None, openai_api_key="", app_port=8000,
        metadata_db=output / "runtime/app.sqlite", checkpoint_db=output / "runtime/checkpoints.sqlite",
        artifact_root=output / "runtime/artifacts", phoenix_working_dir=output / "phoenix",
        phoenix_project_name="advisor-release-smoke", allowed_file_dirs=[str(output)], headless=True)
    runtime = AgentRuntime(settings, RuleBasedGateway())
    app = create_app(settings, runtime)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            fixture = output / "fixture.csv"
            fixture.write_text("item,value\nalpha,1\nbeta,2\n", encoding="utf-8")
            run_id = await runtime.create_run("Read the release fixture file", allowed_files=[str(fixture)])
            assert runtime.store.get_run(run_id)["status"] == "completed"
            (output / "smoke.json").write_text(json.dumps({"run_id": run_id, "trace_id": runtime.store.latest_trace_id(run_id), "gateway": "RuleBasedGateway", "paid_model_calls": 0}), encoding="utf-8")
            yield

    app.router.lifespan_context = lifespan
    uvicorn.run(app, host="127.0.0.1", port=8000, loop="asyncio", log_level="warning")


def query_trace(client, run_id: str) -> dict:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        frame = client.spans.get_spans_dataframe(project_identifier="advisor-release-smoke", limit=2000, timeout=10)
        if "attributes.run_id" in frame.columns:
            selected = frame[frame["attributes.run_id"].astype(str) == run_id]
            if not selected.empty and "m1.run" in set(selected["name"]):
                return {"span_rows": len(selected), "trace_ids": sorted(set(selected["context.trace_id"].astype(str)))}
        time.sleep(0.5)
    raise RuntimeError("Phoenix did not persist the RuleBasedGateway trace")


def browser_acceptance(output: Path) -> dict:
    from playwright.sync_api import expect, sync_playwright

    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "browsers")
    errors = {"console": [], "page": [], "request_failed": [], "http_errors": [], "external_requests": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1366, "height": 768}, base_url=APP)

        def attach(page):
            page.on("console", lambda message: errors["console"].append({"page": page.url, "message": message.text}) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors["page"].append({"page": page.url, "message": str(error)}))

        def route(request_route):
            hostname = urlparse(request_route.request.url).hostname
            if hostname not in {"127.0.0.1", "localhost", "::1"}:
                errors["external_requests"].append(request_route.request.url)
                request_route.abort()
            else:
                request_route.continue_()

        context.route("http**://**/*", route)
        context.on("page", attach)
        context.on("requestfailed", lambda request: errors["request_failed"].append({"url": request.url, "failure": request.failure}))
        context.on("response", lambda response: errors["http_errors"].append({"url": response.url, "status": response.status}) if response.status >= 400 else None)
        page = context.new_page()
        page.set_default_timeout(20_000)
        page.goto("/", wait_until="networkidle")
        expect(page.locator("#runs-list button")).to_have_count(1)
        page.locator("#runs-list button").click()
        expect(page.locator("#run-view")).to_be_visible()
        expect(page.locator("#checkpoint-count")).not_to_have_text("0")
        page.screenshot(path=str(output / "task-history.png"), full_page=True)
        page.locator("#tab-attribution-main").click()
        expect(page.locator("#attribution-cases-body tr")).to_have_count(9)
        expect(page.locator("#attribution-metrics-body tr")).to_have_count(3)
        page.locator("#attribution-method-filter").select_option("rule_based")
        expect(page.locator("#attribution-cases-body tr")).to_have_count(3)
        page.locator("#attribution-source-filter").select_option("TraceElephant")
        expect(page.locator("#attribution-cases-body tr")).to_have_count(1)
        page.locator("#attribution-truncated-filter").select_option("true")
        expect(page.locator("#attribution-cases-body tr")).to_have_count(1)
        page.locator("#attribution-cases-body .case-select").click()
        expect(page.locator("#attribution-detail-content")).to_be_visible()
        expect(page.locator("#attribution-timeline li")).to_have_count(2)
        expect(page.locator("#attribution-truncation-note")).to_be_visible()
        assert page.locator("#prediction-fields").inner_text() != page.locator("#truth-fields").inner_text()
        page.screenshot(path=str(output / "attribution-detail.png"), full_page=True)
        page.locator("#reset-attribution-filters").click()
        expect(page.locator("#attribution-cases-body tr")).to_have_count(9)
        page.locator("#attribution-method-filter").select_option("first_explicit_error")
        expect(page.locator("#attribution-cases-body tr")).to_have_count(3)
        page.locator("#attribution-status-filter").select_option("abstain")
        expect(page.locator("#attribution-cases-body tr")).to_have_count(1)
        page.locator("#attribution-source-filter").select_option("TraceElephant")
        expect(page.locator("#attribution-cases-empty")).to_be_visible()
        page.locator("#reset-attribution-filters").click()
        expect(page.locator("#attribution-cases-body tr")).to_have_count(9)
        page.locator("#tab-runs-main").focus()
        page.locator("#tab-runs-main").press("End")
        expect(page.locator("#tab-attribution-main")).to_have_attribute("aria-selected", "true")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2")
        with page.expect_popup() as popup_info:
            page.locator("#attribution-phoenix-link").click()
        popup = popup_info.value
        popup.wait_for_load_state("networkidle")
        assert popup.url.startswith(PHOENIX + "/datasets/")
        page.screenshot(path=str(output / "attribution-overview.png"), full_page=True)
        # Report upstream Phoenix diagnostics as PARTIAL, never turn them into PASS.
        app_errors = [item for key in ("console", "page", "request_failed", "http_errors") for item in errors[key] if (item.get("page") or item.get("url", "")).startswith(APP + "/")]
        result = {"status": "PARTIAL" if any(errors.values()) else "PASS", "application_status": "PARTIAL" if app_errors else "PASS", "case_rows": 9, "methods": 3, "timeline_steps": 2, "phoenix_popup": True, **errors}
        context.close()
        browser.close()
    return result


def verify(output: Path) -> int:
    from phoenix.client import Client
    from m1_local.attribution import build_attribution_views
    from m1_local.attribution_evaluation import log_suite_to_phoenix, run_attribution_suite
    from m1_local.external_smoke import ensure_phoenix_dataset

    if listeners():
        raise RuntimeError("required ports already have listeners; no existing process will be stopped")
    output.mkdir(parents=True, exist_ok=False)
    env = {key: value for key, value in os.environ.items() if not key.startswith("PHOENIX_")}
    env.update({"OPENAI_API_KEY": "", "RUN_REAL_MODEL_TESTS": "0", "LANGSMITH_TRACING": "false", "PYTHONPATH": "", "PYTHONDONTWRITEBYTECODE": "1", "PHOENIX_ALLOW_EXTERNAL_RESOURCES": "false", "NO_PROXY": "127.0.0.1,localhost,::1"})
    report = {"paid_model_calls": 0, "real_websurfer_calls": 0, "synthetic_cases": 3, "phoenix": "BLOCKED", "api": "BLOCKED", "browser": "BLOCKED"}
    app_process = None
    phoenix_started = False
    exit_code = 1
    powershell = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]

    def start_phoenix():
        command = powershell + [str(ROOT / "scripts/start-phoenix.ps1"), "-DataDirectory", str(output / "phoenix")]
        # Windows children can inherit PIPE handles and prevent communicate() EOF.
        with (output / "service-control.log").open("ab") as log:
            subprocess.run(command, env=env, cwd=ROOT, check=True, timeout=60, stdout=log, stderr=subprocess.STDOUT)
        print("[advisor] Phoenix process started", flush=True)

    def stop_phoenix():
        with (output / "service-control.log").open("ab") as log:
            subprocess.run(powershell + [str(ROOT / "scripts/stop-phoenix.ps1")], env=env, cwd=ROOT, check=True, timeout=30, stdout=log, stderr=subprocess.STDOUT)

    def phoenix_client():
        return Client(base_url=PHOENIX, http_client=httpx.Client(base_url=PHOENIX, trust_env=False, timeout=30))

    try:
        start_phoenix()
        phoenix_started = True
        wait_http(PHOENIX + "/healthz")
        print("[advisor] Phoenix healthy", flush=True)
        phoenix_listeners = listeners()
        assert phoenix_listeners and all(row["address"] == "127.0.0.1:6006" for row in phoenix_listeners), phoenix_listeners
        report["phoenix_listeners"] = phoenix_listeners
        client = phoenix_client()
        records = fixture_records()
        dataset, _ = ensure_phoenix_dataset(client, records)
        result = log_suite_to_phoenix(client, dataset, run_attribution_suite([build_attribution_views(item) for item in records]))
        print("[advisor] Synthetic attribution fixture stored", flush=True)
        report["synthetic_experiment_id"] = result.experiment_id
        with (output / "app.log").open("wb") as log:
            app_process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--serve-offline", str(output)], env=env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        health = wait_http(APP + "/api/health")
        assert health["app"]["status"] == health["phoenix"]["status"] == "healthy"
        report["api"] = "PASS"
        print("[advisor] API healthy; offline smoke completed", flush=True)
        smoke = json.loads((output / "smoke.json").read_text())
        before = query_trace(client, smoke["run_id"])
        stop_phoenix()
        phoenix_started = False
        start_phoenix()
        phoenix_started = True
        wait_http(PHOENIX + "/healthz")
        after = query_trace(phoenix_client(), smoke["run_id"])
        assert before == after
        report.update({"phoenix": "PASS", "trace_persistence": after, "smoke": smoke})
        print("[advisor] Trace survived Phoenix restart; checking Chromium", flush=True)
        report["browser"] = "PARTIAL"
        report["browser"] = browser_acceptance(output)
        exit_code = 0 if report["browser"]["status"] == "PASS" else 1
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if any(message in str(exc) for message in ("Executable doesn't exist", "spawn EPERM", "BrowserType.launch")):
            report["browser"] = "BLOCKED"
    finally:
        if app_process is not None and app_process.poll() is None:
            subprocess.run(["taskkill.exe", "/PID", str(app_process.pid), "/T", "/F"], capture_output=True, timeout=20)
            app_process.wait(timeout=10)
        if phoenix_started:
            try:
                stop_phoenix()
            except Exception as exc:
                report["cleanup_error"] = str(exc)
        for _ in range(20):
            if not listeners():
                break
            time.sleep(0.25)
        report["remaining_listeners"] = listeners()
        if report["remaining_listeners"] or "cleanup_error" in report:
            exit_code = 1
        (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    return exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="new directory for synthetic data and receipts")
    parser.add_argument("--serve-offline", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve_offline:
        serve_offline(args.serve_offline.resolve())
    elif args.output:
        raise SystemExit(verify(args.output.resolve()))
    else:
        parser.error("--output must name a new directory")
