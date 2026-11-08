import React, { useState } from "react";
import { pretty } from "../api.js";

const PATHS = {
  flow: "M5 5h5v5H5zM14 14h5v5h-5zM7.5 10v4a2 2 0 0 0 2 2H14",
  play: "M7 5l11 7-11 7z",
  radar: "M12 12m-8 0a8 8 0 1 0 16 0a8 8 0 1 0-16 0M12 12m-4 0a4 4 0 1 0 8 0a4 4 0 1 0-8 0M12 12l6-6",
  plug: "M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4",
  gear: "M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1",
  plus: "M12 5v14M5 12h14",
  save: "M5 4h11l3 3v13H5zM8 4v5h8V4M8 20v-6h8v6",
  pause: "M7 5h3v14H7zM14 5h3v14h-3z",
  stop: "M6 6h12v12H6z",
  branch: "M6 4v12M6 16a3 3 0 1 0 0.01 0M18 8a3 3 0 1 0 0.01 0M18 11c0 4-6 3-12 5",
  replay: "M4 12a8 8 0 1 0 3-6.2M4 4v4h4",
  trash: "M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13",
  back: "M15 5l-7 7 7 7",
  agent: "M12 4a4 4 0 1 0 0 8a4 4 0 1 0 0-8M4 20c1-4 4.5-6 8-6s7 2 8 6",
  tool: "M14 6a4 4 0 0 0 4.9 4.9L21 13l-8 8-2.1-2.1A4 4 0 0 0 6 14L3 11l8-8z",
  start: "M5 12h12M13 6l6 6-6 6",
  output: "M5 5h14v14H5zM9 12l2 2 4-4",
  condition: "M12 3l9 9-9 9-9-9z",
  llm: "M4 6h16v10H8l-4 4z",
  workflow: "M4 6h6v4H4zM14 14h6v4h-6zM7 10v6h7",
  chain: "M9 15l6-6M7 17a3 3 0 0 1 0-4l2-2M17 7a3 3 0 0 1 0 4l-2 2",
  external: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
  pauseSpan: "M7 5h3v14H7zM14 5h3v14h-3z",
  gate: "M5 4v16M19 4v16M5 12h14",
  edit: "M4 20h4L19 9l-4-4L4 16zM13 7l4 4",
};

export function Icon({ name, size = 16 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="icon">
      <path d={PATHS[name] || PATHS.chain} />
    </svg>
  );
}

const STATUS_LABEL = { ok: "ok", error: "error", running: "running", paused: "paused", resumed: "resumed" };

export function Status({ value }) {
  const v = value || "unknown";
  return <span className={`status status-${v}`}>{STATUS_LABEL[v] || v}</span>;
}

export function Badge({ children, tone = "neutral", title }) {
  return <span className={`badge badge-${tone}`} title={title}>{children}</span>;
}

export function ProviderBadge({ mode }) {
  if (!mode) return null;
  const tone = mode === "openai" ? "real" : mode === "deterministic" ? "det" : "neutral";
  const label = mode === "openai" ? "real model" : mode === "deterministic" ? "deterministic script" : mode;
  return <Badge tone={tone} title="Provider used by agent nodes in this run">{label}</Badge>;
}

export function Json({ value, collapsed = false, label }) {
  const [open, setOpen] = useState(!collapsed);
  const text = pretty(value);
  return (
    <div className="json">
      {label && (
        <button className="json-head" onClick={() => setOpen(!open)}>
          <span>{open ? "▾" : "▸"} {label}</span>
          <span className="muted">{text.length > 60 ? `${text.length} chars` : ""}</span>
        </button>
      )}
      {open && <pre>{text}</pre>}
    </div>
  );
}

export function Tabs({ tabs, value, onChange }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.key} role="tab" aria-selected={value === t.key} className={value === t.key ? "active" : ""}
          onClick={() => onChange(t.key)} disabled={t.disabled} title={t.title}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Modal({ title, onClose, children, footer }) {
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label={title}>
        <div className="modal-head"><h3>{title}</h3><button className="ghost" onClick={onClose} aria-label="Close">✕</button></div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

export function ErrorBox({ error }) {
  if (!error) return null;
  return <div className="error-box" role="alert">{typeof error === "string" ? error : error.message}</div>;
}

export function Empty({ title, children }) {
  return <div className="empty"><strong>{title}</strong><div>{children}</div></div>;
}

export function Field({ label, hint, children }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}
