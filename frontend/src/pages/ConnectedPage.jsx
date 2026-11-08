import React, { useCallback, useEffect, useState } from "react";
import { api, duration, fmtTime } from "../api.js";
import { Badge, Empty, ErrorBox, Field, Icon, Modal, ProviderBadge, Status } from "../components/ui.jsx";

const CAP_LABEL = { checkpoint: "checkpoints", pause_resume: "pause/resume", fork: "fork", replay: "replay", retry: "retry" };

function NewConnectorDialog({ onClose, onCreated }) {
  const [kind, setKind] = useState("m1_legacy");
  const [mode, setMode] = useState("launch");
  const [form, setForm] = useState({ name: "M1 LangGraph executor", port: 8010, model: "rule_based", base_url: "http://127.0.0.1:8000", provider_mode: "openai", token: "" });
  const sdk = kind === "studio_executor";
  const chooseKind = (k) => {
    setKind(k);
    if (k === "studio_executor") { setMode("attach"); setForm({ ...form, name: "My LangGraph agent", base_url: "http://127.0.0.1:8020" }); }
    else setForm({ ...form, name: "M1 LangGraph executor", base_url: "http://127.0.0.1:8000" });
  };
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const submit = async () => {
    setBusy(true); setError(null);
    try {
      const body = sdk
        ? { kind, mode: "attach", name: form.name, base_url: form.base_url.trim(), token: form.token.trim() }
        : mode === "launch"
          ? { kind, mode, name: form.name, port: Number(form.port), model: form.model }
          : { kind, mode, name: form.name, base_url: form.base_url.trim(), provider_mode: form.provider_mode };
      onCreated(await api("/api/connectors", { method: "POST", body }));
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return (
    <Modal title="Connect a LangGraph executor" onClose={onClose}
      footer={<><button onClick={onClose}>Cancel</button><button className="primary" disabled={busy} onClick={submit}><Icon name="plug" /> {busy ? "Saving…" : "Add connector"}</button></>}>
      <ErrorBox error={error} />
      <div className="note" style={{ marginBottom: 12 }}>
        The executor keeps running in <b>its own process</b> with its own environment, credentials and checkpoint database.
        The Studio only talks to its control API and never opens its files.
      </div>
      <Field label="Agent type">
        <select value={kind} onChange={(e) => chooseKind(e.target.value)}>
          <option value="m1_legacy">M1 LangGraph executor (this project's original system)</option>
          <option value="studio_executor">Any LangGraph agent wrapped with the studio-executor SDK</option>
        </select>
      </Field>
      <Field label="Name"><input value={form.name} onChange={set("name")} /></Field>
      {sdk ? (
        <>
          <div className="note" style={{ marginBottom: 12, fontSize: 12.5 }}>
            Start your agent yourself, e.g. <code>python examples/langgraph_agent/support_agent.py --port 8020</code>. It prints its URL
            and token (or reads <code>STUDIO_EXECUTOR_TOKEN</code>). Wrapping your own graph takes a few lines: see <code>docs/studio-executor-sdk.md</code>.
          </div>
          <Field label="Agent URL (loopback only)"><input value={form.base_url} onChange={set("base_url")} /></Field>
          <Field label="Token" hint="kept in the Studio database, never shown again"><input type="password" value={form.token} onChange={set("token")} autoComplete="off" /></Field>
        </>
      ) : (<>
      <Field label="How is the executor started?">
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="launch">The Studio starts it as a separate process (isolated data folder)</option>
          <option value="attach">I start it myself (e.g. m1-local serve) — attach to its URL</option>
        </select>
      </Field>
      {mode === "launch" ? (
        <>
          <Field label="Port for the executor"><input type="number" value={form.port} onChange={set("port")} /></Field>
          <Field label="Model" hint={form.model === "openai" ? "uses OPENAI_API_KEY from the Studio's environment" : "offline scripted gateway, labelled deterministic"}>
            <select value={form.model} onChange={set("model")}>
              <option value="rule_based">Deterministic (RuleBasedGateway, offline)</option>
              <option value="openai">Real model (OpenAI)</option>
            </select>
          </Field>
        </>
      ) : (
        <>
          <Field label="Executor URL (loopback only)"><input value={form.base_url} onChange={set("base_url")} /></Field>
          <Field label="Which model does that process use?">
            <select value={form.provider_mode} onChange={set("provider_mode")}>
              <option value="openai">Real model</option><option value="deterministic">Deterministic</option><option value="unknown">Unknown</option>
            </select>
          </Field>
        </>
      )}
      </>)}
    </Modal>
  );
}

function RemoteRuns({ connector, onImported }) {
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => { api(`/api/connectors/${connector.id}/remote-runs`).then(setItems).catch(setError); }, [connector.id]);
  const importRun = async (remoteId) => {
    setError(null);
    try { const run = await api(`/api/connectors/${connector.id}/import`, { method: "POST", body: { remote_run_id: remoteId } }); onImported(run); }
    catch (e) { setError(e); }
  };
  return (
    <div style={{ marginTop: 10 }}>
      <ErrorBox error={error} />
      {items === null ? <div className="muted">Loading…</div> : items.length === 0 ? <div className="muted">The executor has no runs yet.</div> : (
        <table className="table">
          <thead><tr><th>Status</th><th>Task</th><th>Executor run id</th><th></th></tr></thead>
          <tbody>
            {items.slice(0, 30).map((r) => (
              <tr key={r.run_id}>
                <td><Status value={r.status} /></td>
                <td style={{ maxWidth: 320 }}>{(r.task || "").slice(0, 120)}{r.parent_run_id && <div className="muted">branch of {r.parent_run_id.slice(0, 8)}</div>}</td>
                <td className="mono muted">{r.run_id.slice(0, 13)}…</td>
                <td>{r.studio_run_id ? <a href={`#/runs/${r.studio_run_id}`}>open →</a> : <button className="small" onClick={() => importRun(r.run_id)}>Open in Studio</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ConnectorCard({ connector, onChanged }) {
  const d = connector.describe;
  const [inputs, setInputs] = useState({});
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState("");
  const [showRemote, setShowRemote] = useState(false);
  const proc = connector.process;
  const running = proc && proc.status === "running";
  const call = async (label, fn) => {
    setBusy(label); setError(null);
    try { await fn(); } catch (e) { setError(e); } finally { setBusy(""); onChanged(); }
  };
  const values = Object.fromEntries((d?.input_fields || []).map((f) => [f.name, inputs[f.name] ?? f.default ?? ""]));
  const hasInput = Object.values(values).some((v) => String(v).trim());
  const startRun = () => call("run", async () => {
    const run = await api(`/api/connectors/${connector.id}/runs`, { method: "POST", body: { inputs: values } });
    window.location.hash = `#/runs/${run.id}`;
  });
  return (
    <div className="card card-pad">
      <div className="row">
        <h3 style={{ margin: 0, fontSize: 15 }}>{connector.name}</h3>
        <Badge tone={connector.reachable ? "real" : "ro"}>{connector.reachable ? "connected" : "offline"}</Badge>
        <Badge>{connector.kind === "studio_executor" ? "SDK agent" : connector.mode === "launch" ? "started by the Studio" : "attached"}</Badge>
        {d && <ProviderBadge mode={d.provider_mode} />}
        <span className="spacer" />
        {connector.mode === "launch" && (running
          ? <button disabled={!!busy} onClick={() => call("stop", () => api(`/api/connectors/${connector.id}/stop`, { method: "POST" }))}><Icon name="stop" /> Stop executor</button>
          : <button className="primary" disabled={!!busy} onClick={() => call("start", () => api(`/api/connectors/${connector.id}/start`, { method: "POST" }))}><Icon name="play" /> {busy === "start" ? "Starting…" : "Start executor"}</button>)}
        <button className="small danger" title="Remove the connector; mirrored runs stay" disabled={!!busy}
          onClick={() => call("delete", () => api(`/api/connectors/${connector.id}`, { method: "DELETE" }))}><Icon name="trash" /></button>
      </div>
      <p className="muted mono" style={{ fontSize: 12, margin: "6px 0" }}>
        {connector.base_url}{proc ? ` · pid ${proc.pid} · ${proc.status}` : ""} · {connector.run_count} run(s) in the Studio
      </p>
      {!connector.reachable && <div className="note" style={{ fontSize: 12.5 }}>
        {connector.mode === "launch" ? "Press Start executor to launch it." : "Start the agent yourself, e.g. "}
        {connector.mode === "attach" && <code>{connector.kind === "studio_executor" ? "python your_agent.py --port …" : "m1-local serve"}</code>}
        {connector.unreachable_reason && <div className="muted" style={{ marginTop: 4 }}>{connector.unreachable_reason}</div>}
      </div>}
      {proc?.status === "exited" && proc.stderr_tail && <pre className="error-box">{proc.stderr_tail.slice(-800)}</pre>}
      <ErrorBox error={error} />
      {d && (
        <>
          <div className="row" style={{ fontSize: 12, margin: "8px 0" }}>
            <span className="muted">Graph ({d.graph.nodes.length} nodes, from {d.graph.source}):</span>
            {d.graph.nodes.map((n) => <Badge key={n.id}>{n.label}</Badge>)}
          </div>
          <div className="row" style={{ fontSize: 12, marginBottom: 8 }}>
            <span className="muted">Capabilities:</span>
            {Object.entries(CAP_LABEL).map(([k, label]) => <Badge key={k} tone={d.capabilities[k] ? "real" : "ro"}>{d.capabilities[k] ? "✓" : "✕"} {label}</Badge>)}
            <span className="muted">· fork may edit:</span>{d.fork_fields.map((f) => <code key={f.path} title={f.description}>{f.path}</code>)}
          </div>
          {d.notes?.length > 0 && <ul className="muted" style={{ fontSize: 12, margin: "0 0 10px 18px", padding: 0 }}>{d.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
          {d.input_fields.map((f, i) => (
            <Field key={f.name} label={f.label} hint={f.hint}>
              {i === 0
                ? <textarea rows={2} value={values[f.name]} onChange={(e) => setInputs({ ...inputs, [f.name]: e.target.value })} placeholder={f.name === "task" ? "e.g. Draft code for a UUID validation function" : ""} />
                : <input value={values[f.name]} onChange={(e) => setInputs({ ...inputs, [f.name]: e.target.value })} />}
            </Field>
          ))}
          <div className="row">
            <button className="primary" disabled={!connector.reachable || !!busy || !hasInput} onClick={startRun}><Icon name="play" /> {busy === "run" ? "Starting…" : "Run on executor"}</button>
            <button disabled={!connector.reachable} onClick={() => setShowRemote(!showRemote)}>{showRemote ? "Hide" : "Browse"} the executor's own runs</button>
          </div>
          {showRemote && <RemoteRuns connector={connector} onImported={(run) => { window.location.hash = `#/runs/${run.id}`; }} />}
        </>
      )}
    </div>
  );
}

export default function ConnectedPage() {
  const [items, setItems] = useState(null);
  const [runs, setRuns] = useState([]);
  const [error, setError] = useState(null);
  const [adding, setAdding] = useState(false);
  const load = useCallback(() => Promise.all([api("/api/connectors"), api("/api/runs?origin=connected")])
    .then(([c, r]) => { setItems(c); setRuns(r); }).catch(setError), []);
  useEffect(() => { load(); const t = setInterval(load, 4000); return () => clearInterval(t); }, [load]);
  return (
    <div className="page">
      <div className="page-head">
        <div><h1>Connected agents</h1>
          <p>Debug LangGraph agents that run outside the Studio. The Studio reads their own checkpoints and forwards pause, resume, retry, fork and replay to them; branches keep lineage and can be compared.</p></div>
        <button className="primary" onClick={() => setAdding(true)}><Icon name="plus" /> Connect executor</button>
      </div>
      <ErrorBox error={error} />
      {items === null ? <div className="muted">Loading…</div> : items.length === 0 ? (
        <div className="card"><Empty title="No connected executors">Connect the original M1 executor: the Studio can start it for you (offline deterministic or real model), or attach to one you run yourself.</Empty></div>
      ) : (
        <div className="grid-cards" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(520px, 1fr))" }}>
          {items.map((c) => <ConnectorCard key={c.id} connector={c} onChanged={load} />)}
        </div>
      )}
      <h2 style={{ fontSize: 16, marginTop: 24 }}>Runs on connected executors</h2>
      <div className="card">
        {runs.length === 0 ? <Empty title="No runs yet" /> : (
          <table className="table">
            <thead><tr><th>Status</th><th>Executor</th><th>Task</th><th>Lineage</th><th>Started</th><th>Duration</th></tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className="clickable" onClick={() => (window.location.hash = `#/runs/${r.id}`)}>
                  <td><Status value={r.status} /></td>
                  <td><div>{r.workflow_name}</div><div className="muted mono">{r.id}</div></td>
                  <td style={{ maxWidth: 300 }}>{String(r.input?.task || r.input?.question || Object.values(r.input || {})[0] || "").slice(0, 100)}</td>
                  <td>{r.relation ? <Badge tone="accent">{r.relation} of {r.parent_run_id?.slice(0, 12)}</Badge> : <span className="muted">—</span>}</td>
                  <td>{fmtTime(r.started_at || r.created_at)}</td>
                  <td>{duration(r.started_at, r.ended_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {adding && <NewConnectorDialog onClose={() => setAdding(false)} onCreated={() => { setAdding(false); load(); }} />}
    </div>
  );
}
