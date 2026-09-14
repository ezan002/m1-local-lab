from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .attribution import ATTRIBUTION_METHODS, AttributionMethodName


PROTOCOL_VERSION = "attribution-eval-v1"
BENCHMARK_VERSION = "who-when-benchmark-v1"
BASELINE_VERSION = "deterministic-baseline-v1"
BASELINE_SOURCE_COMMIT = "da3ef193bdd73474eec5191f1e267ee163c308a7"
DATASET_REVISION = "59b9fcba1aaed7bbf206b5f4d3c68b8face2f49c"
REPOSITORY_REVISION = "b2bae5c5b06d681d04ea5e9b63b7a30525c04925"
SELECTION_SEED = "m1-local-lab-attribution-benchmark-v1"
# Canonical JSON digest of the already published v1 protocol; whitespace is immaterial.
PROTOCOL_SHA256 = "280fac35e8a759b3a355d3029bb6f832084f5075bade48bf078a7c4060efb467"
DATASET_NAME = "m1-local-lab-who-when-benchmark-v1"
EXPERIMENT_NAME = "m1-local-lab-attribution-benchmark-v1"
SOURCE_SPLITS = ("Hand-Crafted", "Algorithm-Generated")
METHODS: tuple[AttributionMethodName, ...] = (
    "last_active_agent",
    "first_explicit_error",
    "rule_based",
)


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset: str
    dataset_revision: str
    repository: str
    repository_revision: str
    license_policy: str


class SelectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    seed: str
    algorithm: str
    sample_counts: dict[str, int]
    dev_counts: dict[str, int]
    frozen_test_counts: dict[str, int]
    split_algorithm: str


class ProtocolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: str
    benchmark_version: str
    dataset_name: str
    experiment_name: str
    baseline_version: str
    baseline_source_commit: str
    methods: tuple[AttributionMethodName, ...]
    source: SourceConfig
    selection: SelectionConfig
    normalization: dict[str, Any]
    abstention: dict[str, Any]
    truncation: dict[str, Any]

    @model_validator(mode="after")
    def enforce_v1_constants(self) -> "ProtocolConfig":
        expected = {
            "protocol_version": PROTOCOL_VERSION,
            "benchmark_version": BENCHMARK_VERSION,
            "dataset_name": DATASET_NAME,
            "experiment_name": EXPERIMENT_NAME,
            "baseline_version": BASELINE_VERSION,
            "baseline_source_commit": BASELINE_SOURCE_COMMIT,
        }
        for key, value in expected.items():
            if getattr(self, key) != value:
                raise ValueError(f"{key} must be frozen to {value}")
        if self.methods != METHODS or tuple(ATTRIBUTION_METHODS) != METHODS:
            raise ValueError("the deterministic baseline method order changed")
        if self.source.dataset_revision != DATASET_REVISION:
            raise ValueError("Who&When dataset revision changed")
        if self.source.repository_revision != REPOSITORY_REVISION:
            raise ValueError("Who&When repository revision changed")
        if self.selection.seed != SELECTION_SEED:
            raise ValueError("selection seed changed")
        for split, total in {"Hand-Crafted": 10, "Algorithm-Generated": 20}.items():
            if self.selection.sample_counts.get(split) != total:
                raise ValueError(f"sample count changed for {split}")
            if (
                self.selection.dev_counts.get(split, 0)
                + self.selection.frozen_test_counts.get(split, 0)
                != total
            ):
                raise ValueError(f"benchmark split counts do not add up for {split}")
        if sha256_json(self.model_dump(mode="json")) != PROTOCOL_SHA256:
            raise ValueError("v1 protocol semantics must remain frozen")
        return self


class CanonicalStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    raw: str | None
    canonical: str | None
    position: int | None = Field(default=None, ge=0)
    numbering_origin: Literal["who_when_history_zero_based"]
    scorable: bool
    reason: str | None = None


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def load_protocol(path: Path) -> ProtocolConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid protocol file: {path}") from exc
    return ProtocolConfig.model_validate(payload)


def canonicalize_agent(value: Any) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", str(value))
    normalized = re.sub(r"\s+", " ", normalized.strip())
    return normalized.casefold() or None


def canonicalize_who_when_step(value: Any, *, trace_length: int) -> CanonicalStep:
    raw = None if value is None else str(value).strip()
    if not raw:
        return CanonicalStep(
            raw=raw or None,
            canonical=None,
            position=None,
            numbering_origin="who_when_history_zero_based",
            scorable=False,
            reason="step_label_missing",
        )
    if re.fullmatch(r"[0-9]+", raw) is None:
        return CanonicalStep(
            raw=raw,
            canonical=None,
            position=None,
            numbering_origin="who_when_history_zero_based",
            scorable=False,
            reason="step_is_not_a_base10_integer",
        )
    position = int(raw, 10)
    if position >= trace_length:
        return CanonicalStep(
            raw=raw,
            canonical=str(position),
            position=None,
            numbering_origin="who_when_history_zero_based",
            scorable=False,
            reason="step_not_present_in_retained_trace",
        )
    return CanonicalStep(
        raw=raw,
        canonical=str(position),
        position=position,
        numbering_origin="who_when_history_zero_based",
        scorable=True,
    )


def selection_hash(source_split: str, original_case_id: str) -> str:
    raw = f"{SELECTION_SEED}{source_split}{original_case_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def benchmark_split_hash(source_split: str, original_case_id: str) -> str:
    raw = (
        f"{SELECTION_SEED}:benchmark-split:{source_split}{original_case_id}"
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
