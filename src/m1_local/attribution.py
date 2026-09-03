from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .external_datasets import ExternalTraceRecord
from .observability import safe_json_summary


AttributionMethodName = Literal["last_active_agent", "first_explicit_error", "rule_based"]
PredictionStatus = Literal["predicted", "abstained", "error"]

_FORBIDDEN_LABEL_KEYS = {
    "responsible_agent",
    "decisive_step",
    "failure_reason",
    "ground_truth",
    "groundtruth",
    "label",
    "labels",
    "label_source",
    "expected_responsible_agent",
    "expected_decisive_step",
    "mistake_agent",
    "mistake_step",
    "mistake_reason",
    "agent_scorable",
    "step_scorable",
    "decisive_step_present_in_trace",
}
_FAILURE_TEXT = re.compile(
    r"(?i)\b(error|exception|failed|failure|unable|cannot|timed[ -]?out|timeout|blocked|denied)\b"
)
_RETRY_TEXT = re.compile(r"(?i)\b(retry|retried|trying again|loop|stalled|no progress|repeated)\b")
_FAILED_STATUSES = {"error", "failed", "failure", "blocked", "denied", "timeout", "timed_out"}


def _canonical_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def assert_no_label_fields(value: Any, *, path: str = "$", _seen: set[int] | None = None) -> None:
    """Reject label-bearing keys at any depth instead of silently stripping them."""
    seen = _seen if _seen is not None else set()
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        for key, item in value.items():
            normalized = _canonical_key(key)
            if normalized in _FORBIDDEN_LABEL_KEYS:
                raise ValueError(f"label field is forbidden in attribution input: {path}.{key}")
            assert_no_label_fields(item, path=f"{path}.{key}", _seen=seen)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        for index, item in enumerate(value):
            assert_no_label_fields(item, path=f"{path}[{index}]", _seen=seen)


class AttributionStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_number: str = Field(min_length=1, max_length=100)
    agent_name: str | None = Field(default=None, max_length=300)
    message_type: str = Field(default="message", max_length=200)
    content_summary: str = Field(max_length=4_001)
    tool_called: bool = False
    tool_failed: bool = False
    explicit_error: bool = False
    retry_or_stall: bool = False
    error_type: str | None = Field(default=None, max_length=300)
    status: str | None = Field(default=None, max_length=100)
    has_effective_action: bool = False


class AttributionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_case_id: str = Field(min_length=1, max_length=200)
    source: Literal["TraceElephant", "Who&When"]
    revision: str = Field(min_length=1)
    source_split: str = Field(min_length=1)
    source_case_id: str = Field(min_length=1)
    task: str
    expected_answer: Any = None
    steps: tuple[AttributionStep, ...]
    trace_original_step_count: int = Field(ge=0)
    trace_retained_step_count: int = Field(ge=0)
    trace_truncated: bool
    trace_origin: Literal["imported_historical"] = "imported_historical"
    executed_by_m1_local: Literal[False] = False
    external_task_executed: Literal[False] = False

    @model_validator(mode="before")
    @classmethod
    def reject_labels(cls, value: Any) -> Any:
        assert_no_label_fields(value)
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> "AttributionInput":
        if self.trace_retained_step_count != len(self.steps):
            raise ValueError("trace_retained_step_count must equal the normalized step count")
        if self.trace_original_step_count < self.trace_retained_step_count:
            raise ValueError("original step count cannot be smaller than retained step count")
        return self


class AttributionGroundTruth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_case_id: str
    source: Literal["TraceElephant", "Who&When"]
    revision: str
    source_case_id: str
    responsible_agent: str | None = None
    decisive_step: str | None = None
    failure_reason: str | None = None
    label_source: str
    agent_scorable: bool
    step_scorable: bool
    decisive_step_present_in_trace: bool
    step_unscorable_reason: str | None = None


class AttributionPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    predicted_agent: str | None = None
    predicted_step: str | None = None
    reason: str = Field(min_length=1, max_length=2_000)
    confidence: float = Field(ge=0.0, le=1.0)
    method: AttributionMethodName
    dataset_case_id: str
    status: PredictionStatus
    evidence_step_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_prediction(self) -> "AttributionPrediction":
        has_values = self.predicted_agent is not None and self.predicted_step is not None
        if self.status == "predicted" and not has_values:
            raise ValueError("predicted status requires both agent and step")
        if self.status != "predicted" and (self.predicted_agent is not None or self.predicted_step is not None):
            raise ValueError("non-predicted status cannot contain an agent or step")
        return self


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _walk(value: Any):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key), item
            yield from _walk(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _walk(item)


def _text_values(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, str):
        return [value]
    for _, item in _walk(value):
        if isinstance(item, str):
            result.append(item)
    return result


def _has_nonempty_key(value: Any, names: set[str], *, contains: str | None = None) -> bool:
    for key, item in _walk(value):
        normalized = _canonical_key(key)
        matched = normalized in names or (contains is not None and contains in normalized)
        if matched and item not in (None, "", [], {}, False):
            return True
    return False


def _first_nested_text(value: Any, names: set[str]) -> str | None:
    for key, item in _walk(value):
        if _canonical_key(key) in names and item not in (None, ""):
            return _optional_text(item)
    return None


def _normalize_common(
    raw: Mapping[str, Any], *, step_number: Any, agent_name: Any, message_type: Any
) -> AttributionStep:
    text = "\n".join(_text_values(raw))
    status = _first_nested_text(raw, {"status", "finish_reason"})
    error_type = _first_nested_text(raw, {"error", "error_type", "exception", "exception_type"})
    status_failed = _canonical_key(status) in _FAILED_STATUSES if status else False
    error_key = _has_nonempty_key(raw, {"error", "error_type", "exception", "exception_type"})
    tool_called = _has_nonempty_key(raw, set(), contains="tool")
    failure_text = bool(_FAILURE_TEXT.search(text))
    explicit_error = status_failed or error_key or failure_text
    tool_failed = tool_called and explicit_error
    retry_or_stall = bool(_RETRY_TEXT.search(text))
    agent = _optional_text(agent_name)
    summary = safe_json_summary(raw, limit=4_000)
    has_effective_action = bool(agent and (text.strip() or tool_called or explicit_error))
    return AttributionStep(
        step_number=_optional_text(step_number) or "unknown",
        agent_name=agent,
        message_type=_optional_text(message_type) or "message",
        content_summary=summary,
        tool_called=tool_called,
        tool_failed=tool_failed,
        explicit_error=explicit_error,
        retry_or_stall=retry_or_stall,
        error_type=error_type,
        status=status,
        has_effective_action=has_effective_action,
    )


def _normalize_traceelephant_step(raw: Mapping[str, Any], index: int) -> AttributionStep:
    return _normalize_common(
        raw,
        step_number=raw.get("step_id", raw.get("trace_index", index)),
        agent_name=raw.get("agent_name"),
        message_type="tool" if raw.get("tool_logs") else "agent_step",
    )


def _normalize_who_and_when_step(raw: Mapping[str, Any], index: int) -> AttributionStep:
    return _normalize_common(
        raw,
        step_number=raw.get("trace_index", index),
        agent_name=raw.get("name") or raw.get("role"),
        message_type=raw.get("role", "message"),
    )


def build_attribution_views(record: ExternalTraceRecord) -> tuple[AttributionInput, AttributionGroundTruth]:
    """Split one adapter record into independently owned inference and label views."""
    # Deep copies sever container identity shared with the label-bearing adapter
    # record before the inference view crosses the predictor boundary.
    normalized: list[AttributionStep] = []
    for index, raw in enumerate(copy.deepcopy(record.trace)):
        if not isinstance(raw, Mapping):
            raise ValueError(f"trace step {index} must be a mapping")
        assert_no_label_fields(raw, path=f"$.steps[{index}]")
        if record.source_dataset == "TraceElephant":
            normalized.append(_normalize_traceelephant_step(raw, index))
        elif record.source_dataset == "Who&When":
            normalized.append(_normalize_who_and_when_step(raw, index))
        else:  # pragma: no cover - protected by ExternalTraceRecord
            raise ValueError(f"unsupported source: {record.source_dataset}")

    original_count = int(record.metadata.get("original_trace_items", len(record.trace)))
    retained_count = len(normalized)
    input_payload = {
        "dataset_case_id": record.dataset_case_id,
        "source": record.source_dataset,
        "revision": record.source_version,
        "source_split": record.source_split,
        "source_case_id": record.source_case_id,
        "task": record.task,
        "expected_answer": copy.deepcopy(record.expected_answer),
        "steps": tuple(normalized),
        "trace_original_step_count": original_count,
        "trace_retained_step_count": retained_count,
        "trace_truncated": bool(record.metadata.get("truncated")),
        "trace_origin": "imported_historical",
        "executed_by_m1_local": False,
        "external_task_executed": False,
    }
    inference = AttributionInput.model_validate(input_payload)

    responsible_agent = _optional_text(record.ground_truth.responsible_agent)
    decisive_step = _optional_text(record.ground_truth.decisive_step)
    retained_ids = {step.step_number for step in inference.steps}
    decisive_present = decisive_step is not None and decisive_step in retained_ids
    step_scorable = decisive_step is not None and decisive_present
    if decisive_step is None:
        unscorable = "decisive_step_label_missing"
    elif not decisive_present and inference.trace_truncated:
        unscorable = "decisive_step_not_retained_due_to_truncation"
    elif not decisive_present:
        unscorable = "decisive_step_not_present_in_normalized_trace"
    else:
        unscorable = None
    ground_truth = AttributionGroundTruth(
        dataset_case_id=record.dataset_case_id,
        source=record.source_dataset,
        revision=record.source_version,
        source_case_id=record.source_case_id,
        responsible_agent=responsible_agent,
        decisive_step=decisive_step,
        failure_reason=_optional_text(record.ground_truth.failure_reason),
        label_source=f"{record.source_dataset}@{record.source_version}",
        agent_scorable=responsible_agent is not None,
        step_scorable=step_scorable,
        decisive_step_present_in_trace=decisive_present,
        step_unscorable_reason=unscorable,
    )
    return inference, ground_truth


def _abstain(method: AttributionMethodName, value: AttributionInput, reason: str) -> AttributionPrediction:
    return AttributionPrediction(
        method=method,
        dataset_case_id=value.dataset_case_id,
        status="abstained",
        confidence=0.0,
        reason=reason,
        warnings=("insufficient deterministic evidence",),
    )


def last_active_agent(value: AttributionInput) -> AttributionPrediction:
    assert_no_label_fields(value)
    for step in reversed(value.steps):
        if step.agent_name and step.has_effective_action:
            return AttributionPrediction(
                predicted_agent=step.agent_name,
                predicted_step=step.step_number,
                reason="Selected the last agent step containing an effective message, action, tool event, or error.",
                confidence=0.35,
                method="last_active_agent",
                dataset_case_id=value.dataset_case_id,
                status="predicted",
                evidence_step_ids=(step.step_number,),
            )
    return _abstain("last_active_agent", value, "No retained step has both an agent and an effective action.")


def first_explicit_error(value: AttributionInput) -> AttributionPrediction:
    assert_no_label_fields(value)
    for step in value.steps:
        if step.agent_name and step.explicit_error:
            reason = "Selected the earliest retained step with an explicit error, failed status, or failure expression."
            return AttributionPrediction(
                predicted_agent=step.agent_name,
                predicted_step=step.step_number,
                reason=reason,
                confidence=0.7 if step.tool_failed or step.error_type else 0.55,
                method="first_explicit_error",
                dataset_case_id=value.dataset_case_id,
                status="predicted",
                evidence_step_ids=(step.step_number,),
            )
    return _abstain("first_explicit_error", value, "No explicit error signal exists in the retained trace.")


def _final_answer_mismatch(value: AttributionInput, step: AttributionStep) -> bool:
    if value.expected_answer in (None, ""):
        return False
    content = step.content_summary.lower()
    conclusive = any(marker in content for marker in ("final", "answer", "terminate", "completed"))
    expected = str(value.expected_answer).strip().lower()
    return bool(conclusive and expected and expected not in content)


def rule_based(value: AttributionInput) -> AttributionPrediction:
    assert_no_label_fields(value)
    candidates: list[tuple[int, int, AttributionStep, list[str]]] = []
    last_effective = next(
        (step for step in reversed(value.steps) if step.agent_name and step.has_effective_action), None
    )
    for index, step in enumerate(value.steps):
        if not step.agent_name:
            continue
        score = 0
        signals: list[str] = []
        if step.tool_failed:
            score += 100
            signals.append("tool failure")
        elif step.explicit_error:
            score += 80
            signals.append("explicit error")
        if step.retry_or_stall:
            score += 30
            signals.append("retry/loop/stall")
        if _final_answer_mismatch(value, step):
            score += 20
            signals.append("final-answer mismatch")
        if step is last_effective:
            score += 10
            signals.append("last effective action")
        if score:
            # Higher score wins; negative index deterministically favors the earliest equally strong signal.
            candidates.append((score, -index, step, signals))
    if not candidates:
        return _abstain("rule_based", value, "No retained step supplies a usable deterministic signal.")
    score, _, chosen, signals = max(candidates, key=lambda item: (item[0], item[1]))
    confidence = 0.8 if score >= 100 else 0.7 if score >= 80 else 0.5 if score >= 30 else 0.35
    warnings = ("trace was truncated",) if value.trace_truncated else ()
    return AttributionPrediction(
        predicted_agent=chosen.agent_name,
        predicted_step=chosen.step_number,
        reason="Selected the highest-scoring general signal: " + ", ".join(signals) + ".",
        confidence=confidence,
        method="rule_based",
        dataset_case_id=value.dataset_case_id,
        status="predicted",
        evidence_step_ids=(chosen.step_number,),
        warnings=warnings,
    )


AttributionMethod = Callable[[AttributionInput], AttributionPrediction]
ATTRIBUTION_METHODS: dict[AttributionMethodName, AttributionMethod] = {
    "last_active_agent": last_active_agent,
    "first_explicit_error": first_explicit_error,
    "rule_based": rule_based,
}
