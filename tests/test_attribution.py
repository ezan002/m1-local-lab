from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import ValidationError

from m1_local.attribution import (
    AttributionGroundTruth,
    AttributionInput,
    AttributionPrediction,
    assert_no_label_fields,
    build_attribution_views,
    first_explicit_error,
    last_active_agent,
    rule_based,
)
from m1_local.attribution_evaluation import (
    EVALUATION_NAMES,
    evaluate_prediction,
    log_suite_to_phoenix,
    run_attribution_suite,
)
from m1_local.external_datasets import (
    AttributionGroundTruth as ExternalGroundTruth,
)
from m1_local.external_datasets import ExternalTraceRecord


def _record(
    *,
    source: str = "Who&When",
    responsible_agent: str | None = "WebSurfer",
    decisive_step: str | None = "1",
    truncated: bool = False,
) -> ExternalTraceRecord:
    if source == "Who&When":
        trace = [
            {"trace_index": 0, "role": "Orchestrator", "content": "delegate research"},
            {"trace_index": 1, "role": "WebSurfer", "content": "tool failed with timeout"},
            {"trace_index": 2, "role": "Coder", "content": "final answer"},
        ]
    else:
        trace = [
            {"trace_index": 0, "step_id": 8, "agent_name": "Planner", "response": "plan"},
            {
                "trace_index": 1,
                "step_id": 9,
                "agent_name": "WebSurfer",
                "tool_logs": [{"status": "failed", "error_type": "TimeoutError"}],
            },
        ]
        if decisive_step == "1":
            decisive_step = "9"
    return ExternalTraceRecord(
        dataset_case_id=f"case-{source.lower()}",
        source_dataset=source,
        source_version="revision-1",
        source_split="test",
        source_case_id="source-1",
        source_license="test-only",
        task="Find the answer",
        expected_answer="correct result",
        original_system="test",
        trace=trace,
        ground_truth=ExternalGroundTruth(
            responsible_agent=responsible_agent,
            decisive_step=decisive_step,
            failure_reason="timeout",
        ),
        metadata={
            "original_trace_items": 20 if truncated else len(trace),
            "retained_trace_items": len(trace),
            "truncated": truncated,
        },
    )


def test_views_separate_inference_from_ground_truth_recursively() -> None:
    inference, labels = build_attribution_views(_record())
    dumped = inference.model_dump(mode="json")
    assert_no_label_fields(dumped)
    assert labels.responsible_agent == "WebSurfer"
    assert "responsible_agent" not in str(dumped)
    assert inference is not labels


@pytest.mark.parametrize(
    "payload",
    [
        {"responsible_agent": "x"},
        {"nested": {"ground_truth": {"agent": "x"}}},
        {"metadata": [{"mistake_step": 3}]},
        {"expected": {"label_source": "secret"}},
    ],
)
def test_recursive_label_guard_rejects_top_level_and_nested_fields(payload: Any) -> None:
    with pytest.raises(ValueError, match="label field is forbidden"):
        assert_no_label_fields(payload)


def test_input_model_rejects_extra_or_nested_label_fields() -> None:
    inference, _ = build_attribution_views(_record())
    payload = inference.model_dump(mode="python")
    payload["expected_answer"] = {"metadata": {"decisive_step": 1}}
    with pytest.raises(ValidationError, match="label field is forbidden"):
        AttributionInput.model_validate(payload)
    payload = inference.model_dump(mode="python")
    payload["ground_truth"] = {}
    with pytest.raises(ValidationError):
        AttributionInput.model_validate(payload)


def test_source_specific_normalization_keeps_semantics_distinct() -> None:
    who, _ = build_attribution_views(_record(source="Who&When"))
    elephant, _ = build_attribution_views(_record(source="TraceElephant"))
    assert who.steps[1].step_number == "1"
    assert who.steps[1].message_type == "WebSurfer"
    assert elephant.steps[1].step_number == "9"
    assert elephant.steps[1].message_type == "tool"
    assert elephant.steps[1].error_type == "TimeoutError"


