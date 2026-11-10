import React, { useEffect, useState } from "react";
import { api, duration, fmtTime, meta } from "../api.js";
import { Badge, Empty, ErrorBox, Field, Icon, ProviderBadge, Status } from "../components/ui.jsx";

const MAX_FILE = 150 * 1024 * 1024;

// "Import on top, list below" (like Connected agents): bring traces in from a file or live over OTLP/HTTP.
// Localisation results for imported dataset trajectories: [{"question_ID": "...", "step": 21, "note"?: "..."}]
function MarksImport({ onImported }) {
  const [file, setFile] = useState(null);
  const [source, setSource] = useState("localization");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const upload = async () => {
    if (!file) return;
    setError(null); setResult(null); setBusy(true);
    try {
      let items = JSON.parse(await file.text());
      if (!Array.isArray(items)) items = items.items || Object.entries(items).map(([question_ID, step]) => ({ question_ID, step }));
      const r = await api("/api/marks/import", { method: "POST", body: { items, source } });
      setResult(r); onImported?.();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return (
    <div style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--border)" }}>
      <div className="row" style={{ alignItems: "flex-end" }}>
        <Field label="Step marks for imported trajectories (.json: [{question_ID, step}])">
          <input type="file" accept=".json,application/json" onChange={(e) => { setFile(e.target.files?.[0] || null); setResult(null); setError(null); }} />
        </Field>
        <Field label="Mark as"><input value={source} onChange={(e) => setSource(e.target.value)} style={{ width: 150 }} /></Field>
        <button onClick={upload} disabled={!file || busy}>⚑ {busy ? "Importing…" : "Import marks"}</button>
      </div>
      <ErrorBox error={error} />
      {result && (
        <div className="note" style={{ marginTop: 8 }}>
          Marked {result.applied.length} step(s){result.missing.length ? ` · ${result.missing.length} skipped (${[...new Set(result.missing.map((m) => m.reason))].join(", ")}; import those trajectories first)` : ""}.
          {result.applied.slice(0, 12).map((a) => <a key={a.run_id} href={`#/runs/${a.run_id}`} className="mono" style={{ marginLeft: 8 }}>{a.run_id.slice(4, 14)}·{a.step}</a>)}
        </div>
      )}
    </div>
  );
}

function TraceImport({ onImported }) {
  const [endpoint, setEndpoint] = useState("");
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [showLive, setShowLive] = useState(false);
  useEffect(() => { meta().then((m) => setEndpoint(m.otlp_endpoint)).catch(() => {}); }, []);
  const upload = async () => {
    if (!file) return;
    setError(null); setResult(null);
    if (file.size > MAX_FILE) { setError(new Error("file too large (limit 150 MB)")); return; }
    setBusy(true);
    try {
      let body;
      if (/\.parquet$/i.test(file.name)) {
        const bytes = new Uint8Array(await file.arrayBuffer());
        let bin = ""; for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
        body = { filename: file.name, content: btoa(bin), encoding: "base64" };
      } else body = { filename: file.name, content: await file.text() };
      const r = await api("/api/traces/import", { method: "POST", body });
      setResult(r);
      onImported?.();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return (
    <div className="card card-pad" style={{ marginBottom: 16 }}>
      <div className="row"><h3 style={{ margin: 0, fontSize: 15 }}>Import traces</h3><Badge tone="ro">read-only</Badge></div>
      <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>
        Traces are OpenTelemetry spans. Upload a file in OTLP JSON format (one request object, an array, or JSON
        Lines), or a raw trajectory that is converted to spans on import: <b>Codex session</b> (
        <code>%USERPROFILE%\.codex\sessions\…\rollout-*.jsonl</code>) or a <b>trajectory dataset</b> such as LongRCA
        (<code>.json</code> / <code>.jsonl</code> / <code>.parquet</code> records with a <code>history</code> list; one step per message, numbered by the dataset's own <code>step</code>). Agents can also export spans live to the endpoint below.
      </p>
      <div className="row" style={{ alignItems: "flex-end" }}>
        <Field label="Trace file (.json / .jsonl / .parquet)">
          <input type="file" accept=".json,.jsonl,.ndjson,.parquet,application/json" onChange={(e) => { setFile(e.target.files?.[0] || null); setResult(null); setError(null); }} />
        </Field>
        <button className="primary" onClick={upload} disabled={!file || busy}><Icon name="plug" /> {busy ? "Importing…" : "Import file"}</button>
        <button onClick={() => setShowLive(!showLive)}>{showLive ? "Hide" : "Show"} live endpoint (OTLP/HTTP)</button>
      </div>
      <ErrorBox error={error} />
      {result && (
        <div className="note" style={{ marginTop: 10 }}>
          Imported {result.accepted} span(s) from {result.filename || "file"}{result.format && result.format !== "otlp" ? ` (converted from ${result.format} format)` : ""}
          {result.duplicates ? ` · ${result.duplicates} already present` : ""}
          {result.rejected?.length ? ` · ${result.rejected.length} rejected (${result.rejected[0]})` : ""}.
          {result.runs?.length > 0 && <> Traces: {result.runs.map((id) => <a key={id} href={`#/runs/${id}`} className="mono" style={{ marginLeft: 8 }}>{id.slice(0, 16)}</a>)}</>}
        </div>
      )}
      <MarksImport onImported={onImported} />
      {showLive && (
        <div style={{ marginTop: 12 }}>
          <Field label="OTLP/HTTP traces endpoint"><input readOnly value={endpoint} onFocus={(e) => e.target.select()} /></Field>
          <div className="note" style={{ fontSize: 12.5 }}>
            Python (any framework):
            <pre style={{ marginTop: 6 }}>{`from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
provider.add_span_processor(BatchSpanProcessor(
    OTLPSpanExporter(endpoint="${endpoint}")))`}</pre>
            Or set <code>OTEL_EXPORTER_OTLP_TRACES_ENDPOINT={endpoint}</code>. Recognized attributes: OpenInference
            (<code>openinference.span.kind</code>, <code>input.value</code>, <code>output.value</code>, <code>agent.name</code>, <code>tool.name</code>);
            span errors and <code>exception</code> events become failures with stack traces.
          </div>
        </div>
      )}
    </div>
  );
}

export default function RunsPage({ origin }) {
  const [runs, setRuns] = useState(null);
  const [error, setError] = useState(null);
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const load = () => api(`/api/runs?origin=${origin}`).then(setRuns).catch(setError);
  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [origin]);
  const external = origin === "external";
  const shown = (runs || []).filter((r) => (!status || r.status === status) && (!q || `${r.workflow_name} ${r.id} ${r.integration || ""}`.toLowerCase().includes(q.toLowerCase())));

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>{external ? "External traces" : "Runs"}</h1>
          <p>{external ? "Traces of agents built in any framework, imported as OpenTelemetry spans. Read-only: no checkpoints or execution control." : "Every execution of a Studio workflow, including forks and replays."}</p>
        </div>
      </div>
      {external && <TraceImport onImported={load} />}
      <div className="row" style={{ marginBottom: 12 }}>
        <input placeholder="Search name or id" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 280 }} />
        <select value={status} onChange={(e) => setStatus(e.target.value)} style={{ maxWidth: 180 }}>
          <option value="">All statuses</option>
          {["completed", "failed", "running", "paused", "interrupted", "cancelled", "receiving"].map((s) => <option key={s}>{s}</option>)}
        </select>
      </div>
      <ErrorBox error={error} />
      <div className="card">
        {runs === null ? <div className="empty">Loading…</div> : shown.length === 0 ? (
          <Empty title="No runs">{external ? "Import a trace file above, or export OTLP spans to the live endpoint." : "Open a workflow and press Run."}</Empty>
        ) : (
          <table className="table">
            <thead><tr><th>Status</th><th>{external ? "Source" : "Workflow"}</th><th>Kind</th><th>Started</th><th>Duration</th><th>Spans</th><th>Error</th></tr></thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.id} className="clickable" onClick={() => (window.location.hash = `#/runs/${r.id}`)}>
                  <td><Status value={r.status} /></td>
                  <td><div>{r.workflow_name || r.integration}</div><div className="muted mono">{r.id}</div></td>
                  <td className="row">
                    {external ? <Badge tone="ro">trace-only</Badge> : <ProviderBadge mode={r.provider_mode} />}
                    {r.relation && <Badge tone="accent">{r.relation} of {r.parent_run_id?.slice(0, 12)}</Badge>}
                  </td>
                  <td>{fmtTime(r.started_at || r.created_at)}</td>
                  <td>{duration(r.started_at, r.ended_at)}</td>
                  <td>{r.span_count}</td>
                  <td className="muted" style={{ maxWidth: 280 }}>{r.error ? `${r.error.node_id ? r.error.node_id + ": " : ""}${r.error.message || r.error.type}`.slice(0, 120) : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
