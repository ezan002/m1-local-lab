from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Path as ApiPath, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .attribution import AttributionMethodName
from .attribution_read_model import (
    AttributionCaseDetailRead,
    AttributionCaseNotFound,
    AttributionCasesRead,
    AttributionDatasetNotFound,
    AttributionExperimentNotFound,
    AttributionReadModel,
    AttributionReadUnavailable,
    AttributionSummaryRead,
    ResultStatus,
)
from .config import PROJECT_ROOT, Settings, get_settings
from .runtime import AgentRuntime


class RunRequest(BaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    allowed_files: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("task")
    @classmethod
    def nonblank_task(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task must not be empty")
        return value.strip()


class ReplayRequest(BaseModel):
    checkpoint_id: str = Field(min_length=1, max_length=200)


class ForkRequest(BaseModel):
    checkpoint_id: str = Field(min_length=1, max_length=200)
    changes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("changes")
    @classmethod
    def typed_changes(cls, value: dict[str, Any]) -> dict[str, Any]:
        return AgentRuntime.validate_fork_changes(value)


def public_run(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record.get(key)
        for key in (
            "run_id", "thread_id", "branch_id", "status", "task", "parent_run_id",
            "source_checkpoint_id", "dataset_case_id", "pause_requested", "created_at",
            "updated_at", "error",
        )
    }


def create_app(
    settings: Settings | None = None,
    runtime: AgentRuntime | None = None,
    attribution_reader: AttributionReadModel | None = None,
) -> FastAPI:
    cfg = settings or get_settings()
    engine = runtime or AgentRuntime(cfg)
    csrf_token = secrets.token_urlsafe(32)
    host_name = f"[{cfg.app_host}]" if ":" in cfg.app_host else cfg.app_host
    allowed_hosts = {f"{host_name}:{cfg.app_port}", f"localhost:{cfg.app_port}", f"127.0.0.1:{cfg.app_port}"}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await engine.startup()
        try:
            yield
        finally:
            await engine.shutdown()
            history_store.remote.client.close()

    app = FastAPI(title="M1 Local Lab", lifespan=lifespan)
    app.state.runtime = engine
    app.state.csrf_token = csrf_token
    from .historical_observation import HistoryStore
    from .observation_api import attach_observation
    history_store = HistoryStore(cfg.phoenix_base_url, cfg.metadata_db.parent / "observation")
    attach_observation(app, history_store, csrf_token, execution_enabled=True)
    app.state.attribution_reader = attribution_reader or AttributionReadModel(
        cfg.phoenix_base_url,
        dataset_name=cfg.attribution_dataset_name,
        experiment_name=cfg.attribution_experiment_name,
        experiment_id=cfg.attribution_experiment_id,
    )

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
        return FileResponse(web_root / "observation.html")

    @app.get("/execution", response_class=FileResponse)
    async def execution_index() -> FileResponse:
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
                "fork_fields": ["plan", "worker_instruction", "next_agent"],
            },
        }

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        warning = await asyncio.to_thread(engine.observability.check_phoenix)
        return {
            "app": {"status": "healthy"},
            "phoenix": {"status": "healthy" if warning is None else "unavailable", "warning": warning},
            "build": app.state.build_info,
        }

    @app.get("/api/graph")
    async def graph_structure() -> dict[str, Any]:
        """Read-only topology of the compiled LangGraph, for external debuggers such as Agent Studio."""
        if engine.graph is None:
            raise HTTPException(503, "runtime has not started")
        drawable = engine.graph.get_graph()
        return {
            "nodes": [{"id": node_id} for node_id in drawable.nodes],
            "edges": [{"source": edge.source, "target": edge.target, "conditional": bool(edge.conditional)}
                      for edge in drawable.edges],
        }

    @app.get("/api/runs")
    async def runs() -> list[dict[str, Any]]:
        return [public_run(record) for record in engine.store.list_runs()]

    async def read_attribution(call: Any) -> Any:
        try:
            return await asyncio.to_thread(call)
        except AttributionDatasetNotFound as exc:
            raise HTTPException(404, "Attribution dataset is not available.") from exc
        except AttributionExperimentNotFound as exc:
            raise HTTPException(404, "Attribution experiment is not available.") from exc
        except AttributionCaseNotFound as exc:
            raise HTTPException(404, "Attribution case is not available.") from exc
        except AttributionReadUnavailable as exc:
            raise HTTPException(503, "Phoenix attribution data is temporarily unavailable.") from exc

    @app.get("/api/attribution/summary", response_model=AttributionSummaryRead)
    async def attribution_summary() -> AttributionSummaryRead:
        return await read_attribution(app.state.attribution_reader.summary)

    @app.get("/api/attribution/cases", response_model=AttributionCasesRead)
    async def attribution_cases(
        source: Literal["TraceElephant", "Who&When"] | None = Query(None),
        method: AttributionMethodName | None = Query(None),
        result_status: ResultStatus | None = Query(None),
        agent_correct: bool | None = Query(None),
        step_correct: bool | None = Query(None),
        trace_truncated: bool | None = Query(None),
        limit: int = Query(100, ge=1, le=100),
    ) -> AttributionCasesRead:
        return await read_attribution(
            lambda: app.state.attribution_reader.cases(
                source=source,
                method=method,
                result_status=result_status,
                agent_correct=agent_correct,
                step_correct=step_correct,
                trace_truncated=trace_truncated,
                limit=limit,
            )
        )

    @app.get(
        "/api/attribution/cases/{dataset_case_id}", response_model=AttributionCaseDetailRead
    )
    async def attribution_case_detail(
        dataset_case_id: str = ApiPath(min_length=1, max_length=200),
        method: AttributionMethodName = Query("rule_based"),
    ) -> AttributionCaseDetailRead:
        return await read_attribution(
            lambda: app.state.attribution_reader.case_detail(dataset_case_id, method)
        )

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
        except (KeyError, ValueError, RuntimeError) as exc:
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
        except (KeyError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/runs/{run_id}/events")
    async def events(
        run_id: str,
        last_event_id: int = Query(0, ge=0, le=2**63 - 1),
        last_event_header: int | None = Header(None, alias="Last-Event-ID", ge=0, le=2**63 - 1),
    ) -> StreamingResponse:
        try:
            if engine.store.get_run(run_id) is None:
                raise KeyError(run_id)
            after = last_event_header if last_event_header is not None else last_event_id
            return StreamingResponse(engine.sse(run_id, after), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/runs/{run_id}/events-log")
    async def event_log(run_id: str, after: int = Query(0, ge=0, le=2**63 - 1)) -> list[dict[str, Any]]:
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
