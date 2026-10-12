"""An independently defined two-agent program (Analyst -> Critic) instrumented with OpenTelemetry.

It does NOT import m1_local. It only needs the OpenTelemetry SDK + OTLP/HTTP exporter (and the
openai SDK for --mode openai). Point it at any OTLP/HTTP collector; Agent Studio accepts it at
http://127.0.0.1:8100/v1/traces and shows the run as a read-only external trace.

    python examples/external_agent/research_agent.py --topic "tool failures" --mode deterministic
    python examples/external_agent/research_agent.py --topic "tool failures" --mode openai
    python examples/external_agent/research_agent.py --topic x --fail-tool   # demonstrates error capture
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode


def call_model(mode: str, model: str, prompt: str) -> tuple[str, dict]:
    if mode == "deterministic":
        time.sleep(0.05)
        return f"[deterministic] {prompt[:120]}", {}
    from openai import OpenAI

    client = OpenAI(base_url=os.environ.get("OPENAI_BASE_URL") or None, timeout=60, max_retries=1)
    response = client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}],
                                              max_tokens=250, temperature=0.2)
    return response.choices[0].message.content or "", (response.usage.model_dump() if response.usage else {})


def llm_span(tracer, agent: str, mode: str, model: str, prompt: str) -> str:
    with tracer.start_as_current_span(f"{agent}.llm") as span:
        span.set_attribute("openinference.span.kind", "LLM")
        span.set_attribute("agent.name", agent)
        span.set_attribute("llm.model_name", model if mode == "openai" else "deterministic")
        span.set_attribute("input.value", json.dumps([{"role": "user", "content": prompt}]))
        text, usage = call_model(mode, model, prompt)
        span.set_attribute("output.value", text)
        for key, value in usage.items():
            if isinstance(value, int):
                span.set_attribute(f"llm.token_count.{key}", value)
        return text


def word_count_tool(tracer, text: str, fail: bool) -> int:
    with tracer.start_as_current_span("word_count") as span:
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute("tool.name", "word_count")
        span.set_attribute("input.value", json.dumps({"text": text[:500]}))
        if fail:
            raise RuntimeError("word_count backend unavailable (simulated by --fail-tool)")
        count = len(text.split())
        span.set_attribute("output.value", json.dumps({"words": count}))
        return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", required=True)
    parser.add_argument("--mode", choices=["deterministic", "openai"], default="deterministic")
    parser.add_argument("--model", default=os.environ.get("STUDIO_DEFAULT_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--endpoint", default=os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:8100/v1/traces"))
    parser.add_argument("--fail-tool", action="store_true")
    args = parser.parse_args()

    resource = Resource.create({"service.name": "external-research-agent", "studio.provider_mode": args.mode,
                                **({"studio.launch_id": os.environ["STUDIO_LAUNCH_ID"]} if os.environ.get("STUDIO_LAUNCH_ID") else {})})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=args.endpoint)))
    tracer = provider.get_tracer("external-research-agent")
    exit_code = 0
    with tracer.start_as_current_span("research_session") as root:
        root.set_attribute("openinference.span.kind", "CHAIN")
        root.set_attribute("input.value", json.dumps({"topic": args.topic, "mode": args.mode}))
        try:
            with tracer.start_as_current_span("analyst") as analyst:
                analyst.set_attribute("openinference.span.kind", "AGENT")
                analyst.set_attribute("agent.name", "analyst")
                analyst.set_attribute("input.value", args.topic)
                draft = llm_span(tracer, "analyst", args.mode, args.model, f"In 3 sentences, explain: {args.topic}")
                words = word_count_tool(tracer, draft, args.fail_tool)
                analyst.set_attribute("output.value", draft)
            with tracer.start_as_current_span("critic") as critic:
                critic.set_attribute("openinference.span.kind", "AGENT")
                critic.set_attribute("agent.name", "critic")
                critic.set_attribute("input.value", draft)
                verdict = llm_span(tracer, "critic", args.mode, args.model,
                                   f"Critique this {words}-word draft in one sentence:\n{draft}")
                critic.set_attribute("output.value", verdict)
            root.set_attribute("output.value", json.dumps({"draft": draft, "critique": verdict}))
            print(json.dumps({"draft": draft, "critique": verdict}, ensure_ascii=False))
        except Exception as exc:  # recorded on the active spans, then reported
            root.record_exception(exc)
            root.set_status(Status(StatusCode.ERROR, str(exc)))
            print(f"agent failed: {exc}", file=sys.stderr)
            exit_code = 2
    provider.force_flush()
    provider.shutdown()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
