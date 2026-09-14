from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from .config import Settings
from .graph import GraphDependencies, build_graph
from .models import ModelGateway, OpenAIModelGateway
from .runtime_ownership import RuntimeOwnership
from .observability import Observability
from .state import initial_state
from .storage import AppStore, ArtifactStore
from .telemetry_export import redact_export_text
from .websurfer import WebSurferManager


def checkpoint_id(config: dict[str, Any] | None) -> str | None:
    return ((config or {}).get("configurable") or {}).get("checkpoint_id")


class AgentRuntime:
    def __init__(self, settings: Settings, models: ModelGateway | None = None, *, enable_tracing: bool = True) -> None:
        self.settings = settings
        settings.ensure_directories()
        self.store = AppStore(settings.metadata_db)
        self.artifacts = ArtifactStore(settings.artifact_root)
        self.observability = Observability(settings)
        if enable_tracing:
            self.observability.initialize()
        self.models = models or OpenAIModelGateway(
            settings,
            tracer_provider=self.observability.provider,
            trace_config=self.observability.trace_config,
        )
        self.web = WebSurferManager(settings, self.artifacts)
        self._saver_context: Any = None
        self.saver: Any = None
        self.graph: Any = None
        self._active_lock = asyncio.Lock()
        self._active_run_id: str | None = None
        self._admitting = False
        self._closing = False
        self._startup_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._admission_idle = asyncio.Event()
        self._admission_idle.set()
        self._ownership = RuntimeOwnership([settings.metadata_db, settings.checkpoint_db])
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    @contextlib.contextmanager
    def _admission(self):
        # Reserve before the first await, including read/update checkpoint preparation.
        if self._closing:
            raise RuntimeError("runtime is shutting down")
        if self._admitting or self._active_run_id is not None:
            raise RuntimeError("another run is active; MAX_ACTIVE_RUNS=1")
        self._admitting = True
        self._admission_idle.clear()
        try:
            yield
        finally:
            self._admitting = False
            self._admission_idle.set()

    async def startup(self) -> None:
        async with self._startup_lock:
            if self._closing:
                raise RuntimeError("runtime is shutting down")
            if self.graph is not None:
                return
            self._ownership.acquire()
            try:
                self._saver_context = AsyncSqliteSaver.from_conn_string(str(self.settings.checkpoint_db))
                self.saver = await self._saver_context.__aenter__()
                await self.saver.setup()
                deps = GraphDependencies(
                    settings=self.settings, store=self.store, artifacts=self.artifacts,
                    models=self.models, web=self.web, observability=self.observability,
                )
                self.graph = build_graph(deps, self.saver)
                interrupted = self.store.mark_interrupted_runs()
                if interrupted:
                    self.store.emit("system", "startup_recovery", {"interrupted_runs": interrupted})
            except BaseException:
                try:
                    await self._close_saver()
                finally:
                    self._ownership.release()
                raise

    async def _close_saver(self) -> None:
        try:
            if self._saver_context is not None:
                await self._saver_context.__aexit__(None, None, None)
        finally:
            self.graph = self.saver = self._saver_context = None

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            async with self._startup_lock:
                self._closing = True
            # Let an already admitted checkpoint update finish before releasing ownership.
            await self._admission_idle.wait()
            for task in list(self._tasks.values()):
                if not task.done():
                    task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            async with self._active_lock:
                try:
                    # A cancelled task may never have entered _execute or its finally block.
                    for run_id in self._tasks:
                        record = self.store.get_run(run_id)
                        if record and record["status"] in {"queued", "running", "pausing"}:
                            self.store.update_run(run_id, status="interrupted")
                    self._tasks.clear()
                    self._active_run_id = None
                    await self.web.close_all()
                finally:
                    try:
                        self.observability.shutdown()
                    finally:
                        try:
                            await self._close_saver()
                        finally:
                            self._ownership.release()
                            self._closing = False

    async def create_run(
        self, task: str, *, allowed_files: list[str] | None = None, dataset_case_id: str | None = None,
        start_background: bool = False,
    ) -> str:
        if not task.strip():
            raise ValueError("task must not be empty")
        with self._admission():
            await self.startup()
            run_id, thread_id, branch_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
            config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
            state = initial_state(
                task=task.strip(), thread_id=thread_id, run_id=run_id, branch_id=branch_id,
                allowed_files=allowed_files, dataset_case_id=dataset_case_id,
            )
            self.store.create_run({
                "run_id": run_id, "thread_id": thread_id, "branch_id": branch_id,
                "task": task.strip(), "status": "queued", "head_config": config,
                "dataset_case_id": dataset_case_id,
            })
            self._active_run_id = run_id
            if start_background:
                self._tasks[run_id] = asyncio.create_task(self._execute(run_id, state, config))
        if not start_background:
            await self._execute(run_id, state, config)
        return run_id

    async def _execute(self, run_id: str, input_value: Any, config: dict[str, Any]) -> dict[str, Any]:
        async with self._active_lock:
            record = self._require_run(run_id)
            self.store.update_run(run_id, status="running", error=None)
            self.store.emit(run_id, "run_started", {"thread_id": record["thread_id"], "branch_id": record["branch_id"]})
            previous_checkpoint = checkpoint_id(config)
            live_config = {
                "configurable": {
                    "thread_id": config["configurable"]["thread_id"],
                    "checkpoint_ns": config["configurable"].get("checkpoint_ns", ""),
                }
            }
            try:
                event_types = {item["event"] for item in self.store.events_after(run_id)}
                replay_of = record.get("parent_run_id") if "replay_created" in event_types else None
                fork_of = record.get("parent_run_id") if "fork_created" in event_types else None
                attrs = {
                    "run_id": run_id, "application_run_id": run_id, "thread_id": record["thread_id"],
                    "session.id": record["thread_id"], "branch_id": record["branch_id"],
                    "agent_name": "m1_local", "status": "running", "checkpoint_id": previous_checkpoint,
                    "parent_run_id": record.get("parent_run_id"),
                    "source_checkpoint_id": record.get("source_checkpoint_id"),
                    "dataset_case_id": record.get("dataset_case_id"),
                    "replay_of": replay_of, "fork_of": fork_of,
                }
                with self.observability.trace_context(attrs):
                    with self.observability.span(
                        "m1.run", attrs, kind="AGENT",
                        input_summary={
                            "task_preview": record["task"][:500],
                            "resume": input_value is None or isinstance(input_value, Command),
                            "source_checkpoint_id": record.get("source_checkpoint_id"),
                        },
                        agent_name="m1_local",
                    ) as run_span:
                        async with asyncio.timeout(self.settings.run_timeout_seconds):
                            run_config = {**config, "recursion_limit": self.settings.max_graph_steps * 3 + 10}
                            async for update in self.graph.astream(
                                input_value, run_config,
                                stream_mode="updates", durability="sync",
                            ):
                                # Only one graph execution is active, so the newest descendant in this
                                # thread/namespace is the just-committed branch head. Persist its full config.
                                snapshot = await self.graph.aget_state(live_config)
                                head = dict(snapshot.config)
                                output_checkpoint = checkpoint_id(head)
                                self.store.update_run(run_id, head_config=head)
                                node_name = next(iter(update), "unknown") if isinstance(update, dict) else "unknown"
                                if node_name == "__interrupt__":
                                    # An interrupt saves pending task state, not a new node execution.
                                    continue
                                execution_id = str(update.get(node_name, {}).get("execution_id", ""))
                                previous_checkpoint = checkpoint_id(snapshot.parent_config)
                                trace_id, _ = self.observability.current_ids()
                                if execution_id:
                                    self.store.link_execution(
                                        execution_id=execution_id, run_id=run_id, node_name=node_name,
                                        input_checkpoint_id=previous_checkpoint, output_checkpoint_id=output_checkpoint,
                                        trace_id=trace_id, status="committed",
                                    )
                                commit_attrs = {
                                    "run_id": run_id, "application_run_id": run_id,
                                    "thread_id": record["thread_id"], "branch_id": record["branch_id"],
                                    "execution_id": execution_id, "checkpoint_id": output_checkpoint,
                                    "input_checkpoint_id": previous_checkpoint,
                                    "output_checkpoint_id": output_checkpoint,
                                    "langgraph_node": node_name, "graph.node.name": node_name,
                                    "agent_name": node_name,
                                    "step_number": int(snapshot.values.get("step_count", 0)),
                                    "status": "committed", "dataset_case_id": record.get("dataset_case_id"),
                                    "replay_of": replay_of, "fork_of": fork_of,
                                }
                                with self.observability.trace_context(commit_attrs):
                                    with self.observability.span(
                                        "m1.checkpoint.commit", commit_attrs, kind="CHAIN",
                                        input_summary={"input_checkpoint_id": previous_checkpoint, "langgraph_node": node_name},
                                        agent_name="langgraph_checkpointer",
                                    ) as commit_span:
                                        self.observability.set_output(commit_span, {
                                            "checkpoint_id": output_checkpoint, "status": "committed",
                                        }, status="committed")
                                self.store.emit(run_id, "checkpoint_saved", {
                                    "node": node_name, "execution_id": execution_id,
                                    "input_checkpoint_id": previous_checkpoint,
                                    "output_checkpoint_id": output_checkpoint,
                                })
                                previous_checkpoint = output_checkpoint
                        snapshot = await self.graph.aget_state(live_config)
                        head = dict(snapshot.config)
                        self.store.update_run(run_id, head_config=head)
                        paused = any(getattr(task, "interrupts", ()) for task in snapshot.tasks)
                        if paused:
                            status = "paused"
                            self.store.emit(run_id, "paused", {"checkpoint_id": checkpoint_id(head), "next": list(snapshot.next)})
                        else:
                            status = str(snapshot.values.get("status", "completed"))
                            self.store.emit(run_id, "run_finished", {"status": status, "checkpoint_id": checkpoint_id(head)})
                        self.store.update_run(run_id, status=status)
                        run_span.set_attribute("checkpoint_id", checkpoint_id(head) or "")
                        self.observability.set_output(run_span, {
                            "status": status, "checkpoint_id": checkpoint_id(head),
                            "answer_preview": str(snapshot.values.get("final_answer", ""))[:500],
                            "artifact_count": len(snapshot.values.get("artifacts", [])),
                            "citation_count": len(snapshot.values.get("citations", [])),
                        }, status=status)
                        if status != "paused":
                            await self.web.close(run_id, record["branch_id"])
                        return dict(snapshot.values)
            except asyncio.CancelledError:
                self.store.update_run(run_id, status="interrupted", error="execution cancelled; external action commit may be uncertain")
                self.store.emit(run_id, "error", {"type": "CancelledError", "external_commit_uncertain": True})
                await self.web.close(run_id, record["branch_id"])
                raise
            except Exception as exc:
                message = redact_export_text(
                    f"{type(exc).__name__}: {exc}", (self.settings.openai_api_key.get_secret_value(),)
                )[:1000]
                self.store.update_run(run_id, status="failed", error=message)
                self.store.emit(run_id, "error", {"error": message, "external_commit_uncertain": True})
                await self.web.close(run_id, record["branch_id"])
                return {"status": "failed", "errors": [{"id": str(uuid.uuid4()), "message": message}]}
            finally:
                # All span contexts, including the root, have ended before flushing.
                self.observability.flush()
                self._tasks.pop(run_id, None)
                if self._active_run_id == run_id:
                    self._active_run_id = None

    def request_pause(self, run_id: str) -> None:
        if self.graph is None or self._closing:
            raise RuntimeError("runtime must own the databases before requesting a pause")
        record = self._require_run(run_id)
        if record["status"] != "running":
            raise ValueError("only a running task can request a boundary pause")
        self.store.request_pause(run_id)

    async def resume(self, run_id: str, *, background: bool = False) -> None:
        with self._admission():
            record = self._require_run(run_id)
            if record["status"] not in {"paused", "failed", "interrupted"}:
                raise ValueError("only paused, failed, or interrupted runs can be resumed")
            config = self._require_head(record)
            snapshot = await self.graph.aget_state(config)
            has_interrupt = any(getattr(task, "interrupts", ()) for task in snapshot.tasks)
            input_value: Any = Command(resume=True) if has_interrupt else None
            self.store.update_run(run_id, status="queued")
            self._active_run_id = run_id
            if background:
                self._tasks[run_id] = asyncio.create_task(self._execute(run_id, input_value, config))
        if not background:
            await self._execute(run_id, input_value, config)

    async def history(self, run_id: str) -> list[dict[str, Any]]:
        record = self._require_run(run_id)
        result = []
        cursor: dict[str, Any] | None = self._require_head(record)
        seen: set[str] = set()
        while cursor is not None:
            snapshot = await self.graph.aget_state(cursor)
            current_id = checkpoint_id(dict(snapshot.config))
            if not current_id or current_id in seen:
                break
            seen.add(current_id)
            result.append({
                "config": dict(snapshot.config), "checkpoint_id": checkpoint_id(dict(snapshot.config)),
                "next": list(snapshot.next), "created_at": snapshot.created_at,
                "metadata": dict(snapshot.metadata), "state": dict(snapshot.values),
                "interrupts": [str(value) for task in snapshot.tasks for value in getattr(task, "interrupts", ())],
            })
            cursor = dict(snapshot.parent_config) if snapshot.parent_config else None
        return result

    async def replay(self, run_id: str, source_checkpoint_id: str, *, background: bool = False) -> str:
        with self._admission():
            source_record = self._require_run(run_id)
            source_config, source_snapshot = await self._find_checkpoint(run_id, source_checkpoint_id)
            if not source_snapshot.values.get("task"):
                raise ValueError("the selected checkpoint is not initialized")
            if not source_snapshot.next:
                raise ValueError("the selected checkpoint is terminal; choose an earlier checkpoint")
            new_run, new_branch = str(uuid.uuid4()), str(uuid.uuid4())
            update = {
                "application_run_id": new_run, "branch_id": new_branch, "execution_id": "",
                "parent_run_id": run_id, "source_checkpoint_id": source_checkpoint_id,
                "browser_resume": {**source_snapshot.values.get("browser_resume", {}), "browser_restore_mode": "reconstruct"},
            }
            head = await self.graph.aupdate_state(source_config, update)
            self.store.create_run({
                "run_id": new_run, "thread_id": source_record["thread_id"], "branch_id": new_branch,
                "task": source_snapshot.values["task"], "status": "queued", "head_config": head,
                "parent_run_id": run_id, "source_checkpoint_id": source_checkpoint_id,
                "dataset_case_id": source_snapshot.values.get("dataset_case_id"),
            })
            self._active_run_id = new_run
            self.store.emit(new_run, "replay_created", {"parent_run_id": run_id, "source_checkpoint_id": source_checkpoint_id})
        if background:
            self._tasks[new_run] = asyncio.create_task(self._execute(new_run, None, head))
        else:
            await self._execute(new_run, None, head)
        return new_run

    async def fork(self, run_id: str, source_checkpoint_id: str, changes: dict[str, Any]) -> str:
        with self._admission():
            self.validate_fork_changes(changes)
            source_record = self._require_run(run_id)
            source_config, snapshot = await self._find_checkpoint(run_id, source_checkpoint_id)
            if not snapshot.values.get("task"):
                raise ValueError("the selected checkpoint is not initialized")
            if not snapshot.next:
                raise ValueError("the selected checkpoint is terminal; choose an earlier checkpoint")
            new_run, new_branch = str(uuid.uuid4()), str(uuid.uuid4())
            values = {
                **changes, "application_run_id": new_run, "branch_id": new_branch, "execution_id": "",
                "parent_run_id": run_id, "source_checkpoint_id": source_checkpoint_id,
                "browser_resume": {**snapshot.values.get("browser_resume", {}), "browser_restore_mode": "reconstruct"},
            }
            head = await self.graph.aupdate_state(source_config, values)
            self.store.create_run({
                "run_id": new_run, "thread_id": source_record["thread_id"], "branch_id": new_branch,
                "task": snapshot.values["task"], "status": "paused", "head_config": head,
                "parent_run_id": run_id, "source_checkpoint_id": source_checkpoint_id,
                "dataset_case_id": snapshot.values.get("dataset_case_id"),
            })
            self.store.emit(new_run, "fork_created", {"parent_run_id": run_id, "source_checkpoint_id": source_checkpoint_id, "changes": changes})
            return new_run

    @staticmethod
    def validate_fork_changes(changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {"plan", "worker_instruction"}
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"fork fields are not allowed: {sorted(invalid)}")
        if "plan" in changes and (
            not isinstance(changes["plan"], list) or len(changes["plan"]) > 100
            or any(not isinstance(item, str) or len(item) > 20_000 for item in changes["plan"])
        ):
            raise ValueError("plan must be a list of at most 100 strings, each at most 20000 characters")
        if "worker_instruction" in changes and (
            not isinstance(changes["worker_instruction"], str) or len(changes["worker_instruction"]) > 20_000
        ):
            raise ValueError("worker_instruction must be a string of at most 20000 characters")
        return changes

    async def current_state(self, run_id: str) -> dict[str, Any]:
        record = self._require_run(run_id)
        snapshot = await self.graph.aget_state(self._require_head(record))
        return {
            "config": dict(snapshot.config), "checkpoint_id": checkpoint_id(dict(snapshot.config)),
            "next": list(snapshot.next), "state": dict(snapshot.values), "status": record["status"],
        }

    async def _find_checkpoint(self, run_id: str, wanted: str) -> tuple[dict[str, Any], Any]:
        record = self._require_run(run_id)
        cursor: dict[str, Any] | None = self._require_head(record)
        seen: set[str] = set()
        while cursor is not None:
            snapshot = await self.graph.aget_state(cursor)
            config = dict(snapshot.config)
            current_id = checkpoint_id(config)
            if current_id == wanted:
                return config, snapshot
            if not current_id or current_id in seen:
                break
            seen.add(current_id)
            cursor = dict(snapshot.parent_config) if snapshot.parent_config else None
        raise KeyError(f"checkpoint not found: {wanted}")

    def _require_run(self, run_id: str) -> dict[str, Any]:
        record = self.store.get_run(run_id)
        if record is None:
            raise KeyError(f"run not found: {run_id}")
        return record

    @staticmethod
    def _require_head(record: dict[str, Any]) -> dict[str, Any]:
        if not record.get("head_config"):
            raise RuntimeError("run has no persisted head config")
        return record["head_config"]

    async def sse(self, run_id: str, after: int = 0) -> AsyncIterator[str]:
        self._require_run(run_id)
        cursor = after
        while True:
            events = self.store.events_after(run_id, cursor)
            for event in events:
                cursor = int(event["id"])
                payload = {**event["data"], "_created_at": event["created_at"]}
                yield f"id: {cursor}\nevent: {event['event']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            record = self._require_run(run_id)
            if record["status"] in {"completed", "partial", "failed", "paused", "interrupted"} and not events:
                break
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)
