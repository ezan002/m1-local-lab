from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from m1_local.api import create_app
from m1_local.attribution_evaluation import EVALUATION_NAMES
from m1_local.attribution_read_model import (
    AttributionDatasetNotFound,
    AttributionExperimentNotFound,
    AttributionReadModel,
    AttributionReadUnavailable,
)
from m1_local.models import RuleBasedGateway
from m1_local.runtime import AgentRuntime


METHODS = ("last_active_agent", "first_explicit_error", "rule_based")


class FakeDatasets:
    def __init__(self, examples: list[dict[str, Any]], *, present: bool = True) -> None:
        self.dataset = SimpleNamespace(
            id="dataset-public-id", version_id="version-public-id", examples=examples
        )
        self.present = present

    def list(self) -> list[Any]:
        return [SimpleNamespace(name="m1-local-lab-external-trace-smoke")] if self.present else []

    def get_dataset(self, *, dataset: str) -> Any:
        assert dataset == "m1-local-lab-external-trace-smoke"
        return self.dataset


class FakeExperiments:
    def __init__(
        self,
        task_runs: list[dict[str, Any]],
        evaluations: list[Any],
        *,
        present: bool = True,
    ) -> None:
        self.task_runs = task_runs
        self.evaluations = evaluations
        self.present = present

    def list(self, *, dataset_id: str) -> list[dict[str, Any]]:
        assert dataset_id == "dataset-public-id"
        if not self.present:
            return []
        return [
            {
                "id": "older-public-id",
                "repetitions": 3,
                "metadata": {"methods": list(METHODS), "trace_origin": "imported_historical"},
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "id": "latest-public-id",
                "repetitions": 3,
                "metadata": {
                    "experiment_name": "m1-local-lab-failure-attribution-mvp",
                    "methods": list(METHODS),
                    "trace_origin": "imported_historical",
                },
                "created_at": "2026-02-01T00:00:00+00:00",
                "updated_at": "2026-02-01T01:00:00+00:00",
            },
        ]

    def get_experiment(self, *, experiment_id: str) -> dict[str, Any]:
        assert experiment_id in {"older-public-id", "latest-public-id"}
        return {"task_runs": self.task_runs, "evaluation_runs": self.evaluations}

    def get_experiment_url(self, dataset_id: str, experiment_id: str) -> str:
        return f"http://127.0.0.1:6006/datasets/{dataset_id}/compare?experimentId={experiment_id}"


def _example(case_id: str, source: str, split: str, decisive: str, *, truncated: bool) -> dict[str, Any]:
    if source == "TraceElephant":
        trace = [
            {
                "step_id": 5,
                "agent_name": "WebSurfer",
                "output": {"messages": ["complete historical prompt must not reach UI"]},
            },
            {"step_id": 6, "agent_name": "Coder", "output": "ordinary"},
        ]
    else:
        trace = [
            {"trace_index": 0, "role": "Planner", "content": "plan"},
            {"trace_index": 1, "role": "WebSurfer", "content": "timeout error"},
        ]
    return {
        "id": f"example-{case_id}",
        "input": {
            "dataset_case_id": case_id,
            "source_dataset": source,
            "source_version": "revision-test",
            "source_case_id": f"source-{case_id}",
            "task": "Use sk-testsecret123456789 but do not reveal it <script>alert(1)</script>",
            "historical_trace": trace,
            "trace_origin": "imported_historical",
            "external_task_executed": False,
        },
        "output": {
            "expected_answer": "not exposed",
            "responsible_agent": "WebSurfer",
            "decisive_step": decisive,
            "failure_reason": "Bearer fake-token-value-123456 should stay private",
        },
        "metadata": {
            "source_split": split,
            "source_license": "test",
            "original_system": "test",
            "truncated": truncated,
        },
    }


