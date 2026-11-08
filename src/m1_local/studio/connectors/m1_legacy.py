"""Connector for the original M1 LangGraph executor (``m1_local.api`` / ``AgentRuntime``).

The executor keeps running in its own process with its own checkpoint database and its own HTTP
control API (runs, history, pause, resume, replay, fork). This connector only translates that API into
the Studio executor protocol; it never opens the executor's databases.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import ConnectorConflict, ConnectorError, ConnectorNotFound, ExecutorConnector

# Used only when an older executor build has no GET /api/graph; mirrors m1_local/graph.py.
FALLBACK_GRAPH = {
    "nodes": [{"id": "planner"}, {"id": "orchestrator"}, {"id": "pause_gate"}, {"id": "web_surfer"},
              {"id": "file_reader"}, {"id": "coder"}, {"id": "finalizer"}],
    "edges": [{"source": "__start__", "target": "planner", "conditional": False},
              {"source": "planner", "target": "orchestrator", "conditional": False},
              {"source": "orchestrator", "target": "planner", "conditional": True},
              {"source": "orchestrator", "target": "pause_gate", "conditional": True},
              {"source": "orchestrator", "target": "finalizer", "conditional": True},
              {"source": "pause_gate", "target": "web_surfer", "conditional": True},
              {"source": "pause_gate", "target": "file_reader", "conditional": True},
              {"source": "pause_gate", "target": "coder", "conditional": True},
              {"source": "web_surfer", "target": "orchestrator", "conditional": False},
              {"source": "file_reader", "target": "orchestrator", "conditional": False},
              {"source": "coder", "target": "orchestrator", "conditional": False},
              {"source": "finalizer", "target": "__end__", "conditional": False}],
}
NODE_TYPES = {"planner": "agent", "orchestrator": "agent", "finalizer": "agent", "pause_gate": "gate",
              "web_surfer": "tool", "file_reader": "tool", "coder": "tool"}
NODE_LABELS = {"planner": "Planner", "orchestrator": "Orchestrator", "pause_gate": "Pause gate",
               "web_surfer": "WebSurfer", "file_reader": "File reader", "coder": "Coder", "finalizer": "Finalizer"}
FORK_FIELDS = [
    {"path": "plan", "type": "string_list", "max_items": 100, "max_length": 20_000,
     "description": "Planner output. Edit at a checkpoint after the planner; the orchestrator reads it next."},
    {"path": "worker_instruction", "type": "string", "max_length": 20_000,
     "description": "Instruction the orchestrator gave the next worker. Edit at a checkpoint after the orchestrator."},
    {"path": "next_agent", "type": "string", "choices": ["web_surfer", "file_reader", "coder", "finalizer"],
     "description": "The orchestrator's routing decision: which worker runs next. Edit after the orchestrator to send the "
                    "run down another path; the route is re-evaluated from the new value."},
]
CAPABILITIES = {"trace": True, "execute": True, "checkpoint": True, "pause_resume": True, "fork": True,
                "replay": True, "retry": True, "cancel": False}


class M1LegacyConnector(ExecutorConnector):
    kind = "m1_legacy"

    def __init__(self, base_url: str, *, provider_mode: str = "unknown", timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.provider_mode = provider_mode
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport, trust_env=False)
        self._csrf: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, json: Any = None, retry_csrf: bool = True) -> Any:
        headers = {}
        if method != "GET":
            if self._csrf is None:
                await self._session()
            headers["x-m1-csrf"] = self._csrf or ""
        try:
            response = await self._client.request(method, path, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"executor at {self.base_url} is unreachable: {type(exc).__name__}: {exc}") from exc
        if response.status_code == 403 and method != "GET" and retry_csrf and "CSRF" in response.text:
            self._csrf = None  # the executor restarted and issued a new token
            return await self._request(method, path, json=json, retry_csrf=False)
        if response.status_code == 404:
            raise ConnectorNotFound(self._detail(response))
        if response.status_code == 409:
            raise ConnectorConflict(self._detail(response))
        if response.status_code == 422:
            raise ValueError(self._detail(response))
        if response.status_code >= 400:
            raise ConnectorError(f"executor returned HTTP {response.status_code}: {self._detail(response)}")
        return response.json() if response.content else None

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text[:500]
        detail = body.get("detail") if isinstance(body, dict) else body
        return detail if isinstance(detail, str) else str(detail)[:500]

    async def _session(self) -> dict[str, Any]:
        session = await self._request("GET", "/api/session")
        self._csrf = session.get("csrf_token")
        return session

    # ------------------------------------------------------------ protocol
    async def health(self) -> dict[str, Any]:
        session = await self._session()
        return {"reachable": True, "tracing_warning": session.get("tracing_warning")}

    async def describe(self) -> dict[str, Any]:
        session = await self._session()
        try:
            graph = await self._request("GET", "/api/graph")
            graph_source = "executor"
        except ConnectorNotFound:
            graph, graph_source = FALLBACK_GRAPH, "connector fallback (executor has no /api/graph)"
        nodes = [{"id": n["id"], "type": NODE_TYPES.get(n["id"], "node"), "label": NODE_LABELS.get(n["id"], n["id"])}
                 for n in graph["nodes"] if n["id"] not in {"__start__", "__end__"}]
        declared = set((session.get("capabilities") or {}).get("fork_fields") or [])
        return {
            "name": "M1 LangGraph executor", "framework": "langgraph", "protocol": "m1-legacy-http",
            "graph": {"nodes": nodes, "edges": graph["edges"], "source": graph_source},
            "capabilities": dict(CAPABILITIES),
            "fork_fields": [f for f in FORK_FIELDS if not declared or f["path"] in declared],
            "input_fields": [{"name": "task", "label": "Task", "default": "",
                              "hint": "Planner routes tasks mentioning code to Coder and files to File reader; others use WebSurfer."},
                             {"name": "allowed_files", "label": "Allowed files (comma separated, optional)", "default": ""}],
            "provider_mode": self.provider_mode,
            "notes": ["Pause takes effect at the executor's pause gate (before the next worker).",
                      "One active run at a time (executor limit MAX_ACTIVE_RUNS=1).",
                      "Fork and replay are created by the executor in the same LangGraph thread as a new branch; the source run's head never moves."],
        }

    async def list_runs(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/runs")

    async def start(self, inputs: dict[str, Any]) -> str:
        task = str(inputs.get("task") or "").strip()
        if not task:
            raise ValueError("task must not be empty")
        files = [f.strip() for f in str(inputs.get("allowed_files") or "").split(",") if f.strip()]
        body = await self._request("POST", "/api/runs", json={"task": task, "allowed_files": files})
        return body["run_id"]

    async def run(self, remote_run_id: str) -> dict[str, Any]:
        body = await self._request("GET", f"/api/runs/{remote_run_id}")
        record, state = body.get("run") or {}, body.get("state") or {}
        return {
            "status": record.get("status") or body.get("status"), "error": record.get("error"),
            "output": {"final_answer": state.get("final_answer"), "status": state.get("status"),
                       "artifacts": [a.get("name") or a.get("id") for a in state.get("artifacts", [])],
                       "citations": state.get("citations", [])},
            "task": record.get("task"), "thread_id": record.get("thread_id"), "branch_id": record.get("branch_id"),
            "parent_run_id": record.get("parent_run_id"), "source_checkpoint_id": record.get("source_checkpoint_id"),
            "trace_id": body.get("trace_id"), "created_at": record.get("created_at"),
        }

    async def history(self, remote_run_id: str) -> list[dict[str, Any]]:
        items = await self._request("GET", f"/api/runs/{remote_run_id}/history")
        result = []
        for i, item in enumerate(items):
            parent = items[i + 1]["checkpoint_id"] if i + 1 < len(items) else None
            metadata = item.get("metadata") or {}
            result.append({
                "checkpoint_id": item["checkpoint_id"], "parent_checkpoint_id": parent,
                "created_at": item.get("created_at"), "step": metadata.get("step"), "source": metadata.get("source"),
                "next": list(item.get("next") or []), "values": item.get("state") or {},
                "interrupted": bool(item.get("interrupts")),
            })
        return result

    async def pause(self, remote_run_id: str) -> None:
        await self._request("POST", f"/api/runs/{remote_run_id}/pause")

    async def resume(self, remote_run_id: str) -> None:
        await self._request("POST", f"/api/runs/{remote_run_id}/resume")

    async def fork(self, remote_run_id: str, checkpoint_id: str, changes: dict[str, Any]) -> str:
        self.validate_changes(FORK_FIELDS, changes)
        body = await self._request("POST", f"/api/runs/{remote_run_id}/fork",
                                   json={"checkpoint_id": checkpoint_id, "changes": changes})
        return body["run_id"]  # the executor creates forks paused; the caller decides whether to resume

    async def replay(self, remote_run_id: str, checkpoint_id: str) -> str:
        body = await self._request("POST", f"/api/runs/{remote_run_id}/replay", json={"checkpoint_id": checkpoint_id})
        return body["run_id"]
