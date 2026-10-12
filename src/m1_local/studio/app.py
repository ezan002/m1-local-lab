"""Agent Studio HTTP API + static frontend."""
from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from ..build_info import capture_build_info
from .engine import StudioEngine
from .integrations import INTEGRATIONS, Launcher
from .otlp import OTLPError, decode_request, ingest
from .runtime_support import TOOLS
from .schema import WorkflowDefinition
from .seeds import seed
from .store import StudioStore

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "web" / "studio"
NODE_KINDS = {"start", "agent", "tool", "condition", "output"}


class WorkflowBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    definition: dict[str, Any]


class RunBody(BaseModel):
    inputs: dict[str, str] = Field(default_factory=dict)
    provider_override: Literal["openai", "deterministic"] | None = None
    breakpoints: list[str] = Field(default_factory=list)


class ForkBody(BaseModel):
    checkpoint_id: str = Field(min_length=1, max_length=200)
    changes: dict[str, Any] = Field(default_factory=dict)


class ReplayBody(BaseModel):
    checkpoint_id: str = Field(min_length=1, max_length=200)


class ProviderSettings(BaseModel):
    base_url: str = Field(default="", max_length=300)
    default_model: str = Field(default="", max_length=120)


class LaunchBody(BaseModel):
    params: dict[str, str] = Field(default_factory=dict)


def node_catalog() -> list[dict[str, Any]]:
    return [
        {"type": "start", "label": "Start", "description": "Workflow inputs.", "fields": ["inputs"]},
        {"type": "agent", "label": "Agent", "description": "LLM agent with optional tool loop.",
         "fields": ["role", "system_prompt", "prompt", "provider", "model", "temperature", "max_tokens", "tools",
                    "max_tool_rounds", "mock_response", "mock_tool_calls"]},
        {"type": "tool", "label": "Tool", "description": "Run a built-in tool with templated arguments.", "fields": ["tool", "arguments"]},
        {"type": "condition", "label": "Condition", "description": "Route on a comparison (true/false edges).",
         "fields": ["left", "op", "right", "max_true_visits"]},
        {"type": "output", "label": "Output", "description": "Final templated result.", "fields": ["template"]},
    ]


