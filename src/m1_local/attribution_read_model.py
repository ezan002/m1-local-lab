from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlparse

from phoenix.client import Client
from pydantic import BaseModel, ConfigDict, Field

from .attribution import (
    ATTRIBUTION_METHODS,
    AttributionMethodName,
    AttributionPrediction,
    build_attribution_views,
)
from .external_datasets import (
    AttributionGroundTruth as ExternalGroundTruth,
)
from .external_datasets import ExternalTraceRecord
from .external_smoke import DATASET_NAME
from .observability import redact_sensitive


EXPERIMENT_NAME = "m1-local-lab-failure-attribution-mvp"
ResultStatus = Literal[
    "correct", "agent_error", "step_error", "joint_error", "abstain", "unscorable"
]


class AttributionReadUnavailable(RuntimeError):
    pass


class AttributionDatasetNotFound(LookupError):
    pass


class AttributionExperimentNotFound(LookupError):
    pass


class AttributionCaseNotFound(LookupError):
    pass


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MetricRead(ReadModel):
    value: float | None = None
    numerator: float | None = None
    denominator: int = Field(ge=0)


class MethodMetricsRead(ReadModel):
    method: AttributionMethodName
    agent_exact_match: MetricRead
    step_exact_match: MetricRead
    joint_exact_match: MetricRead
    mean_step_distance: MetricRead
    prediction_coverage: MetricRead


class AttributionSummaryRead(ReadModel):
    dataset_name: str
    dataset_id: str
    experiment_name: str
    experiment_id: str
    example_count: int = Field(ge=0)
    task_run_count: int = Field(ge=0)
    evaluation_count: int = Field(ge=0)
    sources: tuple[str, ...]
    methods: tuple[AttributionMethodName, ...]
    method_metrics: tuple[MethodMetricsRead, ...]
    updated_at: str | None = None
    phoenix_available: bool = True
    phoenix_url: str | None = None
    trace_origin: Literal["imported_historical"] = "imported_historical"
    disclaimer: str = "本结果仅为基线管线验证，不代表算法有效。"


class ProvenanceRead(ReadModel):
    trace_origin: Literal["imported_historical"]
    executed_by_m1_local: bool
    external_task_executed: bool
    guard_score: float | None = None


class AttributionCaseRead(ReadModel):
    dataset_case_id: str
    source: str
    split: str
    method: AttributionMethodName
    result_status: ResultStatus
    predicted_agent: str | None = None
    predicted_step: str | None = None
    prediction_status: Literal["predicted", "abstained", "error"]
    confidence: float = Field(ge=0, le=1)
    responsible_agent: str | None = None
    decisive_step: str | None = None
    agent_exact_match: float | None = None
    step_exact_match: float | None = None
    joint_exact_match: float | None = None
    step_distance: float | None = None
    prediction_coverage: float
    trace_original_step_count: int = Field(ge=0)
    trace_retained_step_count: int = Field(ge=0)
    trace_truncated: bool
    decisive_step_present_in_trace: bool
    agent_scorable: bool
    step_scorable: bool
    unscorable_reason: str | None = None
    provenance: ProvenanceRead


class AttributionCasesRead(ReadModel):
    items: tuple[AttributionCaseRead, ...]
    total: int = Field(ge=0)
    returned: int = Field(ge=0)


class AttributionStepRead(ReadModel):
    step_number: str
    agent_name: str | None = None
    message_type: str
    content_summary: str
    tool_called: bool
    tool_failed: bool
    explicit_error: bool
    retry_or_stall: bool
    error_type: str | None = None
    status: str | None = None


class AttributionCaseDetailRead(AttributionCaseRead):
    revision: str
    source_case_id: str
    task_summary: str
    prediction_reason: str
    evidence_step_ids: tuple[str, ...]
    prediction_warnings: tuple[str, ...]
    failure_reason: str | None = None
    evaluation_status: str
    steps: tuple[AttributionStepRead, ...]
    experiment_name: str
    experiment_id: str
    experiment_url: str | None = None
    trace_id: str | None = None


