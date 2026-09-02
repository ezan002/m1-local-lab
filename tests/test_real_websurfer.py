from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from m1_local.storage import ArtifactStore
from m1_local.websurfer import WebSurferSession
from m1_local.config import Settings


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_REAL_MODEL_TESTS") != "1",
    reason="Requires explicit RUN_REAL_MODEL_TESTS=1, real model access, and billable calls",
)


async def _serve_fixture(root: Path):
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        line = await reader.readline()
        target = line.decode("ascii", "ignore").split(" ")[1].split("?", 1)[0]
        while await reader.readline() not in {b"\r\n", b"\n", b""}:
            pass
        name = "page2.html" if target.endswith("page2.html") else "page1.html"
        body = (root / name).read_bytes()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_official_websurfer_reads_and_clicks_fixture(settings, tmp_path: Path):
    settings = Settings(headless=True, artifact_root=tmp_path / "artifacts")
    assert settings.api_key_configured, "OPENAI_API_KEY must be configured for this real test"
    server, port = await _serve_fixture(Path(__file__).parent / "fixtures")
    base = f"http://127.0.0.1:{port}"
    run_id = "11111111-1111-4111-8111-111111111111"
    branch_id = "22222222-2222-4222-8222-222222222222"
    session = WebSurferSession(
        settings=settings, run_id=run_id, branch_id=branch_id,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        resume={"last_url": base + "/page1.html", "summary": "fresh fixture"}, fixture_base_url=base,
    )
    try:
        observation = await session.act(
            "Read the unique marker on page one, use the visible link yourself to navigate to page two, "
            "then report the unique marker on page two."
        )
        assert "UNIQUE_MARKER_BETA_7394" in observation.summary
        assert observation.url.endswith("page2.html")
        assert observation.artifacts and observation.artifacts[0]["type"] == "screenshot"
    finally:
        await session.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_official_websurfer_public_document(settings, tmp_path: Path):
    settings = Settings(headless=True, artifact_root=tmp_path / "artifacts")
    run_id = "33333333-3333-4333-8333-333333333333"
    branch_id = "44444444-4444-4444-8444-444444444444"
    session = WebSurferSession(
        settings=settings, run_id=run_id, branch_id=branch_id,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        resume={"last_url": "https://playwright.dev/python/docs/library", "summary": "public documentation"},
    )
    try:
        observation = await session.act("Report the Windows asyncio subprocess event-loop requirement and cite this page.")
        assert "Proactor" in observation.summary
        assert any("playwright.dev" in item for item in observation.citations)
    finally:
        await session.close()
