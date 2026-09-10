from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from m1_local.attribution import AttributionPrediction, build_attribution_views
from m1_local.attribution_benchmark import (
    EVALUATION_NAMES,
    BenchmarkManifest,
    ManifestCase,
    benchmark_phoenix_examples,
    build_manifest_payload,
    evaluate_benchmark_prediction,
    load_manifest,
    log_benchmark_to_phoenix,
    run_benchmark,
)
from m1_local.attribution_protocol import sha256_json
from m1_local.external_datasets import AttributionGroundTruth as ExternalGroundTruth
from m1_local.external_datasets import ExternalTraceRecord


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = PROJECT_ROOT / "configs" / "attribution_eval_v1.json"
MANIFEST_PATH = PROJECT_ROOT / "datasets" / "manifests" / "who_when_benchmark_v1.json"


def _row(index: int, *, source_split: str, case_id: str | None = None) -> dict[str, Any]:
    return {
        "question": f"synthetic question {source_split} {index}",
        "question_ID": case_id or f"{source_split}-{index:03d}",
        "history": [
            {"role": "Orchestrator", "content": "plan"},
            {"role": "WebSurfer", "content": "failed with timeout"},
            {"role": "Coder", "content": "final answer"},
        ],
        "mistake_agent": "WebSurfer",
        "mistake_step": "1",
        "mistake_reason": "test-only reason",
        ("groundtruth" if source_split == "Hand-Crafted" else "ground_truth"): "answer",
    }


def test_manifest_selection_is_deterministic_and_preserves_cross_split_ids(tmp_path: Path) -> None:
    rows = {
        "Hand-Crafted": [_row(i, source_split="Hand-Crafted") for i in range(12)],
        "Algorithm-Generated": [_row(i, source_split="Algorithm-Generated") for i in range(22)],
    }
    rows["Hand-Crafted"][0]["question_ID"] = "shared-id"
    rows["Algorithm-Generated"][0]["question_ID"] = "shared-id"
    first, excluded = build_manifest_payload(
        rows,
        raw_root=tmp_path / "raw-1",
        protocol_path=PROTOCOL_PATH,
        source_files={},
    )
    second, _ = build_manifest_payload(
        copy.deepcopy(rows),
        raw_root=tmp_path / "raw-2",
        protocol_path=PROTOCOL_PATH,
        source_files={},
    )
    assert first == second
    assert excluded == []
    assert first["selected_counts"] == {"Algorithm-Generated": 20, "Hand-Crafted": 10}
    assert first["benchmark_split_counts"] == {"Dev": 20, "Frozen Test": 10}
    ids = [item["dataset_case_id"] for item in first["cases"]]
    assert len(ids) == len(set(ids)) == 30
    assert all("question" not in item and "history" not in item and "mistake_reason" not in item for item in first["cases"])


def test_manifest_schema_filter_runs_before_selection_and_records_reason(tmp_path: Path) -> None:
    rows = {
        "Hand-Crafted": [_row(i, source_split="Hand-Crafted") for i in range(11)],
        "Algorithm-Generated": [_row(i, source_split="Algorithm-Generated") for i in range(21)],
    }
    rows["Hand-Crafted"].append({"question_ID": "bad", "question": "bad", "history": []})
    manifest, excluded = build_manifest_payload(
        rows,
        raw_root=tmp_path / "raw",
        protocol_path=PROTOCOL_PATH,
        source_files={},
    )
    assert manifest["excluded_case_count"] == 1
    assert excluded[0]["reason"] == "history_invalid"
    assert manifest["excluded_cases_sha256"] == sha256_json(excluded)


def test_checked_in_manifest_hash_and_safe_metadata_are_valid() -> None:
    manifest = load_manifest(MANIFEST_PATH, protocol_path=PROTOCOL_PATH)
    assert len(manifest.cases) == 30
    assert manifest.selected_counts == {"Algorithm-Generated": 20, "Hand-Crafted": 10}
    raw = MANIFEST_PATH.read_text(encoding="utf-8")
    assert '"history"' not in raw
    assert '"mistake_reason"' not in raw
    assert '"question"' not in raw


def test_manifest_hash_tampering_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["cases"][0]["source_row_index"] += 1
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest SHA-256 mismatch"):
        load_manifest(path, protocol_path=PROTOCOL_PATH)


def _record(index: int, *, source_split: str = "Hand-Crafted", decisive_step: str = "1", truncated: bool = False):
    case_id = f"who-and-when:{source_split.casefold()}:{index}"
    record = ExternalTraceRecord(
        dataset_case_id=case_id,
        source_dataset="Who&When",
        source_version="59b9fcba1aaed7bbf206b5f4d3c68b8face2f49c",
        source_split=source_split,
        source_case_id=str(index),
        source_license="test-only",
        task="test task",
        expected_answer="answer",
        original_system="test",
        trace=[
            {"trace_index": 0, "role": "Orchestrator", "content": "plan"},
            {"trace_index": 1, "role": "WebSurfer", "content": "failed with timeout"},
            {"trace_index": 2, "role": "Coder", "content": "final answer"},
        ],
        ground_truth=ExternalGroundTruth(
            responsible_agent=" WebSurfer ",
            decisive_step=decisive_step,
            failure_reason="test",
        ),
        metadata={
            "original_trace_items": 10 if truncated else 3,
            "retained_trace_items": 3,
            "truncated": truncated,
        },
    )
    inference, labels = build_attribution_views(record)
    case = ManifestCase(
        run_order=index,
        dataset_case_id=case_id,
        original_case_id=str(index),
        source_split=source_split,
        benchmark_split="Dev" if index < 20 else "Frozen Test",
        source_row_index=index,
        selection_sha256="a" * 64,
        benchmark_split_sha256="b" * 64,
        payload_sha256="c" * 64,
        original_trace_items=inference.trace_original_step_count,
        retained_trace_items=inference.trace_retained_step_count,
        trace_truncated=inference.trace_truncated,
        decisive_step_retained=labels.decisive_step_present_in_trace,
    )
    return case, record, inference, labels