def create_app(data_dir: Path | None = None, *, port: int = 8100, seed_demo: bool = True) -> FastAPI:
    data_dir = data_dir or Path(os.environ.get("STUDIO_DATA_DIR") or ROOT / "data" / "studio")
    store = StudioStore(data_dir / "studio.sqlite")
    engine = StudioEngine(store, data_dir / "checkpoints.sqlite")
    launcher = Launcher(store, f"http://127.0.0.1:{port}/v1/traces")
    token = secrets.token_urlsafe(32)
    build = capture_build_info("studio")
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.recovered = await engine.startup()
        if seed_demo:
            seed(store)
        yield
        await engine.shutdown()
        store.close()

    app = FastAPI(title="Agent Studio", lifespan=lifespan)
    app.state.store, app.state.engine, app.state.launcher = store, engine, launcher

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if request.headers.get("host") not in hosts:
            return JSONResponse({"detail": "Invalid Host"}, status_code=400)
        origin = request.headers.get("origin")
        if origin and origin not in {"http://" + h for h in hosts}:
            return JSONResponse({"detail": "Invalid Origin"}, status_code=403)
        path = request.url.path
        if path == "/v1/traces":
            client = request.client.host if request.client else ""
            if client not in {"127.0.0.1", "::1", "testclient"}:
                return JSONResponse({"detail": "OTLP ingest accepts loopback clients only"}, status_code=403)
        elif request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.headers.get("x-studio-csrf") != token:
            return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("Content-Security-Policy",
                                    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        return response

    def bad(exc: Exception, status: int = 422) -> HTTPException:
        return HTTPException(status, str(exc))

    # ------------------------------------------------------------------ meta
    @app.get("/api/meta")
    async def meta() -> dict[str, Any]:
        cfg = engine.provider_config()
        return {"csrf_token": token, "build": build, "recovered_runs": getattr(app.state, "recovered", []),
                "provider": {"openai_key_configured": bool(cfg.api_key), "base_url": cfg.base_url, "default_model": cfg.default_model},
                "otlp_endpoint": f"http://127.0.0.1:{port}/v1/traces"}

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "build": build}

    @app.get("/api/catalog")
    async def catalog() -> dict[str, Any]:
        return {"nodes": node_catalog(), "tools": [{"name": k, "description": v["description"], "parameters": v["parameters"]} for k, v in TOOLS.items()],
                "condition_ops": ["equals", "not_equals", "contains", "not_contains", "greater_than", "less_than", "is_true"]}

    @app.put("/api/settings/provider")
    async def set_provider(body: ProviderSettings) -> dict[str, Any]:
        if body.base_url and not body.base_url.startswith(("http://", "https://")):
            raise HTTPException(422, "base_url must start with http:// or https://")
        store.set_setting("provider", body.model_dump())
        return await meta()

    # ------------------------------------------------------------- workflows
    @app.get("/api/workflows")
    async def workflows() -> list[dict[str, Any]]:
        return store.list_workflows()

    @app.post("/api/workflows/validate")
    async def validate(body: dict[str, Any]) -> dict[str, Any]:
        try:
            WorkflowDefinition.model_validate(body)
        except ValidationError as exc:
            return {"valid": False, "errors": [e["msg"] for e in exc.errors()]}
        return {"valid": True, "errors": []}

    def _validated(body: WorkflowBody) -> dict[str, Any]:
        try:
            return WorkflowDefinition.model_validate(body.definition).model_dump()
        except ValidationError as exc:
            raise HTTPException(422, {"message": "invalid workflow", "errors": [e["msg"] for e in exc.errors()]}) from exc

    @app.post("/api/workflows", status_code=201)
    async def create_workflow(body: WorkflowBody) -> dict[str, Any]:
        return store.save_workflow(None, body.name, body.description, _validated(body))

    @app.get("/api/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str) -> dict[str, Any]:
        wf = store.get_workflow(workflow_id)
        if wf is None:
            raise HTTPException(404, "workflow not found")
        return wf

    @app.put("/api/workflows/{workflow_id}")
    async def update_workflow(workflow_id: str, body: WorkflowBody) -> dict[str, Any]:
        if store.get_workflow(workflow_id) is None:
            raise HTTPException(404, "workflow not found")
        return store.save_workflow(workflow_id, body.name, body.description, _validated(body))

    @app.delete("/api/workflows/{workflow_id}")
    async def delete_workflow(workflow_id: str) -> dict[str, Any]:
        if not store.delete_workflow(workflow_id):
            raise HTTPException(404, "workflow not found")
        return {"deleted": workflow_id, "note": "historical runs keep their own definition snapshot"}

    @app.post("/api/workflows/{workflow_id}/runs", status_code=202)
    async def run_workflow(workflow_id: str, body: RunBody) -> dict[str, Any]:
        wf = store.get_workflow(workflow_id)
        if wf is None:
            raise HTTPException(404, "workflow not found")
        node_ids = {n["id"] for n in wf["definition"]["nodes"]}
        unknown = [b for b in body.breakpoints if b not in node_ids]
        if unknown:
            raise HTTPException(422, f"unknown breakpoint nodes: {unknown}")
        try:
            return await engine.start_run(wf, body.inputs, breakpoints=body.breakpoints, provider_override=body.provider_override)
        except (ValidationError, ValueError) as exc:
            raise bad(exc) from exc

    # ------------------------------------------------------------------ runs
    @app.get("/api/runs")
    async def runs(workflow_id: str | None = None, origin: Literal["studio", "external"] | None = None,
                   limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
        return store.list_runs(workflow_id=workflow_id, origin=origin, limit=limit)

    def _run(run_id: str) -> dict[str, Any]:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        return run

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        run = _run(run_id)
        parent = store.get_run(run["parent_run_id"]) if run.get("parent_run_id") else None
        return {**run, "active": engine.is_active(run_id), "children": store.children(run_id),
                "parent": {"id": parent["id"], "status": parent["status"]} if parent else None}

    @app.get("/api/runs/{run_id}/spans")
    async def spans(run_id: str) -> list[dict[str, Any]]:
        _run(run_id)
        return store.spans(run_id)

    @app.get("/api/runs/{run_id}/checkpoints")
    async def checkpoints(run_id: str) -> dict[str, Any]:
        run = _run(run_id)
        if run["origin"] != "studio":
            return {"supported": False, "reason": "External runs are trace-only: the source process exposes no checkpoints or control interface.", "items": []}
        return {"supported": True, "items": await engine.checkpoints(run_id)}

    async def _control(call) -> Any:
        try:
            return await call()
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'")) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/runs/{run_id}/pause", status_code=202)
    async def pause(run_id: str) -> dict[str, Any]:
        _run(run_id)

        async def call():
            engine.request_pause(run_id)
            return {"status": "pause_requested", "note": "takes effect before the next node starts"}
        return await _control(call)

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str) -> dict[str, Any]:
        _run(run_id)
        return await _control(lambda: engine.resume(run_id))

    @app.post("/api/runs/{run_id}/cancel", status_code=202)
    async def cancel(run_id: str) -> dict[str, Any]:
        _run(run_id)

        async def call():
            await engine.cancel(run_id)
            return store.get_run(run_id)
        return await _control(call)

    @app.post("/api/runs/{run_id}/fork", status_code=202)
    async def fork(run_id: str, body: ForkBody) -> dict[str, Any]:
        _run(run_id)
        return await _control(lambda: engine.fork(run_id, body.checkpoint_id, body.changes, relation="fork"))

    @app.post("/api/runs/{run_id}/replay", status_code=202)
    async def replay(run_id: str, body: ReplayBody) -> dict[str, Any]:
        _run(run_id)
        return await _control(lambda: engine.fork(run_id, body.checkpoint_id, {}, relation="replay"))

    @app.get("/api/runs/{a}/compare/{b}")
    async def compare(a: str, b: str) -> dict[str, Any]:
        result = {}
        for key, run_id in (("a", a), ("b", b)):
            run = _run(run_id)
            nodes: dict[str, Any] = {}
            path = []
            for span in store.spans(run_id):
                if span["kind"] in NODE_KINDS and span["node_id"] and span["status"] in {"ok", "error"}:
                    nodes[span["node_id"]] = {"status": span["status"], "output": span["output"], "error": span["error"]}
                    path.append(span["node_id"])
            result[key] = {"id": run_id, "status": run["status"], "relation": run.get("relation"), "parent_run_id": run.get("parent_run_id"),
                           "fork_changes": run.get("fork_changes"), "output": run.get("output"), "executed_path": path, "nodes": nodes}
        ids = sorted(set(result["a"]["nodes"]) | set(result["b"]["nodes"]))
        result["diff"] = [{"node_id": i, "same": result["a"]["nodes"].get(i, {}).get("output") == result["b"]["nodes"].get(i, {}).get("output"),
                           "in_a": i in result["a"]["nodes"], "in_b": i in result["b"]["nodes"]} for i in ids]
        result["note"] = "Executed path lists node spans recorded in each run; fork runs only record nodes executed after their source checkpoint."
        return result

    # ----------------------------------------------------- external / OTLP
    @app.post("/v1/traces")
    async def otlp(request: Request) -> JSONResponse:
        body = await request.body()
        if len(body) > 8 * 1024 * 1024:
            return JSONResponse({"detail": "payload too large"}, status_code=413)
        try:
            result = ingest(store, decode_request(body, request.headers.get("content-type", "")))
        except OTLPError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if result["rejected"] and not result["accepted"] and not result["duplicates"]:
            return JSONResponse({"detail": "no valid spans", **result}, status_code=400)
        return JSONResponse(result)

    @app.get("/api/integrations")
    async def integrations() -> list[dict[str, Any]]:
        return [{**v, "kind": "executable+trace"} for v in INTEGRATIONS.values()] + [{
            "id": "otlp", "name": "Any OpenTelemetry agent (trace-only)", "kind": "trace",
            "description": "Export OTLP/HTTP (protobuf or JSON) spans to the endpoint below. Runs appear under Traces as read-only.",
            "endpoint": f"http://127.0.0.1:{port}/v1/traces",
            "capabilities": {"trace": True, "execute": False, "checkpoint": False, "pause_resume": False, "fork": False, "replay": False}}]

    @app.post("/api/integrations/{integration_id}/launch", status_code=202)
    async def launch(integration_id: str, body: LaunchBody) -> dict[str, Any]:
        try:
            return await launcher.launch(integration_id, body.params)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/launches/{launch_id}")
    async def launch_status(launch_id: str) -> dict[str, Any]:
        try:
            return launcher.get(launch_id)
        except KeyError as exc:
            raise HTTPException(404, "launch not found") from exc

    # -------------------------------------------------------------- frontend
    if (WEB / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.startswith(("api/", "v1/")):
            raise HTTPException(404, "not found")
        candidate = (WEB / full_path).resolve()
        if full_path and candidate.is_file() and WEB.resolve() in candidate.parents:
            return FileResponse(candidate)
        index = WEB / "index.html"
        if not index.is_file():
            return JSONResponse({"detail": "frontend not built; run: cd frontend && npm install && npm run build"}, status_code=503)
        return FileResponse(index)

    return app
