"""Versioned display/export copies; frozen adapter and evaluator inputs stay intact."""
from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import Any

from .telemetry_export import is_sensitive_export_key, redact_export_text


EXPORT_VIEW_VERSION = "safe-export-v1"
_PROTECTED = {
    "id", "node_id", "output", "name", "method", "source_dataset", "source_license",
    "trace_index", "step", "step_id", "step_index", "role", "agent", "agent_name",
    "responsible_agent", "predicted_agent", "predicted_step", "decisive_step", "filtered_splits",
}


def _text(value: str, secrets: Sequence[str]) -> str:
    cleaned = redact_export_text(value, secrets)
    if cleaned != value:
        try:
            # Do not change formatting of JSON text whose content did not need redaction.
            if json.loads(cleaned) == json.loads(value):
                return value
        except (ValueError, RecursionError):
            pass
    return cleaned


def safe_export_payload(value: Any, *, secrets: Sequence[str] = (), _depth: int = 0) -> Any:
    if _depth > 30:
        raise ValueError("export nesting exceeds the safety limit")
    if isinstance(value, str):
        return _text(value, secrets)
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if not isinstance(key, str) or _text(key, secrets) != key:
                raise ValueError("protected export field/key contains sensitive content")
            cleaned = "[REDACTED]" if is_sensitive_export_key(key) else safe_export_payload(child, secrets=secrets, _depth=_depth + 1)
            if (key in _PROTECTED or key.endswith(("_id", "_ids", "_version", "_split", "_sha256", "_hash"))) and cleaned != child:
                # Never print untrusted keys or values in this exception.
                raise ValueError("protected export field contains sensitive content; export refused without changing research values")
            result[key] = cleaned
        return result
    if isinstance(value, (tuple, list)):
        return type(value)(safe_export_payload(child, secrets=secrets, _depth=_depth + 1) for child in value)
    return copy.deepcopy(value)


def safe_export_example(example: dict[str, Any], *, secrets: Sequence[str] = ()) -> dict[str, Any]:
    result = safe_export_payload(example, secrets=secrets)
    result.setdefault("metadata", {})["export_view_version"] = EXPORT_VIEW_VERSION
    return result


def dataset_export_view(dataset: Any, *, secrets: Sequence[str] = ()) -> Any:
    from phoenix.client.resources.datasets import Dataset

    snapshot = safe_export_payload(dataset.to_dict(), secrets=secrets)
    snapshot["examples"] = [safe_export_example(example, secrets=secrets) for example in snapshot["examples"]]
    return Dataset.from_dict(snapshot)