def _fake_client(*, dataset_present: bool = True, experiment_present: bool = True) -> Any:
    examples = [
        _example("who-1", "Who&When", "Hand-Crafted", "1", truncated=False),
        _example("who-2", "Who&When", "Algorithm-Generated", "1", truncated=False),
        _example("trace-1", "TraceElephant", "assistant-bench", "99", truncated=True),
    ]
    task_runs: list[dict[str, Any]] = []
    evaluations: list[Any] = []
    for example in examples:
        case_id = example["input"]["dataset_case_id"]
        source = example["input"]["source_dataset"]
        truncated = example["metadata"]["truncated"]
        for repetition, method in enumerate(METHODS, start=1):
            abstain = method == "first_explicit_error" and case_id == "who-2"
            predicted_agent = None if abstain else ("Planner" if method == "rule_based" else "WebSurfer")
            predicted_step = None if abstain else ("1" if source == "Who&When" else "6")
            run_id = f"run-{case_id}-{method}"
            step_scorable = not truncated
            agent_score = 0.0 if abstain else float(predicted_agent == "WebSurfer")
            step_score = None if not step_scorable else (0.0 if abstain else float(predicted_step == "1"))
            joint_score = None if step_score is None else float(agent_score == 1 and step_score == 1)
            scores = {
                "attribution.agent_exact_match": agent_score,
                "attribution.step_exact_match": step_score,
                "attribution.joint_exact_match": joint_score,
                "attribution.step_distance": None if step_score is None or abstain else 0.0,
                "attribution.prediction_coverage": 0.0 if abstain else 1.0,
                "attribution.agent_scorable": 1.0,
                "attribution.step_scorable": float(step_scorable),
                "attribution.provenance_guard": 1.0,
            }
            task_runs.append(
                {
                    "id": run_id,
                    "dataset_example_id": example["id"],
                    "repetition_number": repetition,
                    "trace_id": None,
                    "output": {
                        "prediction": {
                            "predicted_agent": predicted_agent,
                            "predicted_step": predicted_step,
                            "reason": "deterministic evidence",
                            "confidence": 0.0 if abstain else 0.5,
                            "method": method,
                            "dataset_case_id": case_id,
                            "status": "abstained" if abstain else "predicted",
                            "evidence_step_ids": [] if abstain else [predicted_step],
                            "warnings": ["trace was truncated"] if truncated else [],
                        },
                        "metadata": {
                            "source": source,
                            "trace_origin": "imported_historical",
                            "executed_by_m1_local": False,
                            "external_task_executed": False,
                            "trace_original_step_count": 31 if truncated else 2,
                            "trace_retained_step_count": 2,
                            "trace_truncated": truncated,
                        },
                    },
                }
            )
            for name in EVALUATION_NAMES:
                evaluations.append(
                    SimpleNamespace(
                        experiment_run_id=run_id,
                        name=name,
                        result={
                            "score": scores[name],
                            "label": "not_scorable" if scores[name] is None else "test",
                            "explanation": "decisive_step_not_retained_due_to_truncation"
                            if not step_scorable
                            else "scored",
                        },
                        metadata={
                            "dataset_case_id": case_id,
                            "method": method,
                            "agent_scorable": True,
                            "step_scorable": step_scorable,
                            "decisive_step_present_in_trace": step_scorable,
                            "trace_truncated": truncated,
                        },
                    )
                )
    return SimpleNamespace(
        datasets=FakeDatasets(examples, present=dataset_present),
        experiments=FakeExperiments(
            task_runs, evaluations, present=experiment_present
        ),
    )


def _reader(**kwargs: Any) -> AttributionReadModel:
    fake = _fake_client(**kwargs)
    return AttributionReadModel("http://127.0.0.1:6006", client_factory=lambda: fake)


def test_summary_calculates_three_methods_and_excludes_none_denominators() -> None:
    summary = _reader().summary()
    assert summary.example_count == 3
    assert summary.task_run_count == 9
    assert summary.evaluation_count == 72
    assert summary.methods == METHODS
    first = next(item for item in summary.method_metrics if item.method == "first_explicit_error")
    assert first.agent_exact_match.denominator == 3
    assert first.step_exact_match.denominator == 2
    assert first.mean_step_distance.denominator == 1
    assert first.prediction_coverage.numerator == 2
    assert summary.experiment_id == "latest-public-id"


