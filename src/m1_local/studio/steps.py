"""Step view: a run as a numbered list of its atomic actions.

A span can be coarse: a node span covers everything one node did, and an agent span may hold several model and
tool calls. The step view flattens a run into the actions the agent actually took, numbered 1..N in time order:

- every model call (``llm``) and tool call (``tool``, ``retriever``, ``embedding``, ``reranker``) is one step;
  nested calls of the same kind (a file change recorded under the tool call that made it) belong to the outer step;
- the same model call recorded twice by two instrumentations (for example LangChain's ``ChatOpenAI`` span and the
  agent's own ``m1.model.*`` span) is listed once, keeping the record with more content;
- a graph node that made no model or tool call (a condition, Start, Output, or a node of an uninstrumented agent)
  is one step of its own, so control steps are not lost;
- plumbing spans (``chain``, the workflow root, pause and edit markers) are never steps.

Each step keeps the id of the span it came from, so the UI can jump from a step to the full span.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

ACTION_KINDS = {"llm", "tool", "retriever", "embedding", "reranker"}
NEVER_STEPS = {"workflow", "pause", "edit", "chain"}
DUPLICATE_WINDOW_S = 0.25
COVER_SLACK_S = 1.0


def _ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _size(value: Any) -> int:
    if value is None:
        return 0
    return len(value) if isinstance(value, str) else len(repr(value))


def _model(span: dict[str, Any]) -> str | None:
    a = span.get("attributes") or {}
    return a.get("response_model") or a.get("model") or a.get("llm.model_name") or a.get("gen_ai.request.model")


def _tokens(span: dict[str, Any]) -> dict[str, int]:
    a = span.get("attributes") or {}
    usage = a.get("usage") if isinstance(a.get("usage"), dict) else {}
    result: dict[str, int] = {}
    for key, studio, otel in (("prompt", "prompt_tokens", "llm.token_count.prompt"),
                              ("completion", "completion_tokens", "llm.token_count.completion"),
                              ("total", "total_tokens", "llm.token_count.total")):
        value = usage.get(studio, a.get(otel))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = int(value)
    if "total" not in result and {"prompt", "completion"} <= result.keys():
        result["total"] = result["prompt"] + result["completion"]
    return result


def build_steps(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {s["id"]: s for s in spans}
    children: dict[str, list[dict[str, Any]]] = {}
    for s in spans:
        if s.get("parent_span_id") in by_id:
            children.setdefault(s["parent_span_id"], []).append(s)

    def ancestors(span: dict[str, Any]):
        seen, cur = set(), span
        while cur.get("parent_span_id") in by_id and cur["parent_span_id"] not in seen:
            seen.add(cur["parent_span_id"])
            cur = by_id[cur["parent_span_id"]]
            yield cur

    def is_graph_node(span: dict[str, Any]) -> bool:
        # A node of the agent's graph: hangs directly under the run's workflow root and names its node.
        parent = by_id.get(span.get("parent_span_id") or "")
        return bool(span.get("node_id")) and parent is not None and parent.get("kind") == "workflow" \
            and span.get("kind") not in NEVER_STEPS

    graph_nodes = [s for s in spans if is_graph_node(s)]
    graph_ids = {s["id"] for s in graph_nodes}

    # 1. Outermost action spans.
    actions = [s for s in spans if s.get("kind") in ACTION_KINDS and s["id"] not in graph_ids
               and not any(a.get("kind") in ACTION_KINDS and a["id"] not in graph_ids for a in ancestors(s))]

    # 2. The same call recorded by two instrumentations.
    def node_key(span: dict[str, Any]) -> str | None:
        a = span.get("attributes") or {}
        return a.get("langgraph_node") or a.get("node_name") or span.get("node_id")

    def richness(span: dict[str, Any]) -> int:
        return _size(span.get("input")) + _size(span.get("output"))

    kept: list[dict[str, Any]] = []
    for s in sorted(actions, key=richness, reverse=True):
        s0, s1 = _ts(s.get("start_time")), _ts(s.get("end_time"))
        twin = None
        for k in kept:
            k0, k1 = _ts(k.get("start_time")), _ts(k.get("end_time"))
            if (k.get("kind") == s.get("kind") and node_key(k) == node_key(s) and None not in (s0, s1, k0, k1)
                    and abs(s0 - k0) <= DUPLICATE_WINDOW_S and abs(s1 - k1) <= DUPLICATE_WINDOW_S):
                twin = k
                break
        if twin is None:
            kept.append(s)
        else:
            twin.setdefault("_duplicates", []).append(s["id"])
    actions = kept

    # 3. Graph nodes that made no model or tool call become steps themselves.
    def covers(node: dict[str, Any], action: dict[str, Any]) -> bool:
        if any(a["id"] == node["id"] for a in ancestors(action)):
            return True
        n0, n1 = _ts(node.get("start_time")), _ts(node.get("end_time"))
        a0 = _ts(action.get("start_time"))
        return node_key(action) == node.get("node_id") and None not in (n0, a0) \
            and n0 - COVER_SLACK_S <= a0 <= (n1 if n1 is not None else a0) + COVER_SLACK_S

    node_steps = [n for n in graph_nodes if not any(covers(n, a) for a in actions)]

    # 4. Leaves of external traces that are neither actions nor plumbing (e.g. an agent span with no calls).
    chosen = {s["id"] for s in actions} | {s["id"] for s in node_steps}
    leaves = [s for s in spans if s["id"] not in chosen and not children.get(s["id"]) and s["id"] not in graph_ids
              and s.get("kind") not in NEVER_STEPS and s.get("kind") not in ACTION_KINDS
              and not any(a["id"] in chosen for a in ancestors(s))
              and not any(is_graph_node(a) for a in ancestors(s))]

    def owner(span: dict[str, Any]) -> str | None:
        if span["id"] in graph_ids:
            return span.get("name") or span.get("node_id")
        for a in ancestors(span):
            if a["id"] in graph_ids:
                return a.get("name") or a.get("node_id")
        key = node_key(span)
        for n in graph_nodes:
            if key and n.get("node_id") == key:
                return n.get("name") or key
        for a in ancestors(span):
            if a.get("kind") == "agent":
                agent_name = (a.get("attributes") or {}).get("agent.name")
                if agent_name and agent_name != a.get("name"):
                    return f"{agent_name} · {a.get('name')}"
                return agent_name or a.get("name")
        return key

    order = {s["id"]: i for i, s in enumerate(spans)}
    selected = actions + node_steps + leaves
    selected.sort(key=lambda s: (_ts(s.get("start_time")) or float("inf"), order[s["id"]]))

    steps = []
    for number, s in enumerate(selected, start=1):
        s0, s1 = _ts(s.get("start_time")), _ts(s.get("end_time"))
        nested = [c["id"] for c in children.get(s["id"], [])] if s.get("kind") in ACTION_KINDS else []
        steps.append({
            "step": number,
            "span_id": s["id"],
            "kind": s.get("kind"),
            "name": s.get("name"),
            "agent": owner(s),
            "status": s.get("status"),
            "start_time": s.get("start_time"),
            "end_time": s.get("end_time"),
            "duration_ms": round((s1 - s0) * 1000) if None not in (s0, s1) else None,
            "model": _model(s) if s.get("kind") == "llm" else None,
            "tokens": _tokens(s) if s.get("kind") == "llm" else {},
            "input": s.get("input"),
            "output": s.get("output"),
            "error": s.get("error"),
            "nested_span_ids": nested,
            "duplicate_span_ids": s.pop("_duplicates", []),
        })
    for s in spans:
        s.pop("_duplicates", None)
    return steps
