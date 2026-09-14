from __future__ import annotations
from m1_local.config import Settings

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from phoenix.client import Client

from m1_local.attribution import assert_no_label_fields
from m1_local.attribution_benchmark import (
    EVALUATION_NAMES,
    benchmark_phoenix_examples,
    ensure_benchmark_dataset,
    load_benchmark_records,
    load_manifest,
    log_benchmark_to_phoenix,
    run_benchmark,
    verify_frozen_baseline,
    verify_source_receipt,
    write_reports,
)
from m1_local.attribution_protocol import (
    BASELINE_SOURCE_COMMIT,
    BASELINE_VERSION,
    BENCHMARK_VERSION,
    DATASET_NAME,
    EXPERIMENT_NAME,
    PROTOCOL_VERSION,
    load_protocol,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = PROJECT_ROOT / "configs" / "attribution_eval_v1.json"
MANIFEST_PATH = PROJECT_ROOT / "datasets" / "manifests" / "who_when_benchmark_v1.json"
RAW_ROOT = PROJECT_ROOT / "data" / "external" / "who_when"
REPORT_ROOT = PROJECT_ROOT / "data" / "reports" / "attribution-benchmark-v1"
AGGREGATE_PATH = PROJECT_ROOT / "docs" / "results" / "attribution-benchmark-v1-summary.json"
PHOENIX_URL = "http://127.0.0.1:6006"


def _value(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


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


def _ensure_phoenix() -> subprocess.Popen[bytes] | None:
    if _phoenix_healthy():
        return None
    if _port_open(6006):
        raise RuntimeError("port 6006 is occupied but is not a healthy Phoenix service")
    phoenix_python = PROJECT_ROOT / ".venv-phoenix" / "Scripts" / "python.exe"
    if not phoenix_python.is_file():
        raise RuntimeError(f"Phoenix Python executable is missing: {phoenix_python}")
    log_root = PROJECT_ROOT / "data" / "logs"
    working_root = PROJECT_ROOT / "data" / "phoenix"
    log_root.mkdir(parents=True, exist_ok=True)
    working_root.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "PHOENIX_HOST": "127.0.0.1",
            "PHOENIX_PORT": "6006",
            "PHOENIX_WORKING_DIR": str(working_root),
            "PHOENIX_ALLOW_EXTERNAL_RESOURCES": "false",
        }
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with (log_root / "attribution-benchmark-phoenix.stdout.log").open("ab") as stdout, (
        log_root / "attribution-benchmark-phoenix.stderr.log"
    ).open("ab") as stderr:
        process = subprocess.Popen(
            [str(phoenix_python), str(PROJECT_ROOT / "scripts" / "serve_phoenix.py")],
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            creationflags=creation_flags,
        )
    for _ in range(60):
        if _phoenix_healthy():
            return process
        if process.poll() is not None:
            raise RuntimeError(f"Phoenix exited during startup with code {process.returncode}")
        time.sleep(1)
    process.terminate()
    process.wait(timeout=20)
    raise RuntimeError("Phoenix did not become healthy within 60 seconds")


def _wait_for_stop() -> None:
    for _ in range(40):
        if not _port_open(6006):
            return
        time.sleep(0.25)
    raise RuntimeError("Phoenix started by this verifier did not stop cleanly")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    service_before = {
        "phoenix_6006": _port_open(6006),
        "app_8000": _port_open(8000),
        "app_8001": _port_open(8001),
    }
    phoenix_process: subprocess.Popen[bytes] | None = None
    result_payload: dict[str, Any] = {}
    try:
        protocol = load_protocol(PROTOCOL_PATH)
        manifest = load_manifest(MANIFEST_PATH, protocol_path=PROTOCOL_PATH)
        receipt = verify_source_receipt(manifest, RAW_ROOT)
        verify_frozen_baseline(PROJECT_ROOT)
        records = load_benchmark_records(manifest, raw_root=RAW_ROOT)
        if len(records) != 30:
            raise RuntimeError(f"expected 30 records, found {len(records)}")
        for _case, _record, inference, _labels in records:
            assert_no_label_fields(inference)
        result = run_benchmark(records)
        repeated = run_benchmark(records)
        if result.model_dump(mode="json") != repeated.model_dump(mode="json"):
            raise RuntimeError("benchmark predictions or aggregates are not reproducible")
        secrets = (Settings().openai_api_key.get_secret_value(),)
        examples = benchmark_phoenix_examples(records, manifest_sha256=manifest.manifest_sha256, secrets=secrets)
        for example in examples:
            assert_no_label_fields(example["input"])

        phoenix_process = _ensure_phoenix()
        client = Client(base_url=PHOENIX_URL)
        dataset, dataset_created, effective_dataset_name = ensure_benchmark_dataset(
            client,
            records,
            manifest_sha256=manifest.manifest_sha256,
            name=DATASET_NAME,
            secrets=secrets,
        )
        dataset_examples = list(_value(dataset, "examples") or ())
        if len(dataset_examples) != 30:
            raise RuntimeError(f"expected 30 Phoenix examples, found {len(dataset_examples)}")
        phoenix_result = log_benchmark_to_phoenix(
            client,
            dataset,
            result,
            manifest=manifest,
            experiment_name=EXPERIMENT_NAME,
            secrets=secrets,
        )
        stored = client.experiments.get_experiment(experiment_id=phoenix_result["experiment_id"])
        task_runs = list(stored["task_runs"])
        evaluation_runs = list(stored["evaluation_runs"])
        stored_names = {str(_value(item, "name")) for item in evaluation_runs}
        expected_evaluations = 90 * len(EVALUATION_NAMES)
        if len(task_runs) != 90:
            raise RuntimeError(f"expected 90 Phoenix runs, found {len(task_runs)}")
        if len(evaluation_runs) != expected_evaluations:
            raise RuntimeError(
                f"expected {expected_evaluations} Phoenix evaluations, found {len(evaluation_runs)}"
            )
        if stored_names != set(EVALUATION_NAMES):
            raise RuntimeError(f"unexpected Phoenix evaluation names: {sorted(stored_names)}")

        metadata = {
            "status": "PASS",
            "benchmark_version": BENCHMARK_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "manifest_sha256": manifest.manifest_sha256,
            "baseline_version": BASELINE_VERSION,
            "baseline_source_commit": BASELINE_SOURCE_COMMIT,
            "dataset_name": effective_dataset_name,
            "dataset_id": str(_value(dataset, "id")),
            "dataset_version_id": str(_value(dataset, "version_id")),
            "dataset_created": dataset_created,
            "dataset_example_count": len(dataset_examples),
            "experiment_name": EXPERIMENT_NAME,
            "experiment_id": phoenix_result["experiment_id"],
            "experiment_run_count": len(task_runs),
            "evaluation_count": len(evaluation_runs),
            "evaluation_names": sorted(stored_names),
            "sample_count": 30,
            "source_split_counts": manifest.selected_counts,
            "benchmark_split_counts": manifest.benchmark_split_counts,
            "excluded_case_count": manifest.excluded_case_count,
            "trace_truncated_count": sum(case.trace_truncated for case in manifest.cases),
            "decisive_step_not_retained_count": sum(not case.decisive_step_retained for case in manifest.cases),
            "label_leakage_check": "passed",
            "fixed_order_reproducibility_check": "passed",
            "frozen_baseline_check": "passed",
            "paid_model_calls": 0,
            "real_websurfer_calls": 0,
            "external_task_executed": False,
            "downloaded_bytes": receipt["transferred_bytes"],
            "transfer_limit_bytes": receipt["transfer_limit_bytes"],
            "full_who_when_downloaded": bool(receipt["full_dataset_downloaded"]),
            "license_policy": receipt["license_policy"],
            "phoenix_ui": f"{PHOENIX_URL}/experiments/{phoenix_result['experiment_id']}",
        }
        write_reports(
            result,
            report_root=REPORT_ROOT,
            aggregate_path=AGGREGATE_PATH,
            metadata=metadata,
        )
        result_payload = metadata
    finally:
        if phoenix_process is not None:
            phoenix_process.terminate()
            try:
                phoenix_process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                phoenix_process.kill()
                phoenix_process.wait(timeout=10)
            _wait_for_stop()
    result_payload["service_before"] = service_before
    result_payload["phoenix_started_by_verifier"] = phoenix_process is not None
    result_payload["service_after"] = {
        "phoenix_6006": _port_open(6006),
        "app_8000": _port_open(8000),
        "app_8001": _port_open(8001),
    }
    print(json.dumps(result_payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
