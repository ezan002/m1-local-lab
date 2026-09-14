"""Sanitize export snapshots without changing frozen attribution preprocessing."""
from __future__ import annotations

import re
import json
from collections.abc import Sequence

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import Link, Status, StatusCode


_KEY = r"(?:api[-_]?key|authorization|cookie|password|passwd|secret|(?:access[-_]?|refresh[-_]?|session[-_]?)?token)"
_QUOTED = re.compile(rf"(?i)([\"']{_KEY}[\"']\s*:\s*)([\"'])(.*?)\2", re.DOTALL)
_ASSIGNMENT = re.compile(rf'''(?i)\b({_KEY}\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;]+)''')
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_OPENAI_KEY = re.compile(r"(?i)\bsk-[a-z0-9_-]{8,}\b")


def is_sensitive_export_key(value: str) -> bool:
    return re.fullmatch(_KEY, value, re.IGNORECASE) is not None


def redact_export_text(value: str, secrets: Sequence[str] = (), *, _depth: int = 0) -> str:
    # Application summaries may contain JSON text inside a JSON string.
    if _depth > 8:
        return "[REDACTED_NESTED_JSON]"
    if value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            pass
        else:
            def clean(item):
                if isinstance(item, dict):
                    return {redact_export_text(key, secrets, _depth=_depth + 1): "[REDACTED]" if is_sensitive_export_key(key) else clean(child) for key, child in item.items()}
                if isinstance(item, list):
                    return [clean(child) for child in item]
                if isinstance(item, str):
                    return redact_export_text(item, secrets, _depth=_depth + 1)
                return item
            return json.dumps(clean(parsed), ensure_ascii=False, separators=(",", ":"))
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = _QUOTED.sub(lambda match: match[1] + match[2] + "[REDACTED]" + match[2], value)
    value = _ASSIGNMENT.sub(r"\1[REDACTED]", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    return _OPENAI_KEY.sub("[REDACTED]", value)


class SanitizingExporter(SpanExporter):
    """Keep span identity/metrics; omit untrusted SDK exception bodies and stacks."""

    def __init__(self, exporter: SpanExporter, *, secrets: Sequence[str] = ()) -> None:
        self.exporter = exporter
        self.secrets = tuple(secret for secret in secrets if secret)

    def _attributes(self, attributes):
        def safe(value):
            if isinstance(value, str):
                return redact_export_text(value, self.secrets)
            if isinstance(value, (tuple, list)):
                return tuple(safe(item) for item in value)
            return value

        return {
            key: "[REDACTED]" if re.fullmatch(_KEY, key, re.IGNORECASE) else safe(value)
            for key, value in (attributes or {}).items()
            if key not in {"exception.message", "exception.stacktrace"}
        }

    def export(self, spans: Sequence[ReadableSpan]):
        sanitized = []
        for span in spans:
            sanitized.append(ReadableSpan(
                name=redact_export_text(span.name, self.secrets),
                context=span.context, parent=span.parent,
                resource=Resource(self._attributes(span.resource.attributes), schema_url=span.resource.schema_url),
                attributes=self._attributes(span.attributes),
                events=tuple(Event(redact_export_text(event.name, self.secrets), self._attributes(event.attributes), event.timestamp) for event in span.events),
                links=tuple(Link(link.context, attributes=self._attributes(link.attributes)) for link in span.links), kind=span.kind,
                status=Status(span.status.status_code, "Error" if span.status.status_code == StatusCode.ERROR else None),
                start_time=span.start_time, end_time=span.end_time,
                instrumentation_scope=span.instrumentation_scope,
            ))
        return self.exporter.export(sanitized)

    def shutdown(self) -> None:
        self.exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.exporter.force_flush(timeout_millis)
