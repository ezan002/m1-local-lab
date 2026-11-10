import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Background, Controls, ReactFlow, ReactFlowProvider } from "@xyflow/react";
import { api, duration, fmtTime, pretty } from "../api.js";
import { nodeStatusFromSpans, nodeTypes, toFlow } from "../components/FlowNode.jsx";
import { Badge, Empty, ErrorBox, Field, Icon, Json, Modal, ProviderBadge, Status, Tabs } from "../components/ui.jsx";

const ACTIVE = new Set(["queued", "running", "pausing", "receiving"]);
const CAP_LABEL = { execute: "execute", checkpoint: "checkpoints", pause_resume: "pause/resume", fork: "fork", replay: "replay", retry: "retry" };

function spanQuery() {
  const m = window.location.hash.match(/[?&]span=([\w-]+)/);
  return m ? m[1] : null;
}

export default function RunDetailPage({ runId }) {
  const [run, setRun] = useState(null);
  const [spans, setSpans] = useState([]);
  const [steps, setSteps] = useState([]);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState(spanQuery() ? "spans" : "graph");
  const [selectedSpan, setSelectedSpan] = useState(spanQuery());
  const [actionError, setActionError] = useState(null);

  const load = useCallback(async () => {
    // Sequential on purpose: spans are written before a run's status changes, so spans read after the run are at
    // least as new. Fetched in parallel, the last poll could show "completed" with the final node still missing.
    const r = await api(`/api/runs/${runId}`);
    const s = await api(`/api/runs/${runId}/spans`);
    const st = await api(`/api/runs/${runId}/steps`).catch(() => []);
    setRun(r); setSpans(s); setSteps(st);
    window.dispatchEvent(new CustomEvent("studio:run-origin", { detail: { studio: "runs", connected: "connected", external: "traces" }[r.origin] || "runs" }));
    return r;
  }, [runId]);

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const r = await load();
        if (!stop && (ACTIVE.has(r.status) || r.active)) setTimeout(tick, 800);
      } catch (e) { setError(e); }
    };
    tick();
    return () => { stop = true; };
  }, [load]);

  const act = async (action) => {
    setActionError(null);
    try { await api(`/api/runs/${runId}/${action}`, { method: "POST" }); setTimeout(() => load().then(() => { const poll = async () => { const r = await load(); if (ACTIVE.has(r.status) || r.active) setTimeout(poll, 800); }; poll(); }), 200); }
    catch (e) { setActionError(e); }
  };

  if (error) return <div className="page"><ErrorBox error={error} /><a href="#/runs">Back to runs</a></div>;
  if (!run) return <div className="page muted">Loading…</div>;
  const caps = run.capabilities || {};
  const external = run.origin === "external";
  const connected = run.origin === "connected";
  const errorSpans = spans.filter((s) => s.status === "error");

  return (
    <div className="page">
      <div className="run-head">
        <div>
          <div className="row muted" style={{ fontSize: 12.5, marginBottom: 6 }}>
            <a href={external ? "#/traces" : connected ? "#/connected" : "#/runs"}>{external ? "External traces" : connected ? "Connected agents" : "Runs"}</a> /
            {run.workflow_id && <a href={`#/workflows/${run.workflow_id}`}>{run.workflow_name}</a>}
          </div>
          <h1 className="row">{run.workflow_name || run.integration} <Status value={run.status} /></h1>
          <div className="row" style={{ fontSize: 12.5 }}>
            <span className="mono muted">{run.id}</span>
            {external ? <Badge tone="ro" title="No control channel into the external process">trace-only (read-only)</Badge> : <ProviderBadge mode={run.provider_mode} />}
            {connected && <Badge tone="accent" title="Runs in its own process; the Studio drives it through its control API">connected executor</Badge>}
            {connected && run.remote_run_id && <span className="muted">executor run <span className="mono">{run.remote_run_id.slice(0, 13)}…</span></span>}
            {run.workflow_version && <Badge>definition v{run.workflow_version}</Badge>}
            <span className="muted">started {fmtTime(run.started_at || run.created_at)} · {duration(run.started_at, run.ended_at)}</span>
          </div>
          <div className="lineage" style={{ marginTop: 8 }}>
            {run.parent && <>
              <Badge tone="accent">{run.relation}</Badge> of <a href={`#/runs/${run.parent.id}`} className="mono">{run.parent.id}</a>
              <span className="muted">from checkpoint <span className="mono">{run.source_checkpoint_id?.slice(0, 18)}…</span></span>
              {run.fork_changes && Object.keys(run.fork_changes).length > 0 && <span className="muted">changed: {Object.keys(run.fork_changes).join(", ")}</span>}
            </>}
            {run.children?.length > 0 && <span className="muted">branches:</span>}
            {run.children?.map((c) => <a key={c.id} href={`#/runs/${c.id}`} className="badge badge-accent">{c.relation} · {c.id.slice(4, 12)} · {c.status}</a>)}
          </div>
        </div>
        {!external && (
          <div className="row">
            {["running", "queued"].includes(run.status) && <button onClick={() => act("pause")}><Icon name="pause" /> Pause</button>}
            {run.active && caps.cancel !== false && <button className="danger" onClick={() => act("cancel")}><Icon name="stop" /> Cancel</button>}
            {["paused", "interrupted", "cancelled"].includes(run.status) && (!run.active || connected) && <button className="primary" onClick={() => act("resume")}><Icon name="play" /> Resume</button>}
            {run.status === "failed" && (!run.active || connected) && <button className="primary" onClick={() => act("resume")} title="Re-executes the failed node from the checkpoint before it"><Icon name="replay" /> Retry failed node</button>}
          </div>
        )}
      </div>
      <ErrorBox error={actionError} />
      {run.connector_unavailable && <div className="note">The executor is not reachable right now, so this page shows the last synced state. {run.connector_unavailable}</div>}
      {run.error && run.status !== "failed" && (
        // External traces: the run succeeded (its root span is ok) but some steps reported errors, e.g. a command an
        // agent tried that failed. That is information about a step, not a failed run.
        <div className="note">
          The run completed, but {run.error.error_span_count || errorSpans.length || 1} step(s) reported an error. First:{" "}
          <strong>{run.error.node_id || run.error.type}</strong> — {run.error.message}
          {errorSpans.length > 0 && <> <button className="small" style={{ marginLeft: 8 }} onClick={() => { setTab("spans"); setSelectedSpan(errorSpans[0].id); }}>Inspect error span</button></>}
        </div>
      )}
      {run.error && run.status === "failed" && (
        <div className="error-box">
          <strong>{run.error.type}</strong>{run.error.node_id ? ` in ${run.error.node_id}` : ""}: {run.error.message}
          {run.error.retry && <div style={{ marginTop: 4, opacity: .85 }}>{run.error.retry}</div>}
          {errorSpans.length > 0 && <div style={{ marginTop: 6 }}><button className="small" onClick={() => { setTab("spans"); setSelectedSpan(errorSpans[errorSpans.length - 1].id); }}>Inspect failing span</button></div>}
        </div>
      )}
      <div className="row" style={{ marginBottom: 10, fontSize: 12 }}>
        <span className="muted">Capabilities:</span>
        {Object.entries(CAP_LABEL).map(([k, label]) => <Badge key={k} tone={caps[k] ? "real" : "ro"}>{caps[k] ? "✓" : "✕"} {label}</Badge>)}
      </div>
      <Tabs value={tab} onChange={setTab} tabs={[
        { key: "graph", label: "Graph" }, { key: "timeline", label: "Timeline" }, { key: "spans", label: `Spans (${spans.length})` },
        { key: "steps", label: `Steps (${steps.length})`, title: "Every model call, tool call and control step, numbered in order" },
        { key: "checkpoints", label: "Checkpoints & fork", disabled: external, title: external ? "External runs expose no checkpoints" : "" },
        { key: "compare", label: "Compare" }, { key: "io", label: "Run input/output" },
      ]} />
      {tab === "graph" && <ReactFlowProvider><RunGraph run={run} spans={spans} onSelect={(id) => { setSelectedSpan(id); setTab("spans"); }} /></ReactFlowProvider>}
      {tab === "timeline" && <Timeline spans={spans} onSelect={(id) => { setSelectedSpan(id); setTab("spans"); }} />}
      {tab === "spans" && <SpanExplorer spans={spans} selected={selectedSpan} onSelect={setSelectedSpan} />}
      {tab === "steps" && <StepsView runId={runId} steps={steps} spans={spans} onChanged={load} onOpenSpan={(id) => { setSelectedSpan(id); setTab("spans"); }} />}
      {tab === "checkpoints" && !external && <Checkpoints run={run} />}
      {tab === "compare" && <Compare run={run} />}
      {tab === "io" && <div className="card card-pad"><Json label="Input" value={run.input} /><Json label="Output" value={run.output} /><Json label="Error" value={run.error} collapsed /></div>}
    </div>
  );
}

