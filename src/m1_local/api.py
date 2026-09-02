from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import PROJECT_ROOT, Settings, get_settings
from .runtime import AgentRuntime


class RunRequest(BaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    allowed_files: list[str] = Field(default_factory=list, max_length=20)


class ReplayRequest(BaseModel):
    checkpoint_id: str


class ForkRequest(BaseModel):
    checkpoint_id: str
    changes: dict[str, Any] = Field(default_factory=dict)


def public_run(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record.get(key)
        for key in (
            "run_id", "thread_id", "branch_id", "status", "task", "parent_run_id",
            "source_checkpoint_id", "dataset_case_id", "pause_requested", "created_at",
            "updated_at", "error",
        )
    }


def create_app(settings: Settings | None = None, runtime: AgentRuntime | None = None) -> FastAPI:
    cfg = settings or get_settings()
    engine = runtime or AgentRuntime(cfg)
    csrf_token = secrets.token_urlsafe(32)
    allowed_hosts = {f"{cfg.app_host}:{cfg.app_port}", f"localhost:{cfg.app_port}", f"127.0.0.1:{cfg.app_port}"}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await engine.startup()
        yield
        await engine.shutdown()

    app = FastAPI(title="M1 Local Lab", lifespan=lifespan)
    app.state.runtime = engine
    app.state.csrf_token = csrf_token

    @app.middleware("http")
    async def local_security(request: Request, call_next: Any):
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return HTMLResponse("Invalid Host", status_code=400)
        origin = request.headers.get("origin")
        expected_origins = {f"http://{item}" for item in allowed_hosts}
        if origin and origin not in expected_origins:
            return HTMLResponse("Invalid Origin", status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.headers.get("x-m1-csrf") != csrf_token:
                return HTMLResponse("CSRF check failed", status_code=403)
        return await call_next(request)

    web_root = PROJECT_ROOT / "web"
    app.mount("/static", StaticFiles(directory=web_root), name="static")

    @app.get("/", response_class=FileResponse)
    async def index() -> FileResponse:
        return FileResponse(web_root / "index.html")

    @app.get("/api/session")
    async def session() -> dict[str, Any]:
        return {
            "csrf_token": csrf_token,
            "phoenix_url": cfg.phoenix_base_url,
            "tracing_warning": engine.observability.warning,
            "capabilities": {
                "resume_statuses": ["paused", "failed", "interrupted"],
                "active_statuses": ["queued", "running", "pausing"],
                "terminal_statuses": ["completed", "partial", "failed", "paused", "interrupted"],
                "fork_fields": ["plan", "worker_instruction"],
            },
        }

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        warning = await asyncio.to_thread(engine.observability.check_phoenix)
        return {
            "app": {"status": "healthy"},
            "phoenix": {"status": "healthy" if warning is None else "unavailable", "warning": warning},
        }

    @app.get("/api/runs")
    async def runs() -> list[dict[str, Any]]:
        return [public_run(record) for record in engine.store.list_runs()]

    @app.post("/api/runs", status_code=202)
    async def create_run(body: RunRequest) -> dict[str, str]:
        try:
            run_id = await engine.create_run(body.task, allowed_files=body.allowed_files, start_background=True)
            return {"run_id": run_id}
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/runs/{run_id}")
    async def state(run_id: str) -> dict[str, Any]:
        try:
            result = await engine.current_state(run_id)
            record = engine.store.get_run(run_id)
            if record is None:
                raise KeyError(run_id)
            return {
                **result,
                "run": public_run(record),
                "trace_id": engine.store.latest_trace_id(run_id),
            }
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/runs/{run_id}/history")
    async def history(run_id: str) -> list[dict[str, Any]]:
        try:
            return await engine.history(run_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/runs/{run_id}/pause", status_code=202)
    async def pause(run_id: str) -> dict[str, str]:
        try:
            engine.request_pause(run_id)
            return {"status": "pausing"}
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str) -> dict[str, str]:
        try:
            await engine.resume(run_id, background=True)
            return {"status": "resuming"}
        except (KeyError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/runs/{run_id}/replay", status_code=202)
    async def replay(run_id: str, body: ReplayRequest) -> dict[str, str]:
        try:
            new_run = await engine.replay(run_id, body.checkpoint_id, background=True)
            return {"run_id": new_run}
        except (KeyError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/runs/{run_id}/fork", status_code=201)
    async def fork(run_id: str, body: ForkRequest) -> dict[str, str]:
        try:
            new_run = await engine.fork(run_id, body.checkpoint_id, body.changes)
            return {"run_id": new_run}
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, last_event_id: int = 0, last_event_header: str | None = Header(None, alias="Last-Event-ID")) -> StreamingResponse:
        try:
            if engine.store.get_run(run_id) is None:
                raise KeyError(run_id)
            after = int(last_event_header) if last_event_header else last_event_id
            return StreamingResponse(engine.sse(run_id, after), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/runs/{run_id}/events-log")
    async def event_log(run_id: str, after: int = 0) -> list[dict[str, Any]]:
        if engine.store.get_run(run_id) is None:
            raise HTTPException(404, "run not found")
        return engine.store.events_after(run_id, after)

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}")
    async def artifact(run_id: str, artifact_id: str) -> FileResponse:
        try:
            current = await engine.current_state(run_id)
            match = next(item for item in current["state"].get("artifacts", []) if item.get("id") == artifact_id)
            path = engine.artifacts.resolve_id_path(match["path"])
            return FileResponse(path, filename=path.name)
        except (KeyError, StopIteration, FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, "artifact not found") from exc

    return app
