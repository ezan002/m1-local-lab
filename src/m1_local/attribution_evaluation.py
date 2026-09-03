from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .attribution import (
    ATTRIBUTION_METHODS,
    AttributionGroundTruth,
    AttributionInput,
    AttributionMethodName,
    AttributionPrediction,
)


EvaluationStatus = Literal[
    "scored", "abstained", "label_missing", "trace_truncated", "evaluation_failed"
]
EVALUATION_NAMES = (
    "attribution.agent_exact_match",
    "attribution.step_exact_match",
    "attribution.joint_exact_match",
    "attribution.step_distance",
    "attribution.prediction_coverage",
    "attribution.agent_scorable",
    "attribution.step_scorable",
    "attribution.provenance_guard",
)


class AttributionCaseEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_case_id: str
    method: AttributionMethodName
    evaluation_status: EvaluationStatus
    agent_exact_match: float | None = Field(default=None, ge=0.0, le=1.0)
    step_exact_match: float | None = Field(default=None, ge=0.0, le=1.0)
    joint_exact_match: float | None = Field(default=None, ge=0.0, le=1.0)
    step_distance: float | None = Field(default=None, ge=0.0)
    prediction_coverage: float = Field(ge=0.0, le=1.0)
    agent_scorable: bool
    step_scorable: bool
    provenance_guard: float = Field(ge=0.0, le=1.0)
    trace_original_step_count: int = Field(ge=0)
    trace_retained_step_count: int = Field(ge=0)
    trace_truncated: bool
    decisive_step_present_in_trace: bool
    unscorable_reason: str | None = None
    error: str | None = None


class MetricAggregate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float | None
    numerator: float | None
    denominator: int = Field(ge=0)


class MethodSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    method: AttributionMethodName
    case_count: int = Field(ge=0)
    predicted_count: int = Field(ge=0)
    abstained_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    metrics: dict[str, MetricAggregate]


class AttributionSuiteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    predictions: tuple[AttributionPrediction, ...]
    evaluations: tuple[AttributionCaseEvaluation, ...]
    summaries: dict[AttributionMethodName, MethodSummary]


class PhoenixAttributionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str
    experiment_name: str
    run_count: int
    evaluation_count: int
    evaluation_names: tuple[str, ...]
    experiment_run_ids: tuple[str, ...]


