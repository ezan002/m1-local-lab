import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Background, Controls, MiniMap, ReactFlow, ReactFlowProvider, addEdge, useEdgesState, useNodesState, useReactFlow } from "@xyflow/react";
import { api, fmtTime, meta } from "../api.js";
import { nodeStatusFromSpans, nodeTypes, toFlow } from "../components/FlowNode.jsx";
import { ErrorBox, Field, Icon, Modal, ProviderBadge, Status } from "../components/ui.jsx";
import NodeInspector from "../components/NodeInspector.jsx";

const PALETTE = [
  { type: "agent", label: "Agent", desc: "LLM agent, optional tools" },
  { type: "tool", label: "Tool", desc: "calculator · http_get · word_count" },
  { type: "condition", label: "Condition", desc: "true / false routing" },
  { type: "output", label: "Output", desc: "final templated result" },
  { type: "start", label: "Start", desc: "inputs (one per workflow)" },
];
const DEFAULT_CONFIG = {
  agent: { role: "agent", system_prompt: "", prompt: "{{input.question}}", provider: "inherit", tools: [], mock_response: "Scripted reply" },
  tool: { tool: "calculator", arguments: { expression: "1 + 1" } },
  condition: { left: "", op: "contains", right: "", max_true_visits: 3 },
  output: { template: "" },
  start: { inputs: [{ name: "question", label: "Question", default: "" }] },
};
const ACTIVE = new Set(["queued", "running", "pausing"]);

function toDefinition(base, nodes, edges) {
  return {
    ...base,
    nodes: nodes.map((n) => ({ id: n.id, type: n.data.nodeType, label: n.data.label || "", position: { x: Math.round(n.position.x), y: Math.round(n.position.y) }, config: n.data.config })),
    edges: edges.map((e) => ({ id: e.id, source: e.source, target: e.target, branch: e.sourceHandle || "" })),
  };
}

