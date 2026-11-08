"""Step view: a run as a numbered list of its atomic actions.

A span can be coarse: a node span covers everything one node did, and an agent span may hold several model and
tool calls. The step view flattens a run into the actions the agent actually took, numbered 1..N in time order:

- every model call (``llm``) and tool call (``tool``, ``retriever``, ``embedding``, ``reranker``) is one step;
  nested calls of the same kind (a file change recorded under the tool call that made it) belong to the outer step;
- the same model call recorded twice by two instrumentations (for example LangChain's ``ChatOpenAI`` span and the
  agent's own ``m1.model.*`` span) is listed once, keeping the record with more content;
- a graph node that made no model or tool call (a condition, Start, Output, or a node of an uninstrumented agent)
  is one step of its own, so control steps are not lost;
- a routing decision of a LangGraph conditional edge (e.g. ``after_orchestrator`` choosing the next worker) is one
  control step, like a condition node of a Studio workflow;
- plumbing spans (other ``chain`` spans such as prompt/parse sequences and checkpoint saves, the workflow root, pause
  and edit markers) are never steps.

``span_roles`` explains every span of the run against the steps (is step N, same call as step N, part of step N,
contains steps a-b, or no operation), so the reader can check how N spans became M steps.

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

    # 4. Routing decisions: LangGraph runs a conditional edge as a childless chain directly under the node that owns it
    #    (OpenInference names it after the routing function). Plumbing runnables are excluded by name.
    def is_route(span: dict[str, Any]) -> bool:
        parent = by_id.get(span.get("parent_span_id") or "")
        grand = by_id.get((parent or {}).get("parent_span_id") or "")
        name = span.get("name") or ""
        return span.get("kind") == "chain" and not children.get(span["id"]) and parent is not None \
            and parent.get("kind") == "chain" and grand is not None and grand.get("name") == "LangGraph" \
            and not name.startswith(("Runnable", "Channel", "__", "_write", "_route"))

    routes = [s for s in spans if is_route(s)]

    # 5. Leaves of external traces that are neither actions nor plumbing (e.g. an agent span with no calls).
    chosen = {s["id"] for s in actions} | {s["id"] for s in node_steps} | {s["id"] for s in routes}
    leaves = [s for s in spans if s["id"] not in chosen and not children.get(s["id"]) and s["id"] not in graph_ids
              and s.get("kind") not in NEVER_STEPS and s.get("kind") not in ACTION_KINDS
              and not any(a["id"] in chosen for a in ancestors(s))
              and not any(is_graph_node(a) for a in ancestors(s))]

    def owner(span: dict[str, Any]) -> str | None:
        if span in routes:
            node = by_id[span["parent_span_id"]].get("name")
            return next((n.get("name") for n in graph_nodes if n.get("node_id") == node), node)
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
    selected = actions + node_steps + routes + leaves
    selected.sort(key=lambda s: (_ts(s.get("start_time")) or float("inf"), order[s["id"]]))

    steps = []
    for number, s in enumerate(selected, start=1):
        s0, s1 = _ts(s.get("start_time")), _ts(s.get("end_time"))
        nested = [c["id"] for c in children.get(s["id"], [])] if s.get("kind") in ACTION_KINDS else []
        output = s.get("output")
        if s in routes:
            # The routing function's value may be redacted; the node that ran next is the decision that was taken.
            later = [n for n in graph_nodes if (_ts(n.get("start_time")) or 0) >= (s1 or s0 or 0) - COVER_SLACK_S
                     and n.get("node_id") != by_id[s["parent_span_id"]].get("name")]
            later.sort(key=lambda n: _ts(n.get("start_time")) or 0)
            chosen_next = later[0].get("name") if later else None
            if chosen_next and (output in (None, "", "__REDACTED__") or isinstance(output, str) and "REDACTED" in output):
                output = {"next": chosen_next, "source": "the node that ran next"}
        steps.append({
            "step": number,
            "span_id": s["id"],
            "kind": "route" if s in routes else s.get("kind"),
            "name": s.get("name"),
            "agent": owner(s),
            "status": s.get("status"),
            "start_time": s.get("start_time"),
            "end_time": s.get("end_time"),
            "duration_ms": round((s1 - s0) * 1000) if None not in (s0, s1) else None,
            "model": _model(s) if s.get("kind") == "llm" else None,
            "tokens": _tokens(s) if s.get("kind") == "llm" else {},
            "input": s.get("input"),
            "output": output,
            "error": s.get("error"),
            "nested_span_ids": nested,
            "duplicate_span_ids": s.pop("_duplicates", []),
        })
    for s in spans:
        s.pop("_duplicates", None)
    return steps


def span_roles(spans: list[dict[str, Any]], steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For every span: how it relates to the steps (so "26 spans -> 4 steps" can be checked span by span)."""
    by_id = {s["id"]: s for s in spans}
    step_of = {st["span_id"]: st["step"] for st in steps}
    dup_of = {d: st["step"] for st in steps for d in st.get("duplicate_span_ids", [])}
    part_of = {d: st["step"] for st in steps for d in st.get("nested_span_ids", [])}

    def chain_of(span_id: str) -> list[str]:
        out, seen, cur = [], set(), by_id.get(span_id)
        while cur and cur.get("parent_span_id") in by_id and cur["parent_span_id"] not in seen:
            seen.add(cur["parent_span_id"])
            out.append(cur["parent_span_id"])
            cur = by_id[cur["parent_span_id"]]
        return out

    contains: dict[str, set[int]] = {}
    for st in steps:
        for sid in [st["span_id"], *st.get("duplicate_span_ids", [])]:
            for anc in chain_of(sid):
                contains.setdefault(anc, set()).add(st["step"])
    # Spans that describe a node without being an ancestor of its calls (a node mirrored from checkpoints, or an
    # instrumentation's node span next to another instrumentation's calls) cover the steps of that node.
    for s in spans:
        key = (s.get("attributes") or {}).get("langgraph_node") or s.get("node_id")
        if not key or s["id"] in step_of:
            continue
        s0, s1 = _ts(s.get("start_time")), _ts(s.get("end_time"))
        for st in steps:
            t = _ts(st.get("start_time"))
            src = by_id.get(st["span_id"], {})
            skey = (src.get("attributes") or {}).get("langgraph_node") or src.get("node_id")
            if skey == key and None not in (s0, t) and s0 - COVER_SLACK_S <= t <= (s1 or t) + COVER_SLACK_S:
                contains.setdefault(s["id"], set()).add(st["step"])

    roles = []
    for s in spans:
        sid, kind = s["id"], s.get("kind")
        if sid in step_of:
            role, nums, why = "step", [step_of[sid]], "this span is the step"
        elif sid in dup_of:
            role, nums, why = "duplicate", [dup_of[sid]], "the same call recorded by another instrumentation"
        elif sid in part_of:
            role, nums, why = "part", [part_of[sid]], "recorded inside the call of this step"
        elif not s.get("parent_span_id") or s.get("parent_span_id") not in by_id:
            role, nums, why = "root", sorted(contains.get(sid, [])), "the whole run"
        elif contains.get(sid):
            role, nums = "container", sorted(contains[sid])
            why = "a node or agent turn that holds these steps" if kind != "chain" else "groups the calls below it"
        elif (s.get("name") or "").endswith("checkpoint.commit"):
            role, nums, why = "plumbing", [], "saves a checkpoint; no model or tool call"
        else:
            role, nums, why = "plumbing", [], "connects or formats data (prompt, parser, sequence); no model or tool call"
        roles.append({"span_id": sid, "name": s.get("name"), "kind": kind, "role": role, "steps": nums, "reason": why})
    return roles
