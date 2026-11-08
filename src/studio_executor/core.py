"""Wrap a compiled LangGraph graph (with its own checkpointer) as a debuggable executor.

The wrapped graph keeps running in the agent's own process with the agent's own checkpointer. This module
adds a run registry and the control operations Agent Studio uses (protocol ``studio-executor/1``):
start, pause at the next step boundary, resume (also after an error or a graph ``interrupt()``), fork
from any checkpoint with declared edits, and replay from a checkpoint. Forks and replays branch inside
the same LangGraph thread, so the source run's checkpoints are never rewritten.

Nothing here imports Agent Studio; the only dependency is LangGraph itself.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as _dt
import json
import sqlite3
import threading
import traceback
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "studio-executor/1"
ACTIVE = {"queued", "running", "pausing"}
FIELD_TYPES = {"string", "string_list", "number", "boolean", "json"}


class ExecutorConflict(Exception):
    """The request is not possible in the run's current state (HTTP 409)."""


class ExecutorNotFound(KeyError):
    """Unknown run or checkpoint (HTTP 404)."""


@dataclasses.dataclass
class ForkField:
    """A top-level state key that a fork may overwrite.

    Only keys whose channel *replaces* its value (LangGraph ``LastValue``) are allowed: an edit to a key with
    a reducer (``Annotated[list, operator.add]``, ``add_messages``) would be merged rather than replaced, which
    is not what someone editing a checkpoint expects.
    """

    path: str
    type: str = "string"
    description: str = ""
    max_length: int = 20_000
    max_items: int = 200

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def jsonable(value: Any, depth: int = 0) -> Any:
    """Best-effort JSON view of graph state (messages, pydantic models, dataclasses, datetimes)."""
    if depth > 12:
        return "…"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v, depth + 1) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        with contextlib.suppress(Exception):
            return jsonable(value.model_dump(), depth + 1)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value), depth + 1)
    return repr(value)[:2000]


def _checkpoint_id(config: Mapping[str, Any] | None) -> str | None:
    return ((config or {}).get("configurable") or {}).get("checkpoint_id")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