function Editor({ workflowId }) {
  const [wf, setWf] = useState(null);
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [dirty, setDirty] = useState(false);
  const [issues, setIssues] = useState([]);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [runs, setRuns] = useState([]);
  const [selectedRun, setSelectedRun] = useState(null);
  const [runSpans, setRunSpans] = useState([]);
  const [showRun, setShowRun] = useState(false);
  const [info, setInfo] = useState(null);
  const flow = useReactFlow();
  const base = useRef({});

  const load = useCallback(async () => {
    const data = await api(`/api/workflows/${workflowId}`);
    setWf(data);
    const { default_provider, default_model } = data.definition;
    base.current = { default_provider, default_model };
    const f = toFlow(data.definition);
    setNodes(f.nodes); setEdges(f.edges); setDirty(false);
    setTimeout(() => flow.fitView({ padding: 0.2, duration: 0 }), 60);
  }, [workflowId, setNodes, setEdges, flow]);

  useEffect(() => { load().catch(setError); meta().then(setInfo); }, [load]);

  const loadRuns = useCallback(() => api(`/api/runs?workflow_id=${workflowId}&limit=30`).then(setRuns).catch(() => {}), [workflowId]);
  useEffect(() => { loadRuns(); const t = setInterval(loadRuns, 3000); return () => clearInterval(t); }, [loadRuns]);

  // Poll the selected run's spans for live node highlighting.
  useEffect(() => {
    if (!selectedRun) { setRunSpans([]); return undefined; }
    let stop = false;
    const tick = async () => {
      try {
        const [run, spans] = await Promise.all([api(`/api/runs/${selectedRun}`), api(`/api/runs/${selectedRun}/spans`)]);
        if (stop) return;
        setRunSpans(spans);
        setRuns((rs) => rs.map((r) => (r.id === run.id ? { ...r, status: run.status } : r)));
        if (ACTIVE.has(run.status) || run.active) setTimeout(tick, 700);
      } catch { /* run list poll will surface errors */ }
    };
    tick();
    return () => { stop = true; };
  }, [selectedRun]);

  // Validate on every structural change (debounced).
  const definition = useMemo(() => (wf ? toDefinition(base.current, nodes, edges) : null), [wf, nodes, edges]);
  useEffect(() => {
    if (!definition) return undefined;
    const t = setTimeout(() => {
      api("/api/workflows/validate", { method: "POST", body: definition }).then((r) => setIssues(r.errors)).catch(() => {});
    }, 300);
    return () => clearTimeout(t);
  }, [definition]);

  const selectedRunRecord = runs.find((r) => r.id === selectedRun);
  const overlay = useMemo(() => nodeStatusFromSpans(runSpans, Object.fromEntries(nodes.map((n) => [n.id, n.data.nodeType]))), [runSpans, nodes]);
  const displayNodes = useMemo(() => nodes.map((n) => ({
    ...n, data: { ...n.data, status: selectedRun ? (overlay.status[n.id] || (selectedRunRecord && !ACTIVE.has(selectedRunRecord.status) ? "skipped" : undefined)) : undefined,
                  visits: selectedRun ? overlay.visits[n.id] || 0 : 0 },
  })), [nodes, overlay, selectedRun, selectedRunRecord]);

  const onConnect = useCallback((params) => {
    setEdges((eds) => {
      const filtered = eds.filter((e) => !(e.source === params.source && (e.sourceHandle || null) === (params.sourceHandle || null)));
      const branch = params.sourceHandle || "";
      return addEdge({ ...params, id: `e_${Date.now()}`, label: branch || undefined,
                       style: branch === "true" ? { stroke: "var(--ok)" } : branch === "false" ? { stroke: "var(--err)" } : undefined }, filtered);
    });
    setDirty(true);
  }, [setEdges]);

  const addNode = (type) => {
    const existing = new Set(nodes.map((n) => n.id));
    let i = 1; while (existing.has(`${type}_${i}`)) i += 1;
    const id = `${type}_${i}`;
    const center = flow.screenToFlowPosition({ x: window.innerWidth / 2, y: window.innerHeight / 2 });
    setNodes((nds) => [...nds, { id, type: "wf", position: { x: center.x - 80 + nds.length * 8, y: center.y - 30 },
                                 data: { id, nodeType: type, label: type[0].toUpperCase() + type.slice(1) + ` ${i}`, config: structuredClone(DEFAULT_CONFIG[type]) } }]);
    setSelectedId(id); setDirty(true);
  };

  const updateNode = (id, patch) => {
    setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } } : n)));
    setDirty(true);
  };
  const deleteNode = (id) => {
    setNodes((nds) => nds.filter((n) => n.id !== id));
    setEdges((eds) => eds.filter((e) => e.source !== id && e.target !== id));
    setSelectedId(null); setDirty(true);
  };

  const save = async () => {
    setSaving(true); setError(null);
    try {
      const saved = await api(`/api/workflows/${workflowId}`, { method: "PUT", body: { name: wf.name, description: wf.description, definition } });
      setWf(saved); setDirty(false);
      return saved;
    } catch (e) { setError(e); return null; } finally { setSaving(false); }
  };

  const control = async (action) => {
    try { await api(`/api/runs/${selectedRun}/${action}`, { method: "POST" }); setSelectedRun(null); setTimeout(() => setSelectedRun(selectedRun), 50); loadRuns(); }
    catch (e) { setError(e); }
  };

  if (!wf) return <div className="page"><ErrorBox error={error} />{!error && <span className="muted">Loading…</span>}</div>;
  const selectedNode = nodes.find((n) => n.id === selectedId);
  const startNode = nodes.find((n) => n.data.nodeType === "start");

  return (
    <div className="editor">
      <div className="editor-top">
        <a href="#/workflows" className="btn ghost" title="All workflows"><Icon name="back" /></a>
        <input className="title" value={wf.name} aria-label="Workflow name" onChange={(e) => { setWf({ ...wf, name: e.target.value }); setDirty(true); }} />
        <span className="muted" style={{ fontSize: 12.5 }}>v{wf.version}{dirty ? " · unsaved changes" : " · saved"}</span>
        {issues.length > 0 && <span className="status status-failed" title={issues.join("\n")}>{issues.length} issue(s)</span>}
        <span className="spacer" />
        {selectedRunRecord && ["running", "queued"].includes(selectedRunRecord.status) && <button onClick={() => control("pause")}><Icon name="pause" /> Pause</button>}
        {selectedRunRecord && ["paused", "failed", "interrupted", "cancelled"].includes(selectedRunRecord.status) && <button onClick={() => control("resume")}><Icon name="play" /> {selectedRunRecord.status === "failed" ? "Retry from failure" : "Resume"}</button>}
        <button onClick={save} disabled={saving || !dirty}><Icon name="save" /> {saving ? "Saving…" : "Save"}</button>
        <button className="primary" onClick={() => setShowRun(true)} disabled={issues.length > 0} title={issues.length ? "Fix validation issues first" : "Run this workflow"}><Icon name="play" /> Run</button>
      </div>
      <div className="editor-body">
        <div className="palette">
          <h4>Add node</h4>
          {PALETTE.map((p) => (
            <button key={p.type} className="palette-item" onClick={() => addNode(p.type)} disabled={p.type === "start" && !!startNode}>
              <span className={`fn-type t-${p.type}`} style={{ width: 22, height: 22, borderRadius: 6, display: "grid", placeItems: "center", color: "#fff" }}><Icon name={p.type} size={13} /></span>
              <span><strong>{p.label}</strong><small>{p.desc}</small></span>
            </button>
          ))}
          <h4 style={{ marginTop: 18 }}>Tips</h4>
          <div className="note" style={{ fontSize: 12 }}>Drag from a node's right handle to connect. Condition nodes have green (true) and red (false) outputs. Select an edge and press Delete to remove it.</div>
        </div>
        <div className="canvas">
          <ReactFlow nodes={displayNodes} edges={edges} nodeTypes={nodeTypes} onNodesChange={(c) => { onNodesChange(c); if (c.some((x) => x.type === "position" && x.dragging === false || x.type === "remove")) setDirty(true); }}
            onEdgesChange={(c) => { onEdgesChange(c); if (c.some((x) => x.type === "remove")) setDirty(true); }} onConnect={onConnect}
            onNodeClick={(_, n) => setSelectedId(n.id)} onPaneClick={() => setSelectedId(null)} fitView fitViewOptions={{ padding: 0.25 }}
            deleteKeyCode={["Delete"]} colorMode="system" proOptions={{ hideAttribution: true }}>
            <Background gap={18} size={1} />
            <Controls showInteractive={false} />
            <MiniMap pannable zoomable style={{ height: 90, width: 140 }} />
          </ReactFlow>
        </div>
        <div className="inspector">
          <ErrorBox error={error} />
          {issues.length > 0 && <div className="error-box">{issues.map((i) => <div key={i}>• {i}</div>)}</div>}
          {selectedNode ? (
            <NodeInspector key={selectedNode.id} node={selectedNode} allNodes={nodes} onChange={(patch) => updateNode(selectedNode.id, patch)}
              onDelete={() => deleteNode(selectedNode.id)} spans={runSpans.filter((s) => s.node_id === selectedNode.id)} runId={selectedRun} />
          ) : (
            <>
              <h4>Workflow</h4>
              <Field label="Description"><textarea value={wf.description} onChange={(e) => { setWf({ ...wf, description: e.target.value }); setDirty(true); }} /></Field>
              <Field label="Default agent provider" hint="Agents set to 'inherit' use this. A run can override it.">
                <select value={base.current.default_provider} onChange={(e) => { base.current = { ...base.current, default_provider: e.target.value }; setNodes((n) => [...n]); setDirty(true); }}>
                  <option value="openai">openai (real model, needs OPENAI_API_KEY)</option>
                  <option value="deterministic">deterministic (scripted, offline)</option>
                </select>
              </Field>
              <Field label="Default model" hint={`Empty = server default (${info?.provider.default_model || "…"})`}>
                <input value={base.current.default_model || ""} onChange={(e) => { base.current = { ...base.current, default_model: e.target.value }; setNodes((n) => [...n]); setDirty(true); }} />
              </Field>
              <div className="note">Select a node to edit it. Templates reference data with <code>{"{{input.<field>}}"}</code> and <code>{"{{nodes.<id>.output}}"}</code>; add <code>?</code> (e.g. <code>{"{{nodes.reviewer.output?}}"}</code>) for values that may not exist yet.</div>
            </>
          )}
        </div>
      </div>
      <div className="run-strip">
        <strong style={{ fontSize: 12.5 }}>Runs</strong>
        {runs.length === 0 && <span className="muted" style={{ fontSize: 12.5 }}>No runs yet — press Run.</span>}
        {runs.map((r) => (
          <span key={r.id} className={`run-chip ${r.id === selectedRun ? "selected" : ""}`} onClick={() => setSelectedRun(r.id === selectedRun ? null : r.id)} title={r.id}>
            <Status value={r.status} /> {r.relation ? <span className="badge badge-accent">{r.relation}</span> : null} {fmtTime(r.created_at)}
            <a href={`#/runs/${r.id}`} onClick={(e) => e.stopPropagation()}>open</a>
          </span>
        ))}
      </div>
      {showRun && (
        <RunDialog wf={wf} startNode={startNode} nodes={nodes} info={info} onClose={() => setShowRun(false)}
          beforeRun={async () => (dirty ? !!(await save()) : true)}
          onStarted={(run) => { setShowRun(false); setSelectedRun(run.id); loadRuns(); }} />
      )}
    </div>
  );
}

