from __future__ import annotations

import copy
from .external_export import EXPORT_VIEW_VERSION, safe_export_example, safe_export_payload
import csv
import hashlib
import json
import statistics
import subprocess
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .attribution import (
    ATTRIBUTION_METHODS,
    AttributionGroundTruth,
    AttributionInput,
    AttributionMethodName,
    AttributionPrediction,
    assert_no_label_fields,
    build_attribution_views,
)
from .attribution_protocol import (
    BASELINE_SOURCE_COMMIT,
    BASELINE_VERSION,
    BENCHMARK_VERSION,
    DATASET_NAME,
    DATASET_REVISION,
    EXPERIMENT_NAME,
    METHODS,
    PROTOCOL_VERSION,
    REPOSITORY_REVISION,
    SOURCE_SPLITS,
    ProtocolConfig,
    benchmark_split_hash,
    canonical_json_bytes,
    canonicalize_agent,
    canonicalize_who_when_step,
    selection_hash,
    sha256_json,
)
from .external_datasets import ExternalTraceRecord, adapt_who_and_when


EVALUATION_NAMES = (
    "attribution.agent_exact_match",
    "attribution.step_exact_match",
    "attribution.joint_exact_match",
    "attribution.step_distance",
    "attribution.prediction_coverage",
    "attribution.agent_scorable",
    "attribution.step_scorable",
    "attribution.provenance_guard",
    "attribution.step_within_1",
    "attribution.step_within_2",
    "attribution.selective_agent_accuracy",
    "attribution.selective_step_accuracy",
    "attribution.abstain_rate",
)


class ManifestCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_order: int = Field(ge=0)
    dataset_case_id: str
    original_case_id: str
    source_split: Literal["Hand-Crafted", "Algorithm-Generated"]
    benchmark_split: Literal["Dev", "Frozen Test"]
    source_row_index: int = Field(ge=0)
    selection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    benchmark_split_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_trace_items: int = Field(ge=0)
    retained_trace_items: int = Field(ge=0)
    trace_truncated: bool
    decisive_step_retained: bool


class BenchmarkManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    benchmark_version: str
    protocol_version: str
    dataset_revision: str
    repository_revision: str
    selection_seed: str
    selection_algorithm: str
    split_algorithm: str
    license_policy: str
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_files: dict[str, dict[str, Any]]
    source_row_counts: dict[str, int]
    selected_counts: dict[str, int]
    benchmark_split_counts: dict[str, int]
    excluded_case_count: int = Field(ge=0)
    excluded_cases_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: tuple[ManifestCase, ...]
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_frozen_manifest(self) -> "BenchmarkManifest":
        if self.benchmark_version != BENCHMARK_VERSION:
            raise ValueError("benchmark version mismatch")
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError("protocol version mismatch")
        if self.dataset_revision != DATASET_REVISION:
            raise ValueError("dataset revision mismatch")
        if self.repository_revision != REPOSITORY_REVISION:
            raise ValueError("repository revision mismatch")
        if len(self.cases) != 30:
            raise ValueError("benchmark manifest must contain exactly 30 cases")
        if [case.run_order for case in self.cases] != list(range(30)):
            raise ValueError("manifest run_order must be contiguous and stable")
        if len({case.dataset_case_id for case in self.cases}) != 30:
            raise ValueError("manifest case IDs must be unique")
        if self.selected_counts != {"Algorithm-Generated": 20, "Hand-Crafted": 10}:
            raise ValueError("source split quotas changed")
        if self.benchmark_split_counts != {"Dev": 20, "Frozen Test": 10}:
            raise ValueError("benchmark split quotas changed")
        return self


class BenchmarkCaseEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_case_id: str
    original_case_id: str
    method: AttributionMethodName
    source_split: str
    benchmark_split: str
    prediction_status: str
    ground_truth_agent_raw: str | None
    ground_truth_agent_canonical: str | None
    predicted_agent_raw: str | None
    predicted_agent_canonical: str | None
    ground_truth_step_raw: str | None
    ground_truth_step_canonical: str | None
    ground_truth_step_position: int | None
    predicted_step_raw: str | None
    predicted_step_canonical: str | None
    predicted_step_position: int | None
    step_numbering_origin: str
    agent_scorable: bool
    step_scorable: bool
    trace_truncated: bool
    unscorable_reason: str | None
    metrics: dict[str, float | None]


