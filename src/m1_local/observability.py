from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any
from urllib.request import urlopen

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.trace import Status, StatusCode
from openinference.instrumentation.config import TraceConfig
from openinference.semconv.trace import (
    OpenInferenceMimeTypeValues,
    OpenInferenceSpanKindValues,
    SpanAttributes,
)

from .config import Settings


_CORRELATION_CONTEXT_KEY = "m1_local.trace_correlation"
_SENSITIVE_KEY = re.compile(
    r"(?i)(api[-_]?key|authorization|cookie|password|passwd|secret|session[-_]?token|access[-_]?token|refresh[-_]?token)"
)
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)\b(sk-[a-z0-9_-]{8,})\b"),
    re.compile(r"(?i)\b((?:api[-_]?key|token|password|secret|cookie)\s*[:=]\s*)[^\s,;]+"),
)


def _redact_text(value: str, limit: int = 1000) -> str:
    text = value
    text = _SENSITIVE_VALUE_PATTERNS[0].sub(r"\1[REDACTED]", text)
    text = _SENSITIVE_VALUE_PATTERNS[1].sub("[REDACTED]", text)
    text = _SENSITIVE_VALUE_PATTERNS[2].sub(r"\1[REDACTED]", text)
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 30:
                result["_truncated"] = True
                break
            name = str(key)
            result[name] = "[REDACTED]" if _SENSITIVE_KEY.search(name) else _safe_value(item, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_safe_value(item, depth=depth + 1) for item in list(value)[:30]]
    return _redact_text(str(value))


def redact_sensitive(value: Any) -> Any:
    """Return a bounded, JSON-compatible copy with the trace redaction rules applied."""
    return _safe_value(value)


def safe_json_summary(value: Any, *, limit: int = 4000) -> str:
    encoded = json.dumps(_safe_value(value), ensure_ascii=False, separators=(",", ":"), default=str)
    return encoded if len(encoded) <= limit else encoded[:limit] + "…"


class CorrelationSpanProcessor(SpanProcessor):
    """Copies current run/node correlation fields onto every child span."""

    def on_start(self, span: Any, parent_context: otel_context.Context | None = None) -> None:
        attributes = otel_context.get_value(_CORRELATION_CONTEXT_KEY, parent_context)
        if not isinstance(attributes, Mapping):
            return
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(str(key), value)

    def on_end(self, span: Any) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