function RunDialog({ wf, startNode, nodes, info, onClose, beforeRun, onStarted }) {
  const fields = startNode?.data.config.inputs || [];
  const [inputs, setInputs] = useState(Object.fromEntries(fields.map((f) => [f.name, f.default || ""])));
  const [provider, setProvider] = useState("");
  const [breakpoints, setBreakpoints] = useState([]);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const keyOk = info?.provider.openai_key_configured;
  const start = async () => {
    setBusy(true); setError(null);
    try {
      if (!(await beforeRun())) { setError("Save failed — fix the workflow first."); return; }
      const run = await api(`/api/workflows/${wf.id}/runs`, { method: "POST", body: { inputs, provider_override: provider || null, breakpoints } });
      onStarted(run);
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return (
    <Modal title={`Run “${wf.name}”`} onClose={onClose}
      footer={<><button onClick={onClose}>Cancel</button><button className="primary" onClick={start} disabled={busy}><Icon name="play" /> {busy ? "Starting…" : "Start run"}</button></>}>
      <ErrorBox error={error} />
      {fields.map((f) => (
        <Field key={f.name} label={f.label || f.name}>
          <textarea rows={2} value={inputs[f.name]} onChange={(e) => setInputs({ ...inputs, [f.name]: e.target.value })} />
        </Field>
      ))}
      <Field label="Agent provider for this run" hint="Deterministic runs use each agent's scripted response and are labeled as such; they are not model-backed evidence.">
        <select value={provider} onChange={(e) => setProvider(e.target.value)}>
          <option value="">As configured in the workflow</option>
          <option value="openai" disabled={!keyOk}>Real model (OpenAI-compatible){keyOk ? "" : " — no key configured"}</option>
          <option value="deterministic">Deterministic script (offline)</option>
        </select>
      </Field>
      <Field label="Breakpoints" hint="Execution pauses before these nodes; resume from the run view.">
        <div className="row">
          {nodes.filter((n) => n.data.nodeType !== "start").map((n) => (
            <label key={n.id} className="check"><input type="checkbox" checked={breakpoints.includes(n.id)}
              onChange={(e) => setBreakpoints(e.target.checked ? [...breakpoints, n.id] : breakpoints.filter((b) => b !== n.id))} />{n.data.label || n.id}</label>
          ))}
        </div>
      </Field>
    </Modal>
  );
}

export default function EditorPage({ workflowId }) {
  return <ReactFlowProvider><Editor workflowId={workflowId} /></ReactFlowProvider>;
}
