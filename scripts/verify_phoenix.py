from __future__ import annotations

import argparse
import json
import time

from opentelemetry import trace
from phoenix.client import Client
from phoenix.otel import register


BASE_URL = "http://127.0.0.1:6006"
OTLP_URL = "http://127.0.0.1:6006/v1/traces"
PROJECT = "m1-local-lab"


def query(trace_id: str) -> bool:
    client = Client(base_url=BASE_URL)
    for _ in range(10):
        frame = client.spans.get_spans_dataframe(project_identifier=PROJECT, limit=1000, timeout=5)
        body = frame.to_csv(index=True)
        if trace_id.lower() in body.lower():
            print(json.dumps({"status": "PASS", "trace_id": trace_id, "span_rows": len(frame)}, ensure_ascii=False))
            return True
        time.sleep(1)
    print(json.dumps({"status": "FAIL", "trace_id": trace_id, "reason": "trace not returned by Phoenix query"}, ensure_ascii=False))
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", help="Only query an existing trace ID")
    args = parser.parse_args()
    if args.query:
        return 0 if query(args.query) else 1
    provider = register(
        endpoint=OTLP_URL, project_name=PROJECT, protocol="http/protobuf",
        batch=False, verbose=False,
    )
    tracer = provider.get_tracer("m1_local.acceptance")
    with tracer.start_as_current_span("acceptance.parent") as parent:
        context = parent.get_span_context()
        trace_id = f"{context.trace_id:032x}"
        parent.set_attribute("application_run_id", "phoenix-acceptance")
        with tracer.start_as_current_span("acceptance.child") as child:
            child.set_attribute("node_name", "verification")
    provider.force_flush(timeout_millis=10_000)
    return 0 if query(trace_id) else 1


if __name__ == "__main__":
    raise SystemExit(main())