def test_abstain_overall_and_selective_denominators_are_distinct() -> None:
    case, _record_value, inference, labels = _record(0)
    prediction = AttributionPrediction(
        method="rule_based",
        dataset_case_id=case.dataset_case_id,
        status="abstained",
        reason="no signal",
        confidence=0,
    )
    evaluation = evaluate_benchmark_prediction(case, inference, labels, prediction)
    assert evaluation.metrics["prediction_coverage"] == 0
    assert evaluation.metrics["abstain_rate"] == 1
    assert evaluation.metrics["agent_exact_match"] == 0
    assert evaluation.metrics["step_exact_match"] == 0
    assert evaluation.metrics["step_distance"] is None
    assert evaluation.metrics["selective_agent_accuracy"] is None
    assert evaluation.metrics["selective_step_accuracy"] is None


def test_step_tolerance_uses_exact_zero_based_distance_without_offset_choice() -> None:
    case, _record_value, inference, labels = _record(0)
    prediction = AttributionPrediction(
        method="rule_based",
        dataset_case_id=case.dataset_case_id,
        status="predicted",
        reason="test",
        confidence=0.5,
        predicted_agent="websurfer",
        predicted_step="2",
    )
    evaluation = evaluate_benchmark_prediction(case, inference, labels, prediction)
    assert evaluation.metrics["agent_exact_match"] == 1
    assert evaluation.metrics["step_exact_match"] == 0
    assert evaluation.metrics["step_distance"] == 1
    assert evaluation.metrics["step_within_1"] == 1
    assert evaluation.metrics["step_within_2"] == 1


def test_truncation_excludes_step_metrics_but_keeps_agent_scorable() -> None:
    case, _record_value, inference, labels = _record(0, decisive_step="9", truncated=True)
    prediction = AttributionPrediction(
        method="rule_based",
        dataset_case_id=case.dataset_case_id,
        status="predicted",
        reason="test",
        confidence=0.5,
        predicted_agent="WebSurfer",
        predicted_step="1",
    )
    evaluation = evaluate_benchmark_prediction(case, inference, labels, prediction)
    assert evaluation.agent_scorable is True
    assert evaluation.step_scorable is False
    assert evaluation.metrics["agent_exact_match"] == 1
    assert evaluation.metrics["step_exact_match"] is None
    assert evaluation.metrics["step_within_1"] is None
    assert evaluation.metrics["step_distance"] is None


@dataclass
class _Example:
    id: str
    input: dict[str, Any]


@dataclass
class _Dataset:
    id: str
    version_id: str
    examples: list[_Example]


class _Experiments:
    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.evaluations: list[dict[str, Any]] = []

    def create(self, **_kwargs: Any) -> dict[str, str]:
        return {"id": "experiment-1"}

    def log_run(self, **kwargs: Any) -> dict[str, str]:
        self.runs.append(kwargs)
        return {"id": f"run-{len(self.runs)}"}

    def log_evaluation(self, **kwargs: Any) -> dict[str, str]:
        self.evaluations.append(kwargs)
        return {"id": f"evaluation-{len(self.evaluations)}"}


class _Client:
    def __init__(self) -> None:
        self.experiments = _Experiments()


def test_full_matrix_is_30_by_3_and_phoenix_payloads_remain_label_free() -> None:
    records = [_record(index, source_split="Hand-Crafted" if index < 10 else "Algorithm-Generated") for index in range(30)]
    result = run_benchmark(records)
    assert len(result.predictions) == 90
    assert len(result.evaluations) == 90
    assert result.summaries[0].run_count == 90
    examples = benchmark_phoenix_examples(records, manifest_sha256="d" * 64)
    dataset = _Dataset(
        id="dataset-1",
        version_id="version-1",
        examples=[_Example(id=f"example-{i}", input=item["input"]) for i, item in enumerate(examples)],
    )
    client = _Client()
    manifest_payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest = BenchmarkManifest.model_validate(manifest_payload)
    logged = log_benchmark_to_phoenix(client, dataset, result, manifest=manifest)
    assert logged["run_count"] == 90
    assert logged["evaluation_count"] == 90 * len(EVALUATION_NAMES)
    assert all("responsible_agent" not in str(item["output"]) for item in client.experiments.runs)
    assert all(item["name"].startswith("attribution.") for item in client.experiments.evaluations)
