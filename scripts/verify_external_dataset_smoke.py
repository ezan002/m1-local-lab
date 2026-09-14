from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from phoenix.client import Client

from m1_local.config import Settings
from m1_local.external_datasets import adapt_traceelephant, adapt_who_and_when
from m1_local.external_smoke import ensure_phoenix_dataset, run_offline_experiment
from m1_local.models import RuleBasedGateway
from m1_local.runtime import AgentRuntime


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WHO_VERSION = "59b9fcba1aaed7bbf206b5f4d3c68b8face2f49c"
TRACEELEPHANT_VERSION = "a78a57cdcdf74a080b1bec0f56f85228d86acbac"


def load_records() -> list[Any]:
    who_root = PROJECT_ROOT / "data/external/who-and-when"
    trace_root = PROJECT_ROOT / "data/external/traceelephant"
    trace_case = trace_root / "assistant-bench-task-7"
    records = [
        adapt_who_and_when(
            who_root / "hand-crafted-1.json",
            allowed_root=who_root,
            source_version=WHO_VERSION,
            source_split="Hand-Crafted",
        ),
        adapt_who_and_when(
            who_root / "algorithm-generated-1.json",
            allowed_root=who_root,
            source_version=WHO_VERSION,
            source_split="Algorithm-Generated",
        ),
        adapt_traceelephant(
            trace_case / "trace_metadata.json",
            trace_case / "step_records.json",
            allowed_root=trace_root,
            source_version=TRACEELEPHANT_VERSION,
            source_split="magentic-runs-assistant-bench",
        ),
    ]
    if len(records) > 5 or {item.source_dataset for item in records} != {"TraceElephant", "Who&When"}:
        raise RuntimeError("sample bound/source coverage invariant failed")
    return records


async def local_rule_based_validation(records: list[Any]) -> dict[str, dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="m1-external-smoke-") as temp:
        root = Path(temp)
        evidence = root / "local-validation.csv"
        evidence.write_text("check,result\nadapter_pipeline,ok\n", encoding="utf-8")
        settings = Settings(
            _env_file=None,
            openai_api_key="",
            headless=True,
            checkpoint_db=root / "checkpoints.sqlite",
            metadata_db=root / "app.sqlite",
            artifact_root=root / "artifacts",
            phoenix_working_dir=PROJECT_ROOT / "data/phoenix",
            allowed_file_dirs=[str(root)],
            phoenix_base_url="http://127.0.0.1:6006",
            phoenix_otlp_endpoint="http://127.0.0.1:6006/v1/traces",
        )
        gateway = RuleBasedGateway()
        runtime = AgentRuntime(settings, models=gateway, enable_tracing=True)
        await runtime.startup()
        results: dict[str, dict[str, Any]] = {}
        try:
            for record in records:
                run_id = await runtime.create_run(
                    "Read the explicitly allowed local validation file and summarize it. "
                    "Do not execute the imported external task.",
                    allowed_files=[str(evidence)],
                    dataset_case_id=record.dataset_case_id,
                )
                state = await runtime.current_state(run_id)
                results[record.dataset_case_id] = {
                    "run_id": run_id,
                    "trace_id": runtime.store.latest_trace_id(run_id),
                    "status": state["status"],
                }
        finally:
            await runtime.shutdown()
        if gateway.calls == 0:
            raise RuntimeError("RuleBasedGateway was not exercised")
        if any(item["status"] != "completed" for item in results.values()):
            raise RuntimeError(f"offline validation did not complete: {results}")
        return results


async def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    records = load_records()
    validation_runs = await local_rule_based_validation(records)
    client = Client(base_url="http://127.0.0.1:6006")
    secrets = (Settings().openai_api_key.get_secret_value(),)
    dataset, created = ensure_phoenix_dataset(client, records, secrets=secrets)
    experiment = run_offline_experiment(client, dataset, validation_runs, secrets=secrets)
    experiment_id = (
        str(experiment.get("experiment_id", ""))
        if isinstance(experiment, dict)
        else str(getattr(experiment, "experiment_id", ""))
    )
    print(json.dumps({
        "status": "PASS",
        "sample_count": len(records),
        "source_counts": {
            source: sum(item.source_dataset == source for item in records)
            for source in ("TraceElephant", "Who&When")
        },
        "dataset_id": str(getattr(dataset, "id", "")),
        "dataset_version_id": str(getattr(dataset, "version_id", "")),
        "dataset_created": created,
        "experiment_id": experiment_id,
        "experiment_name": "m1-local-lab-external-dataset-smoke",
        "paid_model_calls": 0,
        "external_task_executed": False,
        "validation_runs": validation_runs,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
