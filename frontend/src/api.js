let metaPromise = null;

export function meta(refresh = false) {
  if (!metaPromise || refresh) {
    metaPromise = fetch("/api/meta").then((r) => {
      if (!r.ok) throw new Error(`meta failed: ${r.status}`);
      return r.json();
    });
  }
  return metaPromise;
}

export class ApiError extends Error {
  constructor(status, detail) {
    const message = typeof detail === "string" ? detail
      : detail?.message ? `${detail.message}${detail.errors ? ": " + detail.errors.join("; ") : ""}`
      : JSON.stringify(detail);
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

export async function api(path, { method = "GET", body } = {}, retried = false) {
  const headers = {};
  if (method !== "GET") {
    headers["x-studio-csrf"] = (await meta()).csrf_token;
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await response.text();
  // The server issues a new CSRF token on every start; refresh once after a restart.
  if (response.status === 403 && method !== "GET" && !retried && text.includes("CSRF")) {
    await meta(true);
    return api(path, { method, body }, true);
  }
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!response.ok) throw new ApiError(response.status, data?.detail ?? data ?? response.statusText);
  return data;
}

export const TERMINAL = new Set(["completed", "failed", "cancelled", "paused", "interrupted"]);

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function duration(start, end) {
  if (!start) return "—";
  const ms = (end ? new Date(end) : new Date()) - new Date(start);
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

export function pretty(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}