class MetricAggregate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float | None
    numerator: float | None
    denominator: int = Field(ge=0)
    median: float | None = None


class GroupSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grouping: str
    group: dict[str, str]
    run_count: int = Field(ge=0)
    metrics: dict[str, MetricAggregate]


class BenchmarkResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    predictions: tuple[AttributionPrediction, ...]
    evaluations: tuple[BenchmarkCaseEvaluation, ...]
    summaries: tuple[GroupSummary, ...]
    confusion_matrix: tuple[dict[str, Any], ...]


def _field(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        result = getattr(value, name, None)
        if result is not None:
            return result
    return None


def _validate_source_row(row: Any, source_split: str, row_index: int) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(row, Mapping):
        return None, "row_not_object"
    copied = copy.deepcopy(dict(row))
    case_id = copied.get("question_ID")
    if not isinstance(case_id, str) or not case_id.strip():
        return None, "question_ID_missing"
    if not isinstance(copied.get("question"), str) or not copied["question"].strip():
        return None, "question_missing"
    history = copied.get("history")
    if not isinstance(history, list) or not history or not all(isinstance(item, Mapping) for item in history):
        return None, "history_invalid"
    if not isinstance(copied.get("mistake_agent"), str) or not copied["mistake_agent"].strip():
        return None, "mistake_agent_missing"
    raw_step = copied.get("mistake_step")
    if re.fullmatch(r"[0-9]+", str(raw_step).strip()) is None:
        return None, "mistake_step_not_nonnegative_integer"
    if source_split == "Hand-Crafted" and "groundtruth" not in copied:
        return None, "groundtruth_missing"
    if source_split == "Algorithm-Generated" and "ground_truth" not in copied:
        return None, "ground_truth_missing"
    return copied, None


def build_manifest_payload(
    rows_by_split: Mapping[str, Sequence[Any]],
    *,
    raw_root: Path,
    protocol_path: Path,
    source_files: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate all rows before deterministic selection and materialize only selected JSON."""
    protocol_payload = json.loads(protocol_path.read_text(encoding="utf-8"))
    protocol = ProtocolConfig.model_validate(protocol_payload)
    selected_root = raw_root / "selected"
    selected_root.mkdir(parents=True, exist_ok=True)
    excluded: list[dict[str, Any]] = []
    candidates: dict[str, list[tuple[str, int, str, dict[str, Any]]]] = {}
    for source_split in SOURCE_SPLITS:
        candidates[source_split] = []
        seen_ids: set[str] = set()
        for row_index, row in enumerate(rows_by_split[source_split]):
            valid, reason = _validate_source_row(row, source_split, row_index)
            raw_id = row.get("question_ID") if isinstance(row, Mapping) else None
            safe_id = str(raw_id) if raw_id is not None else f"row-{row_index}"
            if reason is not None or valid is None:
                excluded.append(
                    {"source_split": source_split, "source_row_index": row_index, "original_case_id": safe_id, "reason": reason}
                )
                continue
            original_case_id = str(valid["question_ID"]).strip()
            if original_case_id in seen_ids:
                excluded.append(
                    {"source_split": source_split, "source_row_index": row_index, "original_case_id": original_case_id, "reason": "duplicate_question_ID"}
                )
                continue
            seen_ids.add(original_case_id)
            candidates[source_split].append(
                (selection_hash(source_split, original_case_id), row_index, original_case_id, valid)
            )

    chosen: list[dict[str, Any]] = []
    for source_split in SOURCE_SPLITS:
        ordered = sorted(candidates[source_split], key=lambda item: (item[0], item[2]))
        quota = protocol.selection.sample_counts[source_split]
        if len(ordered) < quota:
            raise ValueError(f"not enough schema-valid rows for {source_split}: {len(ordered)} < {quota}")
        selected = ordered[:quota]
        dev_ids = {
            item[2]
            for item in sorted(
                selected,
                key=lambda item: (benchmark_split_hash(source_split, item[2]), item[2]),
            )[: protocol.selection.dev_counts[source_split]]
        }
        for select_hash, row_index, original_case_id, payload in selected:
            payload_hash = sha256_json(payload)
            path = selected_root / f"{payload_hash}.json"
            path.write_bytes(canonical_json_bytes(payload) + b"\n")
            record = adapt_who_and_when(
                path,
                allowed_root=raw_root,
                source_version=DATASET_REVISION,
                source_split=source_split,  # type: ignore[arg-type]
            )
            stable_case_id = f"who-and-when:{source_split.casefold()}:{original_case_id}"
            decisive = str(payload["mistake_step"]).strip()
            retained_ids = {str(step.get("trace_index")) for step in record.trace}
            chosen.append(
                {
                    "dataset_case_id": stable_case_id,
                    "original_case_id": original_case_id,
                    "source_split": source_split,
                    "benchmark_split": "Dev" if original_case_id in dev_ids else "Frozen Test",
                    "source_row_index": row_index,
                    "selection_sha256": select_hash,
                    "benchmark_split_sha256": benchmark_split_hash(source_split, original_case_id),
                    "payload_sha256": payload_hash,
                    "original_trace_items": int(record.metadata["original_trace_items"]),
                    "retained_trace_items": int(record.metadata["retained_trace_items"]),
                    "trace_truncated": bool(record.metadata["truncated"]),
                    "decisive_step_retained": decisive in retained_ids,
                }
            )
    chosen.sort(key=lambda item: (SOURCE_SPLITS.index(item["source_split"]), item["selection_sha256"], item["original_case_id"]))
    for index, item in enumerate(chosen):
        item["run_order"] = index
    selected_counts = dict(sorted(Counter(item["source_split"] for item in chosen).items()))
    split_counts = dict(sorted(Counter(item["benchmark_split"] for item in chosen).items()))
    excluded_digest = sha256_json(excluded)
    payload: dict[str, Any] = {
        "benchmark_version": BENCHMARK_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "dataset_revision": DATASET_REVISION,
        "repository_revision": REPOSITORY_REVISION,
        "selection_seed": protocol.selection.seed,
        "selection_algorithm": protocol.selection.algorithm,
        "split_algorithm": protocol.selection.split_algorithm,
        "license_policy": protocol.source.license_policy,
        "protocol_sha256": sha256_json(protocol_payload),
        "source_files": {key: dict(value) for key, value in sorted(source_files.items())},
        "source_row_counts": {key: len(rows_by_split[key]) for key in sorted(rows_by_split)},
        "selected_counts": selected_counts,
        "benchmark_split_counts": split_counts,
        "excluded_case_count": len(excluded),
        "excluded_cases_sha256": excluded_digest,
        "cases": chosen,
    }
    payload["manifest_sha256"] = sha256_json(payload)
    BenchmarkManifest.model_validate(payload)
    return payload, excluded


def load_manifest(path: Path, *, protocol_path: Path) -> BenchmarkManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    claimed = payload.get("manifest_sha256")
    unhashed = dict(payload)
    unhashed.pop("manifest_sha256", None)
    if claimed != sha256_json(unhashed):
        raise ValueError("manifest SHA-256 mismatch")
    protocol_payload = json.loads(protocol_path.read_text(encoding="utf-8"))
    protocol = ProtocolConfig.model_validate(protocol_payload)
    if payload.get("protocol_sha256") != sha256_json(protocol_payload):
        raise ValueError("protocol file SHA-256 mismatch")
    manifest = BenchmarkManifest.model_validate(payload)
    if Counter(case.source_split for case in manifest.cases) != manifest.selected_counts:
        raise ValueError("manifest actual source counts mismatch")
    if Counter(case.benchmark_split for case in manifest.cases) != manifest.benchmark_split_counts:
        raise ValueError("manifest actual benchmark counts mismatch")
    if (manifest.selection_seed, manifest.selection_algorithm, manifest.split_algorithm, manifest.license_policy) != (
        protocol.selection.seed, protocol.selection.algorithm, protocol.selection.split_algorithm, protocol.source.license_policy,
    ):
        raise ValueError("manifest protocol semantics mismatch")
    for split in SOURCE_SPLITS:
        cases = [case for case in manifest.cases if case.source_split == split]
        ordered = sorted(cases, key=lambda case: (benchmark_split_hash(split, case.original_case_id), case.original_case_id))
        dev_ids = {case.original_case_id for case in ordered[:protocol.selection.dev_counts[split]]}
        for case in cases:
            if case.dataset_case_id != f"who-and-when:{split.casefold()}:{case.original_case_id}":
                raise ValueError("manifest case identity mismatch")
            if case.selection_sha256 != selection_hash(split, case.original_case_id) or case.benchmark_split_sha256 != benchmark_split_hash(split, case.original_case_id):
                raise ValueError("manifest deterministic hash mismatch")
            if case.benchmark_split != ("Dev" if case.original_case_id in dev_ids else "Frozen Test"):
                raise ValueError("manifest deterministic benchmark split mismatch")
    return manifest


def verify_source_receipt(manifest: BenchmarkManifest, raw_root: Path) -> dict[str, Any]:
    receipt = json.loads((raw_root / "download-receipt.json").read_text(encoding="utf-8"))
    if receipt.get("dataset_revision") != DATASET_REVISION or receipt.get("repository_revision") != REPOSITORY_REVISION:
        raise ValueError("download receipt revision mismatch")
    by_name = {str(item["name"]): item for item in receipt.get("files", [])}
    for name, expected in manifest.source_files.items():
        item = by_name.get(name)
        if item is None or item.get("sha256") != expected.get("sha256") or item.get("bytes") != expected.get("bytes"):
            raise ValueError(f"download receipt mismatch for {name}")
        path = raw_root / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected.get("sha256") or path.stat().st_size != expected.get("bytes"):
            raise ValueError(f"source file hash/size mismatch for {name}")
    return receipt


def verify_frozen_baseline(project_root: Path) -> None:
    relative = "src/m1_local/attribution.py"
    expected = subprocess.run(
        ["git", "show", f"{BASELINE_SOURCE_COMMIT}:{relative}"],
        cwd=project_root,
        check=True,
        capture_output=True,
    ).stdout
    current = (project_root / relative).read_bytes()
    if current.replace(b"\r\n", b"\n") != expected.replace(b"\r\n", b"\n"):
        raise ValueError("frozen deterministic attribution baseline changed")


def load_benchmark_records(
    manifest: BenchmarkManifest,
    *,
    raw_root: Path,
) -> list[tuple[ManifestCase, ExternalTraceRecord, AttributionInput, AttributionGroundTruth]]:
    result = []
    for case in manifest.cases:
        path = raw_root / "selected" / f"{case.payload_sha256}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if sha256_json(payload) != case.payload_sha256:
            raise ValueError(f"selected payload hash mismatch: {case.dataset_case_id}")
        record = adapt_who_and_when(
            path,
            allowed_root=raw_root,
            source_version=DATASET_REVISION,
            source_split=case.source_split,
        ).model_copy(
            update={
                "dataset_case_id": case.dataset_case_id,
                "source_license": "local research use only; redistribution not authorized",
            }
        )
        if record.source_case_id != case.original_case_id:
            raise ValueError(f"selected payload identity mismatch: {case.dataset_case_id}")
        inference, labels = build_attribution_views(record)
        assert_no_label_fields(inference)
        result.append((case, record, inference, labels))
    return result


def evaluate_benchmark_prediction(
    case: ManifestCase,
    inference: AttributionInput,
    labels: AttributionGroundTruth,
    prediction: AttributionPrediction,
) -> BenchmarkCaseEvaluation:
    prediction_before = copy.deepcopy(prediction.model_dump(mode="python"))
    if len({case.dataset_case_id, inference.dataset_case_id, labels.dataset_case_id, prediction.dataset_case_id}) != 1:
        raise ValueError("case identity mismatch")
    if (
        inference.source != labels.source or inference.source != "Who&When"
        or inference.revision != labels.revision or inference.revision != DATASET_REVISION
        or inference.source_case_id != labels.source_case_id or inference.source_case_id != case.original_case_id
        or inference.source_split != case.source_split
    ):
        raise ValueError("case provenance mismatch")
    if prediction.status == "error":
        raise ValueError("frozen predictor returned error status")
    gt_agent = canonicalize_agent(labels.responsible_agent)
    pred_agent = canonicalize_agent(prediction.predicted_agent)
    agent_scorable = bool(labels.agent_scorable and gt_agent is not None)
    gt_step = canonicalize_who_when_step(
        labels.decisive_step, trace_length=inference.trace_retained_step_count
    )
    pred_step = canonicalize_who_when_step(
        prediction.predicted_step, trace_length=inference.trace_retained_step_count
    )
    step_scorable = bool(labels.step_scorable and gt_step.scorable)
    predicted = prediction.status == "predicted"
    abstained = prediction.status == "abstained"
    agent_exact = float(predicted and pred_agent == gt_agent) if agent_scorable else None
    if step_scorable:
        step_exact = float(predicted and pred_step.scorable and pred_step.position == gt_step.position)
        distance = (
            float(abs(pred_step.position - gt_step.position))
            if predicted and pred_step.scorable and pred_step.position is not None and gt_step.position is not None
            else None
        )
        within_1 = float(distance is not None and distance <= 1)
        within_2 = float(distance is not None and distance <= 2)
    else:
        step_exact = distance = within_1 = within_2 = None
    joint = (
        float(agent_exact == 1.0 and step_exact == 1.0)
        if agent_exact is not None and step_exact is not None
        else None
    )
    metrics: dict[str, float | None] = {
        "agent_exact_match": agent_exact,
        "step_exact_match": step_exact,
        "joint_exact_match": joint,
        "step_distance": distance,
        "prediction_coverage": float(predicted),
        "agent_scorable": float(agent_scorable),
        "step_scorable": float(step_scorable),
        "provenance_guard": float(
            inference.trace_origin == "imported_historical"
            and inference.executed_by_m1_local is False
            and inference.external_task_executed is False
        ),
        "step_within_1": within_1,
        "step_within_2": within_2,
        "selective_agent_accuracy": agent_exact if agent_scorable and predicted else None,
        "selective_step_accuracy": step_exact if step_scorable and predicted else None,
        "abstain_rate": float(abstained),
    }
    if prediction.model_dump(mode="python") != prediction_before:
        raise RuntimeError("benchmark evaluator mutated a prediction")
    reason = labels.step_unscorable_reason if not step_scorable else pred_step.reason if predicted and not pred_step.scorable else None
    return BenchmarkCaseEvaluation(
        dataset_case_id=case.dataset_case_id,
        original_case_id=case.original_case_id,
        method=prediction.method,
        source_split=case.source_split,
        benchmark_split=case.benchmark_split,
        prediction_status=prediction.status,
        ground_truth_agent_raw=labels.responsible_agent,
        ground_truth_agent_canonical=gt_agent,
        predicted_agent_raw=prediction.predicted_agent,
        predicted_agent_canonical=pred_agent,
        ground_truth_step_raw=labels.decisive_step,
        ground_truth_step_canonical=gt_step.canonical,
        ground_truth_step_position=gt_step.position,
        predicted_step_raw=prediction.predicted_step,
        predicted_step_canonical=pred_step.canonical,
        predicted_step_position=pred_step.position,
        step_numbering_origin=gt_step.numbering_origin,
        agent_scorable=agent_scorable,
        step_scorable=step_scorable,
        trace_truncated=inference.trace_truncated,
        unscorable_reason=reason,
        metrics=metrics,
    )


def _aggregate(evaluations: Sequence[BenchmarkCaseEvaluation], metric: str) -> MetricAggregate:
    values = [item.metrics[metric] for item in evaluations if item.metrics[metric] is not None]
    if not values:
        return MetricAggregate(value=None, numerator=None, denominator=0, median=None)
    numeric = [float(value) for value in values if value is not None]
    total = float(sum(numeric))
    return MetricAggregate(
        value=total / len(numeric),
        numerator=total,
        denominator=len(numeric),
        median=float(statistics.median(numeric)) if metric == "step_distance" else None,
    )


def _group_summary(grouping: str, group: dict[str, str], items: Sequence[BenchmarkCaseEvaluation]) -> GroupSummary:
    return GroupSummary(
        grouping=grouping,
        group=group,
        run_count=len(items),
        metrics={name.removeprefix("attribution."): _aggregate(items, name.removeprefix("attribution.")) for name in EVALUATION_NAMES},
    )


def summarize_benchmark(evaluations: Sequence[BenchmarkCaseEvaluation]) -> tuple[GroupSummary, ...]:
    result = [_group_summary("overall", {"all": "all"}, evaluations)]
    dimensions = (
        ("method", ("method",)),
        ("benchmark_split", ("benchmark_split",)),
        ("source_split", ("source_split",)),
        ("method_x_benchmark_split", ("method", "benchmark_split")),
        ("method_x_source_split", ("method", "source_split")),
    )
    for grouping, fields in dimensions:
        keys = sorted({tuple(str(getattr(item, field)) for field in fields) for item in evaluations})
        for values in keys:
            group = dict(zip(fields, values, strict=True))
            selected = [item for item in evaluations if all(str(getattr(item, field)) == value for field, value in group.items())]
            result.append(_group_summary(grouping, group, selected))
    return tuple(result)


def build_confusion_matrix(evaluations: Sequence[BenchmarkCaseEvaluation]) -> tuple[dict[str, Any], ...]:
    counts: Counter[tuple[str, str, str, str, str]] = Counter()
    for item in evaluations:
        if not item.agent_scorable or item.ground_truth_agent_canonical is None:
            continue
        predicted = item.predicted_agent_canonical
        if item.prediction_status == "abstained":
            predicted = "__ABSTAIN__"
        elif predicted is None:
            predicted = "__ERROR__"
        counts[(item.method, item.benchmark_split, item.source_split, item.ground_truth_agent_canonical, predicted)] += 1
    return tuple(
        {
            "method": key[0],
            "benchmark_split": key[1],
            "source_split": key[2],
            "ground_truth_agent": key[3],
            "predicted_agent": key[4],
            "count": count,
        }
        for key, count in sorted(counts.items())
    )


def run_benchmark(
    records: Sequence[tuple[ManifestCase, ExternalTraceRecord, AttributionInput, AttributionGroundTruth]],
) -> BenchmarkResult:
    predictions: list[AttributionPrediction] = []
    evaluations: list[BenchmarkCaseEvaluation] = []
    for case, _record, inference, labels in records:
        for method_name in METHODS:
            prediction = ATTRIBUTION_METHODS[method_name](inference)
            predictions.append(prediction)
            evaluations.append(evaluate_benchmark_prediction(case, inference, labels, prediction))
    if len(predictions) != 90:
        raise ValueError(f"expected exactly 90 frozen baseline runs, found {len(predictions)}")
    return BenchmarkResult(
        predictions=tuple(predictions),
        evaluations=tuple(evaluations),
        summaries=summarize_benchmark(evaluations),
        confusion_matrix=build_confusion_matrix(evaluations),
    )


def benchmark_phoenix_examples(
    records: Sequence[tuple[ManifestCase, ExternalTraceRecord, AttributionInput, AttributionGroundTruth]],
    *,
    manifest_sha256: str,
    secrets: Sequence[str] = (),
) -> list[dict[str, Any]]:
    examples = []
    for case, record, inference, _labels in records:
        item = safe_export_example(record.phoenix_example(), secrets=secrets)
        assert_no_label_fields(item["input"])
        item["input"].update(
            {
                "benchmark_version": BENCHMARK_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "benchmark_split": case.benchmark_split,
                "manifest_sha256": manifest_sha256,
            }
        )
        item["metadata"].update(
            {
                "benchmark_version": BENCHMARK_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "benchmark_split": case.benchmark_split,
                "manifest_sha256": manifest_sha256,
                "baseline_version": BASELINE_VERSION,
            }
        )
        assert_no_label_fields(inference)
        examples.append(item)
    return examples


def ensure_benchmark_dataset(
    client: Any,
    records: Sequence[tuple[ManifestCase, ExternalTraceRecord, AttributionInput, AttributionGroundTruth]],
    *,
    manifest_sha256: str,
    name: str = DATASET_NAME,
    secrets: Sequence[str] = (),
) -> tuple[Any, bool, str]:
    examples = benchmark_phoenix_examples(records, manifest_sha256=manifest_sha256, secrets=secrets)
    existing = next((item for item in client.datasets.list() if _field(item, "name") == name), None)
    effective_name = name
    if existing is not None:
        dataset = client.datasets.get_dataset(dataset=name)
        existing_examples = list(_field(dataset, "examples") or ())
        existing_ids = {str(_field(example, "input").get("dataset_case_id")) for example in existing_examples}
        existing_hashes = {str(_field(example, "input").get("manifest_sha256")) for example in existing_examples}
        expected_ids = {case.dataset_case_id for case, *_ in records}
        if existing_ids == expected_ids and existing_hashes == {manifest_sha256}:
            return dataset, False, effective_name
        effective_name = f"{name}-{manifest_sha256[:12]}"
        existing = next((item for item in client.datasets.list() if _field(item, "name") == effective_name), None)
        if existing is not None:
            dataset = client.datasets.get_dataset(dataset=effective_name)
            existing_examples = list(_field(dataset, "examples") or ())
            existing_ids = {str(_field(example, "input").get("dataset_case_id")) for example in existing_examples}
            existing_hashes = {str(_field(example, "input").get("manifest_sha256")) for example in existing_examples}
            if existing_ids != expected_ids or existing_hashes != {manifest_sha256}:
                raise ValueError("versioned Phoenix benchmark dataset does not match manifest")
            return dataset, False, effective_name
    dataset = client.datasets.create_dataset(
        name=effective_name,
        examples=examples,
        dataset_description=(
            "Pinned Who&When attribution benchmark. Inputs are label-free imported histories; outputs contain evaluator-only labels."
        ),
    )
    return dataset, True, effective_name


def _evaluation_payload(name: str, evaluation: BenchmarkCaseEvaluation) -> tuple[float | None, str]:
    metric = name.removeprefix("attribution.")
    score = evaluation.metrics[metric]
    if score is None:
        return None, "not_applicable"
    if metric == "step_distance":
        return score, "measured"
    if metric in {"prediction_coverage", "agent_scorable", "step_scorable", "provenance_guard"}:
        return score, "yes" if score else "no"
    if metric == "abstain_rate":
        return score, "abstained" if score else "not_abstained"
    return score, "match" if score else "mismatch"


def log_benchmark_to_phoenix(
    client: Any,
    dataset: Any,
    result: BenchmarkResult,
    *,
    manifest: BenchmarkManifest,
    experiment_name: str = EXPERIMENT_NAME,
    secrets: Sequence[str] = (),
) -> dict[str, Any]:
    export_predictions = {(item.dataset_case_id, item.method): safe_export_payload(item.model_dump(mode="json"), secrets=secrets) for item in result.predictions}
    export_evaluations = {(item.dataset_case_id, item.method): safe_export_payload(item.model_dump(mode="json"), secrets=secrets) for item in result.evaluations}
    safe_export_payload({"name": experiment_name, "dataset_id": _field(dataset, "id"), "version_id": _field(dataset, "version_id")}, secrets=secrets)
    examples = list(_field(dataset, "examples") or ())
    case_to_example = {
        str(_field(example, "input").get("dataset_case_id")): str(_field(example, "id", "example_id", "dataset_example_id"))
        for example in examples
    }
    expected_ids = {item.dataset_case_id for item in result.predictions}
    safe_export_payload({"example_ids": list(case_to_example.values())}, secrets=secrets)
    if set(case_to_example) != expected_ids:
        raise ValueError("Phoenix dataset does not exactly match benchmark cases")
    dataset_id = str(_field(dataset, "id"))
    version_id = _field(dataset, "version_id", "dataset_version_id")
    experiment = client.experiments.create(
        dataset_id=dataset_id,
        dataset_version_id=str(version_id) if version_id else None,
        experiment_name=experiment_name,
        experiment_description="Frozen zero-cost deterministic attribution baselines under attribution-eval-v1.",
        experiment_metadata={
            "benchmark_version": BENCHMARK_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "manifest_sha256": manifest.manifest_sha256,
            "dataset_revision": DATASET_REVISION,
            "repository_revision": REPOSITORY_REVISION,
            "baseline_version": BASELINE_VERSION,
            "baseline_source_commit": BASELINE_SOURCE_COMMIT,
            "methods": list(METHODS),
            "sample_count": 30,
            "run_count": 90,
            "paid_model_calls": 0,
            "real_websurfer_calls": 0,
            "external_task_executed": False,
        },
        repetitions=3,
    )
    experiment_id = str(_field(experiment, "id", "experiment_id"))
    evaluation_by_key = {(item.dataset_case_id, item.method): item for item in result.evaluations}
    run_ids = []
    evaluation_count = 0
    now = datetime.now(timezone.utc)
    for prediction in result.predictions:
        evaluation = evaluation_by_key[(prediction.dataset_case_id, prediction.method)]
        repetition = METHODS.index(prediction.method) + 1
        output = {
            "prediction": export_predictions[(prediction.dataset_case_id, prediction.method)],
            "metadata": {
                "dataset_case_id": prediction.dataset_case_id,
                "method": prediction.method,
                "source_split": evaluation.source_split,
                "benchmark_split": evaluation.benchmark_split,
                "benchmark_version": BENCHMARK_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "manifest_sha256": manifest.manifest_sha256,
                "baseline_version": BASELINE_VERSION,
                "baseline_source_commit": BASELINE_SOURCE_COMMIT,
                "trace_origin": "imported_historical",
                "executed_by_m1_local": False,
                "external_task_executed": False,
                "export_view_version": EXPORT_VIEW_VERSION,
            },
        }
        assert_no_label_fields(output)
        run = client.experiments.log_run(
            experiment_id=experiment_id,
            dataset_example_id=case_to_example[prediction.dataset_case_id],
            output=output,
            start_time=now,
            end_time=datetime.now(timezone.utc),
            repetition_number=repetition,
        )
        run_id = str(_field(run, "id", "experiment_run_id"))
        run_ids.append(run_id)
        for name in EVALUATION_NAMES:
            score, label = _evaluation_payload(name, evaluation)
            client.experiments.log_evaluation(
                experiment_run_id=run_id,
                name=name,
                annotator_kind="CODE",
                score=score,
                label=label,
                explanation=export_evaluations[(evaluation.dataset_case_id, evaluation.method)]["unscorable_reason"] or evaluation.prediction_status,
                metadata={
                    "dataset_case_id": evaluation.dataset_case_id,
                    "method": evaluation.method,
                    "source_split": evaluation.source_split,
                    "benchmark_split": evaluation.benchmark_split,
                    "manifest_sha256": manifest.manifest_sha256,
                    "baseline_version": BASELINE_VERSION,
                    "agent_scorable": evaluation.agent_scorable,
                    "step_scorable": evaluation.step_scorable,
                    "trace_truncated": evaluation.trace_truncated,
                    "trace_origin": "imported_historical",
                    "executed_by_m1_local": False,
                    "external_task_executed": False,
                },
            )
            evaluation_count += 1
    return {
        "experiment_id": experiment_id,
        "experiment_name": experiment_name,
        "run_ids": run_ids,
        "run_count": len(run_ids),
        "evaluation_count": evaluation_count,
        "evaluation_names": list(EVALUATION_NAMES),
    }


def write_reports(result: BenchmarkResult, *, report_root: Path, aggregate_path: Path, metadata: Mapping[str, Any]) -> None:
    report_root.mkdir(parents=True, exist_ok=True)
    aggregate_path.parent.mkdir(parents=True, exist_ok=True)
    predictions = [item.model_dump(mode="json") for item in result.predictions]
    evaluations = [item.model_dump(mode="json") for item in result.evaluations]
    summaries = [item.model_dump(mode="json") for item in result.summaries]
    aggregate = {**dict(metadata), "summaries": summaries, "confusion_matrix": list(result.confusion_matrix)}
    for path, payload in (
        (report_root / "predictions.json", predictions),
        (report_root / "evaluations.json", evaluations),
        (report_root / "summary.json", aggregate),
    ):
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (report_root / "confusion_matrix.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("method", "benchmark_split", "source_split", "ground_truth_agent", "predicted_agent", "count"),
        )
        writer.writeheader()
        writer.writerows(result.confusion_matrix)
    aggregate_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
