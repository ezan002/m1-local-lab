"""Workflow definition model and structural validation.

V1 supports a deliberately small set of node types that all execute for real:
start, agent (LLM, optional tool loop), tool, condition and output.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NodeType = Literal["start", "agent", "tool", "condition", "output"]
ToolName = Literal["calculator", "http_get", "word_count"]
ConditionOp = Literal["equals", "not_equals", "contains", "not_contains", "greater_than", "less_than", "is_true"]
_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class InputField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    label: str = ""
    default: str = ""


class NodeConfig(BaseModel):
    """Union of per-type fields; validated per type in WorkflowNode."""
    model_config = ConfigDict(extra="forbid")
    # start
    inputs: list[InputField] = Field(default_factory=list)
    # agent
    role: str = ""
    system_prompt: str = ""
    prompt: str = ""
    provider: Literal["inherit", "openai", "deterministic"] = "inherit"
    model: str = ""
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=600, ge=16, le=8000)
    tools: list[ToolName] = Field(default_factory=list)
    max_tool_rounds: int = Field(default=3, ge=0, le=8)
    mock_response: str = ""
    mock_tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    # tool
    tool: ToolName | None = None
    arguments: dict[str, str] = Field(default_factory=dict)
    # condition
    left: str = ""
    op: ConditionOp = "contains"
    right: str = ""
    max_true_visits: int = Field(default=3, ge=1, le=20)
    # output
    template: str = ""


class WorkflowNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: NodeType
    label: str = ""
    position: dict[str, float] = Field(default_factory=lambda: {"x": 0.0, "y": 0.0})
    config: NodeConfig = Field(default_factory=NodeConfig)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError("node id must match ^[a-z][a-z0-9_]{0,39}$")
        return value


class WorkflowEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = ""
    source: str
    target: str
    branch: Literal["", "true", "false"] = ""


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge] = Field(default_factory=list)
    default_provider: Literal["openai", "deterministic"] = "deterministic"
    default_model: str = ""

    @model_validator(mode="after")
    def structural_checks(self) -> "WorkflowDefinition":
        problems = validate_structure(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def node(self, node_id: str) -> WorkflowNode:
        return next(n for n in self.nodes if n.id == node_id)


def validate_structure(defn: WorkflowDefinition) -> list[str]:
    problems: list[str] = []
    ids = [n.id for n in defn.nodes]
    if len(ids) != len(set(ids)):
        problems.append("duplicate node ids")
    by_id = {n.id: n for n in defn.nodes}
    starts = [n for n in defn.nodes if n.type == "start"]
    outputs = [n for n in defn.nodes if n.type == "output"]
    if len(starts) != 1:
        problems.append("exactly one start node is required")
    if not outputs:
        problems.append("at least one output node is required")
    outgoing: dict[str, list[WorkflowEdge]] = defaultdict(list)
    incoming: dict[str, int] = defaultdict(int)
    for edge in defn.edges:
        if edge.source not in by_id or edge.target not in by_id:
            problems.append(f"edge {edge.source}->{edge.target} references an unknown node")
            continue
        outgoing[edge.source].append(edge)
        incoming[edge.target] += 1
    for node in defn.nodes:
        out = outgoing[node.id]
        cfg = node.config
        if node.type == "start" and incoming[node.id]:
            problems.append("start node cannot have incoming edges")
        if node.type == "output" and out:
            problems.append(f"output node {node.id} cannot have outgoing edges")
        if node.type == "condition":
            branches = sorted(e.branch for e in out)
            if branches != ["false", "true"]:
                problems.append(f"condition {node.id} needs exactly one 'true' and one 'false' edge")
            if not cfg.left:
                problems.append(f"condition {node.id} needs a left operand")
        elif node.type != "output":
            if len(out) != 1:
                problems.append(f"node {node.id} ({node.type}) needs exactly one outgoing edge")
            if any(e.branch for e in out):
                problems.append(f"only condition nodes may use branch edges ({node.id})")
        if node.type == "agent" and not cfg.prompt.strip():
            problems.append(f"agent {node.id} needs a prompt")
        if node.type == "tool" and cfg.tool is None:
            problems.append(f"tool node {node.id} needs a tool")
    if len(starts) == 1 and not problems:
        seen, queue = {starts[0].id}, deque([starts[0].id])
        while queue:
            for edge in outgoing[queue.popleft()]:
                if edge.target not in seen:
                    seen.add(edge.target)
                    queue.append(edge.target)
        unreachable = [i for i in ids if i not in seen]
        if unreachable:
            problems.append("unreachable nodes: " + ", ".join(unreachable))
        # Every cycle must pass through a condition node, otherwise it can never terminate.
        plain = {n.id for n in defn.nodes if n.type != "condition"}
        if _has_cycle(plain, [(e.source, e.target) for e in defn.edges if e.source in plain and e.target in plain]):
            problems.append("cycles must pass through a condition node")
    return problems


def _has_cycle(nodes: set[str], edges: list[tuple[str, str]]) -> bool:
    graph: dict[str, list[str]] = defaultdict(list)
    for a, b in edges:
        graph[a].append(b)
    state: dict[str, int] = {}

    def visit(n: str) -> bool:
        state[n] = 1
        for m in graph[n]:
            if state.get(m) == 1 or (m not in state and visit(m)):
                return True
        state[n] = 2
        return False

    return any(n not in state and visit(n) for n in nodes)


# Fields a fork may change. Everything else in checkpoint state is runtime bookkeeping.
FORKABLE_PREFIXES = ("input.", "nodes.")