class _Registry:
    """Run bookkeeping (status, branch head, lineage). Checkpoints themselves stay in the graph's checkpointer."""

    COLUMNS = ("run_id", "thread_id", "status", "error", "inputs", "relation", "parent_run_id",
               "source_checkpoint_id", "fork_changes", "head_config", "created_at", "updated_at")
    JSON = {"error", "inputs", "fork_changes", "head_config"}

    def __init__(self, path: str | Path | None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"CREATE TABLE IF NOT EXISTS runs ({', '.join(c + ' TEXT' for c in self.COLUMNS)}, PRIMARY KEY (run_id))")

    def put(self, record: dict[str, Any]) -> None:
        row = [json.dumps(record.get(c)) if c in self.JSON else record.get(c) for c in self.COLUMNS]
        with self._lock:
            self._conn.execute(f"INSERT OR REPLACE INTO runs VALUES ({','.join('?' * len(self.COLUMNS))})", row)

    def update(self, run_id: str, **fields: Any) -> dict[str, Any]:
        record = {**self.get(run_id), **fields, "updated_at": _now()}
        self.put(record)
        return record

    def get(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ExecutorNotFound(f"run {run_id} not found")
        return {k: (json.loads(row[k]) if k in self.JSON and row[k] is not None else row[k]) for k in self.COLUMNS}

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = [r["run_id"] for r in self._conn.execute("SELECT run_id FROM runs ORDER BY created_at DESC")]
        return [self.get(i) for i in ids]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class StudioExecutor:
    """Make a compiled LangGraph graph controllable by Agent Studio.

    graph         compiled ``StateGraph`` with a checkpointer (any saver: memory, SQLite, Postgres …)
    name          shown in the Studio
    input_fields  [{"name", "label", "default"}] the Studio asks for when starting a run
    make_input    inputs dict -> graph input (default: the inputs dict itself)
    fork_fields   ForkField list: state keys a fork may overwrite (replace-semantics channels only)
    output        final state values -> what the Studio shows as the run output (default: all values)
    node_types    optional {node: "agent" | "tool" | "gate" | "node"} for the Studio graph
    registry_path SQLite file for run bookkeeping (default in memory; checkpoints live in your checkpointer)
    """

    def __init__(self, graph: Any, *, name: str, input_fields: list[dict[str, Any]] | None = None,
                 make_input: Callable[[dict[str, Any]], Any] | None = None, fork_fields: list[ForkField] | None = None,
                 output: Callable[[dict[str, Any]], Any] | None = None, node_types: dict[str, str] | None = None,
                 provider_mode: str = "unknown", registry_path: str | Path | None = None, max_active_runs: int = 1,
                 notes: list[str] | None = None) -> None:
        if getattr(graph, "checkpointer", None) is None:
            raise ValueError("compile the graph with a checkpointer: graph = builder.compile(checkpointer=...)")
        self.graph = graph
        self.name = name
        self.input_fields = input_fields or [{"name": "input", "label": "Input (JSON or text)", "default": ""}]
        self.make_input = make_input or (lambda inputs: inputs)
        self.fork_fields = list(fork_fields or [])
        self.output = output or (lambda values: values)
        self.node_types = node_types or {}
        self.provider_mode = provider_mode
        self.max_active_runs = max_active_runs
        self.notes = notes or []
        self._check_fork_fields()
        self.registry = _Registry(registry_path)
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._pause: set[str] = set()
        self._admission = asyncio.Lock()
        for record in self.registry.all():  # a previous process stopped mid-run: resumable from its head
            if record["status"] in ACTIVE:
                self.registry.update(record["run_id"], status="interrupted",
                                     error={"type": "ProcessRestart", "message": "the agent process stopped while this run was active"})

    def _check_fork_fields(self) -> None:
        channels = getattr(self.graph, "channels", {}) or {}
        for field in self.fork_fields:
            if field.type not in FIELD_TYPES:
                raise ValueError(f"fork field {field.path}: type must be one of {sorted(FIELD_TYPES)}")
            if "." in field.path:
                raise ValueError(f"fork field {field.path}: only top-level state keys are supported")
            channel = channels.get(field.path)
            if channel is None:
                raise ValueError(f"fork field {field.path} is not a state key of this graph")
            if type(channel).__name__ not in {"LastValue", "AnyValue", "LastValueAfterFinish"}:
                raise ValueError(f"fork field {field.path} uses a reducer ({type(channel).__name__}); "
                                 "edits would be merged, not replaced. Expose a replace-semantics key instead.")

    # ------------------------------------------------------------------ describe
    def describe(self) -> dict[str, Any]:
        drawable = self.graph.get_graph()
        nodes = [{"id": node_id, "type": self.node_types.get(node_id, "node"),
                  "label": (getattr(node, "name", None) or node_id)}
                 for node_id, node in drawable.nodes.items() if node_id not in {"__start__", "__end__"}]
        return {
            "protocol": PROTOCOL_VERSION, "name": self.name, "framework": "langgraph",
            "graph": {"nodes": nodes, "edges": [{"source": e.source, "target": e.target, "conditional": bool(e.conditional)}
                                                for e in drawable.edges], "source": "executor"},
            "capabilities": {"trace": True, "execute": True, "checkpoint": True, "pause_resume": True, "fork": True,
                             "replay": True, "retry": True, "cancel": False},
            "fork_fields": [f.as_dict() for f in self.fork_fields],
            "input_fields": self.input_fields, "provider_mode": self.provider_mode,
            "notes": ["Pause takes effect at the next step boundary (after the running step commits its checkpoint).",
                      "Fork and replay branch inside the run's LangGraph thread; the source run's checkpoints are not modified.",
                      *self.notes],
        }

    # ---------------------------------------------------------------------- runs
    def _public(self, record: dict[str, Any]) -> dict[str, Any]:
        return {"run_id": record["run_id"], "status": record["status"], "error": record["error"],
                "relation": record["relation"], "parent_run_id": record["parent_run_id"],
                "source_checkpoint_id": record["source_checkpoint_id"], "fork_changes": record["fork_changes"],
                "thread_id": record["thread_id"], "inputs": record["inputs"], "created_at": record["created_at"],
                "updated_at": record["updated_at"], "task": _summary(record["inputs"])}

    def list_runs(self) -> list[dict[str, Any]]:
        return [self._public(r) for r in self.registry.all()]

    async def run(self, run_id: str) -> dict[str, Any]:
        record = self.registry.get(run_id)
        output = None
        if record["head_config"]:
            snapshot = await self.graph.aget_state(record["head_config"])
            if snapshot.values:
                output = jsonable(self.output(dict(snapshot.values)))
        return {**self._public(record), "output": output, "active": run_id in self._tasks}

    async def start(self, inputs: dict[str, Any]) -> str:
        allowed = {f["name"] for f in self.input_fields}
        unknown = sorted(set(inputs) - allowed)
        if unknown:
            raise ValueError(f"unknown inputs {unknown}; accepted: {sorted(allowed)}")
        graph_input = self.make_input(dict(inputs))
        async with self._admission:
            self._admit()
            run_id = str(uuid.uuid4())
            config = {"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}
            self.registry.put({"run_id": run_id, "thread_id": run_id, "status": "queued", "inputs": inputs,
                               "head_config": None, "created_at": _now(), "updated_at": _now()})
            self._launch(run_id, graph_input, config)
        return run_id

    def _admit(self) -> None:
        active = [rid for rid, task in self._tasks.items() if not task.done()]
        if len(active) >= self.max_active_runs:
            raise ExecutorConflict(f"another run is active ({active[0]}); max_active_runs={self.max_active_runs}")

    def _launch(self, run_id: str, graph_input: Any, config: dict[str, Any]) -> None:
        self.registry.update(run_id, status="queued")
        task = asyncio.create_task(self._execute(run_id, graph_input, config))
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t, rid=run_id: self._tasks.pop(rid, None))

    async def _execute(self, run_id: str, graph_input: Any, config: dict[str, Any]) -> None:
        self.registry.update(run_id, status="running", error=None)
        head = config if _checkpoint_id(config) else None
        try:
            async for _mode, event in self.graph.astream(graph_input, config, stream_mode=["checkpoints"], durability="sync"):
                head = event["config"]
                self.registry.update(run_id, head_config=head)
                if run_id in self._pause:
                    break  # the next step has not started: a clean boundary
            self._pause.discard(run_id)
            if head is None:
                self.registry.update(run_id, status="failed", error={"type": "NoCheckpoint", "message": "the graph produced no checkpoint"})
                return
            snapshot = await self.graph.aget_state(head)
            interrupts = [jsonable(getattr(i, "value", i)) for t in snapshot.tasks for i in getattr(t, "interrupts", ())]
            if interrupts:
                self.registry.update(run_id, status="paused", error=None, head_config=head)
            elif snapshot.next:
                self.registry.update(run_id, status="paused", error=None, head_config=head)
            else:
                self.registry.update(run_id, status="completed", error=None, head_config=head)
        except asyncio.CancelledError:
            self.registry.update(run_id, status="interrupted", error={"type": "Cancelled", "message": "the agent process is stopping"})
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the Studio, retry resumes from the head
            self._pause.discard(run_id)
            self.registry.update(run_id, status="failed", head_config=head, error={
                "type": type(exc).__name__, "message": str(exc)[:2000],
                "traceback": "".join(traceback.format_exception(exc))[-4000:]})

    async def pause(self, run_id: str) -> None:
        record = self.registry.get(run_id)
        if record["status"] not in {"queued", "running"} or run_id not in self._tasks:
            raise ExecutorConflict(f"only a running run can be paused (status={record['status']})")
        self._pause.add(run_id)
        self.registry.update(run_id, status="pausing")

    async def resume(self, run_id: str, value: Any = None) -> None:
        from langgraph.types import Command

        async with self._admission:
            record = self.registry.get(run_id)
            if record["status"] not in {"paused", "failed", "interrupted"}:
                raise ExecutorConflict(f"only paused, failed or interrupted runs can be resumed (status={record['status']})")
            if not record["head_config"]:
                raise ExecutorConflict("run has no checkpoint to resume from; start a new run")
            self._admit()
            snapshot = await self.graph.aget_state(record["head_config"])
            if not snapshot.next:
                raise ExecutorConflict("run has nothing left to execute")
            interrupted = any(getattr(t, "interrupts", ()) for t in snapshot.tasks)
            self._launch(run_id, Command(resume=value) if interrupted else None, record["head_config"])

    async def _find(self, run_id: str, checkpoint_id: str) -> Any:
        for snapshot in await self._chain(run_id):
            if _checkpoint_id(snapshot.config) == checkpoint_id:
                return snapshot
        raise ExecutorNotFound(f"checkpoint {checkpoint_id} is not in run {run_id}")

    async def _chain(self, run_id: str) -> list[Any]:
        record = self.registry.get(run_id)
        cursor = record["head_config"]
        chain, seen = [], set()
        while cursor:
            snapshot = await self.graph.aget_state(cursor)
            cid = _checkpoint_id(snapshot.config)
            if not cid or cid in seen:
                break
            seen.add(cid)
            chain.append(snapshot)
            cursor = snapshot.parent_config
        return chain

    def validate_changes(self, changes: dict[str, Any]) -> dict[str, Any]:
        fields = {f.path: f for f in self.fork_fields}
        unknown = sorted(set(changes) - set(fields))
        if unknown:
            raise ValueError(f"fields not editable: {unknown}; allowed: {sorted(fields)}")
        for path, value in changes.items():
            spec = fields[path]
            ok = {"string": isinstance(value, str), "number": isinstance(value, (int, float)) and not isinstance(value, bool),
                  "boolean": isinstance(value, bool), "json": True,
                  "string_list": isinstance(value, list) and all(isinstance(v, str) for v in value)}[spec.type]
            if not ok:
                raise ValueError(f"{path} must be of type {spec.type}")
            if isinstance(value, str) and len(value) > spec.max_length:
                raise ValueError(f"{path} is longer than {spec.max_length} characters")
            if isinstance(value, list) and len(value) > spec.max_items:
                raise ValueError(f"{path} has more than {spec.max_items} items")
        return changes

    async def fork(self, run_id: str, checkpoint_id: str, changes: dict[str, Any]) -> str:
        """Create a paused branch from the checkpoint with the edits applied (resume runs it)."""
        if not changes:
            raise ValueError("fork needs at least one edited field; use replay to continue unchanged")
        self.validate_changes(changes)
        async with self._admission:
            source = self.registry.get(run_id)
            snapshot = await self._branch_point(run_id, checkpoint_id)
            head = await self.graph.aupdate_state(snapshot.config, changes)
            new_id = str(uuid.uuid4())
            self.registry.put({"run_id": new_id, "thread_id": source["thread_id"], "status": "paused", "inputs": source["inputs"],
                               "relation": "fork", "parent_run_id": run_id, "source_checkpoint_id": checkpoint_id,
                               "fork_changes": changes, "head_config": head, "created_at": _now(), "updated_at": _now()})
        return new_id

    async def replay(self, run_id: str, checkpoint_id: str) -> str:
        """Continue from the checkpoint unchanged in a new branch, and start it."""
        async with self._admission:
            self._admit()
            source = self.registry.get(run_id)
            snapshot = await self._branch_point(run_id, checkpoint_id)
            new_id = str(uuid.uuid4())
            self.registry.put({"run_id": new_id, "thread_id": source["thread_id"], "status": "queued", "inputs": source["inputs"],
                               "relation": "replay", "parent_run_id": run_id, "source_checkpoint_id": checkpoint_id,
                               "head_config": dict(snapshot.config), "created_at": _now(), "updated_at": _now()})
            self._launch(new_id, None, dict(snapshot.config))
        return new_id

    async def _branch_point(self, run_id: str, checkpoint_id: str) -> Any:
        snapshot = await self._find(run_id, checkpoint_id)
        if not snapshot.next:
            raise ExecutorConflict("checkpoint is terminal (nothing left to execute); choose an earlier checkpoint")
        if list(snapshot.next) == ["__start__"]:
            raise ExecutorConflict("checkpoint precedes the first step; start a new run with different inputs")
        return snapshot

    async def history(self, run_id: str) -> list[dict[str, Any]]:
        chain = await self._chain(run_id)
        by_id = {_checkpoint_id(s.config): s for s in chain}
        result = []
        for i, snapshot in enumerate(chain):
            metadata = dict(snapshot.metadata or {})
            # The writes that produced this checkpoint are the results of the tasks pending on its parent.
            parent = by_id.get(_checkpoint_id(snapshot.parent_config))
            writes = {t.name: sorted(t.result) for t in (parent.tasks if parent else ())
                      if isinstance(getattr(t, "result", None), dict)} if metadata.get("source") == "loop" else {}
            result.append({
                "checkpoint_id": _checkpoint_id(snapshot.config),
                "parent_checkpoint_id": _checkpoint_id(snapshot.parent_config),
                "created_at": snapshot.created_at, "step": metadata.get("step"), "source": metadata.get("source"),
                "next": list(snapshot.next), "values": jsonable(dict(snapshot.values)),
                "interrupted": any(getattr(t, "interrupts", ()) for t in snapshot.tasks),
                "interrupts": [jsonable(getattr(x, "value", x)) for t in snapshot.tasks for x in getattr(t, "interrupts", ())],
                "writes": writes,   # {node: [state keys it wrote in the step that produced this checkpoint]}
            })
        return result

    async def aclose(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self.registry.close()


def _summary(inputs: Any) -> str:
    if isinstance(inputs, dict):
        for key in ("task", "question", "input", "query", "prompt"):
            if isinstance(inputs.get(key), str):
                return inputs[key][:300]
        return json.dumps(inputs, ensure_ascii=False)[:300]
    return str(inputs)[:300]
