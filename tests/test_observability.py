from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from m1_local.models import RuleBasedGateway
from m1_local.observability import Observability, safe_json_summary
from m1_local.runtime import AgentRuntime


def test_safe_summary_redacts_secret_keys_and_common_token_shapes() -> None:
    summary = safe_json_summary({
        "api_key": "never-export-this",
        "task": "use bearer abc.def.ghi and sk-testsecret123456789",
        "nested": {"password": "also-secret"},
    })
    assert "never-export-this" not in summary
    assert "abc.def.ghi" not in summary
    assert "sk-testsecret123456789" not in summary
    assert "also-secret" not in summary
    assert summary.count("[REDACTED]") >= 4


def test_phoenix_provider_keeps_default_exporter_and_hides_raw_model_content(settings) -> None:
    class PhoenixLikeProvider:
        def __init__(self) -> None:
            self.replace_default_processor = None

        def add_span_processor(self, processor, *, replace_default_processor=True) -> None:
            self.replace_default_processor = replace_default_processor

        def get_tracer(self, name):
            return object()

    provider = PhoenixLikeProvider()
    observability = Observability(settings)
    observability.configure_provider(provider)  # type: ignore[arg-type]
    assert provider.replace_default_processor is False
    assert observability.trace_config.hide_inputs is True
    assert observability.trace_config.hide_outputs is True
    assert observability.trace_config.hide_llm_invocation_parameters is True


@pytest.mark.asyncio
async def test_offline_graph_spans_have_semantics_correlation_and_lineage(settings) -> None:
    allowed = Path(settings.allowed_file_dirs[0]) / "trace.csv"
    allowed.write_text("name,value\ntrace,1\n", encoding="utf-8")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    observability = Observability(settings)
    observability.configure_provider(provider)

    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    runtime.observability = observability
    await runtime.startup()
    secret = "sk-testsecret123456789"
    try:
        original = await runtime.create_run(
            f"Read the file; api_key={secret}",
            allowed_files=[str(allowed)], dataset_case_id="offline-case",
        )
        source = next(item for item in await runtime.history(original) if item["next"])
        replayed = await runtime.replay(original, source["checkpoint_id"])
        forked = await runtime.fork(original, source["checkpoint_id"], {"worker_instruction": "continue safely"})
        await runtime.resume(forked)
    finally:
        await runtime.shutdown()

    spans = list(exporter.get_finished_spans())
    by_name: dict[str, list] = {}
    for span in spans:
        by_name.setdefault(span.name, []).append(span)

    assert {"m1.run", "m1.node.planner", "m1.node.orchestrator", "m1.node.pause_gate", "m1.node.file_reader", "m1.node.finalizer", "m1.checkpoint.commit"} <= set(by_name)
    assert any(span.attributes["openinference.span.kind"] == "AGENT" for span in by_name["m1.run"])
    assert all(span.attributes["openinference.span.kind"] == "CHAIN" for span in by_name["m1.node.planner"])
    assert all(span.attributes["openinference.span.kind"] == "TOOL" for span in by_name["m1.node.file_reader"])
    assert all(span.attributes["openinference.span.kind"] == "LLM" for name, items in by_name.items() if name.startswith("m1.model.") for span in items)

    application_spans = [span for span in spans if span.name.startswith("m1.")]
    assert all(span.attributes.get("input.value") for span in application_spans)
    assert all(span.attributes.get("output.value") for span in application_spans)
    assert all(span.attributes.get("run_id") for span in application_spans)
    assert all(span.attributes.get("thread_id") for span in application_spans)
    assert all(span.attributes.get("branch_id") for span in application_spans)

    node_spans = [span for span in application_spans if span.name.startswith("m1.node.")]
    assert all(span.attributes.get("execution_id") for span in node_spans)
    assert all(span.attributes.get("langgraph_node") for span in node_spans)
    assert all(span.attributes.get("agent_name") for span in node_spans)
    assert all("step_number" in span.attributes for span in node_spans)
    assert all(span.parent is not None for span in node_spans)

    commits = by_name["m1.checkpoint.commit"]
    assert all(span.attributes.get("checkpoint_id") for span in commits)
    assert all(span.attributes.get("status") == "committed" for span in commits)
    assert any(span.attributes.get("dataset_case_id") == "offline-case" for span in application_spans)
    assert any(span.attributes.get("replay_of") == original and span.attributes.get("run_id") == replayed for span in application_spans)
    assert any(span.attributes.get("fork_of") == original and span.attributes.get("run_id") == forked for span in application_spans)

    exported = "\n".join(str(dict(span.attributes)) for span in spans)
    assert secret not in exported
