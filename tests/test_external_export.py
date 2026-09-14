from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from phoenix.client.resources.datasets import Dataset

from m1_local.external_datasets import AttributionGroundTruth, ExternalTraceRecord
from m1_local.external_smoke import ensure_phoenix_dataset, run_offline_experiment
from m1_local.telemetry_export import redact_export_text


def record():
    return ExternalTraceRecord(
        dataset_case_id="who-and-when:synthetic", source_dataset="Who&When",
        source_version="fixture-revision", source_split="Hand-Crafted", source_case_id="synthetic",
        source_license="local research fixture", task='Inspect password="first EXPORT_TAIL"',
        original_system="synthetic", trace=[{"trace_index": 0, "role": "Agent", "content": '{"api_key":"EXPORT_NESTED"}'}],
        expected_answer='{"unchanged":  1}', ground_truth=AttributionGroundTruth(responsible_agent="Agent", decisive_step="0", failure_reason="fixture"),
    )


def test_dataset_create_and_add_use_safe_view_without_mutating_record():
    item = record()
    before = item.model_dump()
    uploads = []
    api = SimpleNamespace(
        list=lambda: [],
        create_dataset=lambda **kw: uploads.extend(kw["examples"]) or SimpleNamespace(),
    )
    client = SimpleNamespace(datasets=api)
    ensure_phoenix_dataset(client, [item])
    api.list = lambda: [SimpleNamespace(name="m1-local-lab-external-trace-smoke")]
    api.get_dataset = lambda **kw: SimpleNamespace(examples=[])
    api.add_examples_to_dataset = lambda **kw: uploads.extend(kw["examples"]) or kw["dataset"]
    ensure_phoenix_dataset(client, [item])
    assert len(uploads) == 2
    for example in uploads:
        serialized = json.dumps(example)
        assert "EXPORT_TAIL" not in serialized
        assert "EXPORT_NESTED" not in serialized
        assert example["output"] == item.phoenix_example()["output"]
        assert example["metadata"]["export_view_version"] == "safe-export-v1"
    assert item.model_dump() == before


def test_export_refuses_sensitive_labels_before_any_client_call():
    item = record().model_copy(update={"expected_answer": "token=PROTECTED_LABEL"})
    calls = []
    client = SimpleNamespace(datasets=SimpleNamespace(list=lambda: calls.append("list") or [], create_dataset=lambda **kw: calls.append("create")))
    with pytest.raises(ValueError, match="protected export field") as error:
        ensure_phoenix_dataset(client, [item])
    assert "PROTECTED_LABEL" not in str(error.value)
    assert calls == []


def test_reused_dataset_is_sanitized_before_sdk_experiment_spans():
    item = record()
    example = {"id": "example-id", "node_id": "node-id", **item.phoenix_example()}
    dataset = Dataset.from_dict({"id": "dataset-id", "name": "fixture", "version_id": "v1", "examples": [example], "filtered_splits": ["dev"]})
    before = dataset.to_dict()
    seen = []
    client = SimpleNamespace(experiments=SimpleNamespace(run_experiment=lambda **kw: seen.append(kw) or "experiment"))
    run_offline_experiment(client, dataset, {item.dataset_case_id: {"run_id": "run", "status": "completed"}})
    exported = seen[0]["dataset"].to_dict()
    assert "EXPORT_TAIL" not in json.dumps(exported)
    assert "EXPORT_NESTED" not in json.dumps(exported)
    assert exported["id"] == before["id"] and exported["version_id"] == before["version_id"]
    assert exported["filtered_splits"] == before["filtered_splits"]
    assert exported["examples"][0]["output"] == before["examples"][0]["output"]
    assert exported["examples"][0]["id"] == "example-id"
    assert dataset.to_dict() == before


def test_export_redactor_covers_quoted_assignment_and_json_key_names():
    assert "SYNTHETIC_TAIL" not in redact_export_text('password="first SYNTHETIC_TAIL"')
    assert "SYNTHETIC_KEY_NAME" not in redact_export_text('{"SYNTHETIC_KEY_NAME":"plain"}', ("SYNTHETIC_KEY_NAME",))


def test_safe_example_rejects_sensitive_identity_and_preserves_json_labels():
    from m1_local.external_export import safe_export_example

    example = record().phoenix_example()
    assert safe_export_example(example)["output"] == example["output"]
    example["input"]["source_case_id"] = "token=PRIVATE_ID"
    with pytest.raises(ValueError, match="protected export field") as error:
        safe_export_example(example)
    assert "PRIVATE_ID" not in str(error.value)
