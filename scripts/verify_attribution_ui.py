from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, Route, sync_playwright

from m1_local.attribution_read_model import AttributionReadModel


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PHOENIX_URL = "http://127.0.0.1:6006"
OUTPUT_DIR = PROJECT_ROOT / "data" / "test-output" / "attribution-ui"
RUNTIME_DIR = OUTPUT_DIR / "runtime"
VIEWPORTS = (
    ("1366x768", 1366, 768, 1.0),
    ("1920x1080", 1920, 1080, 1.0),
    ("125-percent", 1536, 864, 1.25),
)


def _listener_pid(port: int) -> int | None:
    try:
        result = subprocess.run(
            ["netstat.exe", "-ano", "-p", "tcp"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return None
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP":
            continue
        if (
            fields[1].rsplit(":", 1)[-1] == str(port)
            and fields[-2].upper() == "LISTENING"
            and fields[-1].isdigit()
        ):
            return int(fields[-1])
    return None


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.5)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def _http_status(url: str, timeout: float = 2) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except (OSError, urllib.error.URLError):
        return None


def _wait_for(url: str, expected: int, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _http_status(url) == expected:
            return
        time.sleep(0.25)
    raise RuntimeError(f"service did not become ready: {url}")


def _wait_port_closed(port: int, seconds: int = 15) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _port_open(port):
            return
        time.sleep(0.25)
    raise RuntimeError(f"service started by the verifier did not release port {port}")


def _ensure_phoenix() -> subprocess.Popen[str] | None:
    if _http_status(f"{PHOENIX_URL}/healthz") == 200:
        return None
    if _port_open(6006):
        raise RuntimeError("port 6006 is occupied but Phoenix health check failed")
    environment = os.environ.copy()
    environment.update(
        {
            "PHOENIX_HOST": "127.0.0.1",
            "PHOENIX_PORT": "6006",
            "PHOENIX_WORKING_DIR": str(PROJECT_ROOT / "data" / "phoenix"),
            "PHOENIX_ALLOW_EXTERNAL_RESOURCES": "false",
        }
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [
            str(PROJECT_ROOT / ".venv-phoenix" / "Scripts" / "python.exe"),
            "-m",
            "phoenix.server.main",
            "serve",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creation_flags,
    )
    try:
        _wait_for(f"{PHOENIX_URL}/healthz", 200, 90)
    except Exception as exc:
        _stop_process(process)
        output = process.stdout.read() if process.stdout else ""
        raise RuntimeError(f"Phoenix failed to start: {output[-4000:]}") from exc
    return process


def _choose_app_port() -> int:
    if not _port_open(8000):
        return 8000
    if not _port_open(8001):
        return 8001
    raise RuntimeError("ports 8000 and 8001 are both occupied")


def _start_app(port: int) -> subprocess.Popen[str]:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "APP_HOST": "127.0.0.1",
            "APP_PORT": str(port),
            "PHOENIX_BASE_URL": PHOENIX_URL,
            "OPENAI_API_KEY": "",
            "HEADLESS": "true",
            "METADATA_DB": str(RUNTIME_DIR / "app.sqlite"),
            "CHECKPOINT_DB": str(RUNTIME_DIR / "checkpoints.sqlite"),
            "ARTIFACT_ROOT": str(RUNTIME_DIR / "artifacts"),
        }
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--serve-offline",
            str(port),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creation_flags,
    )
    try:
        _wait_for(f"http://127.0.0.1:{port}/api/health", 200, 30)
    except Exception:
        _stop_process(process)
        output = process.stdout.read() if process.stdout else ""
        raise RuntimeError(f"Uvicorn failed to start: {output[-2000:]}")
    return process


def _serve_offline(port: int) -> None:
    import uvicorn

    from m1_local.api import create_app
    from m1_local.config import Settings
    from m1_local.models import RuleBasedGateway
    from m1_local.runtime import AgentRuntime

    settings = Settings(app_port=port, openai_api_key="")
    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    uvicorn.run(
        create_app(settings=settings, runtime=runtime),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _wait_cases(page: Page, expected: int) -> None:
    page.locator("#attribution-case-count").wait_for(state="visible")
    page.wait_for_function(
        "expected => document.querySelectorAll('#attribution-cases-body tr').length === expected",
        arg=expected,
    )


def _assert_no_horizontal_overflow(page: Page) -> None:
    overflow = page.evaluate(
        """() => ({
          root: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          body: document.body.scrollWidth - document.body.clientWidth
        })"""
    )
    if overflow["root"] > 1 or overflow["body"] > 1:
        raise AssertionError(f"unexpected page-level horizontal overflow: {overflow}")


def _exercise_page(
    browser: Browser,
    base_url: str,
    name: str,
    width: int,
    height: int,
    scale: float,
    summary: dict[str, Any],
) -> dict[str, Any]:
    print(f"[attribution-ui] exercising {name}", flush=True)
    context = browser.new_context(
        viewport={"width": width, "height": height},
        device_scale_factor=scale,
        permissions=["clipboard-read", "clipboard-write"],
        base_url=base_url,
    )
    page = context.new_page()
    page.set_default_timeout(60_000)
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []
    server_errors: list[str] = []
    attribution_requests: list[str] = []
    expected_503 = False

    def record_console(message: Any) -> None:
        if message.type != "error":
            return
        if expected_503 and "503" in message.text:
            return
        console_errors.append(message.text)

    page.on("console", record_console)
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("requestfailed", lambda request: failed_requests.append(request.url))

    def record_response(response: Any) -> None:
        if "/api/attribution/" in response.url:
            attribution_requests.append(response.url)
        if response.status >= 400 and not (expected_503 and response.status == 503):
            server_errors.append(f"{response.status} {response.url}")

    page.on("response", record_response)
    page.goto("/", wait_until="networkidle")
    page.locator("#tab-attribution-main").click()
    page.locator("#attribution-content").wait_for(state="visible")
    _wait_cases(page, 9)

    if page.locator("#attribution-metrics-body tr").count() != 3:
        raise AssertionError("expected exactly three attribution metric rows")
    if "3" not in page.locator("#attribution-counts").inner_text():
        raise AssertionError("summary does not expose sample/method counts")
    _assert_no_horizontal_overflow(page)
    page.screenshot(path=OUTPUT_DIR / f"{name}-overview.png")

    # Keyboard behavior is scoped to the primary tablist and exposes focus.
    primary = page.locator("#tab-runs-main")
    primary.focus()
    primary.press("End")
    if page.locator("#tab-attribution-main").get_attribute("aria-selected") != "true":
        raise AssertionError("End did not activate the final primary tab")
    page.locator("#tab-attribution-main").press("Home")
    if primary.get_attribute("aria-selected") != "true":
        raise AssertionError("Home did not activate the first primary tab")
    primary.press("End")
    focus_outline = page.locator("#tab-attribution-main").evaluate(
        "element => getComputedStyle(element).outlineWidth"
    )
    if focus_outline == "0px":
        raise AssertionError("keyboard-focused primary tab has no visible outline")

    # Cover source/method/status/truncation filters and an empty state.
    page.locator("#attribution-method-filter").select_option("rule_based")
    _wait_cases(page, 3)
    page.locator("#attribution-source-filter").select_option("TraceElephant")
    _wait_cases(page, 1)
    page.locator("#attribution-truncated-filter").select_option("true")
    _wait_cases(page, 1)
    page.locator("#attribution-status-filter").select_option("abstain")
    page.locator("#attribution-cases-empty").wait_for(state="visible")
    page.locator("#reset-attribution-filters").click()
    _wait_cases(page, 9)
    page.locator("#attribution-method-filter").select_option("first_explicit_error")
    _wait_cases(page, 3)
    page.locator("#attribution-status-filter").select_option("abstain")
    _wait_cases(page, 2)
    if page.locator("#attribution-cases-body").inner_text().count("Abstain") < 2:
        raise AssertionError("abstain rows are not visibly labelled")
    page.locator("#reset-attribution-filters").click()
    _wait_cases(page, 9)

    # Select three unique cases and then leave the truncated trace open.
    selected: list[str] = []
    page.locator("#attribution-method-filter").select_option("rule_based")
    _wait_cases(page, 3)
    for button in page.locator("#attribution-cases-body .case-select").all():
        selected.append(button.get_attribute("title") or "")
        button.click()
        page.locator("#attribution-detail-content").wait_for(state="visible")
    if len(set(selected)) != 3:
        raise AssertionError(f"expected three unique cases, got {selected}")
    page.locator("#attribution-source-filter").select_option("TraceElephant")
    _wait_cases(page, 1)
    page.locator("#attribution-cases-body .case-select").click()
    page.locator("#attribution-detail-content").wait_for(state="visible")
    if page.locator("#attribution-timeline li").count() != 20:
        raise AssertionError("truncated TraceElephant timeline must retain 20 normalized steps")
    if page.locator("#attribution-timeline .timeline-step.is-predicted").count() != 1:
        raise AssertionError("timeline must visibly identify one predicted step")
    if page.locator("#attribution-timeline .timeline-step.is-truth").count() != 1:
        raise AssertionError("timeline must visibly identify one ground-truth step")
    if page.locator("#attribution-timeline .timeline-tag.is-evidence").count() < 1:
        raise AssertionError("timeline must include a textual evidence label")
    if page.locator("#attribution-truncation-note").is_hidden():
        raise AssertionError("truncated trace warning is not visible")
    if page.locator("#prediction-fields").inner_text() == page.locator("#truth-fields").inner_text():
        raise AssertionError("prediction and ground truth must be separately rendered")
    page.locator("#attribution-detail").screenshot(path=OUTPUT_DIR / f"{name}-detail.png")

    page.locator("#copy-experiment-id").click()
    copied = page.evaluate("navigator.clipboard.readText()")
    if copied != summary["experiment_id"]:
        raise AssertionError(f"copied unexpected experiment id: {copied!r}")
    phoenix_href = page.locator("#attribution-phoenix-link").get_attribute("href") or ""
    if not phoenix_href.startswith(("http://127.0.0.1:6006/", "http://localhost:6006/")):
        raise AssertionError(f"unsafe or missing Phoenix URL: {phoenix_href}")

    # One viewport validates a recoverable 503 without leaking exception details.
    recovery_checked = False
    if name == "1366x768":
        page.locator("#reset-attribution-filters").click()
        _wait_cases(page, 9)

        def unavailable(route: Route) -> None:
            route.fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps({"detail": "Phoenix attribution data is temporarily unavailable."}),
            )

        expected_503 = True
        page.route("**/api/attribution/summary", unavailable)
        page.evaluate("window.m1Console.reloadAttribution()")
        page.locator("#attribution-error").wait_for(state="visible")
        error_text = page.locator("#attribution-error-message").inner_text()
        if "Traceback" in error_text or "ConnectionError" in error_text:
            raise AssertionError("raw exception leaked into attribution error state")
        page.unroute("**/api/attribution/summary", unavailable)
        expected_503 = False
        page.locator("#retry-attribution").click()
        page.locator("#attribution-content").wait_for(state="visible")
        _wait_cases(page, 9)
        recovery_checked = True

    if len(attribution_requests) > 30:
        raise AssertionError(f"unexpected attribution request storm: {len(attribution_requests)}")
    if console_errors or page_errors or failed_requests or server_errors:
        raise AssertionError(
            json.dumps(
                {
                    "console_errors": console_errors,
                    "page_errors": page_errors,
                    "failed_requests": failed_requests,
                    "server_errors": server_errors,
                },
                ensure_ascii=False,
            )
        )
    context.close()
    return {
        "name": name,
        "viewport": {"width": width, "height": height, "device_scale_factor": scale},
        "unique_cases_opened": len(set(selected)),
        "timeline_steps": 20,
        "recovery_503": recovery_checked,
        "attribution_request_count": len(attribution_requests),
        "console_errors": console_errors,
        "page_errors": page_errors,
        "failed_requests": failed_requests,
        "server_errors": server_errors,
    }


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("[attribution-ui] capturing service state", flush=True)
    before = {port: {"open": _port_open(port), "pid": _listener_pid(port)} for port in (6006, 8000, 8001)}
    phoenix_process: subprocess.Popen[str] | None = None
    app_process: subprocess.Popen[str] | None = None
    report: dict[str, Any] = {}
    try:
        print("[attribution-ui] checking Phoenix", flush=True)
        phoenix_process = _ensure_phoenix()
        print("[attribution-ui] reading public Phoenix APIs", flush=True)
        summary = AttributionReadModel(PHOENIX_URL).summary().model_dump(mode="json")
        if summary["example_count"] != 3 or summary["task_run_count"] != 9:
            raise RuntimeError("existing Phoenix attribution data is not the bounded 3-case/9-run smoke set")
        app_port = _choose_app_port()
        print(f"[attribution-ui] starting offline app on {app_port}", flush=True)
        app_process = _start_app(app_port)
        app_pid = app_process.pid
        print("[attribution-ui] launching Chromium", flush=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                results = [
                    _exercise_page(
                        browser,
                        f"http://127.0.0.1:{app_port}",
                        name,
                        width,
                        height,
                        scale,
                        summary,
                    )
                    for name, width, height, scale in VIEWPORTS
                ]
            finally:
                browser.close()
        report = {
            "status": "PASS",
            "dataset_name": summary["dataset_name"],
            "dataset_id": summary["dataset_id"],
            "experiment_name": summary["experiment_name"],
            "experiment_id": summary["experiment_id"],
            "example_count": summary["example_count"],
            "method_count": len(summary["methods"]),
            "paid_model_calls": 0,
            "real_websurfer_calls": 0,
            "external_task_executed": False,
            "app_port": app_port,
            "app_pid_started_by_script": app_pid,
            "phoenix_started_by_script": phoenix_process is not None,
            "phoenix_pid_started_by_script": phoenix_process.pid if phoenix_process else None,
            "viewports": results,
            "screenshots": [str(path.relative_to(PROJECT_ROOT)) for path in sorted(OUTPUT_DIR.glob("*.png"))],
        }
    finally:
        if app_process is not None:
            _stop_process(app_process)
            _wait_port_closed(app_port)
        if phoenix_process is not None:
            _stop_process(phoenix_process)
            _wait_port_closed(6006)
    after = {port: {"open": _port_open(port), "pid": _listener_pid(port)} for port in (6006, 8000, 8001)}
    report["service_before"] = before
    report["service_after"] = after
    report_path = OUTPUT_DIR / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--serve-offline":
        _serve_offline(int(sys.argv[2]))
        raise SystemExit(0)
    raise SystemExit(main())
