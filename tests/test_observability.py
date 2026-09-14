from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from m1_local.models import RuleBasedGateway
from m1_local.observability import Observability, safe_json_summary
from m1_local.runtime import AgentRuntime


def test_export_boundary_hides_sdk_exception_and_preserves_parentage(settings):
    import httpx
    from openai import AuthenticationError, OpenAI
    from openinference.instrumentation.openai import OpenAIInstrumentor
    from m1_local.telemetry_export import SanitizingExporter

    secret = "synthetic-secret-never-export"
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(SanitizingExporter(exporter, secrets=(secret,))))
    observability = Observability(settings)
    observability.configure_provider(provider)
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=observability.trace_config)
    client = OpenAI(
        api_key="offline-fixture", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
            401, json={"error": {"message": "api_key=" + secret, "type": "invalid_request_error"}},
        ))),
    )
    try:
        with provider.get_tracer("test").start_as_current_span("parent") as parent:
            parent.set_attribute("input.value", '{"api_key": "quoted-fixture-value"}')
            parent.set_attribute("output.value", safe_json_summary({"task_preview": '{"api_key": "nested-fixture-value"}'}))
            parent.set_attribute("llm.token_count.total", 17)
            with pytest.raises(AuthenticationError):
                client.chat.completions.create(model="offline-fixture", messages=[{"role": "user", "content": "fixture"}])
        spans = exporter.get_finished_spans()
        root = next(span for span in spans if span.name == "parent")
        child = next(span for span in spans if span.parent is not None)
        assert child.parent.span_id == root.context.span_id
        assert child.context.trace_id == root.context.trace_id
        assert root.attributes["llm.token_count.total"] == 17
        assert child.status.status_code.name == "ERROR"
        assert child.events and any("exception.type" in event.attributes for event in child.events)
        rendered = "\n".join(span.to_json() for span in spans)
        assert secret not in rendered
        assert "quoted-fixture-value" not in rendered
        assert "nested-fixture-value" not in rendered
    finally:
        client.close()
        instrumentor.uninstrument()
        provider.shutdown()


def test_export_boundary_covers_resource_links_and_event_names():
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.trace import Link, SpanContext, TraceFlags
    from m1_local.telemetry_export import SanitizingExporter

    secret = "configured-fixture-value"
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({"service.name": secret}))
    provider.add_span_processor(SimpleSpanProcessor(SanitizingExporter(exporter, secrets=(secret,))))
    try:
        link = Link(SpanContext(123, 456, False, TraceFlags(1)), attributes={"label": secret})
        with provider.get_tracer("test").start_as_current_span("fixture", links=[link]) as span:
            span.add_event(secret, {"detail": secret})
        exported = exporter.get_finished_spans()[0]
        assert secret not in exported.to_json()
        assert exported.links[0].context.span_id == 456
    finally:
        provider.shutdown()


def test_initialize_replaces_phoenix_exporter_before_instrumentation(settings, monkeypatch):
    import phoenix.otel
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from openinference.instrumentation.openai import OpenAIInstrumentor

    exporter = InMemorySpanExporter()
    monkeypatch.setattr(phoenix.otel, "HTTPSpanExporter", lambda **kwargs: exporter)
    monkeypatch.setattr(LangChainInstrumentor, "instrument", lambda *args, **kwargs: None)
    monkeypatch.setattr(OpenAIInstrumentor, "instrument", lambda *args, **kwargs: None)
    obs = Observability(settings)
    monkeypatch.setattr(obs, "check_phoenix", lambda: None)
    global_provider = __import__("opentelemetry.trace", fromlist=["get_tracer_provider"]).get_tracer_provider()
    try:
        obs.initialize()
        assert obs.warning is None
        with obs.span("fixture", input_summary={"task": '{"api_key": "export-fixture"}'}):
            pass
        obs.flush()
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert "export-fixture" not in spans[0].to_json()
        assert spans[0].resource.attributes["openinference.project.name"] == settings.phoenix_project_name
        assert __import__("opentelemetry.trace", fromlist=["get_tracer_provider"]).get_tracer_provider() is global_provider
    finally:
        obs.shutdown()


@pytest.mark.asyncio
async def test_run_flush_happens_after_root_span_ends(settings):
    from opentelemetry.sdk.trace import SpanProcessor

    actions = []

    class Recorder(SpanProcessor):
        def on_end(self, span):
            actions.append(span.name)

        def force_flush(self, timeout_millis=30000):
            actions.append("flush")
            return True

    provider = TracerProvider()
    provider.add_span_processor(Recorder())
    runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=False)
    runtime.observability.configure_provider(provider)
    try:
        await runtime.create_run("Draft code")
        assert actions[-2:] == ["m1.run", "flush"]
    finally:
        await runtime.shutdown()


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
