"""Convert raw agent trajectories that are not OpenTelemetry into OTLP JSON payloads.

The Studio's external traces are OpenTelemetry spans. Agents that keep their own logs instead (no OTel) can still be
imported once their records are mapped onto spans. Each converter returns OTLP JSON (ExportTraceServiceRequest with
resourceSpans), so the result goes through the same ingest path as any OTLP exporter.

Supported formats
  codex   OpenAI Codex session files (desktop app, IDE extension and CLI share the format):
          %USERPROFILE%\\.codex\\sessions\\YYYY\\MM\\DD\\rollout-<time>-<id>.jsonl

Codex mapping (one trace per session file)
  session                         -> root span            (CHAIN)  model, cwd, Codex version
  task_started .. task_complete   -> one span per turn     (AGENT)  input: user message, output: final answer
  token_usage_record              -> one span per model response (LLM)  token counts; output: the text and tool calls
                                     that response produced; starts when its input was ready (turn start or the
                                     previous tool result), ends when the response was recorded
  *_tool_call / function_call + matching *_output (by call_id)
                                  -> tool span            (TOOL)   command, exit code, output; a non-zero exit code is
                                                                    an error span (the turn itself can still succeed)
  item_completed FileChange       -> child of the tool call that made it (TOOL "file change")
  longrca / trajectory   message-level trajectory datasets (LongRCA, Who&When-style): JSON, JSON Lines or Parquet
          records with a `history` (or `messages`) list; one span per message, numbered by the message's `step`.
          See the section further down.

Dropped on purpose (Codex): system/developer instructions, tool definitions, world state, account ids and encrypted reasoning
(large, private, and not part of what the agent did).

Inference, stated in span attributes where it applies: Codex does not record when a model call starts, so an LLM
span starts when its input was complete; a tool call starts when the model response that requested it was recorded.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

MAX_TEXT = 20_000
CODEX_RECORD_TYPES = {"session_meta", "response_item", "event_msg", "turn_context", "token_usage_record", "world_state"}
TOOL_CALL_TYPES = {"custom_tool_call", "function_call", "local_shell_call", "tool_search_call"}
TOOL_OUTPUT_TYPES = {"custom_tool_call_output", "function_call_output", "local_shell_call_output", "tool_search_output"}


class ConversionError(ValueError):
    pass


def _ns(ts: str) -> int:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1_000_000_000)


def _span_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16]


def _trim(text: Any) -> str:
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + f"\n… [truncated, {len(s) - MAX_TEXT} more characters]"


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": _trim(value)}
    return {"key": key, "value": v}


def _text(content: Any) -> str:
    """Text of a Codex message content list ([{type: input_text|output_text|text|Text, text}])."""
    if isinstance(content, str):
        return content
    parts = []
    for item in content or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def _output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(_text([o]) for o in output if isinstance(o, dict))
    if isinstance(output, dict):
        return _text([output]) or json.dumps(output, ensure_ascii=False)
    return str(output)


def parse_jsonl(text: str) -> list[dict[str, Any]] | None:
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(row, dict):
            return None
        rows.append(row)
    return rows


def is_codex(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return False
    types = {r.get("type") for r in rows}
    return "session_meta" in types and types <= CODEX_RECORD_TYPES | {"compacted", "turn_aborted"} or (
        types <= CODEX_RECORD_TYPES and "response_item" in types and all("payload" in r for r in rows[:20]))


class _Turn:
    def __init__(self, turn_id: str, start: int, index: int) -> None:
        self.id, self.start, self.index = turn_id, start, index
        self.end: int | None = None
        self.user_text = ""
        self.final_text = ""
        self.error: str | None = None
        self.llms: list[dict[str, Any]] = []
        self.tools: dict[str, dict[str, Any]] = {}
        self.tool_order: list[str] = []
        self.loose: list[dict[str, Any]] = []      # file changes / commands outside any tool call
        self.cursor = start                        # when the next model call's input was complete
        self.pending: list[str] = []               # what the current model response produced so far
        self.pending_calls: list[str] = []


def codex_to_otlp(rows: list[dict[str, Any]], filename: str = "") -> dict[str, Any]:
    meta = next((r.get("payload") or {} for r in rows if r.get("type") == "session_meta"), {})
    session_id = str(meta.get("session_id") or meta.get("id") or hashlib.sha256(filename.encode()).hexdigest()[:32])
    trace_id = re.sub(r"[^0-9a-f]", "", session_id.lower())[:32].ljust(32, "0")
    model = ""
    times = [_ns(r["timestamp"]) for r in rows if r.get("timestamp")]
    if not times:
        raise ConversionError("the Codex file has no timestamps")
    turns: list[_Turn] = []
    current: _Turn | None = None
    open_tools: list[str] = []

    def turn_for(ts: int) -> _Turn:
        nonlocal current
        if current is None:   # records outside task_started/task_complete (older files): open an implicit turn
            current = _Turn(f"implicit-{len(turns)}", ts, len(turns) + 1)
            turns.append(current)
        return current

    for row in rows:
        if not row.get("timestamp"):
            continue
        ts, kind, p = _ns(row["timestamp"]), row.get("type"), row.get("payload") or {}
        ptype = p.get("type")
        if kind == "event_msg" and ptype == "thread_settings_applied":
            model = (p.get("thread_settings") or {}).get("model") or model
        elif kind == "turn_context" and p.get("model"):
            model = p["model"]
        if kind == "event_msg" and ptype == "task_started":
            current = _Turn(str(p.get("turn_id") or len(turns)), ts, len(turns) + 1)
            turns.append(current)
            continue
        if kind in {"session_meta", "world_state", "turn_context"}:
            continue
        if kind == "event_msg" and ptype in {"task_complete", "turn_aborted"}:
            turn = turn_for(ts)
            turn.end = ts
            turn.final_text = p.get("last_agent_message") or turn.final_text
            if ptype == "turn_aborted":
                turn.error = str(p.get("reason") or "turn aborted")
            current = None
            continue
        if kind == "event_msg" and ptype == "error":
            turn_for(ts).error = str(p.get("message") or "error")
            continue
        if kind == "event_msg" and ptype == "item_completed":
            item = p.get("item") or {}
            turn = turn_for(ts)
            if item.get("type") == "UserMessage" and not turn.user_text:
                turn.user_text = _text(item.get("content")).strip()
                turn.cursor = ts
            elif item.get("type") == "CommandExecution":
                info = {"command": _command(item), "exit_code": item.get("exit_code"), "status": item.get("status"),
                        "duration_s": _duration(item.get("duration")), "output": item.get("aggregated_output")}
                target = turn.tools.get(open_tools[-1]) if open_tools else None
                (target.setdefault("commands", []) if target else turn.loose).append({**info, "ts": ts})
            elif item.get("type") == "FileChange":
                change = {"ts": ts, "changes": item.get("changes") or {}}
                target = turn.tools.get(open_tools[-1]) if open_tools else None
                (target.setdefault("files", []) if target else turn.loose).append(change)
            continue
        if kind == "response_item":
            turn = turn_for(ts)
            if ptype == "message" and p.get("role") == "user":
                text = _text(p.get("content")).strip()
                if text and not text.startswith("<") and not turn.user_text:
                    turn.user_text, turn.cursor = text, ts
            elif ptype == "message" and p.get("role") == "assistant":
                text = _text(p.get("content")).strip()
                if text:
                    turn.pending.append(text)
            elif ptype in TOOL_CALL_TYPES and p.get("call_id"):
                call_id = str(p["call_id"])
                args = p.get("input") if "input" in p else p.get("arguments", p.get("action"))
                turn.tools[call_id] = {"call_id": call_id, "name": p.get("name") or ptype, "input": args, "start": ts,
                                       "end": None, "output": None}
                turn.tool_order.append(call_id)
                turn.pending.append(f"[tool call] {p.get('name') or ptype}: {_trim(args)[:2000]}")
                turn.pending_calls.append(call_id)
                open_tools.append(call_id)
            elif ptype in TOOL_OUTPUT_TYPES and p.get("call_id") and str(p["call_id"]) in turn.tools:
                tool = turn.tools[str(p["call_id"])]
                tool["end"], tool["output"] = ts, _output_text(p.get("output"))
                if tool["call_id"] in open_tools:
                    open_tools.remove(tool["call_id"])
                turn.cursor = max(turn.cursor, ts)
            continue
        if kind == "token_usage_record":
            turn = turn_for(ts)
            usage = p.get("usage") or {}
            turn.llms.append({"start": min(turn.cursor, ts), "end": ts, "usage": usage, "response_id": p.get("response_id"),
                              "output": "\n\n".join(turn.pending), "calls": list(turn.pending_calls)})
            for call_id in turn.pending_calls:     # the tool runs after the response that requested it
                tool = turn.tools[call_id]
                if tool["end"] is None or ts < tool["end"]:
                    tool["start"] = max(tool["start"], ts)
            turn.pending, turn.pending_calls = [], []
            turn.cursor = ts

    if not turns:
        raise ConversionError("no Codex turns found (expected task_started / response_item records)")

    root_id = _span_id(trace_id, "root")
    spans: list[dict[str, Any]] = []
    end_all = max(times)

    def span(sid, parent, name, start, end, kind, attrs, error=None):
        s = {"traceId": trace_id, "spanId": sid, "name": name, "startTimeUnixNano": str(start),
             "endTimeUnixNano": str(max(end, start)), "attributes": [_attr("openinference.span.kind", kind)] + attrs}
        if parent:
            s["parentSpanId"] = parent
        if error:
            s["status"] = {"code": 2, "message": _trim(error)[:500]}
        spans.append(s)

    first_input = next((t.user_text for t in turns if t.user_text), "")
    last_output = next((t.final_text for t in reversed(turns) if t.final_text), "")
    root_error = next((t.error for t in turns if t.error), None)
    span(root_id, None, "codex session", min(times), end_all, "CHAIN", [
        _attr("input.value", first_input), _attr("output.value", last_output), _attr("codex.session_id", session_id),
        _attr("codex.turns", len(turns)), _attr("codex.cwd", meta.get("cwd") or ""),
        _attr("codex.version", meta.get("cli_version") or ""), _attr("codex.originator", meta.get("originator") or ""),
        _attr("llm.model_name", model), _attr("conversion.source", "codex rollout jsonl"),
        _attr("conversion.file", filename)], error=root_error)

    for turn in turns:
        tid = _span_id(trace_id, "turn", turn.index)
        t_end = turn.end or max([turn.start] + [l["end"] for l in turn.llms] + [t["end"] or t["start"] for t in turn.tools.values()])
        usage_total = sum(int((l["usage"] or {}).get("total_tokens") or 0) for l in turn.llms)
        span(tid, root_id, f"turn {turn.index}", turn.start, t_end, "AGENT", [
            _attr("agent.name", "codex"), _attr("input.value", turn.user_text), _attr("output.value", turn.final_text),
            _attr("llm.model_name", model), _attr("llm.token_count.total", usage_total),
            _attr("codex.turn_id", turn.id), _attr("codex.model_calls", len(turn.llms)),
            _attr("codex.tool_calls", len(turn.tools))], error=turn.error)
        for i, llm in enumerate(turn.llms, 1):
            u = llm["usage"] or {}
            span(_span_id(trace_id, "llm", turn.index, i), tid, f"model call {i}", llm["start"], llm["end"], "LLM", [
                _attr("llm.model_name", model), _attr("output.value", llm["output"]),
                _attr("llm.token_count.prompt", int(u.get("input_tokens") or 0)),
                _attr("llm.token_count.completion", int(u.get("output_tokens") or 0)),
                _attr("llm.token_count.total", int(u.get("total_tokens") or 0)),
                _attr("llm.token_count.prompt_details.cache_read", int(u.get("cached_input_tokens") or 0)),
                _attr("codex.response_id", llm["response_id"] or ""),
                _attr("codex.requested_tool_calls", len(llm["calls"])),
                _attr("conversion.timing", "start inferred: when the model's input was complete")])
        for n, call_id in enumerate(turn.tool_order, 1):
            tool = turn.tools[call_id]
            commands = tool.get("commands") or []
            exit_codes = [c["exit_code"] for c in commands if c.get("exit_code") is not None]
            if not exit_codes and tool["output"]:
                exit_codes = [int(m) for m in re.findall(r'"exit_code"\s*:\s*(-?\d+)', tool["output"])]
            failed = next((c for c in exit_codes if c != 0), None)
            label = commands[0]["command"] if commands else str(tool["name"])
            sid = _span_id(trace_id, "tool", turn.index, n)
            end = tool["end"] or t_end
            span(sid, tid, f"{tool['name']}: {label[:80]}", tool["start"], end, "TOOL", [
                _attr("tool.name", str(tool["name"])), _attr("input.value", tool["input"] if tool["input"] is not None else ""),
                _attr("output.value", tool["output"] or ""), _attr("codex.call_id", call_id),
                _attr("codex.commands", json.dumps([{k: v for k, v in c.items() if k not in ("ts", "output")} for c in commands], ensure_ascii=False)),
                _attr("codex.exit_codes", json.dumps(exit_codes)),
                _attr("conversion.timing", "start: when the model response requesting the call was recorded")]
                 + ([] if tool["end"] else [_attr("codex.unfinished", True)]),
                 error=(f"command exited with code {failed}: {label[:200]}" if failed is not None else None))
            for k, change in enumerate(tool.get("files") or [], 1):
                _file_span(span, trace_id, sid, turn.index, f"{n}.{k}", change)
        for k, change in enumerate([c for c in turn.loose if "changes" in c], 1):
            _file_span(span, trace_id, tid, turn.index, f"loose.{k}", change)

    return {"resourceSpans": [{
        "resource": {"attributes": [_attr("service.name", "codex"), _attr("telemetry.source", "converted from Codex session log")]},
        "scopeSpans": [{"scope": {"name": "m1_local.studio.converters.codex"}, "spans": spans}]}]}


def _file_span(span, trace_id: str, parent: str, turn_index: int, key: str, change: dict[str, Any]) -> None:
    files = change.get("changes") or {}
    summary = {path: (info or {}).get("type") for path, info in files.items()}
    content = {path: (info or {}).get("content") or (info or {}).get("unified_diff") or "" for path, info in files.items()}
    names = ", ".join(p.replace("\\", "/").rsplit("/", 1)[-1] for p in files) or "files"
    span(_span_id(trace_id, "file", turn_index, key), parent, f"file change: {names[:80]}", change["ts"], change["ts"], "TOOL", [
        _attr("tool.name", "file change"), _attr("input.value", json.dumps(summary, ensure_ascii=False)),
        _attr("output.value", json.dumps(content, ensure_ascii=False)), _attr("codex.files", len(files))])


def _command(item: dict[str, Any]) -> str:
    parsed = item.get("parsed_cmd") or []
    if parsed and isinstance(parsed[0], dict) and parsed[0].get("cmd"):
        return str(parsed[0]["cmd"])
    cmd = item.get("command")
    if isinstance(cmd, list):
        return " ".join(map(str, cmd[-1:])) if len(cmd) >= 3 else " ".join(map(str, cmd))
    return str(cmd or "")


def _duration(d: Any) -> float | None:
    if isinstance(d, dict):
        return round(float(d.get("secs", 0)) + float(d.get("nanos", 0)) / 1e9, 3)
    return d if isinstance(d, (int, float)) else None


# ---- message-level trajectory datasets (LongRCA, Who&When, TrajDebug-style) -----------------------------------
#
# One record = one failed trajectory: {question_ID, history: [{step, role, name, content}, ...], mistake_agent,
# mistake_step, mistake_reason, ...}. The research group counts steps at message level: every entry of `history` is
# one step (an agent message, <think> and action together, is one step; the environment's reply, e.g.
# Computer_terminal, is the next step). Each entry becomes one span carrying `dataset.step`, the entry's own `step`
# value (or its list index when the entry has none), so step numbers in the Studio are exactly the dataset's and a
# localisation result "step 25" points at the same message everywhere.

MESSAGE_LIST_KEYS = ("history", "messages", "trajectory", "original_traj")
LABEL_STEP_KEYS = ("mistake_step", "failure_step", "error_step", "root_cause_step")
ENV_HINTS = ("terminal", "environment", "observation", "computer", "executor", "tool", "shell", "browser")
SYNTHETIC_START_NS = 1_767_225_600 * 1_000_000_000  # 2026-01-01T00:00:00Z: datasets carry no timestamps


def _messages(record: dict[str, Any]) -> list[Any] | None:
    for key in MESSAGE_LIST_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value[:1] == "[":
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                continue
        if hasattr(value, "tolist"):
            value = value.tolist()
        if isinstance(value, list) and value and all(isinstance(m, dict) for m in value[:5]) \
                and any(k in value[0] for k in ("content", "thought", "action", "observation", "response")):
            return value
    return None


def find_trajectories(data: Any) -> list[dict[str, Any]] | None:
    """Trajectory records in parsed JSON / JSON Lines / Parquet rows, or None when it is not this kind of file."""
    if isinstance(data, dict):
        for key in ("data", "rows", "trajectories", "records"):
            if isinstance(data.get(key), list):
                return find_trajectories(data[key])
        data = [data]
    if not isinstance(data, list) or not data:
        return None
    records = [r for r in data if isinstance(r, dict) and _messages(r) is not None]
    return records if records and len(records) == len([r for r in data if isinstance(r, dict)]) else None


def _content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if content is None:  # turn-level datasets (thought / action / observation in one entry)
        parts = [f"{k}: {message[k]}" for k in ("thought", "action", "observation", "response") if message.get(k)]
        return "\n\n".join(str(p) for p in parts)
    if isinstance(content, list):
        return _text(content) or json.dumps(content, ensure_ascii=False, default=str)
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)


def _message_kind(message: dict[str, Any], position: int) -> str:
    role = str(message.get("role") or "").lower()
    name = str(message.get("name") or message.get("agent") or "").lower()
    if role in ("tool", "function", "ipython", "observation") or any(h in name for h in ENV_HINTS):
        return "TOOL"
    if role in ("assistant", "ai", "agent") or (name and name not in ("user", "human", "system")):
        return "AGENT"
    return "CHAIN"  # the task / system prompt / a human message


def trajectory_to_otlp(record: dict[str, Any], filename: str = "", index: int = 0, dataset: str = "trajectory") -> dict[str, Any]:
    messages = _messages(record) or []
    qid = str(record.get("question_ID") or record.get("trajectory_id") or record.get("instance_id") or record.get("id")
              or f"{filename or 'trajectory'}#{index}")
    trace_id = hashlib.sha256(f"trajectory|{dataset}|{qid}".encode()).hexdigest()[:32]
    root_id = _span_id(trace_id, "root")
    spans: list[dict[str, Any]] = []

    def span(sid, parent, name, start, end, kind, attrs):
        s = {"traceId": trace_id, "spanId": sid, "name": name, "startTimeUnixNano": str(start),
             "endTimeUnixNano": str(end), "attributes": [_attr("openinference.span.kind", kind)] + attrs}
        if parent:
            s["parentSpanId"] = parent
        spans.append(s)

    label_step = next((record.get(k) for k in LABEL_STEP_KEYS if record.get(k) not in (None, "")), None)
    second = 1_000_000_000
    for position, message in enumerate(messages):
        raw_step = message.get("step", position)
        try:
            step = int(raw_step)
        except (TypeError, ValueError):
            step = position
        kind = _message_kind(message, position)
        name = str(message.get("name") or message.get("agent") or message.get("role") or "message")
        content = _content(message)
        think = re.findall(r"<think>(.*?)</think>", content, flags=re.S)
        attrs = [_attr("dataset.step", step), _attr("dataset.position", position),
                 _attr("message.role", str(message.get("role") or "")), _attr("message.name", name),
                 _attr("output.value" if kind != "CHAIN" else "input.value", content),
                 _attr("conversion.timing", "synthetic: the dataset has no timestamps; one second per message keeps the order")]
        if kind == "AGENT":
            attrs.append(_attr("agent.name", name))
        elif kind == "TOOL":
            attrs.append(_attr("tool.name", name))
        if think:
            attrs.append(_attr("message.think", "\n".join(t.strip() for t in think)))
        start = SYNTHETIC_START_NS + (position + 1) * second
        span(_span_id(trace_id, "msg", position), root_id, f"step {step} · {name}", start, start + second - 1, kind, attrs)

    root_attrs = [_attr("dataset.name", dataset), _attr("dataset.messages", len(messages)),
                  _attr("dataset.step_numbering", "each history entry is one step; numbers are the entries' own `step` values"),
                  _attr("conversion.source", f"{dataset} trajectory record"), _attr("conversion.file", filename)]
    for key, value in record.items():
        if key in MESSAGE_LIST_KEYS or value is None or isinstance(value, (list, dict)) or hasattr(value, "tolist"):
            continue
        root_attrs.append(_attr(f"dataset.{key}", value.item() if hasattr(value, "item") else value))
    if label_step is not None:
        root_attrs.append(_attr("dataset.label_step", int(label_step)))
    task = _content(messages[0]) if messages else ""
    root_attrs.append(_attr("input.value", record.get("question") or task))
    span(root_id, None, qid, SYNTHETIC_START_NS, SYNTHETIC_START_NS + (len(messages) + 1) * second, "CHAIN", root_attrs)
    return {"resourceSpans": [{
        "resource": {"attributes": [_attr("service.name", dataset),
                                    _attr("telemetry.source", f"converted from {dataset} trajectory dataset")]},
        "scopeSpans": [{"scope": {"name": "m1_local.studio.converters"}, "spans": spans}]}]}


def trajectory_run_id(question_id: str, dataset: str = "longrca") -> str:
    """Run id the Studio gives an imported trajectory (used to attach localisation results by question id)."""
    return "ext_" + hashlib.sha256(f"trajectory|{dataset}|{question_id}".encode()).hexdigest()[:32]


def _dataset_name(records: list[dict[str, Any]], filename: str) -> str:
    low = filename.lower()
    if "longrca" in low or any("mistake_step" in r and "mistake_agent" in r for r in records[:3]):
        return "longrca"
    return "trajectory"


def trajectories_to_payloads(records: list[dict[str, Any]], filename: str = "") -> list[dict[str, Any]]:
    dataset = _dataset_name(records, filename)
    return [trajectory_to_otlp(r, filename, i, dataset) for i, r in enumerate(records)]


def parquet_rows(data: bytes) -> list[dict[str, Any]]:
    import io
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ConversionError("reading .parquet needs pyarrow: run `pip install pyarrow` in the Studio's environment, "
                              "or upload the dataset's .json files instead") from exc
    return pq.read_table(io.BytesIO(data)).to_pylist()


def convert_bytes(data: bytes, filename: str = "") -> tuple[str, list[dict[str, Any]]] | None:
    """Binary uploads: Parquet trajectory datasets."""
    if data[:4] != b"PAR1":
        return None
    records = find_trajectories(parquet_rows(data))
    if not records:
        raise ConversionError("the Parquet file has no trajectory column (expected `history` / `messages` lists)")
    return _dataset_name(records, filename), trajectories_to_payloads(records, filename)


def convert(text: str, filename: str = "") -> tuple[str, list[dict[str, Any]]] | None:
    """(format name, OTLP payloads) when the text is a known non-OTel trajectory format, else None."""
    rows = parse_jsonl(text)
    if rows and not any("resourceSpans" in r for r in rows[:5]) and is_codex(rows):
        return "codex", [codex_to_otlp(rows, filename)]
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        data = rows
    if data is not None and not (isinstance(data, dict) and "resourceSpans" in data):
        records = find_trajectories(data)
        if records:
            return _dataset_name(records, filename), trajectories_to_payloads(records, filename)
    return None
