"""Workflow execution engine: definition -> LangGraph StateGraph with SQLite checkpoints.

Every node execution produces a persisted span (plus child llm/tool spans). Checkpoints are
LangGraph's own; pause/resume reuse the same thread, while fork and replay copy a checkpoint's
state into a *new* thread so the source run's history is never modified.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import operator
import os
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .runtime_support import (
    DeterministicProvider, MissingCredentials, OpenAICompatibleProvider, ProviderError, TemplateError, ToolError,
    lookup, openai_tool_schema, render, run_tool,
)
from .schema import WorkflowDefinition, WorkflowNode
from .store import StudioStore, now

STUDIO_CAPABILITIES = {"trace": True, "execute": True, "checkpoint": True, "pause_resume": True,
                       "fork": True, "replay": True, "retry": True}
TERMINAL = {"completed", "failed", "cancelled"}


def merge(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


class FlowState(TypedDict, total=False):
    run_id: str
    input: dict[str, Any]
    nodes: Annotated[dict[str, Any], merge]
    visits: Annotated[dict[str, int], merge]
    path: Annotated[list[str], operator.add]
    last_node: str


class NodeFailed(RuntimeError):
    def __init__(self, node_id: str, exc: BaseException) -> None:
        super().__init__(f"{node_id}: {type(exc).__name__}: {exc}")
        self.node_id = node_id
        self.original = exc


@dataclass
class ProviderConfig:
    api_key: str | None
    base_url: str
    default_model: str

    @classmethod
    def from_env(cls, overrides: dict[str, Any] | None = None) -> "ProviderConfig":
        overrides = overrides or {}
        return cls(
            api_key=os.environ.get("OPENAI_API_KEY") or None,
            base_url=overrides.get("base_url") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
            default_model=overrides.get("default_model") or os.environ.get("STUDIO_DEFAULT_MODEL") or "gpt-4.1-mini",
        )


def _cfg(thread_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
    configurable = {"thread_id": thread_id, "checkpoint_ns": ""}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def _checkpoint_id(config: dict[str, Any] | None) -> str | None:
    return ((config or {}).get("configurable") or {}).get("checkpoint_id")


def _evaluate(op: str, left: str, right: str) -> bool:
    if op == "equals":
        return left.strip() == right.strip()
    if op == "not_equals":
        return left.strip() != right.strip()
    if op == "contains":
        return right.lower() in left.lower()
    if op == "not_contains":
        return right.lower() not in left.lower()
    if op == "is_true":
        return left.strip().lower() in {"true", "yes", "1"}
    try:
        a, b = float(left), float(right)
    except ValueError as exc:
        raise ValueError(f"{op} needs numeric operands, got {left!r} and {right!r}") from exc
    return a > b if op == "greater_than" else a < b


class StudioEngine:
    def __init__(self, store: StudioStore, checkpoint_path: Path, *, max_concurrent_runs: int = 2) -> None:
        self.store = store
        self.checkpoint_path = checkpoint_path
        self.saver: Any = None
        self._saver_cm: Any = None
        self._graphs: dict[str, Any] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._slots = asyncio.Semaphore(max_concurrent_runs)
        self.deterministic = DeterministicProvider()

    # ------------------------------------------------------------ lifecycle
    async def startup(self) -> list[str]:
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._saver_cm = AsyncSqliteSaver.from_conn_string(str(self.checkpoint_path))
        self.saver = await self._saver_cm.__aenter__()
        await self.saver.setup()
        return self.store.mark_interrupted()

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        if self._saver_cm is not None:
            await self._saver_cm.__aexit__(None, None, None)
            self._saver_cm = self.saver = None

    def provider_config(self) -> ProviderConfig:
        return ProviderConfig.from_env(self.store.get_settings().get("provider"))

    # ------------------------------------------------------------- compile
    def graph_for(self, definition: dict[str, Any]) -> Any:
        key = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
        if key not in self._graphs:
            self._graphs[key] = self._compile(WorkflowDefinition.model_validate(definition))
        return self._graphs[key]

    def _compile(self, defn: WorkflowDefinition) -> Any:
        builder = StateGraph(FlowState)
        for node in defn.nodes:
            builder.add_node(node.id, self._node_fn(defn, node))
        start = next(n for n in defn.nodes if n.type == "start")
        builder.add_edge(START, start.id)
        for node in defn.nodes:
            out = [e for e in defn.edges if e.source == node.id]
            if node.type == "output":
                builder.add_edge(node.id, END)
            elif node.type == "condition":
                targets = {e.branch: e.target for e in out}
                builder.add_conditional_edges(
                    node.id,
                    lambda state, nid=node.id, t=targets: t["true"] if state["nodes"][nid]["output"]["result"] else t["false"],
                    list(set(targets.values())),
                )
            else:
                builder.add_edge(node.id, out[0].target)
        return builder.compile(checkpointer=self.saver)

    def recursion_limit(self, definition: dict[str, Any]) -> int:
        defn = WorkflowDefinition.model_validate(definition)
        loops = max([n.config.max_true_visits for n in defn.nodes if n.type == "condition"] or [1])
        return len(defn.nodes) * (loops + 1) * 2 + 10

    # ------------------------------------------------------------ node fns
    def _node_fn(self, defn: WorkflowDefinition, node: WorkflowNode):
        async def run_node(state: FlowState) -> dict[str, Any]:
            run_id = state["run_id"]
            run = self.store.get_run(run_id) or {}
            if run.get("pause_requested") or node.id in (run.get("breakpoints") or []):
                reason = "breakpoint" if node.id in (run.get("breakpoints") or []) else "pause_requested"
                if run.get("pause_requested"):
                    self.store.update_run(run_id, pause_requested=0, status="pausing")
                    self.store.start_span(run_id, name=f"paused before {node.id}", kind="pause", node_id=node.id,
                                          parent_span_id=self._root_span(run_id), status="paused",
                                          attributes={"reason": reason, "before_node": node.id})
                    interrupt({"before_node": node.id, "reason": reason})
                elif not self._breakpoint_released(run_id, node.id):
                    self.store.start_span(run_id, name=f"breakpoint before {node.id}", kind="pause", node_id=node.id,
                                          parent_span_id=self._root_span(run_id), status="paused",
                                          attributes={"reason": reason, "before_node": node.id})
                    interrupt({"before_node": node.id, "reason": reason})
            span_id = self.store.start_span(run_id, name=node.label or node.id, kind=node.type, node_id=node.id,
                                            parent_span_id=self._root_span(run_id), trace_id=run_id,
                                            attributes={"node_type": node.type})
            try:
                result, span_input, attrs = await self._execute_node(defn, node, state, run, span_id)
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    self.store.end_span(span_id, status="cancelled", error={"type": "CancelledError", "message": "run cancelled"})
                    raise
                self.store.end_span(span_id, status="error", error={
                    "type": type(exc).__name__, "message": str(exc),
                    "traceback": "".join(traceback.format_exception(exc))[-4000:]})
                raise NodeFailed(node.id, exc) from exc
            self.store.end_span(span_id, status="ok", output=result, attributes=attrs)
            update: dict[str, Any] = {
                "nodes": {node.id: {"output": result, "span_id": span_id, "type": node.type}},
                "path": [node.id], "last_node": node.id,
            }
            if node.type == "condition":
                update["visits"] = {node.id: state.get("visits", {}).get(node.id, 0) + int(bool(result["result"]))}
            self._set_span_input(span_id, span_input)
            return update

        return run_node

    def _breakpoint_released(self, run_id: str, node_id: str) -> bool:
        released = (self.store.get_settings().get(f"released:{run_id}") or [])
        return node_id in released

    def _set_span_input(self, span_id: str, value: Any) -> None:
        with self.store._tx() as c:  # small internal helper; keeps span input in the same table
            c.execute("UPDATE spans SET input=? WHERE id=?", (json.dumps(value, ensure_ascii=False, default=str), span_id))

    def _root_span(self, run_id: str) -> str | None:
        rows = self.store._query("SELECT id FROM spans WHERE run_id=? AND kind='workflow' ORDER BY seq LIMIT 1", (run_id,))
        return rows[0]["id"] if rows else None

    def _scope(self, state: FlowState) -> dict[str, Any]:
        return {"input": state.get("input", {}), "nodes": state.get("nodes", {})}

    async def _execute_node(self, defn: WorkflowDefinition, node: WorkflowNode, state: FlowState, run: dict[str, Any],
                            span_id: str) -> tuple[Any, Any, dict[str, Any]]:
        cfg = node.config
        scope = self._scope(state)
        if node.type == "start":
            values = {f.name: str(state.get("input", {}).get(f.name, f.default)) for f in cfg.inputs}
            missing = [k for k, v in values.items() if not v.strip()]
            if missing:
                raise ValueError("missing required inputs: " + ", ".join(missing))
            return values, {"inputs": state.get("input", {})}, {}
        if node.type == "tool":
            args = {k: render(v, scope) for k, v in cfg.arguments.items()}
            child = self.store.start_span(run["id"], name=f"tool:{cfg.tool}", kind="tool", node_id=node.id,
                                          parent_span_id=span_id, trace_id=run["id"], input=args, attributes={"tool": cfg.tool})
            try:
                result = await asyncio.to_thread(run_tool, cfg.tool or "", args)
            except ToolError as exc:
                self.store.end_span(child, status="error", error={"type": "ToolError", "message": str(exc)})
                raise
            except asyncio.CancelledError:
                self.store.end_span(child, status="cancelled", error={"type": "CancelledError", "message": "run cancelled during the tool call"})
                raise
            self.store.end_span(child, status="ok", output=result)
            return result, {"tool": cfg.tool, "arguments": args}, {"tool": cfg.tool}
        if node.type == "condition":
            left, right = render(cfg.left, scope), render(cfg.right, scope)
            result = _evaluate(cfg.op, left, right)
            guard = False
            if result and state.get("visits", {}).get(node.id, 0) >= cfg.max_true_visits:
                result, guard = False, True
            return ({"result": result, "branch": "true" if result else "false", "loop_guard": guard},
                    {"left": left[:4000], "op": cfg.op, "right": right}, {"loop_guard": guard})
        if node.type == "output":
            text = render(cfg.template or "{{nodes." + (state.get("last_node") or "") + ".output}}", scope)
            return text, {"template": cfg.template}, {}
        if node.type == "agent":
            return await self._run_agent(defn, node, scope, run, span_id)
        raise ValueError(f"unsupported node type {node.type}")

    def resolve_provider(self, defn: WorkflowDefinition, node: WorkflowNode, run: dict[str, Any]) -> str:
        override = (run.get("capabilities") or {}).get("provider_override")
        if override in {"openai", "deterministic"}:
            return override
        return defn.default_provider if node.config.provider == "inherit" else node.config.provider

    async def _run_agent(self, defn: WorkflowDefinition, node: WorkflowNode, scope: dict[str, Any], run: dict[str, Any],
                         span_id: str) -> tuple[Any, Any, dict[str, Any]]:
        cfg = node.config
        provider_name = self.resolve_provider(defn, node, run)
        pcfg = self.provider_config()
        model = cfg.model or defn.default_model or pcfg.default_model
        messages: list[dict[str, Any]] = []
        if cfg.system_prompt.strip():
            messages.append({"role": "system", "content": render(cfg.system_prompt, scope)})
        messages.append({"role": "user", "content": render(cfg.prompt, scope)})
        initial = copy.deepcopy(messages)
        tools = [openai_tool_schema(t) for t in cfg.tools]
        usage_total: dict[str, int] = {}
        tool_log: list[dict[str, Any]] = []
        for round_index in range(cfg.max_tool_rounds + 1):
            llm_span = self.store.start_span(run["id"], name=f"llm:{provider_name}", kind="llm", node_id=node.id,
                                             parent_span_id=span_id, trace_id=run["id"],
                                             input={"messages": copy.deepcopy(messages), "tools": cfg.tools},
                                             attributes={"provider": provider_name, "model": model if provider_name == "openai" else "deterministic-script",
                                                         "round": round_index})
            try:
                if provider_name == "openai":
                    result = await OpenAICompatibleProvider(api_key=pcfg.api_key, base_url=pcfg.base_url, default_model=pcfg.default_model).chat(
                        messages, model=model, temperature=cfg.temperature, max_tokens=cfg.max_tokens,
                        tools=tools if round_index < cfg.max_tool_rounds and tools else None)
                else:
                    result = await self.deterministic.chat(messages, script=cfg.mock_response, state=scope,
                                                           tool_calls=cfg.mock_tool_calls if cfg.tools else [], round_index=round_index)
            except (ProviderError, TemplateError) as exc:
                self.store.end_span(llm_span, status="error", error={"type": type(exc).__name__, "message": str(exc)})
                raise
            except asyncio.CancelledError:
                self.store.end_span(llm_span, status="cancelled", error={"type": "CancelledError", "message": "run cancelled during the model call"})
                raise
            for k, v in (result.usage or {}).items():
                if isinstance(v, int):
                    usage_total[k] = usage_total.get(k, 0) + v
            self.store.end_span(llm_span, status="ok", output={"content": result.content, "tool_calls": result.tool_calls,
                                                                "finish_reason": result.raw_finish_reason},
                                attributes={"usage": result.usage, "latency_ms": result.latency_ms, "response_model": result.model})
            if not result.tool_calls:
                attrs = {"provider": provider_name, "model": model if provider_name == "openai" else "deterministic-script",
                         "usage": usage_total, "tool_calls": len(tool_log), "role": cfg.role}
                return result.content, {"messages": initial, "tools": cfg.tools, "provider": provider_name}, attrs
            messages.append({"role": "assistant", "content": result.content or None,
                             "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                                            for c in result.tool_calls]})
            for call in result.tool_calls:
                try:
                    args = json.loads(call["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {"_raw": call["arguments"]}
                tool_span = self.store.start_span(run["id"], name=f"tool:{call['name']}", kind="tool", node_id=node.id,
                                                  parent_span_id=span_id, trace_id=run["id"], input=args,
                                                  attributes={"tool": call["name"], "tool_call_id": call["id"]})
                if call["name"] not in cfg.tools:
                    payload: dict[str, Any] = {"error": f"tool {call['name']} is not enabled for this agent"}
                    self.store.end_span(tool_span, status="error", error={"type": "ToolNotAllowed", "message": payload["error"]})
                else:
                    try:
                        payload = await asyncio.to_thread(run_tool, call["name"], args)
                        self.store.end_span(tool_span, status="ok", output=payload)
                    except ToolError as exc:
                        payload = {"error": str(exc)}
                        self.store.end_span(tool_span, status="error", error={"type": "ToolError", "message": str(exc)})
                    except asyncio.CancelledError:
                        self.store.end_span(tool_span, status="cancelled", error={"type": "CancelledError", "message": "run cancelled during the tool call"})
                        raise
                tool_log.append({"name": call["name"], "arguments": args, "result": payload})
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(payload, ensure_ascii=False)})
        raise ProviderError(f"agent {node.id} exceeded max_tool_rounds={cfg.max_tool_rounds} without a final answer")

    # ------------------------------------------------------------- runs
    def _capabilities(self, defn: dict[str, Any], provider_override: str | None) -> dict[str, Any]:
        model = WorkflowDefinition.model_validate(defn)
        providers = sorted({self.resolve_provider(model, n, {"capabilities": {"provider_override": provider_override}})
                            for n in model.nodes if n.type == "agent"})
        mode = providers[0] if len(providers) == 1 else ("mixed" if providers else "no-llm")
        return {**STUDIO_CAPABILITIES, "provider_override": provider_override, "provider_mode": mode}

    async def start_run(self, workflow: dict[str, Any], inputs: dict[str, Any], *, breakpoints: list[str] | None = None,
                        provider_override: str | None = None, wait: bool = False) -> dict[str, Any]:
        definition = workflow["definition"]
        model = WorkflowDefinition.model_validate(definition)
        fields = next(n for n in model.nodes if n.type == "start").config.inputs
        unknown = sorted(set(inputs) - {f.name for f in fields})
        if unknown:
            raise ValueError(f"unknown inputs: {unknown}")
        # Defaults are resolved once here so every template sees the same input.* values.
        inputs = {f.name: str(inputs.get(f.name, f.default)) for f in fields}
        caps = self._capabilities(definition, provider_override)
        run = self.store.create_run(
            workflow_id=workflow["id"], workflow_version=workflow["version"], workflow_name=workflow["name"],
            origin="studio", thread_id="th_" + uuid.uuid4().hex, definition=definition, input=inputs,
            provider_mode=caps["provider_mode"], capabilities=caps, breakpoints=breakpoints or [],
        )
        await self._launch(run["id"], {"run_id": run["id"], "input": inputs, "nodes": {}, "visits": {}, "path": []},
                           _cfg(run["thread_id"]), wait=wait)
        return self.store.get_run(run["id"])  # type: ignore[return-value]

    async def _launch(self, run_id: str, input_value: Any, config: dict[str, Any], *, wait: bool) -> None:
        task = asyncio.create_task(self._execute(run_id, input_value, config))
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t, rid=run_id: self._tasks.pop(rid, None))
        if wait:
            await task

    async def _execute(self, run_id: str, input_value: Any, config: dict[str, Any]) -> None:
        async with self._slots:
            run = self.store.get_run(run_id)
            assert run is not None
            graph = self.graph_for(run["definition"])
            if self._root_span(run_id) is None:
                self.store.start_span(run_id, name=run.get("workflow_name") or "workflow", kind="workflow", trace_id=run_id,
                                      input=run.get("input"), attributes={"origin": "studio", "relation": run.get("relation"),
                                                                          "provider_mode": run.get("provider_mode")})
            self.store.update_run(run_id, status="running", error=None, started_at=run.get("started_at") or now())
            try:
                async for update in graph.astream(input_value, {**config, "recursion_limit": self.recursion_limit(run["definition"])},
                                                  stream_mode="updates", durability="sync"):
                    if not isinstance(update, dict) or "__interrupt__" in update:
                        continue
                    snapshot = await graph.aget_state(_cfg(run["thread_id"]))
                    cp = _checkpoint_id(snapshot.config)
                    for payload in update.values():
                        for info in ((payload or {}).get("nodes") or {}).values():
                            if info.get("span_id") and cp:
                                self.store.set_span_checkpoint(info["span_id"], cp)
                snapshot = await graph.aget_state(_cfg(run["thread_id"]))
                root = self._root_span(run_id)
                if any(getattr(t, "interrupts", ()) for t in snapshot.tasks):
                    self.store.update_run(run_id, status="paused")
                    return
                outputs = {nid: info["output"] for nid, info in snapshot.values.get("nodes", {}).items() if info.get("type") == "output"}
                self.store.update_run(run_id, status="completed", output=outputs, ended_at=now())
                if root:
                    self.store.end_span(root, status="ok", output=outputs, checkpoint_id=_checkpoint_id(snapshot.config))
            except asyncio.CancelledError:
                self.store.update_run(run_id, status="cancelled", ended_at=now(),
                                      error={"type": "Cancelled", "message": "run cancelled or service stopping; resume from the last checkpoint"})
                self._close_root(run_id, "cancelled")
                raise
            except NodeFailed as exc:
                original = exc.original
                kind = "MissingCredentials" if isinstance(original, MissingCredentials) else type(original).__name__
                self.store.update_run(run_id, status="failed", ended_at=now(), error={
                    "type": kind, "message": str(original), "node_id": exc.node_id,
                    "retry": "Resume re-executes the failed node from the checkpoint before it."})
                self._close_root(run_id, "error", {"type": kind, "message": str(original), "node_id": exc.node_id})
            except Exception as exc:  # recursion limit, graph errors
                self.store.update_run(run_id, status="failed", ended_at=now(), error={"type": type(exc).__name__, "message": str(exc)[:2000]})
                self._close_root(run_id, "error", {"type": type(exc).__name__, "message": str(exc)[:2000]})

    def _close_root(self, run_id: str, status: str, error: Any = None) -> None:
        root = self._root_span(run_id)
        if root:
            self.store.end_span(root, status=status, error=error)

    # ------------------------------------------------------------ control
    def request_pause(self, run_id: str) -> None:
        run = self._require(run_id)
        if run["status"] not in {"running", "queued"}:
            raise ValueError(f"only running runs can be paused (status={run['status']})")
        self.store.update_run(run_id, pause_requested=1)

    async def resume(self, run_id: str, *, wait: bool = False) -> dict[str, Any]:
        run = self._require(run_id)
        if run["origin"] != "studio":
            raise ValueError("external runs are trace-only and cannot be resumed")
        if run["status"] not in {"paused", "failed", "interrupted", "cancelled"}:
            raise ValueError(f"cannot resume a run with status {run['status']}")
        if run_id in self._tasks:
            raise ValueError("run is already executing")
        graph = self.graph_for(run["definition"])
        snapshot = await graph.aget_state(_cfg(run["thread_id"]))
        if not snapshot.next:
            raise ValueError("run has no pending step to resume")
        interrupts = [i for t in snapshot.tasks for i in getattr(t, "interrupts", ())]
        if interrupts:
            before = (interrupts[0].value or {}).get("before_node")
            if before and before in (run.get("breakpoints") or []):
                released = self.store.get_settings().get(f"released:{run_id}") or []
                self.store.set_setting(f"released:{run_id}", sorted(set(released) | {before}))
        with self.store._tx() as c:
            c.execute("UPDATE spans SET status='resumed', end_time=? WHERE run_id=? AND status='paused'", (now(), run_id))
        self.store.update_run(run_id, status="queued", error=None, ended_at=None)
        await self._launch(run_id, Command(resume={"resumed_at": now()}) if interrupts else None, _cfg(run["thread_id"]), wait=wait)
        return self.store.get_run(run_id)  # type: ignore[return-value]

    async def cancel(self, run_id: str) -> None:
        task = self._tasks.get(run_id)
        if task is None:
            raise ValueError("run is not executing")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        run = self._require(run_id)
        if run["origin"] != "studio":
            return []
        graph = self.graph_for(run["definition"])
        result = []
        async for snap in graph.aget_state_history(_cfg(run["thread_id"])):
            values = dict(snap.values)
            result.append({
                "checkpoint_id": _checkpoint_id(snap.config),
                "parent_checkpoint_id": _checkpoint_id(snap.parent_config),
                "created_at": snap.created_at, "step": (snap.metadata or {}).get("step"),
                "source": (snap.metadata or {}).get("source"),
                "after_node": values.get("last_node"), "next": list(snap.next),
                "interrupted": any(getattr(t, "interrupts", ()) for t in snap.tasks),
                "forkable": bool(snap.next) and bool(values.get("last_node")),
                "state": {"input": values.get("input", {}), "nodes": {k: {"output": v.get("output"), "type": v.get("type")}
                                                                      for k, v in values.get("nodes", {}).items()},
                          "path": values.get("path", []), "visits": values.get("visits", {})},
            })
        return result

    async def fork(self, run_id: str, checkpoint_id: str, changes: dict[str, Any], *, relation: str = "fork",
                   wait: bool = False) -> dict[str, Any]:
        source = self._require(run_id)
        if source["origin"] != "studio":
            raise ValueError("external runs are trace-only; fork/replay is unavailable")
        if relation == "replay" and changes:
            raise ValueError("replay does not accept changes; use fork")
        graph = self.graph_for(source["definition"])
        snap = await graph.aget_state(_cfg(source["thread_id"], checkpoint_id))
        if _checkpoint_id(snap.config) != checkpoint_id or not snap.values:
            raise KeyError(f"checkpoint {checkpoint_id} not found in run {run_id}")
        if not snap.next:
            raise ValueError("checkpoint is terminal (nothing left to execute); choose an earlier checkpoint")
        if not snap.values.get("last_node"):
            raise ValueError("checkpoint precedes the first node; start a new run with different inputs instead")
        values = copy.deepcopy(dict(snap.values))
        applied = self._apply_changes(values, changes, WorkflowDefinition.model_validate(source["definition"]))
        new_run = self.store.create_run(
            workflow_id=source["workflow_id"], workflow_version=source["workflow_version"], workflow_name=source["workflow_name"],
            origin="studio", thread_id="th_" + uuid.uuid4().hex, definition=source["definition"], input=values.get("input"),
            parent_run_id=run_id, source_checkpoint_id=checkpoint_id, fork_changes=applied, relation=relation,
            provider_mode=source["provider_mode"], capabilities=source["capabilities"], breakpoints=[],
            inherited={"path": list(values.get("path", [])), "nodes": sorted(values.get("nodes", {}))},
        )
        values["run_id"] = new_run["id"]
        new_cfg = _cfg(new_run["thread_id"])
        # Seed the new thread: reducers start empty, so merged fields receive the full copied values.
        await graph.aupdate_state(new_cfg, values, as_node=values["last_node"])
        await self._launch(new_run["id"], None, new_cfg, wait=wait)
        return self.store.get_run(new_run["id"])  # type: ignore[return-value]

    @staticmethod
    def _apply_changes(values: dict[str, Any], changes: dict[str, Any], defn: WorkflowDefinition) -> dict[str, Any]:
        applied: dict[str, Any] = {}
        for path, new_value in changes.items():
            parts = path.split(".")
            if len(parts) == 2 and parts[0] == "input":
                if parts[1] not in values.get("input", {}):
                    raise ValueError(f"unknown input field {parts[1]}")
                values["input"][parts[1]] = str(new_value)
            elif len(parts) == 3 and parts[0] == "nodes" and parts[2] == "output":
                node = values.get("nodes", {}).get(parts[1])
                if node is None:
                    raise ValueError(f"node {parts[1]} has not executed at this checkpoint")
                if node.get("type") == "condition":
                    raise ValueError("condition results are recomputed; edit the value they test instead")
                old = node["output"]
                node["output"] = new_value if not isinstance(old, str) else str(new_value)
            else:
                raise ValueError(f"field {path!r} is not editable; allowed: input.<field>, nodes.<id>.output")
            applied[path] = new_value
        return applied

    def _require(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"run {run_id} not found")
        return run

    def is_active(self, run_id: str) -> bool:
        return run_id in self._tasks

    async def wait(self, run_id: str) -> None:
        task = self._tasks.get(run_id)
        if task:
            await asyncio.gather(task, return_exceptions=True)