def _numeric_step(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def evaluate_prediction(
    inference: AttributionInput,
    prediction: AttributionPrediction,
    ground_truth: AttributionGroundTruth,
) -> AttributionCaseEvaluation:
    """Evaluate a prediction without exposing labels to or mutating the predictor output."""
    prediction_before = copy.deepcopy(prediction.model_dump(mode="python"))
    try:
        if prediction.status == "error":
            raise ValueError("predictor returned error status")
        if not (
            inference.dataset_case_id
            == prediction.dataset_case_id
            == ground_truth.dataset_case_id
        ):
            raise ValueError("dataset_case_id mismatch between inference, prediction, and labels")
        if inference.source != ground_truth.source or inference.revision != ground_truth.revision:
            raise ValueError("source provenance mismatch between inference and labels")

        provenance = float(
            inference.trace_origin == "imported_historical"
            and inference.executed_by_m1_local is False
            and inference.external_task_executed is False
        )
        coverage = float(prediction.status == "predicted")
        agent_score = (
            float(prediction.predicted_agent == ground_truth.responsible_agent)
            if ground_truth.agent_scorable
            else None
        )
        step_score = (
            float(prediction.predicted_step == ground_truth.decisive_step)
            if ground_truth.step_scorable
            else None
        )
        joint_score = (
            float(agent_score == 1.0 and step_score == 1.0)
            if agent_score is not None and step_score is not None
            else None
        )
        predicted_number = _numeric_step(prediction.predicted_step)
        expected_number = _numeric_step(ground_truth.decisive_step)
        distance = (
            float(abs(predicted_number - expected_number))
            if ground_truth.step_scorable
            and predicted_number is not None
            and expected_number is not None
            else None
        )

        if prediction.status == "abstained":
            status: EvaluationStatus = "abstained"
        elif not ground_truth.agent_scorable or ground_truth.decisive_step is None:
            status = "label_missing"
        elif not ground_truth.step_scorable and inference.trace_truncated:
            status = "trace_truncated"
        elif not ground_truth.step_scorable:
            status = "label_missing"
        else:
            status = "scored"

        evaluation = AttributionCaseEvaluation(
            dataset_case_id=inference.dataset_case_id,
            method=prediction.method,
            evaluation_status=status,
            agent_exact_match=agent_score,
            step_exact_match=step_score,
            joint_exact_match=joint_score,
            step_distance=distance,
            prediction_coverage=coverage,
            agent_scorable=ground_truth.agent_scorable,
            step_scorable=ground_truth.step_scorable,
            provenance_guard=provenance,
            trace_original_step_count=inference.trace_original_step_count,
            trace_retained_step_count=inference.trace_retained_step_count,
            trace_truncated=inference.trace_truncated,
            decisive_step_present_in_trace=ground_truth.decisive_step_present_in_trace,
            unscorable_reason=ground_truth.step_unscorable_reason,
        )
    except Exception as exc:
        evaluation = AttributionCaseEvaluation(
            dataset_case_id=inference.dataset_case_id,
            method=prediction.method,
            evaluation_status="evaluation_failed",
            prediction_coverage=float(prediction.status == "predicted"),
            agent_scorable=False,
            step_scorable=False,
            provenance_guard=0.0,
            trace_original_step_count=inference.trace_original_step_count,
            trace_retained_step_count=inference.trace_retained_step_count,
            trace_truncated=inference.trace_truncated,
            decisive_step_present_in_trace=False,
            unscorable_reason="evaluation_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    if prediction.model_dump(mode="python") != prediction_before:
        raise RuntimeError("evaluator mutated the prediction")
    return evaluation


def _aggregate(values: Sequence[float | None]) -> MetricAggregate:
    scored = [value for value in values if value is not None]
    numerator = float(sum(scored)) if scored else None
    return MetricAggregate(
        value=(numerator / len(scored)) if scored and numerator is not None else None,
        numerator=numerator,
        denominator=len(scored),
    )


def summarize_method(
    method: AttributionMethodName,
    predictions: Sequence[AttributionPrediction],
    evaluations: Sequence[AttributionCaseEvaluation],
) -> MethodSummary:
    selected_predictions = [item for item in predictions if item.method == method]
    selected = [item for item in evaluations if item.method == method]
    metrics = {
        "agent_exact_match": _aggregate([item.agent_exact_match for item in selected]),
        "step_exact_match": _aggregate([item.step_exact_match for item in selected]),
        "joint_exact_match": _aggregate([item.joint_exact_match for item in selected]),
        # Distance is defined only for scorable numeric prediction/label pairs.
        "step_distance": _aggregate([item.step_distance for item in selected]),
        "prediction_coverage": _aggregate([item.prediction_coverage for item in selected]),
        "agent_scorable": _aggregate([float(item.agent_scorable) for item in selected]),
        "step_scorable": _aggregate([float(item.step_scorable) for item in selected]),
        "provenance_guard": _aggregate([item.provenance_guard for item in selected]),
    }
    return MethodSummary(
        method=method,
        case_count=len(selected),
        predicted_count=sum(item.status == "predicted" for item in selected_predictions),
        abstained_count=sum(item.status == "abstained" for item in selected_predictions),
        failed_count=sum(item.evaluation_status == "evaluation_failed" for item in selected),
        metrics=metrics,
    )


def run_attribution_suite(inputs_and_labels: Sequence[tuple[AttributionInput, AttributionGroundTruth]]) -> AttributionSuiteResult:
    predictions: list[AttributionPrediction] = []
    evaluations: list[AttributionCaseEvaluation] = []
    for inference, ground_truth in inputs_and_labels:
        for method in ATTRIBUTION_METHODS.values():
            # Only the label-free object is passed across the predictor boundary.
            prediction = method(inference)
            predictions.append(prediction)
            evaluations.append(evaluate_prediction(inference, prediction, ground_truth))
    summaries = {
        name: summarize_method(name, predictions, evaluations) for name in ATTRIBUTION_METHODS
    }
    return AttributionSuiteResult(
        predictions=tuple(predictions), evaluations=tuple(evaluations), summaries=summaries
    )


def _field(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        result = getattr(value, name, None)
        if result is not None:
            return result
    return None


def _example_input(example: Any) -> Mapping[str, Any]:
    value = _field(example, "input")
    return value if isinstance(value, Mapping) else {}


def _evaluation_payload(name: str, item: AttributionCaseEvaluation) -> tuple[float | None, str]:
    short_name = name.removeprefix("attribution.")
    value = getattr(item, short_name)
    if short_name in {"agent_scorable", "step_scorable"}:
        score = float(value)
        return score, "scorable" if value else "not_scorable"
    if short_name == "prediction_coverage":
        return float(value), "predicted" if value else "abstained"
    if short_name == "provenance_guard":
        return float(value), "pass" if value else "fail"
    if value is None:
        return None, "not_scorable"
    if short_name == "step_distance":
        return float(value), "measured"
    return float(value), "match" if value else "mismatch"


def log_suite_to_phoenix(
    client: Any,
    dataset: Any,
    suite: AttributionSuiteResult,
    *,
    experiment_name: str = "m1-local-lab-failure-attribution-mvp",
) -> PhoenixAttributionResult:
    """Log precomputed offline predictions with the Phoenix 11.35 experiment API."""
    examples = list(_field(dataset, "examples") or ())
    case_to_example_id: dict[str, str] = {}
    for example in examples:
        case_id = str(_example_input(example).get("dataset_case_id", ""))
        example_id = _field(example, "id", "example_id", "dataset_example_id")
        if case_id and example_id:
            case_to_example_id[case_id] = str(example_id)
    expected_cases = {item.dataset_case_id for item in suite.predictions}
    if set(case_to_example_id) != expected_cases:
        raise ValueError("Phoenix dataset examples do not exactly match attribution cases")

    dataset_id = str(_field(dataset, "id"))
    dataset_version_id = _field(dataset, "version_id", "dataset_version_id")
    experiment = client.experiments.create(
        dataset_id=dataset_id,
        dataset_version_id=str(dataset_version_id) if dataset_version_id else None,
        experiment_name=experiment_name,
        experiment_description="Deterministic offline failure-attribution baselines over imported historical traces.",
        experiment_metadata={
            "methods": list(ATTRIBUTION_METHODS),
            "sample_count": len(expected_cases),
            "trace_origin": "imported_historical",
            "external_task_executed": False,
            "paid_model_calls": 0,
        },
        repetitions=len(ATTRIBUTION_METHODS),
    )
    experiment_id = str(_field(experiment, "id", "experiment_id"))
    if not experiment_id:
        raise RuntimeError("Phoenix did not return an experiment ID")

    evaluation_by_key = {
        (item.dataset_case_id, item.method): item for item in suite.evaluations
    }
    example_input_by_case = {
        str(_example_input(example).get("dataset_case_id", "")): _example_input(example)
        for example in examples
    }
    run_ids: list[str] = []
    evaluation_count = 0
    now = datetime.now(timezone.utc)
    for repetition, method in enumerate(ATTRIBUTION_METHODS, start=1):
        for prediction in (item for item in suite.predictions if item.method == method):
            evaluation = evaluation_by_key[(prediction.dataset_case_id, prediction.method)]
            dataset_input = example_input_by_case[prediction.dataset_case_id]
            run = client.experiments.log_run(
                experiment_id=experiment_id,
                dataset_example_id=case_to_example_id[prediction.dataset_case_id],
                output={
                    "prediction": prediction.model_dump(mode="json"),
                    "metadata": {
                        "method": method,
                        "dataset_case_id": prediction.dataset_case_id,
                        "source": dataset_input.get("source_dataset"),
                        "revision": dataset_input.get("source_version"),
                        "trace_origin": "imported_historical",
                        "executed_by_m1_local": False,
                        "external_task_executed": False,
                        "trace_original_step_count": evaluation.trace_original_step_count,
                        "trace_retained_step_count": evaluation.trace_retained_step_count,
                        "trace_truncated": evaluation.trace_truncated,
                    },
                },
                start_time=now,
                end_time=datetime.now(timezone.utc),
                repetition_number=repetition,
            )
            run_id = str(_field(run, "id", "experiment_run_id"))
            if not run_id:
                raise RuntimeError("Phoenix did not return an experiment run ID")
            run_ids.append(run_id)
            for name in EVALUATION_NAMES:
                score, label = _evaluation_payload(name, evaluation)
                client.experiments.log_evaluation(
                    experiment_run_id=run_id,
                    name=name,
                    annotator_kind="CODE",
                    score=score,
                    label=label,
                    explanation=evaluation.unscorable_reason or evaluation.evaluation_status,
                    metadata={
                        "dataset_case_id": evaluation.dataset_case_id,
                        "method": evaluation.method,
                        "source": dataset_input.get("source_dataset"),
                        "revision": dataset_input.get("source_version"),
                        "evaluation_status": evaluation.evaluation_status,
                        "trace_original_step_count": evaluation.trace_original_step_count,
                        "trace_retained_step_count": evaluation.trace_retained_step_count,
                        "trace_truncated": evaluation.trace_truncated,
                        "decisive_step_present_in_trace": evaluation.decisive_step_present_in_trace,
                        "agent_scorable": evaluation.agent_scorable,
                        "step_scorable": evaluation.step_scorable,
                        "trace_origin": "imported_historical",
                        "executed_by_m1_local": False,
                        "external_task_executed": False,
                    },
                )
                evaluation_count += 1
    return PhoenixAttributionResult(
        experiment_id=experiment_id,
        experiment_name=experiment_name,
        run_count=len(run_ids),
        evaluation_count=evaluation_count,
        evaluation_names=EVALUATION_NAMES,
        experiment_run_ids=tuple(run_ids),
    )
