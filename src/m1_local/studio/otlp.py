"""OTLP/HTTP trace ingest (protobuf or JSON) -> read-only external runs.

Any OpenTelemetry-instrumented agent can export to POST /v1/traces. Attribute conventions
understood: OpenInference (openinference.span.kind, input.value, output.value, llm.*,
tool.name, agent.name) and gen_ai.* basics. Unknown attributes are kept verbatim.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

from .store import StudioStore

EXTERNAL_CAPABILITIES = {"trace": True, "execute": False, "checkpoint": False, "pause_resume": False,
                         "fork": False, "replay": False, "retry": False}
_KINDS = {"LLM": "llm", "TOOL": "tool", "AGENT": "agent", "CHAIN": "chain", "RETRIEVER": "retriever",
          "EMBEDDING": "embedding", "RERANKER": "reranker", "GUARDRAIL": "guardrail", "EVALUATOR": "evaluator"}


class OTLPError(ValueError):
    pass


def _any_value(value: dict[str, Any]) -> Any:
    if not value:
        return None
    key, inner = next(iter(value.items()))
    if key in {"stringValue", "boolValue", "doubleValue"}:
        return inner
    if key == "intValue":
        return int(inner)
    if key == "arrayValue":
        return [_any_value(v) for v in inner.get("values", [])]
    if key == "kvlistValue":
        return {kv["key"]: _any_value(kv.get("value", {})) for kv in inner.get("values", [])}
    if key == "bytesValue":
        return inner
    return inner


def _attrs(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {kv["key"]: _any_value(kv.get("value", {})) for kv in items or []}


def _hex_id(value: Any, length: int) -> str:
    """OTLP JSON uses hex; protobuf->dict (MessageToDict) gives base64. Normalize to hex."""
    if not value:
        return ""
    text = str(value)
    if len(text) == length * 2 and all(c in "0123456789abcdefABCDEF" for c in text):
        return text.lower()
    try:
        return base64.b64decode(text).hex()
    except Exception as exc:  # noqa: BLE001
        raise OTLPError(f"invalid span/trace id {text!r}") from exc


def _iso(nanos: Any) -> str | None:
    if nanos in (None, "", 0, "0"):
        return None
    return datetime.fromtimestamp(int(nanos) / 1e9, timezone.utc).isoformat(timespec="milliseconds")


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def decode_request(body: bytes, content_type: str) -> dict[str, Any]:
    if "json" in content_type:
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OTLPError(f"malformed OTLP JSON: {exc}") from exc
    if "protobuf" in content_type or "octet-stream" in content_type or not content_type:
        from google.protobuf.json_format import MessageToDict
        from google.protobuf.message import DecodeError
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        message = ExportTraceServiceRequest()
        try:
            message.ParseFromString(body)
        except DecodeError as exc:
            raise OTLPError(f"malformed OTLP protobuf: {exc}") from exc
        return MessageToDict(message)
    raise OTLPError(f"unsupported content type {content_type!r}")


def normalize_span(raw: dict[str, Any], service: str) -> dict[str, Any]:
    if not raw.get("traceId") or not raw.get("spanId") or not raw.get("name"):
        raise OTLPError("span requires traceId, spanId and name")
    attrs = _attrs(raw.get("attributes", []))
    kind_attr = str(attrs.get("openinference.span.kind", "")).upper()
    kind = _KINDS.get(kind_attr) or ("llm" if "gen_ai.request.model" in attrs else "external")
    status_code = (raw.get("status") or {}).get("code", 0)
    status_code = {"STATUS_CODE_ERROR": 2, "STATUS_CODE_OK": 1, "STATUS_CODE_UNSET": 0}.get(status_code, status_code)
    error = None
    for event in raw.get("events", []) or []:
        if event.get("name") == "exception":
            ev = _attrs(event.get("attributes", []))
            error = {"type": ev.get("exception.type", "Exception"), "message": ev.get("exception.message", ""),
                     "traceback": ev.get("exception.stacktrace")}
    if int(status_code or 0) == 2 and error is None:
        error = {"type": "Error", "message": (raw.get("status") or {}).get("message", "span reported error status")}
    output = attrs.get("output.value")
    if output is None and kind == "llm":
        output = attrs.get("llm.output_messages.0.message.content")
    return {
        "id": _hex_id(raw["spanId"], 8),
        "trace_id": _hex_id(raw["traceId"], 16),
        "parent_span_id": _hex_id(raw.get("parentSpanId"), 8) or None,
        "name": raw["name"], "kind": kind,
        "node_id": attrs.get("agent.name") or attrs.get("tool.name") or attrs.get("graph.node.id"),
        "status": "error" if error else "ok",
        "input": _maybe_json(attrs.get("input.value")), "output": _maybe_json(output), "error": error,
        "attributes": {**attrs, "service.name": service},
        "start_time": _iso(raw.get("startTimeUnixNano")), "end_time": _iso(raw.get("endTimeUnixNano")),
    }


def ingest(store: StudioStore, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or "resourceSpans" not in payload:
        raise OTLPError("payload must contain resourceSpans")
    accepted, duplicates, rejected, runs = 0, 0, [], set()
    correlated: set[str] = set()
    cache: dict[str, str | None] = {}
    for resource_spans in payload.get("resourceSpans", []):
        resource = _attrs((resource_spans.get("resource") or {}).get("attributes", []))
        service = str(resource.get("service.name", "unknown-service"))
        for scope in resource_spans.get("scopeSpans", []) or []:
            for raw in scope.get("spans", []) or []:
                try:
                    span = normalize_span(raw, service)
                except OTLPError as exc:
                    rejected.append(str(exc))
                    continue
                connected = _connected_run(store, span["attributes"], cache)
                if connected is not None:
                    # Spans the connected executor exports about one of its runs belong to the mirrored run.
                    # node_id is cleared so the graph keeps counting checkpoint-derived node executions only.
                    span = {**span, "node_id": None, "parent_span_id": span["parent_span_id"] or f"cr_{connected}",
                            "attributes": {**span["attributes"], "studio.correlated_by": "application_run_id"}}
                    if store.insert_span(connected, span):
                        accepted += 1
                    else:
                        duplicates += 1
                    correlated.add(connected)
                    continue
                run_id = "ext_" + span["trace_id"]
                if store.get_run(run_id) is None:
                    store.create_run(id=run_id, origin="external", integration=service, workflow_name=service,
                                     thread_id=None, status="receiving", capabilities={**EXTERNAL_CAPABILITIES,
                                                                                       "launch_id": resource.get("studio.launch_id")},
                                     provider_mode=str(resource.get("studio.provider_mode", "unknown")))
                if store.insert_span(run_id, span):
                    accepted += 1
                else:
                    duplicates += 1
                runs.add(run_id)
    for run_id in runs:
        _refresh_external_run(store, run_id)
    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected, "runs": sorted(runs | correlated)}


def parse_trace_file(text: str, filename: str = "") -> list[dict[str, Any]]:
    """Payloads in an uploaded trace file. Accepted (OTLP JSON encoding, as written by OTLP file exporters and the
    collector's file exporter): one ExportTraceServiceRequest object, a JSON array of them, or JSON Lines with one per
    line. Each payload must contain resourceSpans."""
    payloads, _ = parse_trace_file_with_format(text, filename)
    return payloads


def parse_trace_file_with_format(text: str, filename: str = "") -> tuple[list[dict[str, Any]], str]:
    """(payloads, format). Non-OTel trajectory formats (see converters.py) are converted to OTLP first."""
    from .converters import ConversionError, convert

    text = text.lstrip("\ufeff").strip()
    if not text:
        raise OTLPError("the file is empty")
    try:
        converted = convert(text, filename)
    except ConversionError as exc:
        raise OTLPError(str(exc)) from exc
    if converted:
        return converted[1], converted[0]
    payloads: list[Any]
    try:
        data = json.loads(text)
        payloads = data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        payloads = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                payloads.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise OTLPError(f"line {number} is not valid JSON ({exc.msg}); expected OTLP JSON or JSON Lines") from exc
    bad = [i for i, item in enumerate(payloads, 1) if not isinstance(item, dict) or "resourceSpans" not in item]
    if bad:
        raise OTLPError(f"{len(bad)} of {len(payloads)} records have no resourceSpans (first: record {bad[0]}); "
                        "the file must be OpenTelemetry traces in OTLP JSON format, or a supported raw trajectory "
                        "(Codex session .jsonl, or a trajectory dataset with a `history` list such as LongRCA)")
    return payloads, "otlp"


def ingest_file(store: StudioStore, text: str | bytes, filename: str = "") -> dict[str, Any]:
    if isinstance(text, bytes):
        from .converters import ConversionError, convert_bytes

        try:
            converted = convert_bytes(text, filename)
        except ConversionError as exc:
            raise OTLPError(str(exc)) from exc
        if converted:
            payloads, fmt = converted[1], converted[0]
        else:
            try:
                payloads, fmt = parse_trace_file_with_format(text.decode("utf-8"), filename)
            except UnicodeDecodeError as exc:
                raise OTLPError("binary file that is not Parquet; expected OTLP JSON, JSON Lines or a Parquet dataset") from exc
    else:
        payloads, fmt = parse_trace_file_with_format(text, filename)
    total = {"accepted": 0, "duplicates": 0, "rejected": [], "runs": set(), "payloads": 0, "format": fmt}
    for payload in payloads:
        result = ingest(store, payload)
        total["accepted"] += result["accepted"]
        total["duplicates"] += result["duplicates"]
        total["rejected"] += result["rejected"]
        total["runs"] |= set(result["runs"])
        total["payloads"] += 1
    total["runs"] = sorted(total["runs"])
    return total


def _connected_run(store: StudioStore, attrs: dict[str, Any], cache: dict[str, str | None]) -> str | None:
    remote = attrs.get("application_run_id") or attrs.get("run_id")
    if not remote or not isinstance(remote, str):
        return None
    if remote not in cache:
        rows = store._query("SELECT id FROM runs WHERE origin='connected' AND remote_run_id=? LIMIT 1", (remote,))
        cache[remote] = rows[0]["id"] if rows else None
    return cache[remote]


def _refresh_external_run(store: StudioStore, run_id: str) -> None:
    spans = store.spans(run_id)
    roots = [s for s in spans if not s["parent_span_id"] or s["parent_span_id"] not in {x["id"] for x in spans}]
    root = next((s for s in roots if not s["parent_span_id"]), None)
    errors = [s for s in spans if s["status"] == "error"]
    status = "failed" if root and root["status"] == "error" else ("completed" if root else "receiving")
    starts = [s["start_time"] for s in spans if s["start_time"]]
    ends = [s["end_time"] for s in spans if s["end_time"]]
    fields: dict[str, Any] = {"status": status, "started_at": min(starts) if starts else None, "ended_at": max(ends) if ends else None}
    if root:
        fields["input"] = root["input"] if isinstance(root["input"], dict) else {"value": root["input"]}
        fields["output"] = {"value": root["output"]}
        fields["workflow_name"] = f"{root['attributes'].get('service.name')} · {root['name']}"
    if errors:
        first = errors[0]
        fields["error"] = {**(first["error"] or {}), "node_id": first["node_id"] or first["name"], "error_span_count": len(errors)}
    store.update_run(run_id, **fields)