def test_cases_support_all_read_only_filters_and_statuses() -> None:
    reader = _reader()
    assert reader.cases().total == 9
    assert reader.cases(source="TraceElephant").total == 3
    assert reader.cases(method="rule_based").total == 3
    assert reader.cases(result_status="abstain").total == 1
    assert reader.cases(result_status="unscorable").total == 3
    assert reader.cases(agent_correct=False).total >= 1
    assert reader.cases(step_correct=True).total >= 1
    assert reader.cases(trace_truncated=True).total == 3
    assert reader.cases(limit=2).returned == 2


def test_case_detail_separates_prediction_truth_and_returns_only_safe_timeline() -> None:
    detail = _reader().case_detail("trace-1", "rule_based")
    payload = detail.model_dump(mode="json")
    encoded = str(payload)
    assert detail.predicted_agent == "Planner"
    assert detail.responsible_agent == "WebSurfer"
    assert detail.trace_truncated is True
    assert detail.step_scorable is False
    assert detail.result_status == "unscorable"
    assert detail.unscorable_reason == "decisive_step_not_retained_due_to_truncation"
    assert detail.steps and detail.steps[0].step_number == "5"
    assert "historical_trace" not in encoded
    assert "expected_answer" not in encoded
    assert "complete historical prompt" not in encoded
    assert "structured historical payload omitted" in encoded
    assert "sk-testsecret123456789" not in encoded
    assert "fake-token-value-123456" not in encoded


def test_read_model_uses_name_metadata_or_optional_config_not_a_fixed_opaque_id() -> None:
    source = inspect.getsource(AttributionReadModel)
    assert "RXhwZXJpbWVudDo" not in source
    fake = _fake_client()
    reader = AttributionReadModel(
        "http://127.0.0.1:6006",
        experiment_id="older-public-id",
        client_factory=lambda: fake,
    )
    assert reader.summary().experiment_id == "older-public-id"


def test_read_model_reuses_a_short_lived_public_api_snapshot() -> None:
    fake = _fake_client()
    factory_calls = 0

    def factory() -> Any:
        nonlocal factory_calls
        factory_calls += 1
        return fake

    reader = AttributionReadModel(
        "http://127.0.0.1:6006", client_factory=factory, cache_seconds=60
    )
    reader.summary()
    reader.cases(method="rule_based")
    reader.case_detail("trace-1", "rule_based")
    assert factory_calls == 1


def test_missing_dataset_experiment_and_unavailable_phoenix_are_distinct() -> None:
    with pytest.raises(AttributionDatasetNotFound):
        _reader(dataset_present=False).summary()
    with pytest.raises(AttributionExperimentNotFound):
        _reader(experiment_present=False).summary()
    reader = AttributionReadModel(
        "http://127.0.0.1:6006",
        client_factory=lambda: (_ for _ in ()).throw(RuntimeError("secret upstream detail")),
    )
    with pytest.raises(AttributionReadUnavailable, match="Phoenix attribution data is unavailable"):
        reader.summary()


def test_attribution_api_is_read_only_typed_filterable_and_sanitizes_errors(settings) -> None:
    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    app = create_app(settings, runtime, _reader())
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        summary = client.get("/api/attribution/summary")
        assert summary.status_code == 200 and summary.json()["task_run_count"] == 9
        cases = client.get(
            "/api/attribution/cases",
            params={"source": "Who&When", "method": "first_explicit_error", "limit": 2},
        )
        assert cases.status_code == 200 and cases.json()["returned"] == 2
        detail = client.get("/api/attribution/cases/who-1", params={"method": "rule_based"})
        assert detail.status_code == 200
        assert detail.json()["responsible_agent"] == "WebSurfer"
        assert client.get("/api/attribution/cases", params={"method": "invalid"}).status_code == 422
        csrf = client.get("/api/session").json()["csrf_token"]
        assert client.post(
            "/api/attribution/summary", headers={"x-m1-csrf": csrf}
        ).status_code == 405

    unavailable = AttributionReadModel(
        "http://127.0.0.1:6006",
        client_factory=lambda: (_ for _ in ()).throw(RuntimeError("Bearer private-upstream-value")),
    )
    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    app = create_app(settings, runtime, unavailable)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.get("/api/attribution/summary")
        assert response.status_code == 503
        assert response.json() == {"detail": "Phoenix attribution data is temporarily unavailable."}
        assert "private-upstream-value" not in response.text
