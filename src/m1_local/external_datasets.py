from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field

from .observability import redact_sensitive


ADAPTER_VERSION = "v1"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_TRACE_ITEMS = 200
MAX_TRACE_JSON_CHARS = 100_000
SAFE_ATTACHMENT_SUFFIXES = {".csv", ".json", ".md", ".png", ".jpg", ".jpeg", ".txt"}
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


class AttributionGroundTruth(BaseModel):
    responsible_agent: str | None = None
    decisive_step: str | None = None
    failure_reason: str | None = None


class ExternalTraceRecord(BaseModel):
    dataset_case_id: str = Field(min_length=1, max_length=200)
    source_dataset: Literal["TraceElephant", "Who&When"]
    source_version: str = Field(min_length=1)
    source_split: str = Field(min_length=1)
    source_case_id: str = Field(min_length=1)
    source_license: str
    task: str
    expected_answer: Any = None
    original_system: str
    trace: list[dict[str, Any]]
    ground_truth: AttributionGroundTruth
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def phoenix_example(self) -> dict[str, Any]:
        """Represent imported history as dataset content, never as an observed execution."""
        return {
            "input": {
                "dataset_case_id": self.dataset_case_id,
                "source_dataset": self.source_dataset,
                "source_version": self.source_version,
                "source_case_id": self.source_case_id,
                "task": self.task,
                "historical_trace": self.trace,
                "trace_origin": "imported_historical",
                "external_task_executed": False,
            },
            "output": {
                "expected_answer": self.expected_answer,
                **self.ground_truth.model_dump(),
            },
            "metadata": {
                "dataset_case_id": self.dataset_case_id,
                "source_dataset": self.source_dataset,
                "source_version": self.source_version,
                "source_split": self.source_split,
                "source_case_id": self.source_case_id,
                "source_license": self.source_license,
                "original_system": self.original_system,
                "trace_origin": "imported_historical",
                "executed_by_m1_local": False,
                "adapter_version": ADAPTER_VERSION,
                "capabilities": self.metadata.get("capabilities", {}),
                "truncated": bool(self.metadata.get("truncated")),
            },
        }


def _read_json(path: Path, allowed_root: Path) -> tuple[Any, int]:
    root = allowed_root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"source path escapes allowed root: {path}") from exc
    if resolved.suffix.lower() != ".json":
        raise ValueError("only JSON source files are accepted")
    size = resolved.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise ValueError(f"source JSON exceeds {MAX_SOURCE_BYTES} bytes")
    try:
        return json.loads(resolved.read_text(encoding="utf-8")), size
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid UTF-8 JSON source: {path.name}") from exc


