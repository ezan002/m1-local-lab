from __future__ import annotations

import json
from pathlib import Path

import pytest

from m1_local.attribution_protocol import (
    METHODS,
    benchmark_split_hash,
    canonicalize_agent,
    canonicalize_who_when_step,
    load_protocol,
    selection_hash,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = PROJECT_ROOT / "configs" / "attribution_eval_v1.json"


def test_frozen_protocol_loads_with_exact_method_order_and_quotas() -> None:
    protocol = load_protocol(PROTOCOL_PATH)
    assert protocol.protocol_version == "attribution-eval-v1"
    assert protocol.baseline_version == "deterministic-baseline-v1"
    assert protocol.methods == METHODS
    assert protocol.selection.sample_counts == {
        "Hand-Crafted": 10,
        "Algorithm-Generated": 20,
    }
    assert protocol.selection.dev_counts == {
        "Hand-Crafted": 7,
        "Algorithm-Generated": 13,
    }


def test_protocol_rejects_a_changed_frozen_revision(tmp_path: Path) -> None:
    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    payload["source"]["dataset_revision"] = "moving-main"
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="dataset revision changed"):
        load_protocol(path)


@pytest.mark.parametrize("section,key,value", [
    ("selection", "dev_counts", {"Hand-Crafted": 6, "Algorithm-Generated": 14}),
    ("normalization", "unknown_step", "scorable"),
    ("abstention", "overall_denominator", "exclude abstentions"),
    ("truncation", "agent_label_may_remain_scorable", False),
    ("source", "license_policy", "redistribution permitted"),
])
def test_protocol_rejects_semantic_drift(tmp_path, section, key, value):
    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    payload[section][key] = value
    if key == "dev_counts":
        payload["selection"]["frozen_test_counts"] = {"Hand-Crafted": 4, "Algorithm-Generated": 6}
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen"):
        load_protocol(path)


def test_agent_normalization_is_nfkc_whitespace_and_case_only() -> None:
    assert canonicalize_agent("  ＷebSurfer\tAgent  ") == "websurfer agent"
    assert canonicalize_agent("Orchestrator (thought)") != canonicalize_agent("Orchestrator")
    assert canonicalize_agent(None) is None


def test_step_normalization_is_strict_zero_based_and_never_offset_seeks() -> None:
    exact = canonicalize_who_when_step("03", trace_length=5)
    assert exact.canonical == "3"
    assert exact.position == 3
    assert exact.scorable is True
    out_of_range = canonicalize_who_when_step("5", trace_length=5)
    assert out_of_range.scorable is False
    assert out_of_range.position is None
    assert out_of_range.reason == "step_not_present_in_retained_trace"
    assert canonicalize_who_when_step("+1", trace_length=5).scorable is False
    assert canonicalize_who_when_step("1.0", trace_length=5).scorable is False


def test_selection_and_split_hashes_are_stable_and_domain_separated() -> None:
    first = selection_hash("Hand-Crafted", "case-1")
    assert first == selection_hash("Hand-Crafted", "case-1")
    assert first != selection_hash("Algorithm-Generated", "case-1")
    assert first != benchmark_split_hash("Hand-Crafted", "case-1")
