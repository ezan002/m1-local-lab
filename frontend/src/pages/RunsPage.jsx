import React, { useEffect, useState } from "react";
import { api, duration, fmtTime } from "../api.js";
import { Badge, Empty, ErrorBox, ProviderBadge, Status } from "../components/ui.jsx";

export default function RunsPage({ origin }) {
  const [runs, setRuns] = useState(null);
  const [error, setError] = useState(null);
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const load = () => api(`/api/runs?origin=${origin}`).then(setRuns).catch(setError);
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
          <p>{external ? "Runs reported by external agents over OpenTelemetry. Read-only: no checkpoints or execution control." : "Every execution of a Studio workflow, including forks and replays."}</p>
        </div>
      </div>
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
          <Empty title="No runs">{external ? "Launch the example agent from Integrations, or export OTLP spans to /v1/traces." : "Open a workflow and press Run."}</Empty>
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
