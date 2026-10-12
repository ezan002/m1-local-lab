import React, { useEffect, useState } from "react";
import { api, fmtTime } from "../api.js";
import { Empty, ErrorBox, Icon, Status } from "../components/ui.jsx";

const BLANK = {
  default_provider: "deterministic",
  default_model: "",
  nodes: [
    { id: "start", type: "start", label: "Start", position: { x: 0, y: 100 }, config: { inputs: [{ name: "question", label: "Question", default: "What is a checkpoint?" }] } },
    { id: "assistant", type: "agent", label: "Assistant", position: { x: 280, y: 100 },
      config: { role: "assistant", system_prompt: "Answer briefly.", prompt: "{{input.question}}", mock_response: "Scripted answer to: {{input.question}}" } },
    { id: "answer", type: "output", label: "Answer", position: { x: 560, y: 100 }, config: { template: "{{nodes.assistant.output}}" } },
  ],
  edges: [{ id: "e1", source: "start", target: "assistant" }, { id: "e2", source: "assistant", target: "answer" }],
};

export default function WorkflowsPage() {
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);
  const load = () => api("/api/workflows").then(setItems).catch(setError);
  useEffect(() => { load(); }, []);

  const create = async () => {
    try {
      const wf = await api("/api/workflows", { method: "POST", body: { name: "Untitled workflow", description: "", definition: BLANK } });
      window.location.hash = `#/workflows/${wf.id}`;
    } catch (e) { setError(e); }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div><h1>Workflows</h1><p>Build, run and debug multi-agent workflows. Changes are saved locally (SQLite).</p></div>
        <button className="primary" onClick={create}><Icon name="plus" /> New workflow</button>
      </div>
      <ErrorBox error={error} />
      {items === null ? <div className="muted">Loading…</div> : items.length === 0 ? (
        <Empty title="No workflows yet">Create one to get started.</Empty>
      ) : (
        <div className="grid-cards">
          {items.map((wf) => (
            <div key={wf.id} className="card wf-card" onClick={() => (window.location.hash = `#/workflows/${wf.id}`)} role="link" tabIndex={0}
              onKeyDown={(e) => e.key === "Enter" && (window.location.hash = `#/workflows/${wf.id}`)}>
              <div className="row"><Icon name="workflow" /><h3>{wf.name}</h3></div>
              <p>{wf.description || "No description"}</p>
              <div className="row muted" style={{ fontSize: 12.5 }}>
                <span>v{wf.version}</span>·<span>{wf.run_count} run(s)</span>·<span>updated {fmtTime(wf.updated_at)}</span>
                <span className="spacer" />
                {wf.last_run && <Status value={wf.last_run.status} />}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
