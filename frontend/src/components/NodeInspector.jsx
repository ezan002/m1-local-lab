import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import { Field, Icon, Json, Status } from "./ui.jsx";

const OPS = ["contains", "not_contains", "equals", "not_equals", "greater_than", "less_than", "is_true"];

function useCatalog() {
  const [catalog, setCatalog] = useState(null);
  useEffect(() => { api("/api/catalog").then(setCatalog).catch(() => {}); }, []);
  return catalog;
}

function JsonField({ label, value, onChange, hint }) {
  const [text, setText] = useState(JSON.stringify(value ?? [], null, 2));
  const [bad, setBad] = useState(false);
  return (
    <Field label={label} hint={bad ? "Invalid JSON — not applied" : hint}>
      <textarea rows={4} value={text} style={bad ? { borderColor: "var(--err)" } : undefined}
        onChange={(e) => { setText(e.target.value); try { onChange(JSON.parse(e.target.value)); setBad(false); } catch { setBad(true); } }} />
    </Field>
  );
}

export default function NodeInspector({ node, allNodes, onChange, onDelete, spans, runId }) {
  const catalog = useCatalog();
  const t = node.data.nodeType;
  const cfg = node.data.config || {};
  const set = (patch) => onChange({ config: { ...cfg, ...patch } });
  const refs = ["input.<field>", ...allNodes.filter((n) => n.id !== node.id && n.data.nodeType !== "start").map((n) => `nodes.${n.id}.output`)];
  const last = [...spans].reverse().find((s) => s.kind === t);
  const children = last ? spans.filter((s) => s.parent_span_id === last.id) : [];

  return (
    <div>
      <div className="row" style={{ marginBottom: 10 }}>
        <span className={`fn-type t-${t}`} style={{ width: 24, height: 24, borderRadius: 6, display: "grid", placeItems: "center", color: "#fff" }}><Icon name={t} size={13} /></span>
        <strong>{t}</strong><span className="muted mono">{node.id}</span>
        <span className="spacer" />
        <button className="small danger" onClick={onDelete} title="Delete node"><Icon name="trash" size={14} /></button>
      </div>
      <Field label="Label"><input value={node.data.label || ""} onChange={(e) => onChange({ label: e.target.value })} /></Field>

      {t === "start" && (
        <>
          <h4>Inputs</h4>
          {(cfg.inputs || []).map((inp, i) => (
            <div key={i} className="card card-pad" style={{ padding: 10, marginBottom: 8 }}>
              <div className="row">
                <input placeholder="name" value={inp.name} style={{ flex: 1 }} onChange={(e) => set({ inputs: cfg.inputs.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)) })} />
                <button className="small danger" onClick={() => set({ inputs: cfg.inputs.filter((_, j) => j !== i) })}>✕</button>
              </div>
              <input placeholder="label" value={inp.label} style={{ marginTop: 6 }} onChange={(e) => set({ inputs: cfg.inputs.map((x, j) => (j === i ? { ...x, label: e.target.value } : x)) })} />
              <input placeholder="default value" value={inp.default} style={{ marginTop: 6 }} onChange={(e) => set({ inputs: cfg.inputs.map((x, j) => (j === i ? { ...x, default: e.target.value } : x)) })} />
            </div>
          ))}
          <button className="small" onClick={() => set({ inputs: [...(cfg.inputs || []), { name: `field_${(cfg.inputs || []).length + 1}`, label: "", default: "" }] })}><Icon name="plus" size={13} /> Add input</button>
        </>
      )}

      {t === "agent" && (
        <>
          <Field label="Role"><input value={cfg.role || ""} onChange={(e) => set({ role: e.target.value })} /></Field>
          <Field label="System prompt"><textarea rows={3} value={cfg.system_prompt || ""} onChange={(e) => set({ system_prompt: e.target.value })} /></Field>
          <Field label="Prompt (template)" hint={`References: ${refs.join(", ")}`}><textarea rows={5} value={cfg.prompt || ""} onChange={(e) => set({ prompt: e.target.value })} /></Field>
          <div className="row">
            <Field label="Provider"><select value={cfg.provider || "inherit"} onChange={(e) => set({ provider: e.target.value })}>
              <option value="inherit">inherit</option><option value="openai">openai</option><option value="deterministic">deterministic</option></select></Field>
            <Field label="Model"><input placeholder="default" value={cfg.model || ""} onChange={(e) => set({ model: e.target.value })} /></Field>
          </div>
          <div className="row">
            <Field label="Temperature"><input type="number" step="0.1" min="0" max="2" value={cfg.temperature ?? 0.2} onChange={(e) => set({ temperature: Number(e.target.value) })} /></Field>
            <Field label="Max tokens"><input type="number" min="16" max="8000" value={cfg.max_tokens ?? 600} onChange={(e) => set({ max_tokens: Number(e.target.value) })} /></Field>
          </div>
          <Field label="Tools the agent may call">
            <div className="row">
              {(catalog?.tools || []).map((tool) => (
                <label key={tool.name} className="check" title={tool.description}>
                  <input type="checkbox" checked={(cfg.tools || []).includes(tool.name)}
                    onChange={(e) => set({ tools: e.target.checked ? [...(cfg.tools || []), tool.name] : (cfg.tools || []).filter((x) => x !== tool.name) })} />{tool.name}
                </label>
              ))}
            </div>
          </Field>
          <Field label="Max tool rounds"><input type="number" min="0" max="8" value={cfg.max_tool_rounds ?? 3} onChange={(e) => set({ max_tool_rounds: Number(e.target.value) })} /></Field>
          <h4>Deterministic script</h4>
          <Field label="Scripted response" hint="Used only by the deterministic provider. May use templates and {{tool_results}}."><textarea rows={3} value={cfg.mock_response || ""} onChange={(e) => set({ mock_response: e.target.value })} /></Field>
          <JsonField label="Scripted tool calls" value={cfg.mock_tool_calls || []} onChange={(v) => set({ mock_tool_calls: v })}
            hint='e.g. [{"name":"calculator","arguments":{"expression":"2*21"}}]' />
        </>
      )}

      {t === "tool" && (
        <>
          <Field label="Tool"><select value={cfg.tool || ""} onChange={(e) => set({ tool: e.target.value, arguments: {} })}>
            {(catalog?.tools || []).map((tool) => <option key={tool.name} value={tool.name}>{tool.name}</option>)}</select></Field>
          {Object.keys(catalog?.tools.find((x) => x.name === cfg.tool)?.parameters.properties || {}).map((arg) => (
            <Field key={arg} label={`Argument: ${arg}`} hint="Template allowed"><input value={(cfg.arguments || {})[arg] || ""} onChange={(e) => set({ arguments: { ...(cfg.arguments || {}), [arg]: e.target.value } })} /></Field>
          ))}
        </>
      )}

      {t === "condition" && (
        <>
          <Field label="Left (template)" hint="e.g. {{nodes.reviewer.output}}"><input value={cfg.left || ""} onChange={(e) => set({ left: e.target.value })} /></Field>
          <Field label="Operator"><select value={cfg.op || "contains"} onChange={(e) => set({ op: e.target.value })}>{OPS.map((o) => <option key={o}>{o}</option>)}</select></Field>
          <Field label="Right"><input value={cfg.right || ""} onChange={(e) => set({ right: e.target.value })} /></Field>
          <Field label="Max true visits" hint="Loop guard: after this many true results the condition is forced false."><input type="number" min="1" max="20" value={cfg.max_true_visits ?? 3} onChange={(e) => set({ max_true_visits: Number(e.target.value) })} /></Field>
        </>
      )}

      {t === "output" && <Field label="Template" hint={`References: ${refs.join(", ")}`}><textarea rows={6} value={cfg.template || ""} onChange={(e) => set({ template: e.target.value })} /></Field>}

      {runId && (
        <>
          <h4 style={{ marginTop: 18 }}>Selected run · last execution</h4>
          {!last ? <div className="note">This node has not executed in the selected run.</div> : (
            <>
              <div className="row" style={{ marginBottom: 8 }}><Status value={last.status} /><a href={`#/runs/${runId}?span=${last.id}`}>inspect in run view</a></div>
              {last.error && <div className="error-box">{last.error.type}: {last.error.message}</div>}
              <Json label="Input" value={last.input} collapsed />
              <Json label="Output" value={last.output} />
              {children.length > 0 && <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>{children.length} child call(s): {children.map((c) => c.name).join(", ")}</div>}
            </>
          )}
        </>
      )}
    </div>
  );
}
