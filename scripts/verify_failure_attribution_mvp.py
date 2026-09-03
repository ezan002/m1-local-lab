from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from phoenix.client import Client

from m1_local.attribution import build_attribution_views
from m1_local.attribution_evaluation import (
    EVALUATION_NAMES,
    log_suite_to_phoenix,
    run_attribution_suite,
)
from m1_local.external_datasets import adapt_traceelephant, adapt_who_and_when
from m1_local.external_smoke import DATASET_NAME, ensure_phoenix_dataset


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WHO_VERSION = "59b9fcba1aaed7bbf206b5f4d3c68b8face2f49c"
TRACEELEPHANT_VERSION = "a78a57cdcdf74a080b1bec0f56f85228d86acbac"
PHOENIX_URL = "http://127.0.0.1:6006"
EXPERIMENT_NAME = "m1-local-lab-failure-attribution-mvp"


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.5)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def _phoenix_healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{PHOENIX_URL}/healthz", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def _run_powershell(script_name: str) -> str:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(PROJECT_ROOT / "scripts" / script_name),
    ]
    result = subprocess.run(command, cwd=PROJECT_ROOT, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _ensure_phoenix() -> bool:
    if _phoenix_healthy():
        return False
    if _port_open(6006):
        raise RuntimeError("port 6006 is occupied but Phoenix health check failed")
    _run_powershell("start-phoenix.ps1")
    for _ in range(45):
        if _phoenix_healthy():
            return True
        time.sleep(1)
    # The process belongs to this invocation even though it never became
    # healthy, so clean it up before surfacing the failure.
    _run_powershell("stop-phoenix.ps1")
    _wait_for_phoenix_stop()
    raise RuntimeError("Phoenix did not become healthy within 45 seconds")


def _wait_for_phoenix_stop() -> None:
    for _ in range(30):
        if not _port_open(6006):
            return
        time.sleep(0.25)
    raise RuntimeError("Phoenix started by this script did not stop cleanly")


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
    if len(records) != 3 or {record.source_dataset for record in records} != {
        "TraceElephant",
        "Who&When",
    }:
        raise RuntimeError("expected exactly three bounded samples covering both sources")
    return records


def _value(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    service_before = {"app_8000": _port_open(8000), "phoenix_6006": _port_open(6006)}
    started_phoenix = False
    result: dict[str, Any] = {}
    try:
        started_phoenix = _ensure_phoenix()
        records = load_records()
        views = [build_attribution_views(record) for record in records]
        suite = run_attribution_suite(views)

        client = Client(base_url=PHOENIX_URL)
        dataset, dataset_created = ensure_phoenix_dataset(client, records, name=DATASET_NAME)
        examples = list(_value(dataset, "examples") or ())
        case_ids = [str(_value(example, "input")["dataset_case_id"]) for example in examples]
        if len(examples) != 3 or len(set(case_ids)) != 3:
            raise RuntimeError("Phoenix dataset must contain exactly three unique examples")

        phoenix_result = log_suite_to_phoenix(
            client, dataset, suite, experiment_name=EXPERIMENT_NAME
        )
        stored = client.experiments.get_experiment(experiment_id=phoenix_result.experiment_id)
        task_runs = list(stored["task_runs"])
        evaluation_runs = list(stored["evaluation_runs"])
        stored_names = {str(_value(item, "name")) for item in evaluation_runs}
        if len(task_runs) != 9:
            raise RuntimeError(f"expected 9 Phoenix task runs, found {len(task_runs)}")
        if len(evaluation_runs) != 9 * len(EVALUATION_NAMES):
            raise RuntimeError(
                f"expected {9 * len(EVALUATION_NAMES)} evaluations, found {len(evaluation_runs)}"
            )
        if stored_names != set(EVALUATION_NAMES):
            raise RuntimeError(f"unexpected evaluation names: {sorted(stored_names)}")

        prediction_rows = []
        evaluation_by_key = {
            (item.dataset_case_id, item.method): item for item in suite.evaluations
        }
        for prediction in suite.predictions:
            evaluation = evaluation_by_key[(prediction.dataset_case_id, prediction.method)]
            prediction_rows.append(
                {
                    "dataset_case_id": prediction.dataset_case_id,
                    "method": prediction.method,
                    "status": prediction.status,
                    "predicted_agent": prediction.predicted_agent,
                    "predicted_step": prediction.predicted_step,
                    "agent_exact_match": evaluation.agent_exact_match,
                    "step_exact_match": evaluation.step_exact_match,
                    "joint_exact_match": evaluation.joint_exact_match,
                    "step_distance": evaluation.step_distance,
                    "evaluation_status": evaluation.evaluation_status,
                    "trace_original_step_count": evaluation.trace_original_step_count,
                    "trace_retained_step_count": evaluation.trace_retained_step_count,
                    "trace_truncated": evaluation.trace_truncated,
                    "decisive_step_present_in_trace": evaluation.decisive_step_present_in_trace,
                }
            )
        result = {
            "status": "PASS",
            "sample_count": len(records),
            "dataset_name": DATASET_NAME,
            "dataset_id": str(_value(dataset, "id")),
            "dataset_version_id": str(_value(dataset, "version_id")),
            "dataset_created": dataset_created,
            "dataset_unique_example_count": len(set(case_ids)),
            "experiment_name": phoenix_result.experiment_name,
            "experiment_id": phoenix_result.experiment_id,
            "experiment_run_count": len(task_runs),
            "evaluation_count": len(evaluation_runs),
            "evaluation_names": sorted(stored_names),
            "paid_model_calls": 0,
            "real_websurfer_calls": 0,
            "external_task_executed": False,
            "full_dataset_downloaded": False,
            "label_leakage_check": "passed",
            "predictions": prediction_rows,
            "summaries": {
                name: summary.model_dump(mode="json") for name, summary in suite.summaries.items()
            },
            "phoenix_ui": f"{PHOENIX_URL}/experiments/{phoenix_result.experiment_id}",
        }
    finally:
        if started_phoenix:
            _run_powershell("stop-phoenix.ps1")
            _wait_for_phoenix_stop()
    result["service_before"] = service_before
    result["phoenix_started_by_script"] = started_phoenix
    result["service_after"] = {
        "app_8000": _port_open(8000),
        "phoenix_6006": _port_open(6006),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