def validate_attachment_reference(value: str) -> str:
    """Validate metadata-only attachments. The adapter never opens or executes them."""
    if "\\" in value:
        raise ValueError("attachment references must use relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("unsafe attachment reference")
    if path.suffix.lower() not in SAFE_ATTACHMENT_SUFFIXES:
        raise ValueError("untrusted attachment type")
    return path.as_posix()


def _case_id(source: str, source_case_id: str) -> str:
    prefix = "traceelephant" if source == "TraceElephant" else "who-and-when"
    clean = _SAFE_ID.sub("-", source_case_id).strip("-")
    if not clean or len(clean) > 160:
        clean = hashlib.sha256(source_case_id.encode("utf-8")).hexdigest()
    return f"{prefix}:{clean}"


def _fallback_source_id(payload: Any) -> tuple[str, bool]:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest(), False


def _bounded_trace(items: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    result: list[dict[str, Any]] = []
    used = 2
    truncated = len(items) > MAX_TRACE_ITEMS
    for index, raw in enumerate(items[:MAX_TRACE_ITEMS]):
        safe = redact_sensitive(raw)
        item = safe if isinstance(safe, dict) else {"value": safe}
        encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)
        if used + len(encoded) + 1 > MAX_TRACE_JSON_CHARS:
            truncated = True
            break
        result.append({"trace_index": index, **item})
        used += len(encoded) + 1
    return result, truncated


def _source_attachments(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    raw = payload.get("attachments")
    if raw is None:
        return [], False
    if not isinstance(raw, list):
        raise ValueError("attachments must be a list")
    result = []
    for item in raw:
        reference = item if isinstance(item, str) else item.get("path") if isinstance(item, dict) else None
        if not isinstance(reference, str):
            raise ValueError("invalid attachment descriptor")
        result.append({"path": validate_attachment_reference(reference), "opened": False, "executed": False})
    return result, bool(result)


def adapt_who_and_when(
    path: Path,
    *,
    allowed_root: Path,
    source_version: str,
    source_split: Literal["Hand-Crafted", "Algorithm-Generated"],
) -> ExternalTraceRecord:
    payload, source_size = _read_json(path, allowed_root)
    if not isinstance(payload, dict):
        raise ValueError("Who&When record must be a JSON object")
    task = payload.get("question")
    history = payload.get("history")
    if not isinstance(task, str) or not task.strip() or not isinstance(history, list):
        raise ValueError("Who&When record requires non-empty question and history")
    source_case_id = payload.get("question_ID")
    id_present = isinstance(source_case_id, str) and bool(source_case_id.strip())
    if not id_present:
        source_case_id, id_present = _fallback_source_id(payload)
    trace, truncated = _bounded_trace(history)
    attachments, has_attachments = _source_attachments(payload)
    system_prompt = redact_sensitive(payload.get("system_prompt")) if "system_prompt" in payload else None
    return ExternalTraceRecord(
        dataset_case_id=_case_id("Who&When", str(source_case_id)),
        source_dataset="Who&When",
        source_version=source_version,
        source_split=source_split,
        source_case_id=str(source_case_id),
        source_license="repository MIT; dataset card has no separate license declaration",
        task=task.strip(),
        expected_answer=redact_sensitive(payload.get("ground_truth", payload.get("groundtruth"))),
        original_system="Magentic-One" if source_split == "Hand-Crafted" else "CaptainAgent",
        trace=trace,
        ground_truth=AttributionGroundTruth(
            responsible_agent=_optional_text(payload.get("mistake_agent")),
            decisive_step=_optional_text(payload.get("mistake_step")),
            failure_reason=_optional_text(redact_sensitive(payload.get("mistake_reason"))),
        ),
        attachments=attachments,
        metadata={
            "adapter_version": ADAPTER_VERSION,
            "source_size_bytes": source_size,
            "original_trace_items": len(history),
            "retained_trace_items": len(trace),
            "truncated": truncated,
            "system_prompt": system_prompt,
            "capabilities": {
                "source_case_id_present": id_present,
                "ground_truth_attribution": any(payload.get(key) is not None for key in ("mistake_agent", "mistake_step", "mistake_reason")),
                "system_prompt_present": system_prompt is not None,
                "attachments_present": has_attachments,
            },
            "trace_origin": "imported_historical",
            "executed_by_m1_local": False,
        },
    )


def adapt_traceelephant(
    metadata_path: Path,
    step_records_path: Path,
    *,
    allowed_root: Path,
    source_version: str,
    source_split: str,
) -> ExternalTraceRecord:
    metadata, metadata_size = _read_json(metadata_path, allowed_root)
    steps, steps_size = _read_json(step_records_path, allowed_root)
    if not isinstance(metadata, dict) or not isinstance(steps, list):
        raise ValueError("TraceElephant sample requires object metadata and a step-record list")
    if metadata_path.resolve().parent != step_records_path.resolve().parent:
        raise ValueError("TraceElephant metadata and steps must belong to the same run directory")
    task = metadata.get("task_instruction")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("TraceElephant metadata requires task_instruction")
    source_case_id = metadata.get("run_id") or metadata.get("task_id")
    id_present = isinstance(source_case_id, str) and bool(source_case_id.strip())
    if not id_present:
        source_case_id, id_present = _fallback_source_id({"metadata": metadata, "steps": steps})
    trace, truncated = _bounded_trace(steps)
    attachments, has_attachments = _source_attachments(metadata)
    return ExternalTraceRecord(
        dataset_case_id=_case_id("TraceElephant", str(source_case_id)),
        source_dataset="TraceElephant",
        source_version=source_version,
        source_split=source_split,
        source_case_id=str(source_case_id),
        source_license="CC BY 4.0",
        task=task.strip(),
        expected_answer=redact_sensitive(metadata.get("ground_truth")),
        original_system=_optional_text(metadata.get("system_name")) or "unknown",
        trace=trace,
        ground_truth=AttributionGroundTruth(
            responsible_agent=_optional_text(metadata.get("mistake_agent")),
            decisive_step=_optional_text(metadata.get("mistake_step")),
            failure_reason=_optional_text(redact_sensitive(metadata.get("mistake_reason"))),
        ),
        attachments=attachments,
        metadata={
            "adapter_version": ADAPTER_VERSION,
            "source_size_bytes": metadata_size + steps_size,
            "original_trace_items": len(steps),
            "retained_trace_items": len(trace),
            "truncated": truncated,
            "agent_configuration": redact_sensitive(metadata.get("agent_configuration")),
            "agent_system_intro": redact_sensitive(metadata.get("agent_system_intro")),
            "capabilities": {
                "source_case_id_present": id_present,
                "ground_truth_attribution": any(metadata.get(key) is not None for key in ("mistake_agent", "mistake_step", "mistake_reason")),
                "agent_configuration_present": metadata.get("agent_configuration") is not None,
                "attachments_present": has_attachments,
            },
            "trace_origin": "imported_historical",
            "executed_by_m1_local": False,
        },
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
