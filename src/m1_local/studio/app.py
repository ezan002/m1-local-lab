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
from .connected import ConnectedService, connector_error_status
from .connectors import CONNECTOR_KINDS, PROTOCOL_VERSION, ConnectorConflict, ConnectorError, ConnectorNotFound
from .engine import StudioEngine
from .integrations import INTEGRATIONS, Launcher
from .otlp import OTLPError, decode_request, ingest, ingest_file
from .runtime_support import TOOLS
from .schema import WorkflowDefinition
from .seeds import seed
from .steps import build_steps, span_roles
from .store import StudioStore

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "web" / "studio"
NODE_KINDS = {"start", "agent", "tool", "condition", "output", "gate", "node"}


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


class ConnectorBody(BaseModel):
    kind: Literal["m1_legacy", "studio_executor"] = "m1_legacy"
    name: str = Field(default="", max_length=120)
    mode: Literal["attach", "launch"]
    base_url: str | None = Field(default=None, max_length=200)
    port: int | None = None
    model: Literal["rule_based", "openai"] = "rule_based"
    provider_mode: Literal["openai", "deterministic", "unknown"] = "unknown"
    token: str | None = Field(default=None, max_length=200)


class ConnectedRunBody(BaseModel):
    inputs: dict[str, str] = Field(default_factory=dict)


