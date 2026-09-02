from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import platform
import sqlite3
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from .config import Settings


async def run_doctor(settings: Settings, *, online: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: Any) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    add("platform", "PASS" if sys.platform == "win32" else "FAIL", {
        "python": sys.version.split()[0], "executable": sys.executable,
        "platform": platform.platform(), "bits": struct.calcsize("P") * 8,
    })
    imports = [
        "langgraph", "langgraph.checkpoint.sqlite.aio", "autogen_agentchat",
        "autogen_ext.agents.web_surfer", "playwright.async_api", "fastapi", "phoenix.otel",
    ]
    failures = []
    for name in imports:
        try:
            importlib.import_module(name)
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    add("imports", "PASS" if not failures else "FAIL", failures or imports)

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "doctor.sqlite"
            with contextlib.closing(sqlite3.connect(database)) as db:
                db.execute("CREATE TABLE doctor(value TEXT)")
                db.execute("INSERT INTO doctor VALUES('ok')")
                value = db.execute("SELECT value FROM doctor").fetchone()[0]
                db.commit()
        add("sqlite_write", "PASS", value)
    except Exception as exc:
        add("sqlite_write", "FAIL", f"{type(exc).__name__}: {exc}")

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            version = browser.version
            await browser.close()
        loop_name = type(asyncio.get_running_loop()).__name__
        add("chromium_launch", "PASS", {"browser_version": version, "event_loop": loop_name})
    except Exception as exc:
        add("chromium_launch", "FAIL", f"{type(exc).__name__}: {exc}")

    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(settings.phoenix_base_url)
            response.raise_for_status()
        add("phoenix", "PASS", {"url": settings.phoenix_base_url, "http_status": response.status_code})
    except Exception as exc:
        add("phoenix", "BLOCKED", f"{type(exc).__name__}: {exc}")

    if not online:
        for name in ("model_text", "model_image", "model_tool_call"):
            add(name, "NOT RUN", "Use doctor --online; maximum 3 model calls.")
    elif not settings.api_key_configured:
        for name in ("model_text", "model_image", "model_tool_call"):
            add(name, "BLOCKED", "OPENAI_API_KEY is not configured (value was not read or printed).")
    else:
        await _online_model_checks(settings, add)

    return {"safe_config": settings.safe_summary(), "checks": checks}


async def _online_model_checks(settings: Settings, add: Any) -> None:
    from PIL import Image as PILImage
    from autogen_core import CancellationToken, Image
    from autogen_core.models import UserMessage
    from autogen_core.tools import FunctionTool
    from autogen_ext.models.openai import OpenAIChatCompletionClient

    client = OpenAIChatCompletionClient(
        model=settings.websurfer_model,
        api_key=settings.openai_api_key.get_secret_value(),
        base_url=settings.openai_base_url,
        timeout=settings.model_timeout_seconds,
        max_retries=0,
    )
    try:
        try:
            result = await client.create([UserMessage(content="Reply with exactly: TEXT_OK", source="doctor")])
            add("model_text", "PASS" if "TEXT_OK" in str(result.content) else "FAIL", str(result.content)[:200])
        except Exception as exc:
            add("model_text", "FAIL", f"{type(exc).__name__}: {exc}")
        try:
            image = PILImage.new("RGB", (16, 16), color=(17, 34, 51))
            result = await client.create([UserMessage(content=["Reply with exactly: IMAGE_OK", Image(image)], source="doctor")])
            add("model_image", "PASS" if "IMAGE_OK" in str(result.content) else "FAIL", str(result.content)[:200])
        except Exception as exc:
            add("model_image", "FAIL", f"{type(exc).__name__}: {exc}")
        try:
            async def doctor_ping(value: str) -> str:
                return value
            tool = FunctionTool(doctor_ping, description="Return the supplied value")
            result = await client.create(
                [UserMessage(content="Call doctor_ping with value TOOL_OK. Do not answer directly.", source="doctor")],
                tools=[tool], cancellation_token=CancellationToken(),
            )
            add("model_tool_call", "PASS" if isinstance(result.content, list) and result.content else "FAIL", str(result.content)[:300])
        except Exception as exc:
            add("model_tool_call", "FAIL", f"{type(exc).__name__}: {exc}")
    finally:
        await client.close()