function RunGraph({ run, spans, onSelect }) {
  const flow = useMemo(() => {
    if (run.origin !== "external" && run.definition) {
      const { status, visits } = nodeStatusFromSpans(spans, Object.fromEntries(run.definition.nodes.map((n) => [n.id, n.type])));
      const done = !ACTIVE.has(run.status);
      const inherited = new Set(run.inherited?.nodes || []);
      const filled = Object.fromEntries(run.definition.nodes.map((n) => [n.id, status[n.id] || (inherited.has(n.id) ? "inherited" : done ? "skipped" : undefined)]));
      return toFlow(run.definition, filled, visits, run.breakpoints || []);
    }
    // External: lay out the span tree (depth -> x, order -> y).
    const byId = Object.fromEntries(spans.map((s) => [s.id, s]));
    const depth = (s) => { let d = 0, cur = s; while (cur.parent_span_id && byId[cur.parent_span_id] && d < 20) { d += 1; cur = byId[cur.parent_span_id]; } return d; };
    const nodes = spans.map((s, i) => ({ id: s.id, type: "wf", position: { x: depth(s) * 260, y: i * 92 },
      data: { id: s.id, nodeType: ["agent", "tool", "llm", "chain"].includes(s.kind) ? s.kind : "external", label: s.name,
              config: { role: s.node_id || s.kind, tool: s.attributes?.["tool.name"] || s.node_id || s.name }, status: s.status === "ok" ? "ok" : s.status } }));
    const edges = spans.filter((s) => s.parent_span_id && byId[s.parent_span_id]).map((s) => ({ id: `${s.parent_span_id}-${s.id}`, source: s.parent_span_id, target: s.id }));
    return { nodes, edges };
  }, [run, spans]);
  const nodeSpan = (nodeId) => {
    if (run.origin === "external") return nodeId;
    const type = run.definition.nodes.find((n) => n.id === nodeId)?.type;
    const list = spans.filter((s) => s.node_id === nodeId && s.kind === type);
    return list.length ? list[list.length - 1].id : null;
  };
  return (
    <div>
      <div className="graph-box">
        <ReactFlow nodes={flow.nodes} edges={flow.edges} nodeTypes={nodeTypes} fitView nodesDraggable={false} nodesConnectable={false}
          onNodeClick={(_, n) => { const id = nodeSpan(n.id); if (id) onSelect(id); }} colorMode="system" proOptions={{ hideAttribution: true }}>
          <Background gap={18} size={1} /><Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <p className="muted" style={{ fontSize: 12.5 }}>
        {run.origin === "connected" ? "Graph reported by the connected executor (dashed = conditional route). Status comes from the executor's own checkpoints: each committed checkpoint is one node execution (×N = repeated visits; inherited = executed before the branch point). Click a node to inspect its latest step."
          : run.origin === "studio" ? "Workflow graph with per-node status from this run (×N = executions in loops; faded = not executed; inherited = result copied from the parent run's checkpoint). Click a node to inspect its latest span."
          : "Span tree reported by the external process (parent → child). Click a node to inspect it."}
      </p>
    </div>
  );
}

