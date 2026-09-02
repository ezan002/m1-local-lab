from __future__ import annotations

from typing import Annotated, Any, TypedDict


def append_unique(left: list[dict[str, Any]] | None, right: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Append records once by stable id, making resumed nodes idempotent."""
    result = list(left or [])
    seen = {str(item.get("id")) for item in result if item.get("id")}
    for item in right or []:
        item_id = str(item.get("id", ""))
        if not item_id or item_id not in seen:
            result.append(item)
            if item_id:
                seen.add(item_id)
    return result


class AgentState(TypedDict, total=False):
    schema_version: int
    task: str
    messages: Annotated[list[dict[str, Any]], append_unique]
    task_ledger: dict[str, Any]
    plan: list[str]
    progress_ledger: Annotated[list[dict[str, Any]], append_unique]
    next_agent: str
    worker_instruction: str
    artifacts: Annotated[list[dict[str, Any]], append_unique]
    browser_resume: dict[str, Any]
    step_count: int
    stall_count: int
    replan_count: int
    model_call_count: int
    final_answer: str
    status: str
    errors: Annotated[list[dict[str, Any]], append_unique]
    thread_id: str
    application_run_id: str
    branch_id: str
    execution_id: str
    parent_run_id: str | None
    source_checkpoint_id: str | None
    dataset_case_id: str | None
    allowed_files: list[str]
    citations: list[str]


def initial_state(
    *, task: str, thread_id: str, run_id: str, branch_id: str, allowed_files: list[str] | None = None,
    parent_run_id: str | None = None, source_checkpoint_id: str | None = None,
    dataset_case_id: str | None = None,
) -> AgentState:
    return {
        "schema_version": 1,
        "task": task,
        "messages": [{"id": f"{run_id}:task", "role": "user", "content": task}],
        "task_ledger": {"facts": [], "unknowns": [], "done": []},
        "plan": [],
        "progress_ledger": [],
        "next_agent": "",
        "worker_instruction": "",
        "artifacts": [],
        "browser_resume": {"browser_restore_mode": "fresh"},
        "step_count": 0,
        "stall_count": 0,
        "replan_count": 0,
        "model_call_count": 0,
        "final_answer": "",
        "status": "queued",
        "errors": [],
        "thread_id": thread_id,
        "application_run_id": run_id,
        "branch_id": branch_id,
        "execution_id": "",
        "parent_run_id": parent_run_id,
        "source_checkpoint_id": source_checkpoint_id,
        "dataset_case_id": dataset_case_id,
        "allowed_files": allowed_files or [],
        "citations": [],
    }