@dataclass(frozen=True)
class _LoadedAttribution:
    dataset: Any
    experiment: Mapping[str, Any]
    examples: tuple[Mapping[str, Any], ...]
    task_runs: tuple[Mapping[str, Any], ...]
    evaluation_runs: tuple[Any, ...]
    experiment_url: str | None


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_text(value: Any, limit: int = 1_000) -> str:
    redacted = redact_sensitive("" if value is None else str(value))
    text = " ".join(str(redacted).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _safe_step_summary(step: Any) -> str:
    raw = _safe_text(_field(step, "content_summary", ""), 320)
    structured_markers = ("{'", '{"', "'messages':", '"messages":', "<html", "<!doctype")
    if any(marker in raw.lower() for marker in structured_markers):
        signals = [str(_field(step, "message_type", "event"))]
        if _field(step, "tool_called", False):
            signals.append("tool call")
        if _field(step, "tool_failed", False):
            signals.append("tool failure")
        if _field(step, "explicit_error", False):
            signals.append("explicit error")
        if _field(step, "retry_or_stall", False):
            signals.append("retry/stall")
        error_type = _optional_text(_field(step, "error_type"))
        if error_type:
            signals.append(_safe_text(error_type, 80))
        return "; ".join(signals) + "; structured historical payload omitted"
    return raw or "Historical step has no safe text summary."


def _safe_local_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    return value


def _aggregate(values: Sequence[float | None]) -> MetricRead:
    applicable = [float(value) for value in values if value is not None]
    if not applicable:
        return MetricRead(value=None, numerator=None, denominator=0)
    numerator = float(sum(applicable))
    return MetricRead(
        value=numerator / len(applicable), numerator=numerator, denominator=len(applicable)
    )


class AttributionReadModel:
    """Public-Phoenix-API projection for the post-prediction attribution UI."""

    def __init__(
        self,
        phoenix_base_url: str,
        *,
        dataset_name: str = DATASET_NAME,
        experiment_name: str = EXPERIMENT_NAME,
        experiment_id: str | None = None,
        cache_seconds: float = 60.0,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.phoenix_base_url = phoenix_base_url.rstrip("/")
        self.dataset_name = dataset_name
        self.experiment_name = experiment_name
        self.experiment_id = experiment_id
        self.cache_seconds = max(0.0, cache_seconds)
        self._client_factory = client_factory or (lambda: Client(base_url=self.phoenix_base_url))
        self._cache: tuple[float, _LoadedAttribution] | None = None
        self._cache_lock = Lock()

    def _load(self) -> _LoadedAttribution:
        with self._cache_lock:
            if self._cache is not None and monotonic() - self._cache[0] <= self.cache_seconds:
                return self._cache[1]
        try:
            client = self._client_factory()
            datasets = list(client.datasets.list())
            descriptor = next(
                (item for item in datasets if str(_field(item, "name", "")) == self.dataset_name),
                None,
            )
            if descriptor is None:
                raise AttributionDatasetNotFound(self.dataset_name)
            dataset = client.datasets.get_dataset(dataset=self.dataset_name)
            experiments = list(client.experiments.list(dataset_id=str(_field(dataset, "id", ""))))
            experiment = self._select_experiment(experiments)
            ran = client.experiments.get_experiment(experiment_id=str(experiment["id"]))
            generated_url = client.experiments.get_experiment_url(
                str(_field(dataset, "id", "")), str(experiment["id"])
            )
            examples = tuple(
                item for item in (_mapping(value) for value in _field(dataset, "examples", ())) if item
            )
            loaded = _LoadedAttribution(
                dataset=dataset,
                experiment=experiment,
                examples=examples,
                task_runs=tuple(_mapping(value) for value in ran.get("task_runs", ())),
                evaluation_runs=tuple(ran.get("evaluation_runs", ())),
                experiment_url=_safe_local_url(generated_url),
            )
            with self._cache_lock:
                self._cache = (monotonic(), loaded)
            return loaded
        except (AttributionDatasetNotFound, AttributionExperimentNotFound):
            raise
        except Exception as exc:
            raise AttributionReadUnavailable("Phoenix attribution data is unavailable") from exc

    def _select_experiment(self, experiments: Sequence[Any]) -> Mapping[str, Any]:
        normalized = [_mapping(value) for value in experiments]
        if self.experiment_id:
            selected = next(
                (item for item in normalized if str(item.get("id", "")) == self.experiment_id), None
            )
            if selected is None:
                raise AttributionExperimentNotFound(self.experiment_name)
            return selected

        exact = [
            item
            for item in normalized
            if _mapping(item.get("metadata")).get("experiment_name") == self.experiment_name
        ]
        # Phoenix 11.35's list response omits the display name for older
        # experiments, so use their explicit attribution metadata signature.
        legacy = [
            item
            for item in normalized
            if set(_mapping(item.get("metadata")).get("methods", ())) == set(ATTRIBUTION_METHODS)
            and _mapping(item.get("metadata")).get("trace_origin") == "imported_historical"
            and int(item.get("repetitions", 0)) == len(ATTRIBUTION_METHODS)
        ]
        candidates = exact or legacy
        if not candidates:
            raise AttributionExperimentNotFound(self.experiment_name)
        return max(candidates, key=lambda item: str(item.get("created_at", "")))

    @staticmethod
    def _evaluation_map(loaded: _LoadedAttribution) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for evaluation in loaded.evaluation_runs:
            run_id = str(_field(evaluation, "experiment_run_id", ""))
            name = str(_field(evaluation, "name", ""))
            if not run_id or not name:
                continue
            item = result.setdefault(run_id, {"metadata": {}})
            evaluation_result = _mapping(_field(evaluation, "result", {}))
            score = evaluation_result.get("score")
            item[name] = float(score) if score is not None else None
            metadata = _mapping(_field(evaluation, "metadata", {}))
            if metadata:
                item["metadata"] = dict(metadata)
            explanation = evaluation_result.get("explanation")
            if name == "attribution.step_exact_match" and explanation:
                item["step_explanation"] = _safe_text(explanation, 300)
        return result

    @staticmethod
    def _examples_by_id(loaded: _LoadedAttribution) -> dict[str, Mapping[str, Any]]:
        return {str(item.get("id", "")): item for item in loaded.examples}

    def _case_rows(self, loaded: _LoadedAttribution) -> list[AttributionCaseRead]:
        examples = self._examples_by_id(loaded)
        evaluations = self._evaluation_map(loaded)
        rows: list[AttributionCaseRead] = []
        for run in loaded.task_runs:
            example = examples.get(str(run.get("dataset_example_id", "")))
            if example is None:
                continue
            dataset_input = _mapping(example.get("input"))
            dataset_output = _mapping(example.get("output"))
            dataset_metadata = _mapping(example.get("metadata"))
            output = _mapping(run.get("output"))
            prediction = AttributionPrediction.model_validate(_mapping(output.get("prediction")))
            run_metadata = _mapping(output.get("metadata"))
            evaluation = evaluations.get(str(run.get("id", "")), {})
            evaluation_metadata = _mapping(evaluation.get("metadata"))
            agent_score = evaluation.get("attribution.agent_exact_match")
            step_score = evaluation.get("attribution.step_exact_match")
            joint_score = evaluation.get("attribution.joint_exact_match")
            agent_scorable = bool(evaluation_metadata.get("agent_scorable"))
            step_scorable = bool(evaluation_metadata.get("step_scorable"))
            result_status = self._result_status(
                prediction.status, agent_scorable, step_scorable, agent_score, step_score
            )
            provenance_score = evaluation.get("attribution.provenance_guard")
            rows.append(
                AttributionCaseRead(
                    dataset_case_id=prediction.dataset_case_id,
                    source=str(dataset_input.get("source_dataset", run_metadata.get("source", "unknown"))),
                    split=str(dataset_metadata.get("source_split", "unknown")),
                    method=prediction.method,
                    result_status=result_status,
                    predicted_agent=prediction.predicted_agent,
                    predicted_step=prediction.predicted_step,
                    prediction_status=prediction.status,
                    confidence=prediction.confidence,
                    responsible_agent=_optional_text(dataset_output.get("responsible_agent")),
                    decisive_step=_optional_text(dataset_output.get("decisive_step")),
                    agent_exact_match=agent_score,
                    step_exact_match=step_score,
                    joint_exact_match=joint_score,
                    step_distance=evaluation.get("attribution.step_distance"),
                    prediction_coverage=float(
                        evaluation.get("attribution.prediction_coverage", 0.0)
                    ),
                    trace_original_step_count=int(
                        run_metadata.get("trace_original_step_count", len(dataset_input.get("historical_trace", ())))
                    ),
                    trace_retained_step_count=int(
                        run_metadata.get("trace_retained_step_count", len(dataset_input.get("historical_trace", ())))
                    ),
                    trace_truncated=bool(run_metadata.get("trace_truncated")),
                    decisive_step_present_in_trace=bool(
                        evaluation_metadata.get("decisive_step_present_in_trace")
                    ),
                    agent_scorable=agent_scorable,
                    step_scorable=step_scorable,
                    unscorable_reason=(
                        _safe_text(evaluation.get("step_explanation"), 300)
                        if not step_scorable
                        else None
                    ),
                    provenance=ProvenanceRead(
                        trace_origin="imported_historical",
                        executed_by_m1_local=bool(run_metadata.get("executed_by_m1_local", False)),
                        external_task_executed=bool(
                            run_metadata.get("external_task_executed", False)
                        ),
                        guard_score=provenance_score,
                    ),
                )
            )
        return rows

    @staticmethod
    def _result_status(
        prediction_status: str,
        agent_scorable: bool,
        step_scorable: bool,
        agent_score: Any,
        step_score: Any,
    ) -> ResultStatus:
        if prediction_status == "abstained":
            return "abstain"
        if prediction_status == "error" or not agent_scorable or not step_scorable:
            return "unscorable"
        agent_correct = agent_score == 1.0
        step_correct = step_score == 1.0
        if agent_correct and step_correct:
            return "correct"
        if not agent_correct and step_correct:
            return "agent_error"
        if agent_correct and not step_correct:
            return "step_error"
        return "joint_error"

    def summary(self) -> AttributionSummaryRead:
        loaded = self._load()
        rows = self._case_rows(loaded)
        methods = tuple(
            method for method in ATTRIBUTION_METHODS if any(item.method == method for item in rows)
        )
        metrics: list[MethodMetricsRead] = []
        for method in methods:
            selected = [item for item in rows if item.method == method]
            metrics.append(
                MethodMetricsRead(
                    method=method,
                    agent_exact_match=_aggregate([item.agent_exact_match for item in selected]),
                    step_exact_match=_aggregate([item.step_exact_match for item in selected]),
                    joint_exact_match=_aggregate([item.joint_exact_match for item in selected]),
                    mean_step_distance=_aggregate([item.step_distance for item in selected]),
                    prediction_coverage=_aggregate(
                        [item.prediction_coverage for item in selected]
                    ),
                )
            )
        return AttributionSummaryRead(
            dataset_name=self.dataset_name,
            dataset_id=str(_field(loaded.dataset, "id", "")),
            experiment_name=self.experiment_name,
            experiment_id=str(loaded.experiment.get("id", "")),
            example_count=len(loaded.examples),
            task_run_count=len(loaded.task_runs),
            evaluation_count=len(loaded.evaluation_runs),
            sources=tuple(sorted({item.source for item in rows})),
            methods=methods,
            method_metrics=tuple(metrics),
            updated_at=_optional_text(loaded.experiment.get("updated_at")),
            phoenix_url=loaded.experiment_url,
        )

    def cases(
        self,
        *,
        source: str | None = None,
        method: AttributionMethodName | None = None,
        result_status: ResultStatus | None = None,
        agent_correct: bool | None = None,
        step_correct: bool | None = None,
        trace_truncated: bool | None = None,
        limit: int = 100,
    ) -> AttributionCasesRead:
        rows = self._case_rows(self._load())
        if source is not None:
            rows = [item for item in rows if item.source == source]
        if method is not None:
            rows = [item for item in rows if item.method == method]
        if result_status is not None:
            rows = [item for item in rows if item.result_status == result_status]
        if agent_correct is not None:
            rows = [
                item
                for item in rows
                if item.agent_exact_match is not None
                and (item.agent_exact_match == 1.0) is agent_correct
            ]
        if step_correct is not None:
            rows = [
                item
                for item in rows
                if item.step_exact_match is not None
                and (item.step_exact_match == 1.0) is step_correct
            ]
        if trace_truncated is not None:
            rows = [item for item in rows if item.trace_truncated is trace_truncated]
        rows.sort(key=lambda item: (item.dataset_case_id, item.method))
        return AttributionCasesRead(items=tuple(rows[:limit]), total=len(rows), returned=min(len(rows), limit))

    def case_detail(
        self, dataset_case_id: str, method: AttributionMethodName
    ) -> AttributionCaseDetailRead:
        loaded = self._load()
        row = next(
            (
                item
                for item in self._case_rows(loaded)
                if item.dataset_case_id == dataset_case_id and item.method == method
            ),
            None,
        )
        if row is None:
            raise AttributionCaseNotFound(dataset_case_id)
        example = next(
            (
                item
                for item in loaded.examples
                if str(_mapping(item.get("input")).get("dataset_case_id", "")) == dataset_case_id
            ),
            None,
        )
        run = next(
            (
                item
                for item in loaded.task_runs
                if _mapping(_mapping(item.get("output")).get("prediction")).get("dataset_case_id")
                == dataset_case_id
                and _mapping(_mapping(item.get("output")).get("prediction")).get("method")
                == method
            ),
            None,
        )
        if example is None or run is None:
            raise AttributionCaseNotFound(dataset_case_id)
        dataset_input = _mapping(example.get("input"))
        dataset_output = _mapping(example.get("output"))
        dataset_metadata = _mapping(example.get("metadata"))
        prediction = AttributionPrediction.model_validate(
            _mapping(_mapping(run.get("output")).get("prediction"))
        )
        record = ExternalTraceRecord(
            dataset_case_id=dataset_case_id,
            source_dataset=dataset_input.get("source_dataset"),
            source_version=str(dataset_input.get("source_version", "unknown")),
            source_split=str(dataset_metadata.get("source_split", "unknown")),
            source_case_id=str(dataset_input.get("source_case_id", dataset_case_id)),
            source_license=str(dataset_metadata.get("source_license", "not exposed")),
            task=str(dataset_input.get("task", "")),
            # Expected answer is unnecessary after prediction and is deliberately
            # omitted from the UI projection.
            expected_answer=None,
            original_system=str(dataset_metadata.get("original_system", "unknown")),
            trace=list(dataset_input.get("historical_trace", ())),
            ground_truth=ExternalGroundTruth(
                responsible_agent=dataset_output.get("responsible_agent"),
                decisive_step=dataset_output.get("decisive_step"),
                failure_reason=dataset_output.get("failure_reason"),
            ),
            metadata={
                "original_trace_items": row.trace_original_step_count,
                "retained_trace_items": row.trace_retained_step_count,
                "truncated": row.trace_truncated,
            },
        )
        inference, _ = build_attribution_views(record)
        evaluation_status = "scored"
        if row.result_status == "abstain":
            evaluation_status = "abstained"
        elif row.result_status == "unscorable":
            evaluation_status = "trace_truncated" if row.trace_truncated else "label_missing"
        return AttributionCaseDetailRead(
            **row.model_dump(),
            revision=inference.revision,
            source_case_id=inference.source_case_id,
            task_summary=_safe_text(inference.task),
            prediction_reason=_safe_text(prediction.reason, 2_000),
            evidence_step_ids=prediction.evidence_step_ids,
            prediction_warnings=tuple(_safe_text(value, 300) for value in prediction.warnings),
            failure_reason=(
                _safe_text(dataset_output.get("failure_reason"), 1_000)
                if dataset_output.get("failure_reason") is not None
                else None
            ),
            evaluation_status=evaluation_status,
            steps=tuple(
                AttributionStepRead(
                    step_number=step.step_number,
                    agent_name=step.agent_name,
                    message_type=step.message_type,
                    content_summary=_safe_step_summary(step),
                    tool_called=step.tool_called,
                    tool_failed=step.tool_failed,
                    explicit_error=step.explicit_error,
                    retry_or_stall=step.retry_or_stall,
                    error_type=step.error_type,
                    status=step.status,
                )
                for step in inference.steps
            ),
            experiment_name=self.experiment_name,
            experiment_id=str(loaded.experiment.get("id", "")),
            experiment_url=loaded.experiment_url,
            trace_id=_optional_text(run.get("trace_id")),
        )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
