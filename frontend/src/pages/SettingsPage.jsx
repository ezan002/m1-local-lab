import React, { useEffect, useState } from "react";
import { api, meta } from "../api.js";
import { Badge, ErrorBox, Field, Icon } from "../components/ui.jsx";

export default function SettingsPage() {
  const [info, setInfo] = useState(null);
  const [form, setForm] = useState({ base_url: "", default_model: "" });
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => { meta(true).then((m) => { setInfo(m); setForm({ base_url: m.provider.base_url, default_model: m.provider.default_model }); }).catch(setError); }, []);
  const save = async () => {
    setError(null); setSaved(false);
    try { const m = await api("/api/settings/provider", { method: "PUT", body: form }); setInfo(m); setSaved(true); } catch (e) { setError(e); }
  };
  if (!info) return <div className="page"><ErrorBox error={error} /></div>;
  return (
    <div className="page" style={{ maxWidth: 760 }}>
      <div className="page-head"><div><h1>Settings</h1><p>Model provider and runtime information.</p></div></div>
      <div className="card card-pad" style={{ marginBottom: 14 }}>
        <h3 style={{ marginTop: 0 }}>LLM provider (OpenAI-compatible)</h3>
        <div className="row" style={{ marginBottom: 12 }}>
          API key: {info.provider.openai_key_configured ? <Badge tone="real">configured via OPENAI_API_KEY</Badge> : <Badge tone="det">not set — real model runs will fail with MissingCredentials</Badge>}
        </div>
        <div className="note" style={{ marginBottom: 12 }}>The key is read from the server environment only and is never stored or returned by the API. Any OpenAI-compatible endpoint works (e.g. a local vLLM/Ollama gateway).</div>
        <Field label="Base URL"><input value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} /></Field>
        <Field label="Default model"><input value={form.default_model} onChange={(e) => setForm({ ...form, default_model: e.target.value })} /></Field>
        <ErrorBox error={error} />
        <div className="row"><button className="primary" onClick={save}><Icon name="save" /> Save</button>{saved && <span className="muted">Saved.</span>}</div>
      </div>
      <div className="card card-pad">
        <h3 style={{ marginTop: 0 }}>Runtime</h3>
        <dl className="kv">
          <dt>Build commit</dt><dd className="mono">{info.build.commit}</dd>
          <dt>Uncommitted changes</dt><dd>{String(info.build.uncommitted_changes)}</dd>
          <dt>Process started</dt><dd>{info.build.started_at}</dd>
          <dt>OTLP endpoint</dt><dd className="mono">{info.otlp_endpoint}</dd>
          <dt>Recovered at start</dt><dd>{info.recovered_runs.length ? info.recovered_runs.join(", ") + " (marked interrupted; resumable)" : "none"}</dd>
        </dl>
      </div>
    </div>
  );
}
