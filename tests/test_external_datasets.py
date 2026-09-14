from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from m1_local.external_datasets import (
    MAX_TRACE_ITEMS,
    adapt_traceelephant,
    adapt_who_and_when,
    validate_attachment_reference,
)
from m1_local.external_smoke import ensure_phoenix_dataset, run_offline_experiment


VERSION = "test-revision"


def write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def who_record(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "question_ID": "case-1",
        "question": "Find the failure.",
        "groundtruth": "expected",
        "history": [{
            "role": "WebSurfer",
            "content": "Bearer abc123, sk-abcdefghijk, Cookie: content-cookie",
            "cookie": "session-cookie",
            "password": "password-value",
        }],
        "mistake_agent": "WebSurfer",
        "mistake_step": "2",
        "mistake_reason": "opened the wrong page",
    }
    value.update(changes)
    return value


def test_who_and_when_schema_variants_and_redaction(tmp_path: Path) -> None:
    hand = write_json(tmp_path / "hand.json", who_record())
    algorithm = write_json(tmp_path / "algorithm.json", who_record(
        question_ID="case-2",
        groundtruth=None,
        ground_truth="8",
        system_prompt={"Excel_Expert": "token=topsecret"},
    ))

    first = adapt_who_and_when(
        hand, allowed_root=tmp_path, source_version=VERSION, source_split="Hand-Crafted"
    )
    second = adapt_who_and_when(
        algorithm, allowed_root=tmp_path, source_version=VERSION, source_split="Algorithm-Generated"
    )

    assert first.dataset_case_id == "who-and-when:case-1"
    assert first.original_system == "Magentic-One"
    assert first.expected_answer == "expected"
    assert "abc123" not in json.dumps(first.trace)
    assert "sk-abcdefghijk" not in json.dumps(first.trace)
    assert "content-cookie" not in json.dumps(first.trace)
    assert "session-cookie" not in json.dumps(first.trace)
    assert "password-value" not in json.dumps(first.trace)
    assert second.original_system == "CaptainAgent"
    assert second.expected_answer == "8"
    assert "topsecret" not in json.dumps(second.metadata)


def test_missing_source_id_is_deterministic_and_capability_is_explicit(tmp_path: Path) -> None:
    path = write_json(
        tmp_path / "missing.json",
        who_record(question_ID=None, mistake_agent=None, mistake_step=None, mistake_reason=None),
    )
    first = adapt_who_and_when(
        path, allowed_root=tmp_path, source_version=VERSION, source_split="Hand-Crafted"
    )
    second = adapt_who_and_when(
        path, allowed_root=tmp_path, source_version=VERSION, source_split="Hand-Crafted"
    )
    assert first.dataset_case_id == second.dataset_case_id
    assert first.metadata["capabilities"]["source_case_id_present"] is False
    assert first.metadata["capabilities"]["ground_truth_attribution"] is False
    assert first.ground_truth.responsible_agent is None


def test_traceelephant_mapping_and_bounded_trace(tmp_path: Path) -> None:
    metadata = write_json(tmp_path / "case" / "trace_metadata.json", {
        "run_id": "run-7",
        "task_instruction": "Research a museum price.",
        "system_name": "magentic-one",
        "ground_truth": "45",
        "mistake_agent": "WebSurfer",
        "mistake_step": "5",
        "mistake_reason": "failed to open the source",
    })
    steps = write_json(
        tmp_path / "case" / "step_records.json",
        [{"step_id": index, "agent_name": "Agent", "output": "ok"} for index in range(MAX_TRACE_ITEMS + 5)],
    )
    record = adapt_traceelephant(
        metadata,
        steps,
        allowed_root=tmp_path,
        source_version=VERSION,
        source_split="magentic-runs-assistant-bench",
    )
    assert record.dataset_case_id == "traceelephant:run-7"
    assert len(record.trace) == MAX_TRACE_ITEMS
    assert record.metadata["truncated"] is True
    assert record.ground_truth.decisive_step == "5"
    assert record.phoenix_example()["input"]["external_task_executed"] is False
    assert record.phoenix_example()["metadata"]["trace_origin"] == "imported_historical"


@pytest.mark.parametrize("reference", ["../secret.txt", "/absolute/file.txt", "C:\\secret.txt", "run.sh"])
def test_untrusted_attachment_rejected(reference: str) -> None:
    with pytest.raises(ValueError):
        validate_attachment_reference(reference)


def test_source_path_must_remain_inside_allowed_root(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = write_json(tmp_path / "outside.json", who_record())
    with pytest.raises(ValueError, match="escapes allowed root"):
        adapt_who_and_when(
            outside, allowed_root=allowed, source_version=VERSION, source_split="Hand-Crafted"
        )


class FakeDatasets:
    def __init__(self) -> None:
        self.dataset = None

    def list(self):
        return [] if self.dataset is None else [self.dataset]

    def create_dataset(self, *, name, examples, dataset_description):
        self.dataset = SimpleNamespace(
            name=name,
            id="dataset-id",
            version_id="version-id",
            examples=[{"id": f"generated-{index}", **item} for index, item in enumerate(examples)],
            uploaded=list(examples),
        )
        self.dataset.to_dict = lambda: {"id": self.dataset.id, "name": self.dataset.name, "version_id": self.dataset.version_id, "examples": self.dataset.examples}
        return self.dataset

    def get_dataset(self, *, dataset):
        assert dataset == self.dataset.name
        return self.dataset

    def add_examples_to_dataset(self, *, dataset, examples):
        dataset.examples.extend({"id": "generated", **item} for item in examples)
        return dataset


class FakeExperiments:
    def run_experiment(self, *, dataset, task, evaluators, **kwargs):
        outputs = []
        for item in dataset.examples:
            output = task(item["input"], item["output"])
            assert evaluators["provenance_guard"](output)[0] is True
            outputs.append(output)
        return SimpleNamespace(id="experiment-id", name=kwargs["experiment_name"], outputs=outputs)


def test_phoenix_dataset_reuse_and_zero_cost_experiment(tmp_path: Path) -> None:
    path = write_json(tmp_path / "record.json", who_record())
    record = adapt_who_and_when(
        path, allowed_root=tmp_path, source_version=VERSION, source_split="Hand-Crafted"
    )
    client = SimpleNamespace(datasets=FakeDatasets(), experiments=FakeExperiments())
    dataset, created = ensure_phoenix_dataset(client, [record])
    reused, created_again = ensure_phoenix_dataset(client, [record])
    assert created is True
    assert created_again is False
    assert reused is dataset
    assert dataset.examples[0]["input"]["dataset_case_id"] == record.dataset_case_id

    experiment = run_offline_experiment(client, dataset, {
        record.dataset_case_id: {"run_id": "local-run", "trace_id": "local-trace", "status": "completed"}
    })
    assert experiment.outputs[0]["external_task_executed"] is False
    assert experiment.outputs[0]["rule_based_gateway"] is True
