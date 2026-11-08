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
Dropped on purpose: system/developer instructions, tool definitions, world state, account ids and encrypted reasoning
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


def convert(text: str, filename: str = "") -> tuple[str, list[dict[str, Any]]] | None:
    """(format name, OTLP payloads) when the text is a known non-OTel trajectory format, else None."""
    rows = parse_jsonl(text)
    if rows and not any("resourceSpans" in r for r in rows[:5]) and is_codex(rows):
        return "codex", [codex_to_otlp(rows, filename)]
    return None
