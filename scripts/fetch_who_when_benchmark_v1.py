from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
import urllib.request
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "external" / "who_when"
DEFAULT_MANIFEST_PATH = PROJECT_ROOT / "datasets" / "manifests" / "who_when_benchmark_v1.json"
DEFAULT_PROTOCOL_PATH = PROJECT_ROOT / "configs" / "attribution_eval_v1.json"
DEFAULT_REPORT_ROOT = PROJECT_ROOT / "data" / "reports" / "attribution-benchmark-v1"
DATASET_REVISION = "59b9fcba1aaed7bbf206b5f4d3c68b8face2f49c"
REPOSITORY_REVISION = "b2bae5c5b06d681d04ea5e9b63b7a30525c04925"
MAX_TRANSFER_BYTES = 25 * 1024 * 1024
CHUNK_BYTES = 64 * 1024

SOURCES = (
    {
        "name": "Hand-Crafted.parquet",
        "kind": "dataset",
        "url": (
            "https://huggingface.co/datasets/Kevin355/Who_and_When/resolve/"
            f"{DATASET_REVISION}/Hand-Crafted.parquet?download=true"
        ),
    },
    {
        "name": "Algorithm-Generated.parquet",
        "kind": "dataset",
        "url": (
            "https://huggingface.co/datasets/Kevin355/Who_and_When/resolve/"
            f"{DATASET_REVISION}/Algorithm-Generated.parquet?download=true"
        ),
    },
    {
        "name": "dataset-README.md",
        "kind": "dataset_documentation",
        "url": (
            "https://huggingface.co/datasets/Kevin355/Who_and_When/resolve/"
            f"{DATASET_REVISION}/README.md?download=true"
        ),
    },
    {
        "name": "repository-README.md",
        "kind": "repository_documentation",
        "url": (
            "https://raw.githubusercontent.com/mingyin1/Agents_Failure_Attribution/"
            f"{REPOSITORY_REVISION}/README.md"
        ),
    },
    {
        "name": "repository-LICENSE",
        "kind": "repository_documentation",
        "url": (
            "https://raw.githubusercontent.com/mingyin1/Agents_Failure_Attribution/"
            f"{REPOSITORY_REVISION}/LICENSE"
        ),
    },
)


def _download(
    url: str,
    destination: Path,
    *,
    remaining_bytes: int,
    opener: Any,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "m1-local-lab-attribution-benchmark-v1/1.0"},
    )
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    downloaded = 0
    try:
        with opener.open(request, timeout=60) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > remaining_bytes:
                raise RuntimeError(
                    f"declared payload exceeds remaining transfer budget: {declared} > {remaining_bytes}"
                )
            with temporary.open("wb") as handle:
                while True:
                    chunk = response.read(min(CHUNK_BYTES, remaining_bytes - downloaded + 1))
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > remaining_bytes:
                        raise RuntimeError("fixed 25 MiB cumulative transfer limit exceeded")
                    digest.update(chunk)
                    handle.write(chunk)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "bytes": downloaded,
        "sha256": digest.hexdigest(),
    }


def fetch(output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    receipts: list[dict[str, Any]] = []
    transferred = 0
    for source in SOURCES:
        destination = output_root / str(source["name"])
        result = _download(
            str(source["url"]),
            destination,
            remaining_bytes=MAX_TRANSFER_BYTES - transferred,
            opener=opener,
        )
        transferred += int(result["bytes"])
        receipts.append({**source, **result})
    receipt = {
        "dataset": "Kevin355/Who_and_When",
        "dataset_revision": DATASET_REVISION,
        "repository": "mingyin1/Agents_Failure_Attribution",
        "repository_revision": REPOSITORY_REVISION,
        "license_policy": "local research use only; redistribution not authorized",
        "transfer_limit_bytes": MAX_TRANSFER_BYTES,
        "transferred_bytes": transferred,
        # The two compact parquet files contain all 184 rows at this revision.
        "full_dataset_downloaded": True,
        "files": receipts,
    }
    (output_root / "download-receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def materialize(
    output_root: Path,
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
    report_root: Path = DEFAULT_REPORT_ROOT,
) -> dict[str, Any]:
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError(
            "materialization requires the existing Phoenix environment with pyarrow; no install is needed"
        ) from exc
    from m1_local.attribution_benchmark import build_manifest_payload

    receipt = json.loads((output_root / "download-receipt.json").read_text(encoding="utf-8"))
    if receipt.get("dataset_revision") != DATASET_REVISION:
        raise RuntimeError("download receipt dataset revision mismatch")
    if receipt.get("repository_revision") != REPOSITORY_REVISION:
        raise RuntimeError("download receipt repository revision mismatch")
    receipt["full_dataset_downloaded"] = True
    for item in receipt.get("files", []):
        item.pop("final_url", None)
    (output_root / "download-receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    rows_by_split = {
        "Hand-Crafted": parquet.read_table(output_root / "Hand-Crafted.parquet").to_pylist(),
        "Algorithm-Generated": parquet.read_table(output_root / "Algorithm-Generated.parquet").to_pylist(),
    }
    source_files = {
        str(item["name"]): {
            "kind": item["kind"],
            "url": item["url"],
            "bytes": item["bytes"],
            "sha256": item["sha256"],
        }
        for item in receipt["files"]
    }
    manifest, excluded = build_manifest_payload(
        rows_by_split,
        raw_root=output_root,
        protocol_path=protocol_path,
        source_files=source_files,
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report_root.mkdir(parents=True, exist_ok=True)
    (report_root / "excluded_cases.json").write_text(
        json.dumps(excluded, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest["manifest_sha256"],
        "source_row_counts": manifest["source_row_counts"],
        "selected_counts": manifest["selected_counts"],
        "benchmark_split_counts": manifest["benchmark_split_counts"],
        "excluded_case_count": len(excluded),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch only the pinned, bounded Who&When benchmark-v1 source files."
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--protocol-path", type=Path, default=DEFAULT_PROTOCOL_PATH)
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--materialize", action="store_true")
    parser.add_argument("--materialize-only", action="store_true")
    args = parser.parse_args()
    try:
        receipt = (
            json.loads((args.output_root / "download-receipt.json").read_text(encoding="utf-8"))
            if args.materialize_only
            else fetch(args.output_root)
        )
        materialized = (
            materialize(
                args.output_root,
                manifest_path=args.manifest_path,
                protocol_path=args.protocol_path,
                report_root=args.report_root,
            )
            if args.materialize or args.materialize_only
            else None
        )
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    if materialized is not None:
        print(json.dumps(materialized, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