def tree_order(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Depth-first order: each span before its children, siblings by start time.

    Spans are stored in arrival order. OTLP exporters send a span when it ends, so children of an external trace
    arrive before their parent; listing them in arrival order put leaves above the agent that called them."""
    ids = {s["id"] for s in spans}
    children: dict[str | None, list[dict[str, Any]]] = {}
    for position, span in enumerate(spans):
        parent = span.get("parent_span_id") if span.get("parent_span_id") in ids else None
        children.setdefault(parent, []).append({**span, "_pos": position})
    ordered: list[dict[str, Any]] = []
    stack = sorted(children.get(None, []), key=lambda s: (s.get("start_time") or "", s["_pos"]), reverse=True)
    while stack:
        span = stack.pop()
        ordered.append({k: v for k, v in span.items() if k != "_pos"})
        stack += sorted(children.get(span["id"], []), key=lambda s: (s.get("start_time") or "", s["_pos"]), reverse=True)
    return ordered


class TraceFileBody(BaseModel):
    filename: str = Field(default="", max_length=300)
    content: str


class ImportBody(BaseModel):
    remote_run_id: str = Field(min_length=1, max_length=200)


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
    connected = ConnectedService(store, studio_port=port, data_root=data_dir / "connected")
    token = secrets.token_urlsafe(32)
    build = capture_build_info("studio")
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.recovered = await engine.startup()
        if seed_demo:
            seed(store)
        await connected.startup()
        yield
        await connected.shutdown()
        await engine.shutdown()
        store.close()

    app = FastAPI(title="Agent Studio", lifespan=lifespan)
    app.state.store, app.state.engine, app.state.launcher, app.state.connected = store, engine, launcher, connected

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
    async def runs(workflow_id: str | None = None, origin: Literal["studio", "external", "connected"] | None = None,
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
        if run["origin"] == "connected" and not connected.is_polling(run_id):
            try:  # cheap re-sync so a run viewed after a restart reflects the executor's current state
                run = await connected.sync(run_id)
            except (ConnectorError, ConnectorNotFound) as exc:
                run = {**run, "connector_unavailable": str(exc)}
        parent = store.get_run(run["parent_run_id"]) if run.get("parent_run_id") else None
        return {**run, "active": engine.is_active(run_id) or connected.is_polling(run_id), "children": store.children(run_id),
                "parent": {"id": parent["id"], "status": parent["status"]} if parent else None}

    @app.get("/api/runs/{run_id}/spans")
    async def spans(run_id: str) -> list[dict[str, Any]]:
        _run(run_id)
        return tree_order(store.spans(run_id))

    @app.get("/api/runs/{run_id}/steps")
    async def steps(run_id: str) -> list[dict[str, Any]]:
        """The run as numbered atomic actions (model calls, tool calls, control nodes); see studio/steps.py."""
        _run(run_id)
        return build_steps(tree_order(store.spans(run_id)))

    @app.get("/api/runs/{run_id}/step-map")
    async def step_map(run_id: str) -> dict[str, Any]:
        """Every span of the run and its relation to the steps (step, duplicate, part, container, plumbing)."""
        _run(run_id)
        spans = tree_order(store.spans(run_id))
        steps = build_steps(spans)
        return {"spans": len(spans), "steps": len(steps), "roles": span_roles(spans, steps)}

    @app.get("/api/runs/{run_id}/checkpoints")
    async def checkpoints(run_id: str) -> dict[str, Any]:
        run = _run(run_id)
        if run["origin"] == "connected":
            items = await _control(lambda: connected.checkpoints(run_id))
            return {"supported": True, "connected": True, "items": items,
                    "note": "Read live from the agent's own checkpoint store. Fork edits are limited to the fields the agent declares."}
        if run["origin"] != "studio":
            return {"supported": False, "reason": "External runs are trace-only: the source process exposes no checkpoints or control interface.", "items": []}
        return {"supported": True, "items": await engine.checkpoints(run_id)}

    async def _control(call) -> Any:
        try:
            return await call()
        except (ConnectorError, ConnectorNotFound, ConnectorConflict) as exc:
            raise HTTPException(connector_error_status(exc), str(exc).strip("'")) from exc
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'")) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    def _is_connected(run_id: str) -> bool:
        return _run(run_id)["origin"] == "connected"

    @app.post("/api/runs/{run_id}/pause", status_code=202)
    async def pause(run_id: str) -> dict[str, Any]:
        if _is_connected(run_id):
            return await _control(lambda: connected.pause(run_id))

        async def call():
            engine.request_pause(run_id)
            return {"status": "pause_requested", "note": "takes effect before the next node starts"}
        return await _control(call)

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str) -> dict[str, Any]:
        if _is_connected(run_id):
            return await _control(lambda: connected.resume(run_id))
        return await _control(lambda: engine.resume(run_id))

    @app.post("/api/runs/{run_id}/cancel", status_code=202)
    async def cancel(run_id: str) -> dict[str, Any]:
        if _is_connected(run_id):
            raise HTTPException(409, "the connected executor has no cancel operation; use Pause")

        async def call():
            await engine.cancel(run_id)
            return store.get_run(run_id)
        return await _control(call)

    @app.post("/api/runs/{run_id}/fork", status_code=202)
    async def fork(run_id: str, body: ForkBody) -> dict[str, Any]:
        if _is_connected(run_id):
            return await _control(lambda: connected.fork(run_id, body.checkpoint_id, body.changes, relation="fork"))
        return await _control(lambda: engine.fork(run_id, body.checkpoint_id, body.changes, relation="fork"))

    @app.post("/api/runs/{run_id}/replay", status_code=202)
    async def replay(run_id: str, body: ReplayBody) -> dict[str, Any]:
        if _is_connected(run_id):
            return await _control(lambda: connected.fork(run_id, body.checkpoint_id, {}, relation="replay"))
        return await _control(lambda: engine.fork(run_id, body.checkpoint_id, {}, relation="replay"))

    @app.get("/api/runs/{a}/compare/{b}")
    async def compare(a: str, b: str) -> dict[str, Any]:
        result = {}
        for key, run_id in (("a", a), ("b", b)):
            run = _run(run_id)
            nodes: dict[str, Any] = {}
            path = []
            all_spans = store.spans(run_id)
            roots = {s["id"] for s in all_spans if s["kind"] == "workflow"}
            for span in all_spans:
                # Node spans hang directly under the workflow span; llm/tool children share node_id but are not executions.
                if (span["kind"] in NODE_KINDS and span["node_id"] and span["parent_span_id"] in roots
                        and span["status"] in {"ok", "error"}):
                    nodes[span["node_id"]] = {"status": span["status"], "output": span["output"], "error": span["error"]}
                    path.append(span["node_id"])
            result[key] = {"id": run_id, "status": run["status"], "relation": run.get("relation"), "parent_run_id": run.get("parent_run_id"),
                           "inherited": (run.get("inherited") or {}).get("nodes", []),
                           "fork_changes": run.get("fork_changes"), "output": run.get("output"), "executed_path": path, "nodes": nodes}
        ids = sorted(set(result["a"]["nodes"]) | set(result["b"]["nodes"]))
        diff = []
        for i in ids:
            in_a, in_b = i in result["a"]["nodes"], i in result["b"]["nodes"]
            inherited = (not in_a and i in result["a"]["inherited"]) or (not in_b and i in result["b"]["inherited"])
            same = result["a"]["nodes"].get(i, {}).get("output") == result["b"]["nodes"].get(i, {}).get("output")
            diff.append({"node_id": i, "in_a": in_a, "in_b": in_b,
                         "verdict": "inherited" if inherited else ("same" if same else "differs"), "same": same and not inherited})
        result["diff"] = diff
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

    @app.post("/api/traces/import")
    async def import_traces(body: TraceFileBody) -> dict[str, Any]:
        """Import a trace file (OTLP JSON / JSON Lines) uploaded from the External traces page."""
        if len(body.content) > 32 * 1024 * 1024:
            raise HTTPException(413, "file too large (limit 32 MB)")
        try:
            result = ingest_file(store, body.content, body.filename)
        except OTLPError as exc:
            raise HTTPException(400, str(exc)) from exc
        if result["rejected"] and not result["accepted"] and not result["duplicates"]:
            raise HTTPException(400, f"no valid spans: {result['rejected'][0]}")
        return {**result, "filename": body.filename}

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

    # ------------------------------------------------- connected executors
    @app.get("/api/connector-kinds")
    async def connector_kinds() -> dict[str, Any]:
        return {"protocol": PROTOCOL_VERSION, "kinds": list(CONNECTOR_KINDS.values())}

    @app.get("/api/connectors")
    async def connectors() -> list[dict[str, Any]]:
        return await connected.list_status()

    @app.post("/api/connectors", status_code=201)
    async def create_connector(body: ConnectorBody) -> dict[str, Any]:
        try:
            record = connected.create_connector(kind=body.kind, name=body.name, mode=body.mode, base_url=body.base_url,
                                                port=body.port, model=body.model, provider_mode=body.provider_mode,
                                                token=body.token)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return await connected.status(record["id"])

    @app.get("/api/connectors/{connector_id}")
    async def connector(connector_id: str) -> dict[str, Any]:
        return await _control(lambda: connected.status(connector_id))

    @app.delete("/api/connectors/{connector_id}")
    async def delete_connector(connector_id: str) -> dict[str, Any]:
        await _control(lambda: connected.delete_connector(connector_id))
        return {"deleted": connector_id, "note": "mirrored runs stay in the Studio; the agent's own data is untouched"}

    @app.post("/api/connectors/{connector_id}/start", status_code=202)
    async def start_connector(connector_id: str) -> dict[str, Any]:
        return await _control(lambda: connected.start_process(connector_id))

    @app.post("/api/connectors/{connector_id}/stop", status_code=202)
    async def stop_connector(connector_id: str) -> dict[str, Any]:
        return await _control(lambda: connected.stop_process(connector_id))

    @app.get("/api/connectors/{connector_id}/describe")
    async def describe_connector(connector_id: str) -> dict[str, Any]:
        return await _control(lambda: connected.describe(connector_id, refresh=True))

    @app.get("/api/connectors/{connector_id}/remote-runs")
    async def remote_runs(connector_id: str) -> list[dict[str, Any]]:
        return await _control(lambda: connected.remote_runs(connector_id))

    @app.post("/api/connectors/{connector_id}/runs", status_code=202)
    async def start_connected_run(connector_id: str, body: ConnectedRunBody) -> dict[str, Any]:
        return await _control(lambda: connected.start_run(connector_id, body.inputs))

    @app.post("/api/connectors/{connector_id}/import", status_code=201)
    async def import_connected_run(connector_id: str, body: ImportBody) -> dict[str, Any]:
        return await _control(lambda: connected.import_run(connector_id, body.remote_run_id))

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
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app
