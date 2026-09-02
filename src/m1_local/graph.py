from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .config import Settings
from .models import ModelGateway
from .observability import Observability
from .security import validate_allowed_file
from .state import AgentState
from .storage import AppStore, ArtifactStore
from .websurfer import WebSurferManager


class GraphDependencies:
    def __init__(
        self, *, settings: Settings, store: AppStore, artifacts: ArtifactStore,
        models: ModelGateway, web: WebSurferManager, observability: Observability,
    ) -> None:
        self.settings = settings
        self.store = store
        self.artifacts = artifacts
        self.models = models
        self.web = web
        self.observability = observability

    def node_context(self, state: AgentState, name: str):
        execution_id = str(uuid.uuid4())
        run_id = state["application_run_id"]
        self.store.emit(run_id, "node_started", {"node": name, "execution_id": execution_id, "step": state.get("step_count", 0)})
        attrs = {
            "run_id": run_id, "application_run_id": run_id,
            "thread_id": state.get("thread_id"), "branch_id": state.get("branch_id"),
            "execution_id": execution_id, "langgraph_node": name, "node_name": name,
            "graph.node.name": name, "agent_name": name,
            "step_number": state.get("step_count", 0), "status": state.get("status", "running"),
            "source_checkpoint_id": state.get("source_checkpoint_id"),
            "dataset_case_id": state.get("dataset_case_id"),
        }
        kind = "TOOL" if name in {"web_surfer", "file_reader", "coder"} else "CHAIN"

        @contextmanager
        def enter() -> Iterator[Any]:
            with self.observability.trace_context(attrs):
                with self.observability.span(
                    f"m1.node.{name}", attrs, kind=kind,
                    input_summary=self.state_summary(state),
                    agent_name=name,
                    tool_name=name if kind == "TOOL" else None,
                ) as span:
                    yield span

        return execution_id, enter()

    @staticmethod
    def state_summary(state: AgentState) -> dict[str, Any]:
        return {
            "task_preview": str(state.get("task", ""))[:500],
            "status": state.get("status"),
            "next_agent": state.get("next_agent"),
            "step_number": state.get("step_count", 0),
            "plan_steps": len(state.get("plan", [])),
            "progress_items": len(state.get("progress_ledger", [])),
            "artifact_count": len(state.get("artifacts", [])),
            "citation_count": len(state.get("citations", [])),
            "error_count": len(state.get("errors", [])),
        }

    def model_context(self, operation: str, input_summary: dict[str, Any]):
        return self.observability.span(
            f"m1.model.{operation}",
            {"llm.model_name": self.settings.orchestrator_model, "agent_name": operation},
            kind="LLM", input_summary=input_summary, agent_name=operation,
        )


