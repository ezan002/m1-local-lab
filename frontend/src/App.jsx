import React, { useEffect, useState } from "react";
import { meta } from "./api.js";
import WorkflowsPage from "./pages/WorkflowsPage.jsx";
import EditorPage from "./pages/EditorPage.jsx";
import RunsPage from "./pages/RunsPage.jsx";
import RunDetailPage from "./pages/RunDetailPage.jsx";
import IntegrationsPage from "./pages/IntegrationsPage.jsx";
import SettingsPage from "./pages/SettingsPage.jsx";
import { Icon } from "./components/ui.jsx";

function useHashRoute() {
  const [hash, setHash] = useState(window.location.hash || "#/workflows");
  useEffect(() => {
    const onChange = () => setHash(window.location.hash || "#/workflows");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash.replace(/^#/, "").split("?")[0].split("/").filter(Boolean);
}

const NAV = [
  { key: "workflows", label: "Workflows", icon: "flow" },
  { key: "runs", label: "Runs", icon: "play" },
  { key: "traces", label: "External traces", icon: "radar" },
  { key: "integrations", label: "Integrations", icon: "plug" },
  { key: "settings", label: "Settings", icon: "gear" },
];

export default function App() {
  const parts = useHashRoute();
  const [info, setInfo] = useState(null);
  const [metaError, setMetaError] = useState(null);
  useEffect(() => { meta().then(setInfo).catch((e) => setMetaError(e.message)); }, []);
  const section = parts[0] || "workflows";

  let page;
  if (section === "workflows" && parts[1]) page = <EditorPage key={parts[1]} workflowId={parts[1]} />;
  else if (section === "workflows") page = <WorkflowsPage />;
  else if (section === "runs" && parts[1]) page = <RunDetailPage key={parts[1]} runId={parts[1]} />;
  else if (section === "runs") page = <RunsPage origin="studio" />;
  else if (section === "traces") page = <RunsPage origin="external" />;
  else if (section === "integrations") page = <IntegrationsPage />;
  else if (section === "settings") page = <SettingsPage />;
  else page = <WorkflowsPage />;

  const activeKey = section === "runs" && parts[1] ? "runs" : section;
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <img src="/favicon.svg" alt="" width="26" height="26" />
          <div><strong>Agent Studio</strong><span>local · single user</span></div>
        </div>
        <nav>
          {NAV.map((item) => (
            <a key={item.key} href={`#/${item.key}`} className={activeKey === item.key ? "active" : ""}>
              <Icon name={item.icon} /> {item.label}
            </a>
          ))}
        </nav>
        <div className="sidebar-foot">
          {metaError && <div className="pill err">API unreachable</div>}
          {info && (
            <>
              <div className={`pill ${info.provider.openai_key_configured ? "ok" : "warn"}`}>
                {info.provider.openai_key_configured ? "OpenAI key configured" : "No model key — deterministic only"}
              </div>
              <div className="build" title={info.build.commit}>
                build {info.build.commit === "unknown" ? "unknown" : info.build.commit.slice(0, 8)}
                {info.build.uncommitted_changes === true ? " · dirty" : ""}
              </div>
            </>
          )}
        </div>
      </aside>
      <main className="main">{page}</main>
    </div>
  );
}
