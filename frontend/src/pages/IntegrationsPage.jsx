import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import { Badge, ErrorBox, Field, Icon, Status } from "../components/ui.jsx";

function LaunchCard({ spec }) {
  const [params, setParams] = useState(Object.fromEntries(spec.parameters.map((p) => [p.name, p.default])));
  const [launch, setLaunch] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    if (!launch || (launch.status !== "running" && launch.run_id)) return undefined;
    const t = setTimeout(() => api(`/api/launches/${launch.id}`).then(setLaunch).catch(setError), 700);
    return () => clearTimeout(t);
  }, [launch]);
  const start = async () => {
    setError(null);
    try { setLaunch(await api(`/api/integrations/${spec.id}/launch`, { method: "POST", body: { params } })); } catch (e) { setError(e); }
  };
  return (
    <div className="card card-pad">
      <div className="row"><h3 style={{ margin: 0, fontSize: 15 }}>{spec.name}</h3><Badge tone="accent">executable</Badge><Badge tone="ro">trace-only control</Badge></div>
      <p className="muted" style={{ fontSize: 13 }}>{spec.description}</p>
      <p className="mono muted" style={{ fontSize: 12 }}>{spec.script}</p>
      {spec.parameters.map((p) => (
        <Field key={p.name} label={p.label}><input value={params[p.name]} onChange={(e) => setParams({ ...params, [p.name]: e.target.value })} /></Field>
      ))}
      <ErrorBox error={error} />
      <div className="row">
        <button className="primary" onClick={start} disabled={launch?.status === "running"}><Icon name="play" /> Launch agent process</button>
        {launch && <><Status value={launch.status === "exited" ? "completed" : launch.status} /><span className="muted">pid {launch.pid} {launch.exit_code !== null ? `· exit ${launch.exit_code}` : ""}</span></>}
        {launch?.run_id && <a href={`#/runs/${launch.run_id}`}>Open trace →</a>}
      </div>
      {launch?.stderr && <pre className="error-box" style={{ marginTop: 10 }}>{launch.stderr.slice(-800)}</pre>}
    </div>
  );
}

export default function IntegrationsPage() {
  const [items, setItems] = useState([]);
  const [error, setError] = useState(null);
  useEffect(() => { api("/api/integrations").then(setItems).catch(setError); }, []);
  const otlp = items.find((i) => i.id === "otlp");
  return (
    <div className="page">
      <div className="page-head"><div><h1>Integrations</h1><p>Bring agents built outside the Studio. Agents on this page report traces only (read-only). LangGraph agents that expose their checkpoints can be debugged with pause/resume and fork under <a href="#/connected">Connected agents</a>.</p></div></div>
      <ErrorBox error={error} />
      <div className="grid-cards" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(420px, 1fr))" }}>
        {items.filter((i) => i.kind === "executable+trace").map((spec) => <LaunchCard key={spec.id} spec={spec} />)}
        {otlp && (
          <div className="card card-pad">
            <div className="row"><h3 style={{ margin: 0, fontSize: 15 }}>{otlp.name}</h3><Badge tone="ro">read-only</Badge></div>
            <p className="muted" style={{ fontSize: 13 }}>{otlp.description}</p>
            <Field label="OTLP/HTTP traces endpoint"><input readOnly value={otlp.endpoint} onFocus={(e) => e.target.select()} /></Field>
            <div className="note" style={{ fontSize: 12.5 }}>
              Python (any framework):
              <pre style={{ marginTop: 6 }}>{`from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
provider.add_span_processor(BatchSpanProcessor(
    OTLPSpanExporter(endpoint="${otlp.endpoint}")))`}</pre>
              Or set <code>OTEL_EXPORTER_OTLP_TRACES_ENDPOINT={otlp.endpoint}</code>. Recognized attributes: OpenInference
              (<code>openinference.span.kind</code>, <code>input.value</code>, <code>output.value</code>, <code>agent.name</code>, <code>tool.name</code>);
              span errors and <code>exception</code> events become failures with stack traces.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