class Observability:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.provider: Any = None
        self.tracer = trace.get_tracer("m1_local")
        self.warning: str | None = None
        # Third-party instrumentation must not export raw prompts, web text,
        # model responses, tool definitions, or invocation parameters.
        self.trace_config = TraceConfig(
            hide_inputs=True,
            hide_outputs=True,
            hide_input_messages=True,
            hide_output_messages=True,
            hide_prompts=True,
            hide_choices=True,
            hide_llm_tools=True,
            hide_llm_invocation_parameters=True,
        )

    def configure_provider(self, provider: TracerProvider) -> None:
        self.provider = provider
        processor = CorrelationSpanProcessor()
        try:
            # Phoenix's provider otherwise replaces its default OTLP exporter.
            provider.add_span_processor(processor, replace_default_processor=False)  # type: ignore[call-arg]
        except TypeError:
            # The standard OpenTelemetry provider has no Phoenix-specific flag.
            provider.add_span_processor(processor)
        self.tracer = provider.get_tracer("m1_local")

    def initialize(self) -> None:
        self.warning = self.check_phoenix()
        try:
            from phoenix.otel import register
            from openinference.instrumentation.langchain import LangChainInstrumentor
            from openinference.instrumentation.openai import OpenAIInstrumentor

            provider = register(
                endpoint=self.settings.phoenix_otlp_endpoint,
                project_name=self.settings.phoenix_project_name,
                batch=True,
                protocol="http/protobuf",
                verbose=False,
            )
            self.configure_provider(provider)
            LangChainInstrumentor().instrument(tracer_provider=provider, config=self.trace_config)
            OpenAIInstrumentor().instrument(tracer_provider=provider, config=self.trace_config)
        except Exception as exc:  # tracing must not stop the task
            self.warning = f"Phoenix tracing unavailable: {type(exc).__name__}: {exc}"
            self.tracer = trace.get_tracer("m1_local")

    def check_phoenix(self) -> str | None:
        try:
            with urlopen(self.settings.phoenix_base_url, timeout=1) as response:
                if response.status != 200:
                    return f"Phoenix tracing endpoint returned HTTP {response.status}"
        except Exception as exc:
            return (
                "Phoenix is unreachable; task execution will continue without confirmed trace receipt: "
                f"{type(exc).__name__}: {exc}"
            )
        return None

    @contextlib.contextmanager
    def trace_context(self, attributes: dict[str, Any]) -> Iterator[None]:
        inherited = otel_context.get_value(_CORRELATION_CONTEXT_KEY)
        merged = dict(inherited) if isinstance(inherited, Mapping) else {}
        merged.update({key: value for key, value in attributes.items() if value is not None})
        token = otel_context.attach(otel_context.set_value(_CORRELATION_CONTEXT_KEY, merged))
        try:
            yield
        finally:
            otel_context.detach(token)

    @contextlib.contextmanager
    def span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        *,
        kind: OpenInferenceSpanKindValues | str = OpenInferenceSpanKindValues.CHAIN,
        input_summary: Any | None = None,
        agent_name: str | None = None,
        tool_name: str | None = None,
    ) -> Iterator[Any]:
        clean = {key: value for key, value in (attributes or {}).items() if value is not None}
        clean[SpanAttributes.OPENINFERENCE_SPAN_KIND] = kind.value if hasattr(kind, "value") else str(kind)
        if agent_name:
            clean[SpanAttributes.AGENT_NAME] = agent_name
        if tool_name:
            clean[SpanAttributes.TOOL_NAME] = tool_name
        if input_summary is not None:
            clean[SpanAttributes.INPUT_VALUE] = safe_json_summary(input_summary)
            clean[SpanAttributes.INPUT_MIME_TYPE] = OpenInferenceMimeTypeValues.JSON.value
        with self.tracer.start_as_current_span(
            name, attributes=clean, record_exception=False, set_status_on_exception=False
        ) as span:
            try:
                yield span
            except BaseException as exc:
                if type(exc).__name__ in {"GraphInterrupt", "GraphBubbleUp"}:
                    raise
                span.set_attribute("status", "error")
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, safe_json_summary({"error_type": type(exc).__name__}))
                span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, OpenInferenceMimeTypeValues.JSON.value)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise

    @staticmethod
    def set_output(span: Any, summary: Any, *, status: str | None = None) -> None:
        span.set_attribute(SpanAttributes.OUTPUT_VALUE, safe_json_summary(summary))
        span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, OpenInferenceMimeTypeValues.JSON.value)
        if status is not None:
            span.set_attribute("status", status)
            span.set_status(Status(StatusCode.ERROR if status in {"failed", "error", "interrupted"} else StatusCode.OK))

    def current_ids(self) -> tuple[str | None, str | None]:
        context = trace.get_current_span().get_span_context()
        if not context.is_valid:
            return None, None
        return f"{context.trace_id:032x}", f"{context.span_id:016x}"

    def flush(self) -> None:
        if self.provider is not None:
            try:
                self.provider.force_flush(timeout_millis=10_000)
            except Exception:
                pass

    def shutdown(self) -> None:
        if self.provider is None:
            return
        # The normal run-level flush occurs while the root span is still open.
        # Flush again here after all run contexts have closed so the complete
        # parent/child tree is handed to the exporter before shutdown.
        self.flush()
        try:
            from openinference.instrumentation.langchain import LangChainInstrumentor
            from openinference.instrumentation.openai import OpenAIInstrumentor

            LangChainInstrumentor().uninstrument()
            OpenAIInstrumentor().uninstrument()
        except Exception:
            pass
        try:
            self.provider.shutdown()
        except Exception:
            pass
        self.provider = None