function Timeline({ spans, onSelect }) {
  const timed = spans.filter((s) => s.start_time);
  if (!timed.length) return <Empty title="No timed spans" />;
  const t0 = Math.min(...timed.map((s) => +new Date(s.start_time)));
  const t1 = Math.max(...timed.map((s) => +new Date(s.end_time || Date.now())));
  const total = Math.max(t1 - t0, 1);
  const byId = Object.fromEntries(spans.map((s) => [s.id, s]));
  const depth = (s) => { let d = 0, cur = s; while (cur.parent_span_id && byId[cur.parent_span_id] && d < 10) { d += 1; cur = byId[cur.parent_span_id]; } return d; };
  return (
    <div className="card card-pad timeline">
      <div className="tl-row muted" style={{ cursor: "default" }}><span>span</span><span>0 → {duration(new Date(t0).toISOString(), new Date(t1).toISOString())}</span><span>duration</span></div>
      {timed.map((s) => {
        const start = +new Date(s.start_time) - t0;
        const end = +new Date(s.end_time || Date.now()) - t0;
        return (
          <div key={s.id} className="tl-row" onClick={() => onSelect(s.id)} title={`${s.name} · ${s.status}`}>
            <span style={{ paddingLeft: depth(s) * 14, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              <span className={`badge t-${s.kind}`} style={{ color: "#fff", border: "none", marginRight: 6 }}>{s.kind}</span>{s.name}
            </span>
            <div className="tl-track"><div className={`tl-bar ${s.status}`} style={{ left: `${(start / total) * 100}%`, width: `${Math.max(((end - start) / total) * 100, 0.4)}%` }} /></div>
            <span className="muted">{duration(s.start_time, s.end_time)}</span>
          </div>
        );
      })}
    </div>
  );
}

const STEP_KIND_LABEL = { route: "routing decision", llm: "agent message (model call)", tool: "tool result", retriever: "retrieval", embedding: "embedding", reranker: "rerank",
  start: "start", output: "output", condition: "condition", agent: "agent node", node: "node", external: "span" };

function fmtTokens(t) {
  if (!t || t.total === undefined) return null;
  return t.prompt !== undefined ? `${t.total} (in ${t.prompt} · out ${t.completion ?? "?"})` : `${t.total}`;
}

const ROLE_LABEL = {
  step: (n) => `is step ${n[0]}`,
  duplicate: (n) => `same call as step ${n[0]}`,
  part: (n) => `part of step ${n[0]}`,
  container: (n) => n.length ? `contains step${n.length > 1 ? "s" : ""} ${fmtRange(n)}` : "container",
  root: (n) => n.length ? `whole run (steps ${fmtRange(n)})` : "whole run",
  plumbing: () => "not a step",
  control: () => "control, not numbered",
};
const ROLE_TONE = { control: "accent", step: "real", duplicate: "det", part: "neutral", container: "neutral", root: "neutral", plumbing: "neutral" };

function fmtRange(nums) {
  if (!nums.length) return "";
  const out = []; let a = nums[0], b = nums[0];
  for (const n of nums.slice(1)) { if (n === b + 1) b = n; else { out.push(a === b ? `${a}` : `${a}–${b}`); a = b = n; } }
  out.push(a === b ? `${a}` : `${a}–${b}`);
  return out.join(", ");
}

function SpanStepMap({ runId, onOpenSpan }) {
  const [data, setData] = useState(null);
  useEffect(() => { api(`/api/runs/${runId}/step-map`).then(setData).catch(() => setData(null)); }, [runId]);
  if (!data) return null;
  const count = (role) => data.roles.filter((r) => r.role === role).length;
  return (
    <details className="card step-map">
      <summary><strong>How {data.spans} spans became {data.steps} steps</strong>
        <span className="muted"> · {count("step")} are steps · {count("duplicate")} duplicate records · {count("part")} part of a step · {count("container") + count("root")} containers · {count("control")} control · {count("plumbing")} not steps</span></summary>
      <p className="muted" style={{ margin: "8px 0 10px" }}>A span is any timed record an instrumentation chose to write, so its count depends on how the agent was instrumented: one model call can be recorded by two instrumentations, and nodes, sequences and the run itself are containers around the calls. A step is one message: an agent's message (a model call) or a result coming back from a tool or the environment.</p>
      <table className="step-map-table">
        <thead><tr><th>Span</th><th>Kind</th><th>Relation to steps</th><th>Why</th></tr></thead>
        <tbody>
          {data.roles.map((r) => (
            <tr key={r.span_id}>
              <td><a href="#" onClick={(e) => { e.preventDefault(); onOpenSpan(r.span_id); }} className="mono">{r.name}</a></td>
              <td><span className={`kind t-${r.kind}`} style={{ display: "inline-flex" }}><Icon name={r.kind} size={11} /></span> <span className="muted">{r.kind}</span></td>
              <td><Badge tone={ROLE_TONE[r.role]}>{(ROLE_LABEL[r.role] || (() => r.role))(r.steps)}</Badge></td>
              <td className="muted">{r.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

function stepKindLabel(s) {
  if (s.source === "dataset") return s.kind === "agent" ? "agent message" : s.kind === "tool" ? "environment / tool result" : "task / user message";
  return STEP_KIND_LABEL[s.kind] || s.kind;
}

function StepFlags({ step }) {
  return (
    <>
      {step.labels?.length > 0 && <span className="flag flag-label" title={`Dataset annotation: ${step.labels[0].agent || ""}`}>⚑ annotated</span>}
      {(step.marks || []).map((m) => <span key={m.source} className="flag flag-mark" title={m.note || m.source}>⚑ {m.source}</span>)}
    </>
  );
}

function StepsView({ runId, steps, spans, onOpenSpan, onChanged }) {
  const [selected, setSelected] = useState(steps[0]?.step ?? null);
  const [kind, setKind] = useState("");
  const [q, setQ] = useState("");
  const [flaggedOnly, setFlaggedOnly] = useState(false);
  const [markSource, setMarkSource] = useState("localization");
  const [note, setNote] = useState("");
  const [error, setError] = useState(null);
  const byId = Object.fromEntries(spans.map((s) => [s.id, s]));
  const dataset = steps[0]?.source === "dataset";
  const kinds = [...new Set(steps.map((s) => s.kind))];
  const flagged = (s) => s.labels?.length || s.marks?.length;
  const shown = steps.filter((s) => (!kind || s.kind === kind) && (!flaggedOnly || flagged(s))
    && (!q || `${s.step} ${s.name} ${s.agent || ""}`.toLowerCase().includes(q.toLowerCase())));
  const step = steps.find((s) => s.step === selected) || null;
  const agentMsgs = steps.filter((s) => s.kind === "llm" || (dataset && s.kind === "agent")).length;
  const results = steps.filter((s) => ["tool", "retriever", "embedding", "reranker"].includes(s.kind)).length;
  const tokens = steps.reduce((n, s) => n + (s.tokens?.total || 0), 0);
  const errors = steps.filter((s) => s.status === "error").length;
  const annotated = steps.filter((s) => s.labels?.length);
  const marked = steps.filter((s) => s.marks?.length);
  const mark = async (remove) => {
    setError(null);
    try {
      if (remove) await api(`/api/runs/${runId}/marks?step=${step.step}&source=${encodeURIComponent(markSource)}`, { method: "DELETE" });
      else await api(`/api/runs/${runId}/marks`, { method: "PUT", body: { step: step.step, source: markSource, note } });
      setNote(""); await onChanged?.();
    } catch (e) { setError(e); }
  };
  if (!steps.length) return <Empty title="No steps yet">Steps appear once the run has recorded an agent message (model call) or a tool / environment result.</Empty>;
  const myMark = step?.marks?.find((m) => m.source === markSource);
  return (
    <div>
      <div className="steps-summary">
        <span><strong>{steps.length}</strong> steps</span>
        <span><strong>{agentMsgs}</strong> agent messages</span>
        <span><strong>{results}</strong> tool / environment results</span>
        {steps.length - agentMsgs - results > 0 && <span><strong>{steps.length - agentMsgs - results}</strong> other messages</span>}
        <span className="muted">from <strong>{spans.length}</strong> spans</span>
        {tokens > 0 && <span><strong>{tokens.toLocaleString()}</strong> tokens</span>}
        {errors > 0 && <span className="err"><strong>{errors}</strong> with errors</span>}
        {annotated.length > 0 && <span>annotated root-cause step: <a href="#" onClick={(e) => { e.preventDefault(); setSelected(annotated[0].step); }}><strong>{annotated.map((s) => s.step).join(", ")}</strong></a></span>}
        {marked.length > 0 && <span>marked: <strong>{marked.map((s) => `${s.step} (${s.marks.map((m) => m.source).join(", ")})`).join(" · ")}</strong></span>}
      </div>
      <p className="muted" style={{ fontSize: 12.5, margin: "0 0 10px" }}>
        One step = one message: an agent's message (its reasoning and the action it takes, together) is one step, and the result that comes back from a tool or the environment is the next step. Control decisions (routing, conditions) are not numbered.
        {dataset && " Step numbers are the dataset's own (each history entry's `step` field), so a localisation result points at the same message here."}
      </p>
      {!dataset && <SpanStepMap runId={runId} onOpenSpan={onOpenSpan} />}
      <div className="split">
        <div className="card">
          <div className="row" style={{ padding: 10, borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
            <input placeholder="Step no. / name / agent" value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: 1, minWidth: 120 }} />
            <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: 150 }}><option value="">all kinds</option>{kinds.map((k) => <option key={k} value={k}>{stepKindLabel({ kind: k, source: steps[0].source })}</option>)}</select>
            <label className="muted" style={{ fontSize: 12.5, display: "flex", gap: 4, alignItems: "center" }}><input type="checkbox" checked={flaggedOnly} onChange={(e) => setFlaggedOnly(e.target.checked)} style={{ width: "auto" }} /> flagged only</label>
          </div>
          <div className="span-list">
            {shown.map((s) => (
              <div key={s.span_id} className={`span-row step-row ${s.step === selected ? "selected" : ""} ${flagged(s) ? "flagged" : ""}`} onClick={() => setSelected(s.step)}>
                <span className="step-no">{s.step}</span>
                <span className={`kind t-${s.kind}`}><Icon name={s.kind} size={12} /></span>
                <span className="name"><span className="step-agent">{s.agent || "—"}</span><span className="muted"> · {stepKindLabel(s)}</span> <StepFlags step={s} />{!dataset && s.name !== s.agent && <><br /><span className="step-sub">{s.name}</span></>}</span>
                {!dataset && <span className="muted" style={{ fontSize: 11.5 }}>{duration(s.start_time, s.end_time)}</span>}
                <Status value={s.status} />
              </div>
            ))}
          </div>
        </div>
        <div className="card span-detail">
          {!step ? <Empty title="Select a step">Pick a step on the left.</Empty> : (
            <>
              <div className="row"><span className="step-no big">{step.step}</span><span className={`badge t-${step.kind}`} style={{ color: "#fff", border: "none" }}>{stepKindLabel(step)}</span><strong style={{ fontSize: 15 }}>{step.agent || step.name}</strong><Status value={step.status} /><StepFlags step={step} /></div>
              {step.labels?.map((l, i) => (
                <div key={i} className="note note-flag"><strong>Dataset annotation{l.dataset ? ` (${l.dataset})` : ""}: root-cause step {step.step}</strong>{l.agent && <> · responsible: <b>{l.agent}</b></>}{l.reason && <div style={{ marginTop: 4, whiteSpace: "pre-wrap" }}>{l.reason}</div>}</div>
              ))}
              {step.marks?.map((m) => (
                <div key={m.source} className="note note-mark"><strong>Marked by “{m.source}”</strong>{m.note && <div style={{ marginTop: 4, whiteSpace: "pre-wrap" }}>{m.note}</div>}</div>
              ))}
              <div className="row mark-bar">
                <input value={markSource} onChange={(e) => setMarkSource(e.target.value)} title="Who marks this step (e.g. the localisation method)" style={{ width: 130 }} />
                {!myMark && <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="note (optional)" style={{ flex: 1, minWidth: 100 }} />}
                {myMark ? <button onClick={() => mark(true)}>Remove mark</button> : <button className="primary" onClick={() => mark(false)}>⚑ Mark step {step.step}</button>}
              </div>
              <ErrorBox error={error} />
              <dl className="kv">
                <dt>Step</dt><dd>{step.step}{dataset ? ` (history entry ${step.dataset_position + 1} of ${steps.length})` : ` of ${steps.length}`}</dd>
                <dt>{dataset ? "Sender" : "Agent / node"}</dt><dd>{step.agent || "—"}{step.role ? ` · role ${step.role}` : ""}</dd>
                {!dataset && <><dt>Name</dt><dd>{step.name}</dd></>}
                {step.model && <><dt>Model</dt><dd>{step.model}</dd></>}
                {fmtTokens(step.tokens) && <><dt>Tokens</dt><dd className="mono">{fmtTokens(step.tokens)}</dd></>}
                {!dataset && <><dt>Start / end</dt><dd>{fmtTime(step.start_time)} → {step.end_time ? fmtTime(step.end_time) : "…"}</dd><dt>Duration</dt><dd>{duration(step.start_time, step.end_time)}</dd></>}
                <dt>Source span</dt><dd><a href="#" onClick={(e) => { e.preventDefault(); onOpenSpan(step.span_id); }} className="mono">{byId[step.span_id]?.name || step.span_id}</a></dd>
                {step.nested_span_ids?.length > 0 && <><dt>Includes</dt><dd>{step.nested_span_ids.map((id) => <a key={id} href="#" onClick={(e) => { e.preventDefault(); onOpenSpan(id); }} style={{ marginRight: 8 }}>{byId[id]?.name || id}</a>)}</dd></>}
                {step.duplicate_span_ids?.length > 0 && <><dt>Also recorded as</dt><dd className="muted">{step.duplicate_span_ids.map((id) => byId[id]?.name || id).join(", ")} (same call, another instrumentation)</dd></>}
              </dl>
              {step.error && <div className="error-box"><strong>{step.error.type}</strong>: {step.error.message}</div>}
              {step.think && <div className="json"><div className="json-head" style={{ cursor: "default" }}>Reasoning (&lt;think&gt;)</div><pre style={{ margin: 0, padding: 10 }}>{step.think}</pre></div>}
              {dataset ? (
                <div className="json"><div className="json-head" style={{ cursor: "default" }}>Message</div><pre style={{ margin: 0, padding: 10, whiteSpace: "pre-wrap" }}>{typeof (step.output ?? step.input) === "string" ? (step.output ?? step.input) : pretty(step.output ?? step.input)}</pre></div>
              ) : step.kind === "llm" && step.input?.messages ? (
                <div className="json"><div className="json-head" style={{ cursor: "default" }}>Messages sent to the model</div>
                  <div style={{ padding: 10, display: "flex", flexDirection: "column", gap: 8 }}>
                    {step.input.messages.map((m, i) => (
                      <div key={i}><Badge tone={m.role === "user" ? "accent" : m.role === "tool" ? "det" : "neutral"}>{m.role}</Badge>
                        <pre style={{ marginTop: 4 }}>{m.content ?? pretty(m.tool_calls)}</pre></div>
                    ))}
                  </div>
                </div>
              ) : <Json label="Input" value={step.input} />}
              {!dataset && <Json label="Output" value={step.output} />}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function SpanExplorer({ spans, selected, onSelect }) {
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const byId = Object.fromEntries(spans.map((s) => [s.id, s]));
  const depth = (s) => { let d = 0, cur = s; while (cur.parent_span_id && byId[cur.parent_span_id] && d < 10) { d += 1; cur = byId[cur.parent_span_id]; } return d; };
  const kinds = [...new Set(spans.map((s) => s.kind))];
  const shown = spans.filter((s) => (!kind || s.kind === kind) && (!status || s.status === status) && (!q || `${s.name} ${s.node_id || ""}`.toLowerCase().includes(q.toLowerCase())));
  const span = byId[selected] || null;
  const children = span ? spans.filter((s) => s.parent_span_id === span.id) : [];
  return (
    <div className="split">
      <div className="card">
        <div className="row" style={{ padding: 10, borderBottom: "1px solid var(--border)" }}>
          <input placeholder="Filter by name / agent" value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: 1, minWidth: 120 }} />
          <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: 110 }}><option value="">all kinds</option>{kinds.map((k) => <option key={k}>{k}</option>)}</select>
          <select value={status} onChange={(e) => setStatus(e.target.value)} style={{ width: 100 }}><option value="">all</option>{["ok", "error", "running", "paused", "resumed"].map((k) => <option key={k}>{k}</option>)}</select>
        </div>
        <div className="span-list">
          {shown.map((s) => (
            <div key={s.id} className={`span-row ${s.id === selected ? "selected" : ""}`} onClick={() => onSelect(s.id)} style={{ paddingLeft: 10 + depth(s) * 14 }}>
              <span className={`kind t-${s.kind}`}><Icon name={s.kind === "pause" ? "pauseSpan" : s.kind} size={12} /></span>
              <span className="name">{s.name}</span>
              <span className="muted" style={{ fontSize: 11.5 }}>{duration(s.start_time, s.end_time)}</span>
              <Status value={s.status} />
            </div>
          ))}
        </div>
      </div>
      <div className="card span-detail">
        {!span ? <Empty title="Select a span">Pick a span on the left, a node in the graph, or a bar in the timeline.</Empty> : (
          <>
            <div className="row"><span className={`badge t-${span.kind}`} style={{ color: "#fff", border: "none" }}>{span.kind}</span><strong style={{ fontSize: 15 }}>{span.name}</strong><Status value={span.status} /></div>
            <dl className="kv">
              <dt>Agent / node</dt><dd>{span.node_id || "—"}</dd>
              <dt>Span id</dt><dd className="mono">{span.id}</dd>
              <dt>Parent</dt><dd>{span.parent_span_id ? <a href="#" onClick={(e) => { e.preventDefault(); onSelect(span.parent_span_id); }} className="mono">{byId[span.parent_span_id]?.name || span.parent_span_id}</a> : "— (root)"}</dd>
              <dt>Start / end</dt><dd>{fmtTime(span.start_time)} → {span.end_time ? fmtTime(span.end_time) : "…"}</dd>
              <dt>Duration</dt><dd>{duration(span.start_time, span.end_time)}</dd>
              {span.attributes?.provider && <><dt>Provider / model</dt><dd>{span.attributes.provider} · {span.attributes.response_model || span.attributes.model}</dd></>}
              {span.attributes?.usage && Object.keys(span.attributes.usage).length > 0 && <><dt>Token usage</dt><dd className="mono">{["prompt_tokens", "completion_tokens", "total_tokens"].filter((k) => span.attributes.usage[k] !== undefined).map((k) => `${k.split("_")[0]} ${span.attributes.usage[k]}`).join(" · ")}</dd></>}
              {span.checkpoint_id && <><dt>Checkpoint</dt><dd className="mono">{span.checkpoint_id}</dd></>}
              {children.length > 0 && <><dt>Children</dt><dd>{children.map((c) => <a key={c.id} href="#" onClick={(e) => { e.preventDefault(); onSelect(c.id); }} style={{ marginRight: 8 }}>{c.name}</a>)}</dd></>}
            </dl>
            {span.error && (
              <div className="error-box"><strong>{span.error.type}</strong>: {span.error.message}
                {span.error.traceback && <details style={{ marginTop: 6 }}><summary>Stack trace</summary><pre>{span.error.traceback}</pre></details>}
              </div>
            )}
            {span.kind === "llm" && span.input?.messages ? (
              <div className="json"><div className="json-head" style={{ cursor: "default" }}>Messages sent to the model</div>
                <div style={{ padding: 10, display: "flex", flexDirection: "column", gap: 8 }}>
                  {span.input.messages.map((m, i) => (
                    <div key={i}><Badge tone={m.role === "user" ? "accent" : m.role === "tool" ? "det" : "neutral"}>{m.role}</Badge>
                      <pre style={{ marginTop: 4 }}>{m.content ?? pretty(m.tool_calls)}</pre></div>
                  ))}
                </div>
              </div>
            ) : <Json label="Input" value={span.input} />}
            <Json label="Output" value={span.output} />
            <Json label="Attributes" value={span.attributes} collapsed />
          </>
        )}
      </div>
    </div>
  );
}

function Checkpoints({ run }) {
  const [data, setData] = useState(null);
  const [selected, setSelected] = useState(null);
  const [mode, setMode] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let stop = false;
    const load = () => api(`/api/runs/${run.id}/checkpoints`).then((d) => { if (!stop) { setData(d); setSelected((s) => s || d.items.find((c) => c.forkable)?.checkpoint_id || null); } }).catch(setError);
    load();
    const t = ACTIVE.has(run.status) ? setInterval(load, 1500) : null;
    return () => { stop = true; if (t) clearInterval(t); };
  }, [run.id, run.status]);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <div className="muted">Loading…</div>;
  if (!data.supported) return <div className="note">{data.reason}</div>;
  const cp = data.items.find((c) => c.checkpoint_id === selected);
  return (
    <div className="split">
      <div className="card cp-list span-list">
        {data.items.map((c, i) => (
          <div key={c.checkpoint_id} className={`cp ${c.checkpoint_id === selected ? "selected" : ""}`} onClick={() => setSelected(c.checkpoint_id)}>
            <div className="row"><strong>#{data.items.length - 1 - i}</strong>
              <span>{c.after_node ? <>after <b>{c.after_label || c.after_node}</b></> : <span className="muted">initial</span>}</span>
              <span className="muted">→ {c.next.length ? c.next.join(", ") : "end"}</span>
              <span className="spacer" />{c.inherited && <Badge title="Written before this branch existed (parent run)">parent</Badge>}{c.interrupted && <Status value="paused" />}{c.forkable && <Badge tone="accent">forkable</Badge>}
            </div>
            <div className="muted mono" style={{ fontSize: 11 }}>{c.checkpoint_id}</div>
          </div>
        ))}
      </div>
      <div className="card span-detail">
        {!cp ? <Empty title="Select a checkpoint" /> : (
          <>
            <div className="row"><strong>Checkpoint after {cp.after_label || cp.after_node || "start"}</strong><span className="muted">step {cp.step}</span><span className="spacer" />
              <button disabled={!cp.forkable} onClick={() => setMode("replay")} title="Restore this state in a new branch and continue without changes"><Icon name="replay" /> Replay from here</button>
              <button className="primary" disabled={!cp.forkable || !cp.editable?.length} onClick={() => setMode("fork")}
                title={cp.forkable && !cp.editable?.length ? "Nothing produced at this step can be edited" : ""}><Icon name="branch" /> Fork with edits</button>
            </div>
            {!cp.forkable && <div className="note">{cp.next.length ? "This checkpoint precedes the first node. Start a new run with different inputs instead." : "Terminal checkpoint: nothing left to execute."}</div>}
            {cp.forkable && (
              <div className="note">
                <b>Continues from:</b> {cp.next.join(", ")}.{" "}
                {cp.editable?.length
                  ? <>A fork here may change only what <b>{cp.after_label || cp.after_node}</b> produced: {cp.editable.map((e) => <code key={e.path} style={{ marginRight: 6 }}>{e.path}</code>)}</>
                  : <>Nothing produced at this step can be edited (for example a condition, whose result is recomputed); replay is still possible.</>}
                {" "}To change an earlier step, fork from the checkpoint after that step.
              </div>
            )}
            {data.connected ? (
              <>
                <div className="note">Read live from the executor's own checkpoint store. Fork and replay ask the <b>executor</b> to create a new branch from this checkpoint (it becomes a new run here, with lineage). The source run's head and checkpoints are not modified. Model-backed steps may produce different outputs.</div>
                {cp.editable.map((e) => <Json key={e.path} label={`${e.path} (written at this step · editable in a fork)`} value={e.value} />)}
                <Json label="Full executor state at this checkpoint" value={cp.state} collapsed />
              </>
            ) : (
              <>
                <div className="note">Fork and replay copy this checkpoint's state into a <b>new run</b> (new thread). The original run and its history are never modified. Model-backed steps may produce different outputs: state is restored, outputs are not guaranteed identical.</div>
                <Json label="Executed path at this point" value={cp.state.path} />
                <Json label="Inputs" value={cp.state.input} />
                {Object.entries(cp.state.nodes).map(([id, n]) => <Json key={id} label={`nodes.${id}.output (${n.type})`} value={n.output} collapsed={n.type === "start"} />)}
              </>
            )}
          </>
        )}
      </div>
      {mode && cp && <ForkDialog run={run} cp={cp} mode={mode} onClose={() => setMode(null)} />}
    </div>
  );
}

function ForkDialog({ run, cp, mode, onClose }) {
  // The server lists what may be edited at this checkpoint: only what the step that just ran produced.
  const editable = (cp.editable || []).map((e) => ({ path: e.path, value: e.value, type: e.type, description: e.description, choices: e.choices }));
  const [edits, setEdits] = useState({});
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true); setError(null);
    try {
      const changes = {};
      for (const [path, text] of Object.entries(edits)) {
        const spec = editable.find((e) => e.path === path);
        changes[path] = spec?.type === "string" || (spec?.type === undefined && typeof spec?.value === "string") ? text : JSON.parse(text);
      }
      const body = mode === "fork" ? { checkpoint_id: cp.checkpoint_id, changes } : { checkpoint_id: cp.checkpoint_id };
      const created = await api(`/api/runs/${run.id}/${mode}`, { method: "POST", body });
      window.location.hash = `#/runs/${created.id}`;
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return (
    <Modal title={mode === "fork" ? `Fork after “${cp.after_label || cp.after_node}”` : `Replay after “${cp.after_label || cp.after_node}”`} onClose={onClose}
      footer={<><button onClick={onClose}>Cancel</button><button className="primary" disabled={busy || (mode === "fork" && !Object.keys(edits).length)} onClick={submit}>
        <Icon name={mode === "fork" ? "branch" : "replay"} /> {busy ? "Starting…" : mode === "fork" ? "Create branch and run" : "Replay in new branch"}</button></>}>
      <ErrorBox error={error} />
      {mode === "replay" ? <div className="note">Continues from this checkpoint ({cp.next.join(", ")} next) in a new run with identical state. Deterministic nodes reproduce the same results; model-backed nodes are called again.</div> : (
        <>
          <div className="note" style={{ marginBottom: 12 }}>
            You can change what <b>{cp.after_label || cp.after_node}</b> produced at this step. Everything else keeps its
            checkpoint value, and nodes before this checkpoint are not re-run (shown as <i>inherited</i>). Execution
            continues with <b>{cp.next.join(", ")}</b>{run.origin === "connected" ? "; the executor validates the values again. Lists are edited as JSON arrays." : "; later conditions are evaluated with the new value."}
            {editable.some((e) => e.choices) && <> Editing a routing decision (a field with fixed choices) changes where execution continues.</>}
          </div>
          {editable.map((e) => (
            <Field key={e.path} label={e.path} hint={edits[e.path] !== undefined ? "edited" : e.description || ""}>
              {e.choices ? (
                <select defaultValue={e.value ?? ""} onChange={(ev) => setEdits({ ...edits, [e.path]: ev.target.value })}>
                  {!e.choices.includes(e.value) && <option value={e.value ?? ""}>{String(e.value ?? "")}</option>}
                  {e.choices.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              ) : (
                <textarea rows={Math.min(8, Math.max(2, pretty(e.value).split("\n").length))} defaultValue={pretty(e.value)}
                  onChange={(ev) => setEdits({ ...edits, [e.path]: ev.target.value })} />
              )}
            </Field>
          ))}
        </>
      )}
    </Modal>
  );
}

function Compare({ run }) {
  const candidates = [run.parent && { id: run.parent.id, label: `parent ${run.parent.id}` }, ...(run.children || []).map((c) => ({ id: c.id, label: `${c.relation} ${c.id}` }))].filter(Boolean);
  const [other, setOther] = useState(candidates[0]?.id || "");
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => { if (other) api(`/api/runs/${run.id}/compare/${other}`).then(setData).catch(setError); }, [run.id, other]);
  return (
    <div className="card card-pad">
      <div className="row" style={{ marginBottom: 12 }}>
        <span>Compare with</span>
        <select value={other} onChange={(e) => setOther(e.target.value)} style={{ maxWidth: 380 }}>
          <option value="">— choose —</option>
          {candidates.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
        </select>
        <input placeholder="or paste any run id" style={{ maxWidth: 260 }} onKeyDown={(e) => e.key === "Enter" && setOther(e.target.value.trim())} />
      </div>
      <ErrorBox error={error} />
      {!other ? <div className="note">No related runs. Fork this run from the Checkpoints tab to create a branch, or paste another run id.</div> : !data ? <div className="muted">Loading…</div> : (
        <>
          <div className="row" style={{ marginBottom: 10 }}>
            <div><b>A</b> {data.a.id} <Status value={data.a.status} /></div><div><b>B</b> {data.b.id} <Status value={data.b.status} /></div>
          </div>
          <Json label="Executed path A" value={data.a.executed_path.join(" → ")} />
          <Json label="Executed path B" value={data.b.executed_path.join(" → ")} />
          <table className="table" style={{ marginTop: 12 }}>
            <thead><tr><th>Node</th><th>A output</th><th>B output</th><th></th></tr></thead>
            <tbody>
              {data.diff.map((d) => (
                <tr key={d.node_id}>
                  <td className="mono">{d.node_id}</td>
                  <td><pre style={{ maxHeight: 140, overflow: "auto" }}>{d.in_a ? pretty(data.a.nodes[d.node_id].output) : data.a.inherited?.includes(d.node_id) ? "— inherited from parent checkpoint (not re-executed)" : "— not executed"}</pre></td>
                  <td><pre style={{ maxHeight: 140, overflow: "auto" }}>{d.in_b ? pretty(data.b.nodes[d.node_id].output) : data.b.inherited?.includes(d.node_id) ? "— inherited from parent checkpoint (not re-executed)" : "— not executed"}</pre></td>
                  <td className={d.verdict === "differs" ? "diff-changed" : "diff-same"}>{d.verdict}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ fontSize: 12 }}>{data.note}</p>
        </>
      )}
    </div>
  );
}
