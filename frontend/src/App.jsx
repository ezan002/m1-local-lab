import React, { useEffect, useState } from "react";
import { meta } from "./api.js";
import WorkflowsPage from "./pages/WorkflowsPage.jsx";
import EditorPage from "./pages/EditorPage.jsx";
import RunsPage from "./pages/RunsPage.jsx";
import RunDetailPage from "./pages/RunDetailPage.jsx";
import IntegrationsPage from "./pages/IntegrationsPage.jsx";
import ConnectedPage from "./pages/ConnectedPage.jsx";
import SettingsPage from "./pages/SettingsPage.jsx";
import { Icon } from "./components/ui.jsx";

function useHashRoute() {
  const [hash, setHash] = useState(window.location.hash || `#/${DEFAULT_SECTION}`);
  useEffect(() => {
    const onChange = () => setHash(window.location.hash || `#/${DEFAULT_SECTION}`);
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash.replace(/^#/, "").split("?")[0].split("/").filter(Boolean);
}

// The product focuses on bringing in external agents (Connected agents) and external traces. Building workflows in the
// Studio (Workflows, Runs) and the Integrations test launcher stay available but are hidden from the sidebar; open
// them by URL (#/workflows, #/runs, #/integrations) or show them with localStorage "studio.showHidden" = "1".
const NAV = [
  { key: "workflows", label: "Workflows", icon: "flow", hidden: true },
  { key: "runs", label: "Runs", icon: "play", hidden: true },
  { key: "connected", label: "Connected agents", icon: "branch" },
  { key: "traces", label: "External traces", icon: "radar" },
  { key: "integrations", label: "Integrations", icon: "plug", hidden: true },
  { key: "settings", label: "Settings", icon: "gear" },
];
const DEFAULT_SECTION = "connected";

function showHidden() {
  try { return window.localStorage.getItem("studio.showHidden") === "1"; } catch { return false; }
}

export default function App() {
  const parts = useHashRoute();
  const [info, setInfo] = useState(null);
  const [metaError, setMetaError] = useState(null);
  const [runSection, setRunSection] = useState(null);
  useEffect(() => { meta().then(setInfo).catch((e) => setMetaError(e.message)); }, []);
  // A run page tells the shell which list it belongs to (Runs, Connected agents or External traces).
  useEffect(() => {
    const onNav = (e) => setRunSection(e.detail);
    window.addEventListener("studio:run-origin", onNav);
    return () => window.removeEventListener("studio:run-origin", onNav);
  }, []);
  const section = parts[0] || DEFAULT_SECTION;

  let page;
  if (section === "workflows" && parts[1]) page = <EditorPage key={parts[1]} workflowId={parts[1]} />;
  else if (section === "workflows") page = <WorkflowsPage />;
  else if (section === "runs" && parts[1]) page = <RunDetailPage key={parts[1]} runId={parts[1]} />;
  else if (section === "runs") page = <RunsPage origin="studio" />;
  else if (section === "traces") page = <RunsPage origin="external" />;
  else if (section === "connected") page = <ConnectedPage />;
  else if (section === "integrations") page = <IntegrationsPage />;
  else if (section === "settings") page = <SettingsPage />;
  else page = <ConnectedPage />;

  const activeKey = section === "runs" && parts[1] ? (runSection || "runs") : section;
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <img src="/favicon.svg" alt="" width="26" height="26" />
          <div><strong>Agent Studio</strong><span>local · single user</span></div>
        </div>
        <nav>
          {NAV.filter((item) => !item.hidden || showHidden() || item.key === activeKey).map((item) => (
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
