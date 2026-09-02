from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .external_datasets import ExternalTraceRecord


DATASET_NAME = "m1-local-lab-external-trace-smoke"
EXPERIMENT_NAME = "m1-local-lab-external-dataset-smoke"


def _dataset_name(value: Any) -> str | None:
    if isinstance(value, Mapping):
        return value.get("name")  # type: ignore[return-value]
    return getattr(value, "name", None)


def _example_input(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        item = value.get("input")
    else:
        item = getattr(value, "input", None)
    return item if isinstance(item, Mapping) else {}


def ensure_phoenix_dataset(
    client: Any,
    records: Sequence[ExternalTraceRecord],
    *,
    name: str = DATASET_NAME,
) -> tuple[Any, bool]:
    if not records or len(records) > 5:
        raise ValueError("smoke dataset must contain between 1 and 5 records")
    ids = [record.dataset_case_id for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("dataset_case_id values must be unique")
    # Phoenix 11.x does not accept caller-supplied example IDs. Keep the stable
    # cross-version key inside input/metadata and use it for idempotency checks.
    examples = [record.phoenix_example() for record in records]
    existing = next((item for item in client.datasets.list() if _dataset_name(item) == name), None)
    if existing is None:
        return (
            client.datasets.create_dataset(
                name=name,
                examples=examples,
                dataset_description=(
                    "Bounded imported-history compatibility samples. These examples are not m1-local-lab executions."
                ),
            ),
            True,
        )

    dataset = client.datasets.get_dataset(dataset=name)
    existing_ids = {
        str(_example_input(example).get("dataset_case_id", ""))
        for example in dataset.examples
    }
    missing = [
        example
        for example in examples
        if str(example["input"]["dataset_case_id"]) not in existing_ids
    ]
    if missing:
        dataset = client.datasets.add_examples_to_dataset(dataset=dataset, examples=missing)
    return dataset, False


def run_offline_experiment(
    client: Any,
    dataset: Any,
    validation_runs: Mapping[str, Mapping[str, Any]],
    *,
    experiment_name: str = EXPERIMENT_NAME,
) -> Any:
    """Log only precomputed RuleBasedGateway validations against imported examples."""

    def task(input: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
        case_id = str(input["dataset_case_id"])
        local = validation_runs[case_id]
        return {
            "dataset_case_id": case_id,
            "source_dataset": input["source_dataset"],
            "trace_origin": "imported_historical",
            "external_task_executed": False,
            "validation_mode": "adapter_pipeline_only",
            "rule_based_gateway": True,
            "local_run_id": local["run_id"],
            "local_trace_id": local.get("trace_id"),
            "local_status": local["status"],
            "ground_truth_preserved": all(
                key in expected for key in ("responsible_agent", "decisive_step", "failure_reason")
            ),
        }

    def provenance_guard(output: Mapping[str, Any]) -> tuple[bool, str]:
        passed = (
            output.get("trace_origin") == "imported_historical"
            and output.get("external_task_executed") is False
            and output.get("rule_based_gateway") is True
            and bool(output.get("local_run_id"))
        )
        return passed, "imported history is separated from the local offline validation run"

    return client.experiments.run_experiment(
        dataset=dataset,
        task=task,
        evaluators={"provenance_guard": provenance_guard},
        experiment_name=experiment_name,
        experiment_description="Zero-cost adapter/pipeline smoke test using RuleBasedGateway only.",
        experiment_metadata={
            "validation_mode": "adapter_pipeline_only",
            "external_task_executed": False,
            "gateway": "RuleBasedGateway",
            "paid_model_calls": 0,
        },
        print_summary=False,
        repetitions=1,
        retries=0,
    )