def build_graph(deps: GraphDependencies, checkpointer: Any) -> Any:
    async def planner(state: AgentState) -> dict[str, Any]:
        execution_id, span_context = deps.node_context(state, "planner")
        with span_context as node_span:
            is_replan = bool(state.get("plan"))
            with deps.model_context("planner", {
                "task_preview": state["task"][:500],
                "allowed_file_count": len(state.get("allowed_files", [])),
                "is_replan": is_replan,
            }) as model_span:
                decision = await deps.models.plan(dict(state))
                deps.observability.set_output(model_span, {
                    "plan_steps": len(decision.plan), "facts": len(decision.facts),
                    "unknowns": len(decision.unknowns),
                }, status="completed")
            update = {
                "plan": decision.plan,
                "task_ledger": {**state.get("task_ledger", {}), "facts": decision.facts, "unknowns": decision.unknowns},
                "status": "running",
                "replan_count": state.get("replan_count", 0) + (1 if is_replan else 0),
                "model_call_count": deps.models.calls,
                "execution_id": execution_id,
            }
            deps.observability.set_output(node_span, {
                "status": update["status"], "plan_steps": len(decision.plan),
                "replan_count": update["replan_count"],
            }, status=update["status"])
        deps.store.emit(state["application_run_id"], "node_finished", {"node": "planner", "execution_id": execution_id})
        return update

    async def orchestrator(state: AgentState) -> dict[str, Any]:
        execution_id, span_context = deps.node_context(state, "orchestrator")
        step = state.get("step_count", 0) + 1
        with span_context as node_span:
            if step > deps.settings.max_graph_steps:
                decision = None
                update = {"status": "failed", "next_agent": "finalizer", "worker_instruction": "graph step budget exhausted"}
            elif deps.models.calls >= deps.settings.max_model_calls:
                decision = None
                update = {"status": "failed", "next_agent": "finalizer", "worker_instruction": "model call budget exhausted"}
            else:
                with deps.model_context("orchestrator", {
                    "task_preview": state["task"][:500],
                    "step_number": step,
                    "plan_steps": len(state.get("plan", [])),
                    "progress_items": len(state.get("progress_ledger", [])),
                    "stall_count": state.get("stall_count", 0),
                }) as model_span:
                    decision = await deps.models.route(dict(state))
                    deps.observability.set_output(model_span, {
                        "complete": decision.complete, "made_progress": decision.made_progress,
                        "loop_detected": decision.loop_detected, "next_agent": decision.next_agent,
                    }, status="completed")
                stall = 0 if decision.made_progress else state.get("stall_count", 0) + 1
                if (decision.loop_detected or stall >= 2) and state.get("replan_count", 0) < deps.settings.max_replans:
                    update = {"status": "replanning", "next_agent": "planner", "worker_instruction": decision.reason, "stall_count": stall}
                else:
                    next_agent = "finalizer" if decision.complete else decision.next_agent
                    update = {
                        "next_agent": next_agent, "worker_instruction": decision.instruction,
                        "stall_count": stall, "status": "running",
                    }
            update.update({"step_count": step, "model_call_count": deps.models.calls, "execution_id": execution_id})
            deps.observability.set_output(node_span, {
                "status": update["status"], "next_agent": update["next_agent"],
                "step_number": step, "stall_count": update.get("stall_count", state.get("stall_count", 0)),
            }, status=update["status"])
        deps.store.emit(state["application_run_id"], "node_finished", {"node": "orchestrator", "execution_id": execution_id, "next": update["next_agent"]})
        return update

    async def pause_gate(state: AgentState) -> dict[str, Any]:
        execution_id, span_context = deps.node_context(state, "pause_gate")
        run_id = state["application_run_id"]
        with span_context as node_span:
            if deps.store.consume_pause_request(run_id):
                deps.store.emit(run_id, "paused", {"boundary_before": state.get("next_agent")})
                deps.observability.set_output(node_span, {
                    "status": "paused", "boundary_before": state.get("next_agent"),
                }, status="paused")
                interrupt({"reason": "user_requested", "boundary_before": state.get("next_agent")})
                deps.store.emit(run_id, "resumed", {"boundary_before": state.get("next_agent")})
            deps.observability.set_output(node_span, {
                "status": "completed", "next_agent": state.get("next_agent"),
            }, status="completed")
        deps.store.emit(run_id, "node_finished", {"node": "pause_gate", "execution_id": execution_id})
        return {"execution_id": execution_id}

    async def web_surfer(state: AgentState) -> dict[str, Any]:
        execution_id, span_context = deps.node_context(state, "web_surfer")
        with span_context as node_span:
            session = deps.web.session(
                state["application_run_id"], state["branch_id"], state.get("browser_resume")
            )
            observation = await session.act(state["worker_instruction"])
            progress = {
                "id": execution_id, "agent": "web_surfer", "summary": observation.summary,
                "citations": observation.citations, "made_progress": True,
            }
            update = {
                "progress_ledger": [progress], "messages": [{"id": execution_id, "role": "assistant", "name": "web_surfer", "content": observation.summary}],
                "artifacts": observation.artifacts, "browser_resume": observation.browser_resume,
                "citations": sorted(set(state.get("citations", []) + observation.citations)),
                "execution_id": execution_id,
            }
            parts = urlsplit(observation.url)
            safe_url = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme and parts.netloc else ""
            deps.observability.set_output(node_span, {
                "status": "completed", "url": safe_url,
                "summary_preview": observation.summary[:500],
                "citation_count": len(observation.citations), "artifact_count": len(observation.artifacts),
            }, status="completed")
        deps.store.emit(state["application_run_id"], "node_finished", {"node": "web_surfer", "execution_id": execution_id})
        return update

    async def file_reader(state: AgentState) -> dict[str, Any]:
        execution_id, span_context = deps.node_context(state, "file_reader")
        with span_context as node_span:
            allowed = state.get("allowed_files", [])
            if not allowed:
                raise ValueError("file_reader requires an explicitly allowed file")
            roots = [Path(item) for item in deps.settings.allowed_file_dirs]
            chunks = []
            for item in allowed:
                path = validate_allowed_file(item, roots, deps.settings.max_file_bytes)
                chunks.append(f"## {path.name}\n{path.read_text(encoding='utf-8-sig')}")
            content = "\n\n".join(chunks)
            progress = {"id": execution_id, "agent": "file_reader", "summary": content[:10000], "made_progress": True}
            update = {
                "progress_ledger": [progress],
                "messages": [{"id": execution_id, "role": "assistant", "name": "file_reader", "content": content[:10000]}],
                "execution_id": execution_id,
            }
            deps.observability.set_output(node_span, {
                "status": "completed", "file_count": len(allowed), "characters_read": len(content),
            }, status="completed")
        deps.store.emit(state["application_run_id"], "node_finished", {"node": "file_reader", "execution_id": execution_id})
        return update

    async def coder(state: AgentState) -> dict[str, Any]:
        execution_id, span_context = deps.node_context(state, "coder")
        with span_context as node_span:
            prompt = (
                "Generate or analyze code for the task below. Do not claim it was executed. Do not include installation or execution actions. "
                f"Task: {state['task']}\nInstruction: {state['worker_instruction']}"
            )
            with deps.model_context("coder", {
                "task_preview": state["task"][:500],
                "instruction_characters": len(state.get("worker_instruction", "")),
                "execution_allowed": False,
            }) as model_span:
                content = await deps.models.complete(prompt)
                deps.observability.set_output(model_span, {
                    "response_characters": len(content), "execution_allowed": False,
                }, status="completed")
            artifact = deps.artifacts.write_text(
                state["application_run_id"], state["branch_id"], f"code-{execution_id}.md",
                "# Unexecuted code artifact\n\n" + content, "unexecuted_code",
            )
            progress = {"id": execution_id, "agent": "coder", "summary": "Created an unexecuted code artifact.", "made_progress": True}
            update = {
                "progress_ledger": [progress], "artifacts": [artifact],
                "messages": [{"id": execution_id, "role": "assistant", "name": "coder", "content": content}],
                "model_call_count": deps.models.calls, "execution_id": execution_id,
            }
            deps.observability.set_output(node_span, {
                "status": "completed", "artifact_type": artifact["type"],
                "artifact_size": artifact["size"], "execution_allowed": False,
            }, status="completed")
        deps.store.emit(state["application_run_id"], "node_finished", {"node": "coder", "execution_id": execution_id})
        return update

    async def finalizer(state: AgentState) -> dict[str, Any]:
        execution_id, span_context = deps.node_context(state, "finalizer")
        with span_context as node_span:
            if state.get("status") == "failed":
                answer = (
                    "Task did not complete successfully. " + state.get("worker_instruction", "A controlled limit or error stopped execution.")
                    + " Unresolved items remain; this is not a successful result."
                )
                status = "failed"
            else:
                evidence = json.dumps(state.get("progress_ledger", []), ensure_ascii=False, default=str)
                with deps.model_context("finalizer", {
                    "task_preview": state["task"][:500],
                    "evidence_items": len(state.get("progress_ledger", [])),
                    "citation_count": len(state.get("citations", [])),
                }) as model_span:
                    answer = await deps.models.complete(
                        "Answer only from the evidence. Include citations and unresolved items; do not invent success.\n"
                        f"Task: {state['task']}\nEvidence: {evidence}\nCitations: {state.get('citations', [])}"
                    )
                    deps.observability.set_output(model_span, {
                        "answer_preview": answer[:500], "answer_characters": len(answer),
                    }, status="completed")
                status = "completed"
            update = {"final_answer": answer, "status": status, "model_call_count": deps.models.calls, "execution_id": execution_id}
            deps.observability.set_output(node_span, {
                "status": status, "answer_preview": answer[:500], "answer_characters": len(answer),
            }, status=status)
        deps.store.emit(state["application_run_id"], "node_finished", {"node": "finalizer", "execution_id": execution_id})
        return update

    def after_orchestrator(state: AgentState) -> str:
        return "planner" if state.get("next_agent") == "planner" else "finalizer" if state.get("next_agent") == "finalizer" else "pause_gate"

    def selected_worker(state: AgentState) -> str:
        worker = state.get("next_agent", "")
        if worker not in {"web_surfer", "file_reader", "coder"}:
            raise ValueError(f"invalid worker selection: {worker}")
        return worker

    builder = StateGraph(AgentState)
    for name, node in {
        "planner": planner, "orchestrator": orchestrator, "pause_gate": pause_gate,
        "web_surfer": web_surfer, "file_reader": file_reader, "coder": coder, "finalizer": finalizer,
    }.items():
        builder.add_node(name, node)
    builder.add_edge(START, "planner")
    builder.add_edge("planner", "orchestrator")
    builder.add_conditional_edges("orchestrator", after_orchestrator, ["planner", "pause_gate", "finalizer"])
    builder.add_conditional_edges("pause_gate", selected_worker, ["web_surfer", "file_reader", "coder"])
    for worker in ("web_surfer", "file_reader", "coder"):
        builder.add_edge(worker, "orchestrator")
    builder.add_edge("finalizer", END)
    return builder.compile(checkpointer=checkpointer)
