"""Explicit, loss-visible historical formats; never an execution/replay adapter.

Only public Phoenix v1 HTTP APIs are used. All payloads are redacted copies.
The importer journal is ours, NOT Phoenix's database.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

import httpx

from .runtime_ownership import RuntimeOwnership
from .telemetry_export import is_sensitive_export_key, redact_export_text

ADAPTER_VERSION = "historical-observation-v2"
PROJECT = "m1-imported-history-v1"
ROOT = Path(__file__).resolve().parents[2]
CATALOG = {
    "who-short": {
        "source": "Who&When", "version": "59b9fcba1aaed7bbf206b5f4d3c68b8face2f49c",
        "files": ["who-and-when/algorithm-generated-1.json"],
        "url": "https://huggingface.co/datasets/Kevin355/Who_and_When",
        "license": "Repository MIT; separate dataset license not declared; local review only",
    },
    "traceelephant-7": {
        "source": "TraceElephant", "version": "a78a57cdcdf74a080b1bec0f56f85228d86acbac",
        "files": ["traceelephant/assistant-bench-task-7/trace_metadata.json", "traceelephant/assistant-bench-task-7/step_records.json"],
        "url": "https://huggingface.co/datasets/TraceElephant/TraceElephant", "license": "CC BY 4.0",
    },
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def local_endpoint(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1"}
            or parts.username or parts.password or parts.path not in {"", "/"}
            or parts.query or parts.fragment or not parts.port):
        raise ValueError("Phoenix must be an explicit numeric loopback HTTP address and port")
    return url.rstrip("/")


def redact(value, depth=0):
    # No clipping. Reject unsupported nesting instead of silently dropping content.
    if depth > 30:
        raise ValueError("source nesting exceeds 30; nothing imported")
    if isinstance(value, str):
        # Extend only this display adapter, never frozen research preprocessing.
        value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]", value, flags=re.DOTALL)
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", value)
        return redact_export_text(value, ())
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if redact_export_text(k, ()) != k:
                raise ValueError("sensitive field name; nothing imported")
            result[k] = "[REDACTED]" if is_sensitive_export_key(k) else redact(v, depth + 1)
        return result
    if isinstance(value, list):
        return [redact(v, depth + 1) for v in value]
    return value


def load_catalog(path: Path | None = None) -> dict:
    """An operator-owned startup allowlist, never a path accepted from the browser.

    Explicit known schemas only. Read once at startup; changing registration
    requires a restart. Built-in sample identities cannot be overridden.
    """
    catalog = {key: {**spec, "files": list(spec["files"])} for key, spec in CATALOG.items()}
    if path is None:
        return catalog
    with path.open("rb") as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise ValueError("catalog exceeds 64 KiB")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate catalog field")
            result[key] = value
        return result

    payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique)
    if (not isinstance(payload, dict) or set(payload) != {"schema_version", "samples"}
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or not isinstance(payload["samples"], list) or not 1 <= len(payload["samples"]) <= 32):
        raise ValueError("catalog requires schema_version 1 and 1..32 explicit samples")
    fields = {"sample", "label", "source", "version", "url", "license", "files"}
    for spec in payload["samples"]:
        if not isinstance(spec, dict) or set(spec) != fields:
            raise ValueError("catalog entry has missing or unsupported fields")
        for key in fields - {"files"}:
            value = spec[key]
            if (not isinstance(value, str) or not value.strip() or len(value) > 500
                    or any(ord(c) < 32 for c in value) or redact(value) != value):
                raise ValueError("catalog text is invalid or sensitive")
        sample = spec["sample"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", sample) or sample in catalog:
            raise ValueError("catalog sample ID invalid, duplicate or reserved")
        if spec["source"] not in {"Who&When", "TraceElephant"}:
            raise ValueError("unsupported catalog schema")
        url = urlsplit(spec["url"])
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ValueError("catalog source URL must be a credential-free HTTPS reference")
        files = spec["files"]
        expected = 1 if spec["source"] == "Who&When" else 2
        if not isinstance(files, list) or len(files) != expected:
            raise ValueError("catalog requires one Who JSON or metadata/steps JSON pair")
        for relative in files:
            if (not isinstance(relative, str) or len(relative) > 240 or redact(relative) != relative
                    or "\\" in relative or ":" in relative or any(ord(c) < 32 for c in relative)
                    or PurePosixPath(relative).is_absolute() or any(p in {"", ".", ".."} for p in relative.split("/"))
                    or PurePosixPath(relative).suffix.lower() != ".json"):
                raise ValueError("catalog requires safe relative JSON paths under data/external")
        if len(set(files)) != len(files):
            raise ValueError("catalog file roles must be distinct")
        catalog[sample] = {k: v for k, v in spec.items() if k != "sample"}
        catalog[sample]["version_source"] = "operator-declared catalog; not independently verified; current bytes identified by SHA256"
    return catalog


def normalize(sample: str, source_root: Path = ROOT / "data/external", *, catalog=None) -> dict:
    catalog = CATALOG if catalog is None else catalog
    if sample not in catalog:
        raise ValueError("unsupported sample; only explicitly registered local samples are accepted")
    spec = catalog[sample]
    payloads, files = [], []
    for relative in spec["files"]:
        path = (source_root / relative).resolve(strict=True)
        path.relative_to(source_root.resolve(strict=True))
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("source exceeds 2 MiB; nothing imported, no truncation")
        raw = path.read_bytes()
        payloads.append(json.loads(raw.decode("utf-8")))
        files.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    meta = payloads[0]
    if not isinstance(meta, dict):
        raise ValueError("metadata must be an object")
    who = spec["source"] == "Who&When"
    rows = meta.get("history") if who else payloads[1]
    case = meta.get("question_ID") if who else meta.get("run_id")
    task = meta.get("question") if who else meta.get("task_instruction")
    if not isinstance(rows, list) or not rows or len(rows) > 500 or not isinstance(case, str) or not isinstance(task, str):
        raise ValueError("required case ID, task or 1..500 records missing; nothing imported")
    if redact(case) != case:
        raise ValueError("sensitive case identity; nothing imported")
    identity = digest([ADAPTER_VERSION, PROJECT, spec["source"], spec["version"], case, files])
    root_id = digest([identity, "group"])[:16]
    records, issues = [], []
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise ValueError(f"record {index} is not an object; nothing imported")
        safe = redact(raw)
        pointer = f"/history/{index}" if who else f"/{index}"
        if who and not isinstance(raw.get("content"), str):
            raise ValueError(f"history {index} has no text content; unsupported schema")
        if not who and ("step_id" not in raw or not isinstance(raw.get("input"), dict) or not isinstance(raw.get("output"), dict)):
            raise ValueError(f"step {index} requires step_id/input/output; unsupported schema")
        # A chat completion response provides evidence of an LLM call; a message does not.
        llm = not who and raw["output"].get("object") == "chat.completion" and isinstance(raw["output"].get("choices"), list)
        kind = "message" if who else "model_call" if llm else "agent_activity"
        original_id = index if who else raw["step_id"]
        if not isinstance(original_id, (int, str)) or isinstance(original_id, bool) or redact(str(original_id)) != str(original_id):
            raise ValueError("unsupported or sensitive step identity; nothing imported")
        actor = raw.get("name") if who else raw.get("agent_name")
        if actor is not None and not isinstance(actor, str):
            raise ValueError("actor must be a string or absent")
        if actor is not None and redact(actor) != actor:
            raise ValueError("sensitive actor identity; source refused")
        created = raw.get("output", {}).get("created") if not who else None
        original_time = None
        if isinstance(created, (int, float)) and not isinstance(created, bool):
            try:
                original_time = datetime.fromtimestamp(created, timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                issues.append(f"{pointer}/output/created: invalid timestamp, retained as source data")
        tools = safe.get("tool_logs") if not who else None
        if tools:
            issues.append(f"{pointer}/tool_logs: tool-call declarations only; no verified execution/result span")
        record = {
            "index": index, "original_step_id": original_id,
            "numbering": "zero-based history index (no source step ID)" if who else "source step_id unchanged; array index separately retained",
            "source_file": files[0 if who else 1]["path"], "source_pointer": pointer,
            "kind": kind, "agent_name": actor, "role": safe.get("role"),
            "span_id": digest([identity, pointer])[:16], "parent_id": root_id,
            "parent_relation": "import-generated grouping, NOT a source call relationship",
            "request": None if who else safe["input"], "response": None if who else safe["output"],
            "historical_message": safe["content"] if who else None,
            "request_pointer": None if who else pointer + "/input",
            "response_pointer": None if who else pointer + "/output",
            "message_pointer": pointer + "/content" if who else None,
            "tool_declarations": tools, "raw_record_redacted": safe,
            "redacted": safe != raw, "truncated": False, "original_time": original_time,
            "time_source": pointer + "/output/created" if original_time else None,
            "time_precision": "Unix seconds, response creation only; call duration unknown" if original_time else "unknown",
            "duration_ms": None, "cost": None, "checkpoint_id": None,
            "prompt_reconstructed": False, "source_status": "not provided",
        }
        record["content_sha256"] = digest(safe)
        records.append(record)
    result = {
        "sample": sample, "import_id": identity, "trace_id": identity[:32], "project": PROJECT,
        "source": spec["source"], "source_version": spec["version"], "source_url": spec["url"],
        "version_source": spec.get("version_source", "pinned local acquisition documentation; current bytes identified by SHA256, no network revalidation"),
        "source_license": spec["license"], "case_id": case, "task": redact(task),
        "files": files, "adapter_version": ADAPTER_VERSION,
        "trace_origin": "imported_historical", "executed_by_m1_local": False, "external_task_executed": False,
        "original_steps": len(rows), "retained_steps": len(records), "excluded_steps": 0,
        "exclusion_reasons": [], "expected_spans": len(records) + 1,
        "task_failed": meta.get("is_correct") is False if "is_correct" in meta else None,
        "integrity": "All supplied records retained; dataset history, not a complete runtime capture",
        "completeness": {"source_file_parsed": True, "truncated": False,
            "termination_message_present": who and raw.get("content", "").strip() == "TERMINATE",
            "original_prompts": not who, "durations": False, "call_parentage": False,
            "source_completeness_guaranteed": False},
        "issues": ["No executable checkpoints: historical viewing only; Replay/Fork forbidden.",
                   "Missing values are not zero. Span timings are display coordinates; do not compare performance.",
                   "Dataset task failure does not establish a span exception."] + issues,
        "records": records, "root_span_id": root_id,
    }
    # Metadata and system prompts are retained, but are NOT reconstructed into call input.
    result["source_metadata_redacted"] = redact({k: v for k, v in meta.items() if k != "history"})
    return result


def spans_for(trace: dict) -> list[dict]:
    base = {
        "trace_origin": "imported_historical", "executed_by_m1_local": False, "external_task_executed": False,
        "historical.import_id": trace["import_id"], "historical.source": trace["source"],
        "historical.source_version": trace["source_version"], "dataset_case_id": trace["case_id"],
        "historical.adapter_version": ADAPTER_VERSION, "historical.source_files": encoded(trace["files"]),
        "historical.real_duration_known": False, "historical.time_is_display_only": True,
        "historical.checkpoint_available": False, "historical.task_failed": encoded(trace["task_failed"]),
    }
    times = [r["original_time"] for r in trace["records"] if r["original_time"]]
    # Preserve known instants; otherwise a fixed epoch, NEVER the import time.
    fallback = "2000-01-01T00:00:00+00:00"
    result = [{"name": "Imported history [generated grouping; not execution]", "span_kind": "CHAIN",
               "context": {"trace_id": trace["trace_id"], "span_id": trace["root_span_id"]},
               "start_time": min(times) if times else fallback, "end_time": max(times) if times else fallback,
               "status_code": "UNSET", "attributes": {**base, "historical.group_generated": True,
                   "historical.task": trace["task"], "historical.original_steps": trace["original_steps"],
                   "historical.source_metadata": encoded(trace["source_metadata_redacted"])}}]
    for r in trace["records"]:
        attrs = {**base, "agent_name": r["agent_name"] or "not provided", "step_number": r["original_step_id"],
                 "historical.record_kind": r["kind"], "historical.source_file": r["source_file"],
                 "historical.source_pointer": r["source_pointer"], "historical.numbering": r["numbering"],
                 "historical.original_time": r["original_time"] or "not provided",
                 "historical.time_precision": r["time_precision"], "historical.parent_relation": r["parent_relation"],
                 "historical.content_sha256": r["content_sha256"], "historical.redacted": r["redacted"],
                 "historical.raw_record": encoded(r["raw_record_redacted"]), "historical.truncated": False}
        if r["request"] is not None:
            attrs.update({"input.value": encoded(r["request"]), "input.mime_type": "application/json",
                          "output.value": encoded(r["response"]), "output.mime_type": "application/json",
                          "historical.input_source": r["request_pointer"], "historical.output_source": r["response_pointer"]})
        else:
            attrs["historical.message"] = r["historical_message"]
            attrs["historical.input_output_missing"] = "Original call request/response not provided; this is a message"
        # Keep model/usage inside the original response, not Phoenix cost estimation attributes.
        # No measured cost/duration was provided; Phoenix may otherwise estimate cost from model name.
        result.append({"name": f"{r['original_step_id']} · {r['agent_name'] or 'unknown'} · {r['kind']}",
                       "context": {"trace_id": trace["trace_id"], "span_id": r["span_id"]},
                       "parent_id": r["parent_id"], "span_kind": "LLM" if r["kind"] == "model_call" else "AGENT" if r["kind"] == "agent_activity" else "UNKNOWN",
                       "start_time": r["original_time"] or fallback, "end_time": r["original_time"] or fallback,
                       "status_code": "UNSET", "attributes": attrs})
    return result


class PhoenixHistory:
    """Pinned public API subset; no remote redirects/proxy/env/auth discovery."""
    def __init__(self, endpoint: str):
        self.endpoint = local_endpoint(endpoint)
        self.client = httpx.Client(base_url=self.endpoint + "/", trust_env=False, follow_redirects=False, timeout=15)

    def project(self, create=False):
        response = self.client.get(f"v1/projects/{PROJECT}")
        if response.status_code == 404:
            if not create:
                return None
            response = self.client.post("v1/projects", json={"name": PROJECT, "description": "Imported historical records; synthetic grouping/timing, not m1 executions; exclude from performance comparisons."})
        response.raise_for_status()
        return response.json()["data"]

    def read(self, trace_id):
        if not self.project():
            return []
        # Server 11.35 lacks the client's newer trace_ids filter. Paginate all spans.
        result, cursor, seen = [], None, set()
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = self.client.get(f"v1/projects/{PROJECT}/spans", params=params)
            response.raise_for_status()
            body = response.json()
            result.extend(s for s in body["data"] if s["context"]["trace_id"] == trace_id)
            cursor = body.get("next_cursor")
            if not cursor:
                return result
            if cursor in seen:
                raise ValueError("Phoenix pagination repeated cursor; cannot establish import status")
            seen.add(cursor)

    def write(self, spans):
        self.project(create=True)
        response = self.client.post(f"v1/projects/{PROJECT}/spans", json={"data": spans})
        response.raise_for_status()
        body = response.json()
        if body.get("total_queued") != len(spans):
            raise ValueError("Phoenix did not acknowledge the entire batch; reconcile before retry")


class HistoryStore:
    def __init__(self, endpoint: str, directory: Path = ROOT / "data/observation", source_root: Path = ROOT / "data/external", remote=None, *, catalog=None):
        self.endpoint = local_endpoint(endpoint)
        self.directory, self.source_root = directory, source_root
        self.catalog = load_catalog() if catalog is None else catalog
        self.remote = remote or PhoenixHistory(self.endpoint)
        self.lock = threading.Lock()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.ownership = RuntimeOwnership([directory / "imports"])

    def _path(self, identity):
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("invalid import identity")
        return self.directory / (identity + ".json")

    def _save(self, record):
        target = self._path(record["trace"]["import_id"])
        temp = target.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(encoded(record))
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(target)

    def get(self, identity):
        return json.loads(self._path(identity).read_text(encoding="utf-8"))

    def list(self):
        return [self.summary(json.loads(p.read_text(encoding="utf-8"))) for p in sorted(self.directory.glob("*.json"))]

    @staticmethod
    def summary(record):
        return {**{k: v for k, v in record.items() if k != "trace"},
                "trace": {k: v for k, v in record["trace"].items() if k not in {"records", "source_metadata_redacted"}}}

    def preview(self, sample):
        trace = normalize(sample, self.source_root, catalog=self.catalog)
        return {k: v for k, v in trace.items() if k not in {"records", "source_metadata_redacted"}}

    def verify(self, record):
        trace = record["trace"]
        actual = self.remote.read(trace["trace_id"])
        expected = {s["context"]["span_id"]: s for s in spans_for(trace)}
        ids = [s["context"]["span_id"] for s in actual]
        if len(ids) != len(set(ids)) or any(i not in expected for i in ids):
            raise ValueError("duplicate/unexpected spans; automatic import stopped")
        for span in actual:
            wanted = expected[span["context"]["span_id"]]
            attrs = span.get("attributes", {})
            # Phoenix may expand dotted keys; normalize without losing long values.
            def flatten(d, prefix=""):
                out = {}
                for k, v in d.items():
                    key = prefix + k
                    out.update(flatten(v, key + ".") if isinstance(v, dict) else {key: v})
                return out
            flat = flatten(attrs)
            if any(flat.get(k) != v for k, v in wanted["attributes"].items()):
                raise ValueError("Phoenix attribute read-back mismatch (content/identity); no further writes")
            if span.get("parent_id") != wanted.get("parent_id") or span["span_kind"].upper() != wanted["span_kind"]:
                raise ValueError("Phoenix parent/kind mismatch; no further writes")
            if span.get("name") != wanted["name"] or span.get("status_code", "").upper() != wanted["status_code"]:
                raise ValueError("Phoenix name/status mismatch; no further writes")
            for field in ("start_time", "end_time"):
                if datetime.fromisoformat(span[field].replace("Z", "+00:00")) != datetime.fromisoformat(wanted[field]):
                    raise ValueError("Phoenix time mismatch; no further writes")
        return set(ids), expected

    def import_sample(self, sample, expected_id):
        with self.lock:
            self.ownership.acquire()
            try:
                trace = normalize(sample, self.source_root, catalog=self.catalog)
                if trace["import_id"] != expected_id:
                    raise ValueError("source changed since preview; preview again before import")
                path = self._path(expected_id)
                record = self.get(expected_id) if path.exists() else {"trace": trace, "attempted": [], "status": "planned", "imported_at": datetime.now(timezone.utc).isoformat(), "endpoint": self.endpoint}
                if record["endpoint"] != self.endpoint:
                    raise ValueError("ledger belongs to a different Phoenix endpoint; use a separate ledger directory")
                present, expected = self.verify(record)
                missing = set(expected) - present
                # A previous timeout/crash/202 can still be queued. NEVER blindly resend these IDs.
                uncertain = missing.intersection(record["attempted"])
                send = [s for i, s in expected.items() if i in missing and i not in uncertain]
                record.update({"present": sorted(present), "missing": sorted(missing), "uncertain": sorted(uncertain)})
                if send:
                    record["attempted"] = sorted(set(record["attempted"]) | {s["context"]["span_id"] for s in send})
                    record["status"] = "sending"
                    self._save(record)  # Write-ahead journal, survives uncertain transport/process failure.
                    try:
                        self.remote.write(send)
                    except Exception:
                        record["status"] = "uncertain"
                        record["message"] = "Write acknowledgement unknown. Use reconcile; attempted IDs will not be resent. No data was deleted."
                        self._save(record)
                        return self.summary(record)
                for _ in range(10 if send else 1):
                    present, expected = self.verify(record)
                    if len(present) == len(expected):
                        break
                    if send:
                        time.sleep(0.2)
                record.update({"present": sorted(present), "missing": sorted(set(expected) - present),
                               "uncertain": sorted((set(expected) - present).intersection(record["attempted"])),
                               "status": "verified" if len(present) == len(expected) else "partial",
                               "message": "All identities, full redacted content and parent/kind verified" if len(present) == len(expected) else "Reconcile later. Unattempted records can be imported; attempted-but-absent records require operator confirmation of Phoenix queue failure, not automatic resend."})
                self._save(record)
                return self.summary(record)
            finally:
                self.ownership.release()