def test_three_baselines_are_deterministic_and_general() -> None:
    inference, _ = build_attribution_views(_record())
    assert last_active_agent(inference) == last_active_agent(inference)
    assert first_explicit_error(inference) == first_explicit_error(inference)
    assert rule_based(inference) == rule_based(inference)
    assert last_active_agent(inference).predicted_agent == "Coder"
    assert first_explicit_error(inference).predicted_agent == "WebSurfer"
    assert rule_based(inference).predicted_agent == "WebSurfer"


def test_baselines_abstain_when_no_deterministic_signal_exists() -> None:
    record = _record(responsible_agent=None, decisive_step=None)
    record.trace = [{"trace_index": 0, "role": None, "content": "ordinary message"}]
    record.metadata["original_trace_items"] = 1
    inference, _ = build_attribution_views(record)
    for predictor in (last_active_agent, first_explicit_error, rule_based):
        result = predictor(inference)
        assert result.status == "abstained"
        assert result.predicted_agent is None
        assert result.predicted_step is None


def test_prediction_schema_enforces_confidence_and_status() -> None:
    base = {
        "method": "rule_based",
        "dataset_case_id": "case",
        "status": "predicted",
        "reason": "evidence",
        "predicted_agent": "Coder",
        "predicted_step": "1",
    }
    assert AttributionPrediction(**base, confidence=0).confidence == 0
    assert AttributionPrediction(**base, confidence=1).confidence == 1
    with pytest.raises(ValidationError):
        AttributionPrediction(**base, confidence=1.01)
    with pytest.raises(ValidationError):
        AttributionPrediction(**{**base, "predicted_step": None}, confidence=0.5)


def test_metrics_exact_joint_distance_coverage_and_no_mutation() -> None:
    inference, labels = build_attribution_views(_record())
    prediction = first_explicit_error(inference)
    before = copy.deepcopy(prediction.model_dump())
    evaluation = evaluate_prediction(inference, prediction, labels)
    assert evaluation.agent_exact_match == 1
    assert evaluation.step_exact_match == 1
    assert evaluation.joint_exact_match == 1
    assert evaluation.step_distance == 0
    assert evaluation.prediction_coverage == 1
    assert evaluation.provenance_guard == 1
    assert prediction.model_dump() == before


def test_missing_labels_are_none_not_zero_and_excluded_from_denominator() -> None:
    inference, labels = build_attribution_views(_record(responsible_agent=None, decisive_step=None))
    prediction = last_active_agent(inference)
    evaluation = evaluate_prediction(inference, prediction, labels)
    assert evaluation.evaluation_status == "label_missing"
    assert evaluation.agent_exact_match is None
    assert evaluation.step_exact_match is None
    assert evaluation.joint_exact_match is None
    suite = run_attribution_suite([(inference, labels)])
    summary = suite.summaries["last_active_agent"]
    assert summary.metrics["agent_exact_match"].denominator == 0
    assert summary.metrics["prediction_coverage"].denominator == 1


def test_truncated_trace_with_missing_decisive_step_is_not_scorable() -> None:
    inference, labels = build_attribution_views(_record(decisive_step="99", truncated=True))
    evaluation = evaluate_prediction(inference, last_active_agent(inference), labels)
    assert inference.trace_original_step_count == 20
    assert inference.trace_retained_step_count == 3
    assert inference.trace_truncated is True
    assert labels.decisive_step_present_in_trace is False
    assert evaluation.evaluation_status == "trace_truncated"
    assert evaluation.step_exact_match is None
    assert evaluation.step_distance is None


def test_non_truncated_missing_step_is_label_missing() -> None:
    inference, labels = build_attribution_views(_record(decisive_step="99"))
    evaluation = evaluate_prediction(inference, last_active_agent(inference), labels)
    assert evaluation.evaluation_status == "label_missing"
    assert evaluation.step_scorable is False
    assert evaluation.unscorable_reason == "decisive_step_not_present_in_normalized_trace"


