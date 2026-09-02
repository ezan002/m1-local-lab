from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .runtime import AgentRuntime


class DatasetCase(BaseModel):
    case_id: str = Field(min_length=1, max_length=200)
    task: str = Field(min_length=1, max_length=20_000)
    allowed_files: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


async def run_jsonl(runtime: AgentRuntime, path: Path, *, limit: int) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("an explicit positive --limit is required")
    cases: list[DatasetCase] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            cases.append(DatasetCase.model_validate_json(raw))
        except ValidationError as exc:
            raise ValueError(f"invalid JSONL line {line_number}: {exc}") from exc
    results = []
    for case in cases[:limit]:
        started = time.perf_counter()
        try:
            run_id = await runtime.create_run(
                case.task, allowed_files=case.allowed_files, dataset_case_id=case.case_id
            )
            state = await runtime.current_state(run_id)
            value = state["state"]
            results.append({
                "case_id": case.case_id, "run_id": run_id, "status": state["status"],
                "final_answer": value.get("final_answer"), "citations": value.get("citations", []),
                "duration_seconds": round(time.perf_counter() - started, 3),
                "model_calls": value.get("model_call_count"), "tokens": "unknown",
                "errors": value.get("errors", []), "artifacts": value.get("artifacts", []),
            })
        except Exception as exc:
            results.append({
                "case_id": case.case_id, "status": "failed", "duration_seconds": round(time.perf_counter() - started, 3),
                "error": f"{type(exc).__name__}: {exc}", "tokens": "unknown",
            })
    return results

