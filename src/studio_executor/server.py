"""HTTP side of the studio-executor protocol: a dependency-free ASGI app plus a ``serve`` helper.

Endpoints (JSON, prefix ``/studio/v1``)::

    GET  /describe                     graph, capabilities, fork fields, input fields
    GET  /health
    GET  /runs                         POST /runs {"inputs": {...}}            -> {"run_id"}
    GET  /runs/{id}                    GET  /runs/{id}/history
    POST /runs/{id}/pause              POST /runs/{id}/resume {"value"?: any}
    POST /runs/{id}/fork {"checkpoint_id", "changes"} -> {"run_id"} (created paused)
    POST /runs/{id}/replay {"checkpoint_id"}           -> {"run_id"} (started)

Every request must carry ``x-studio-executor-token``. Requests that carry a browser ``Origin`` header are
refused, the Host header must be the loopback address the server listens on, and ``serve`` only binds to
127.0.0.1.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
from typing import Any

from .core import ExecutorConflict, ExecutorNotFound, StudioExecutor

TOKEN_HEADER = b"x-studio-executor-token"
_ROUTE = re.compile(r"^/studio/v1/runs/(?P<run>[A-Za-z0-9_.:-]{1,200})(?:/(?P<action>history|pause|resume|fork|replay))?$")
MAX_BODY = 2 * 1024 * 1024


def create_asgi_app(executor: StudioExecutor, *, token: str, port: int | None = None):
    if not token or len(token) < 16:
        raise ValueError("token must be at least 16 characters")
    hosts = {f"127.0.0.1:{port}".encode(), f"localhost:{port}".encode()} if port else None

    async def app(scope: dict[str, Any], receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await executor.aclose()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        headers = dict(scope.get("headers") or [])
        if hosts is not None and headers.get(b"host") not in hosts:
            return await _send(send, 400, {"detail": "invalid Host"})
        if b"origin" in headers:
            return await _send(send, 403, {"detail": "browser requests are not accepted"})
        if not hmac.compare_digest(headers.get(TOKEN_HEADER, b"").decode("latin-1"), token):
            return await _send(send, 401, {"detail": "missing or invalid x-studio-executor-token"})
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > MAX_BODY:
                return await _send(send, 413, {"detail": "body too large"})
        try:
            payload = json.loads(body) if body else {}
            if not isinstance(payload, dict):
                raise ValueError("JSON object expected")
        except ValueError as exc:
            return await _send(send, 400, {"detail": f"invalid JSON: {exc}"})
        try:
            status, result = await _dispatch(executor, scope["method"], scope["path"], payload)
        except ExecutorNotFound as exc:
            status, result = 404, {"detail": str(exc).strip("'\"")}
        except ExecutorConflict as exc:
            status, result = 409, {"detail": str(exc)}
        except ValueError as exc:
            status, result = 422, {"detail": str(exc)}
        await _send(send, status, result)

    return app


async def _dispatch(executor: StudioExecutor, method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    if path == "/studio/v1/describe" and method == "GET":
        return 200, executor.describe()
    if path == "/studio/v1/health" and method == "GET":
        return 200, {"status": "ok", "name": executor.name, "active_runs": len(executor._tasks)}
    if path == "/studio/v1/runs":
        if method == "GET":
            return 200, executor.list_runs()
        if method == "POST":
            inputs = body.get("inputs") or {}
            if not isinstance(inputs, dict):
                raise ValueError("inputs must be an object")
            return 202, {"run_id": await executor.start(inputs)}
    match = _ROUTE.match(path)
    if match:
        run_id, action = match["run"], match["action"]
        if action is None and method == "GET":
            return 200, await executor.run(run_id)
        if action == "history" and method == "GET":
            return 200, await executor.history(run_id)
        if action == "pause" and method == "POST":
            await executor.pause(run_id)
            return 202, {"status": "pausing"}
        if action == "resume" and method == "POST":
            await executor.resume(run_id, body.get("value"))
            return 202, {"status": "resuming"}
        if action == "fork" and method == "POST":
            return 201, {"run_id": await executor.fork(run_id, _required(body, "checkpoint_id"), body.get("changes") or {})}
        if action == "replay" and method == "POST":
            return 202, {"run_id": await executor.replay(run_id, _required(body, "checkpoint_id"))}
    return 404, {"detail": f"no route for {method} {path}"}


def _required(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


async def _send(send, status: int, payload: Any) -> None:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
                            (b"x-content-type-options", b"nosniff")]})
    await send({"type": "http.response.body", "body": data})


def _announce(executor: StudioExecutor, port: int, token: str | None) -> str:
    resolved = token or os.environ.get("STUDIO_EXECUTOR_TOKEN") or secrets.token_urlsafe(24)
    origin = "from STUDIO_EXECUTOR_TOKEN" if os.environ.get("STUDIO_EXECUTOR_TOKEN") and not token else f"token: {resolved}"
    print(f"[studio-executor] {executor.name} on http://127.0.0.1:{port}  {origin}", flush=True)
    return resolved


async def serve_async(executor: StudioExecutor, *, port: int = 8020, token: str | None = None) -> None:
    """Serve on 127.0.0.1:<port> inside the running event loop (use this when the graph's checkpointer
    must be created inside the loop, e.g. AsyncSqliteSaver)."""
    import uvicorn

    app = create_asgi_app(executor, token=_announce(executor, port, token), port=port)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, workers=1, reload=False, loop="asyncio", access_log=False)
    await uvicorn.Server(config).serve()


def serve(executor: StudioExecutor, *, port: int = 8020, token: str | None = None) -> None:
    """Blocking variant of :func:`serve_async`. The token comes from the argument, STUDIO_EXECUTOR_TOKEN,
    or is generated and printed once at startup."""
    import asyncio

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(serve_async(executor, port=port, token=token))