def test_abstention_counts_as_zero_only_when_label_is_scorable() -> None:
    inference, labels = build_attribution_views(_record())
    prediction = AttributionPrediction(
        method="rule_based",
        dataset_case_id=inference.dataset_case_id,
        status="abstained",
        reason="not enough evidence",
        confidence=0,
    )
    evaluation = evaluate_prediction(inference, prediction, labels)
    assert evaluation.evaluation_status == "abstained"
    assert evaluation.agent_exact_match == 0
    assert evaluation.step_exact_match == 0
    assert evaluation.joint_exact_match == 0
    assert evaluation.step_distance is None
    assert evaluation.prediction_coverage == 0


def test_predictor_error_is_distinct_and_excluded_from_accuracy() -> None:
    inference, labels = build_attribution_views(_record())
    prediction = AttributionPrediction(
        method="rule_based",
        dataset_case_id=inference.dataset_case_id,
        status="error",
        reason="predictor failed",
        confidence=0,
    )
    evaluation = evaluate_prediction(inference, prediction, labels)
    assert evaluation.evaluation_status == "evaluation_failed"
    assert evaluation.agent_exact_match is None
    assert evaluation.step_exact_match is None
    assert evaluation.error and "predictor returned error status" in evaluation.error


@dataclass
class _Example:
    id: str
    input: dict[str, Any]


@dataclass
class _Dataset:
    id: str
    version_id: str
    examples: list[_Example]


class _FakeExperiments:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.evaluations: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, str]:
        self.created.append(kwargs)
        return {"id": "experiment-1"}

    def log_run(self, **kwargs: Any) -> dict[str, str]:
        self.runs.append(kwargs)
        return {"id": f"run-{len(self.runs)}"}

    def log_evaluation(self, **kwargs: Any) -> dict[str, str]:
        self.evaluations.append(kwargs)
        return {"id": f"evaluation-{len(self.evaluations)}"}


class _FakeClient:
    def __init__(self) -> None:
        self.experiments = _FakeExperiments()


def test_phoenix_logging_creates_three_by_three_matrix_and_namespaced_evaluations() -> None:
    views = [build_attribution_views(_record())]
    suite = run_attribution_suite(views)
    case_id = views[0][0].dataset_case_id
    dataset = _Dataset(
        "dataset-1",
        "version-1",
        [
            _Example(
                "example-1",
                {
                    "dataset_case_id": case_id,
                    "source_dataset": "Who&When",
                    "source_version": "revision-1",
                },
            )
        ],
    )
    client = _FakeClient()
    result = log_suite_to_phoenix(client, dataset, suite)
    assert result.run_count == 3
    assert result.evaluation_count == 3 * len(EVALUATION_NAMES)
    assert set(result.evaluation_names) == set(EVALUATION_NAMES)
    assert client.experiments.created[0]["repetitions"] == 3
    assert all(item["name"].startswith("attribution.") for item in client.experiments.evaluations)
    assert all("responsible_agent" not in str(item["output"]) for item in client.experiments.runs)
    metadata = client.experiments.runs[0]["output"]["metadata"]
    assert metadata["source"] == "Who&When"
    assert metadata["revision"] == "revision-1"
    assert metadata["executed_by_m1_local"] is False
    assert "agent_scorable" not in metadata
    assert "step_scorable" not in metadata
    evaluation_metadata = client.experiments.evaluations[0]["metadata"]
    assert evaluation_metadata["agent_scorable"] is True
    assert evaluation_metadata["step_scorable"] is True


def test_phoenix_prediction_export_redacts_reason_without_mutating_suite() -> None:
    suite = run_attribution_suite([build_attribution_views(_record())])
    prediction = suite.predictions[0].model_copy(update={"reason": 'password="first SUITE_EXPORT_TAIL"'})
    suite = suite.model_copy(update={"predictions": (prediction, *suite.predictions[1:])})
    before = suite.model_dump()
    dataset = _Dataset("dataset", "version", [_Example("example", {"dataset_case_id": prediction.dataset_case_id})])
    client = _FakeClient()
    log_suite_to_phoenix(client, dataset, suite)
    assert "SUITE_EXPORT_TAIL" not in str(client.experiments.runs)
    assert suite.model_dump() == before
