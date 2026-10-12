import React from "react";
import { Handle, Position } from "@xyflow/react";
import { Icon, Status } from "./ui.jsx";

const SUB = {
  start: (c) => `${(c.inputs || []).length} input(s)`,
  agent: (c) => [c.role || "agent", (c.tools || []).length ? `tools: ${c.tools.join(", ")}` : null].filter(Boolean).join(" · "),
  tool: (c) => c.tool || "choose tool",
  condition: (c) => `${c.op || ""} ${c.right || ""}`.trim(),
  output: () => "final result",
};

export function WorkflowNodeView({ data, selected }) {
  const t = data.nodeType;
  const cfg = data.config || {};
  const st = data.status;
  return (
    <div className={`flow-node ${selected ? "selected" : ""} ${st ? "st-" + st : ""}`} data-testid={`node-${data.id}`}>
      {t !== "start" && <Handle type="target" position={Position.Left} />}
      <div className="fn-head">
        <div className={`fn-type t-${t}`}><Icon name={t} size={14} /></div>
        <div style={{ minWidth: 0 }}>
          <div className="fn-label">{data.label || data.id}</div>
          <div className="fn-sub">{t} · {SUB[t] ? SUB[t](cfg) : ""}</div>
        </div>
      </div>
      {(st || data.visits > 1 || data.breakpoint) && (
        <div className="fn-foot">
          {st ? <Status value={st === "skipped" ? "not run" : st === "inherited" ? "inherited" : st} /> : <span />}
          <span className="muted">
            {data.breakpoint ? "● breakpoint " : ""}{data.visits > 1 ? `×${data.visits}` : ""}
          </span>
        </div>
      )}
      {t === "condition" ? (
        <>
          <Handle type="source" id="true" position={Position.Right} style={{ top: "32%", background: "var(--ok)" }} />
          <Handle type="source" id="false" position={Position.Right} style={{ top: "72%", background: "var(--err)" }} />
          <span className="handle-label" style={{ top: "22%", color: "var(--ok)" }}>true</span>
          <span className="handle-label" style={{ top: "62%", color: "var(--err)" }}>false</span>
        </>
      ) : t !== "output" ? (
        <Handle type="source" position={Position.Right} />
      ) : null}
    </div>
  );
}

export const nodeTypes = { wf: WorkflowNodeView };

export function toFlow(definition, statusByNode = {}, visitsByNode = {}, breakpoints = []) {
  const nodes = definition.nodes.map((n) => ({
    id: n.id, type: "wf", position: n.position || { x: 0, y: 0 },
    data: { id: n.id, nodeType: n.type, label: n.label, config: n.config || {}, status: statusByNode[n.id],
            visits: visitsByNode[n.id] || 0, breakpoint: breakpoints.includes(n.id) },
  }));
  const edges = definition.edges.map((e, i) => ({
    id: e.id || `e_${i}_${e.source}_${e.target}`, source: e.source, target: e.target,
    sourceHandle: e.branch || null, label: e.branch || undefined, animated: false,
    style: e.branch === "true" ? { stroke: "var(--ok)" } : e.branch === "false" ? { stroke: "var(--err)" } : undefined,
    labelStyle: { fontSize: 11, fontWeight: 700 },
  }));
  return { nodes, edges };
}

/** Status per workflow node from a run's spans (last execution wins; counts visits). */
export function nodeStatusFromSpans(spans, typeById = {}) {
  const status = {}, visits = {};
  for (const s of spans || []) {
    if (!s.node_id) continue;
    if (s.kind === "pause") { if (s.status === "paused") status[s.node_id] = "paused"; continue; }
    // Only the node's own span counts as an execution (child llm/tool spans share the node_id).
    if (typeById[s.node_id] ? s.kind !== typeById[s.node_id] : !["start", "agent", "tool", "condition", "output"].includes(s.kind)) continue;
    status[s.node_id] = s.status === "ok" ? "ok" : s.status;
    visits[s.node_id] = (visits[s.node_id] || 0) + 1;
  }
  return { status, visits };
}
