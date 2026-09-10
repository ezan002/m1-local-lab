"use strict";

const byId = id => document.getElementById(id);
const EVENT_TYPES = [
  "run_started", "node_started", "node_finished", "checkpoint_saved",
  "pause_requested", "paused", "resumed", "replay_created", "fork_created",
  "error", "run_finished"
];
const STATUS_META = {
  queued: {label: "排队中", symbol: "○"},
  running: {label: "运行中", symbol: "●"},
  pausing: {label: "正在暂停", symbol: "◐"},
  paused: {label: "已暂停", symbol: "Ⅱ"},
  interrupted: {label: "已中断", symbol: "!"},
  completed: {label: "已完成", symbol: "✓"},
  partial: {label: "部分完成", symbol: "△"},
  failed: {label: "失败", symbol: "×"}
};
const NODE_LABELS = {
  planner: "Planner / 规划",
  orchestrator: "Orchestrator / 调度",
  pause_gate: "安全暂停边界",
  web_surfer: "WebSurfer / 网页",
  file_reader: "File Reader / 文件",
  coder: "Coder / 代码产物",
  finalizer: "Finalizer / 总结"
};
const ui = {
  csrf: "",
  capabilities: {
    resume_statuses: ["paused", "failed", "interrupted"],
    active_statuses: ["queued", "running", "pausing"],
    terminal_statuses: ["completed", "partial", "failed", "paused", "interrupted"],
    fork_fields: ["plan", "worker_instruction"]
  },
  runs: [],
  currentRunId: null,
  current: null,
  history: [],
  selectedCheckpoint: null,
  events: [],
  eventIds: new Set(),
  source: null,
  lastEventId: 0,
  submitting: false,
  detailTimer: null,
  toastTimer: null,
  attribution: {
    loaded: false,
    summary: null,
    cases: [],
    selectedKey: null,
    summaryController: null,
    casesController: null,
    detailController: null,
    summaryToken: 0,
    casesToken: 0,
    detailToken: 0
  }
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = {...(options.headers || {})};
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers["X-M1-CSRF"] = ui.csrf;
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, {...options, headers});
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

function statusMeta(status) {
  return STATUS_META[status] || {label: status || "未知", symbol: "?"};
}

function statusBadge(status) {
  const meta = statusMeta(status);
  const badge = element("span", "status-badge", `${meta.symbol} ${meta.label}`);
  badge.dataset.status = status || "unknown";
  return badge;
}

function formatTime(value, includeDate = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    ...(includeDate ? {month: "2-digit", day: "2-digit"} : {}),
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
  }).format(date);
}

function shortId(value, length = 10) {
  if (!value) return "—";
  const text = String(value);
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

function showToast(message, timeout = 3200) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(ui.toastTimer);
  ui.toastTimer = setTimeout(() => { toast.hidden = true; }, timeout);
}

async function copyText(text, label) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    showToast(`${label}已复制。`);
  } catch {
    const area = element("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
    showToast(`${label}已复制。`);
  }
}

function isActive(status) {
  return ui.capabilities.active_statuses.includes(status);
}

function isStreamTerminal(status) {
  return ui.capabilities.terminal_statuses.includes(status);
}

function anyActiveRun() {
  return ui.runs.some(run => isActive(run.status));
}

function setSubmitting(value) {
  ui.submitting = value;
  const button = byId("start-run");
  button.classList.toggle("is-busy", value);
  button.querySelector(".button-label").textContent = value ? "正在提交" : "开始运行";
  button.disabled = value || anyActiveRun();
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    setHealthChip("app-health", health.app.status === "healthy", "应用正常", "应用异常");
    const phoenixOk = health.phoenix.status === "healthy";
    setHealthChip("phoenix-health", phoenixOk, "Phoenix 正常", "Phoenix 不可用");
    byId("phoenix-health").title = health.phoenix.warning || "Phoenix 首页可访问";
  } catch (error) {
    setHealthChip("app-health", false, "应用正常", "健康检查失败");
    byId("app-health").title = error.message;
  }
}

function setHealthChip(id, healthy, healthyText, errorText) {
  const chip = byId(id);
  chip.classList.remove("is-loading", "is-healthy", "is-unavailable");
  chip.classList.add(healthy ? "is-healthy" : "is-unavailable");
  chip.replaceChildren(element("span", "health-dot"), document.createTextNode(healthy ? healthyText : errorText));
}

async function refreshRuns({quiet = false} = {}) {
  const status = byId("runs-status");
  if (!quiet) {
    status.hidden = false;
    status.textContent = "正在加载运行记录…";
  }
  try {
    ui.runs = await api("/api/runs");
    renderRuns();
    setSubmitting(ui.submitting);
  } catch (error) {
    status.hidden = false;
    status.textContent = `加载失败：${error.message}`;
    byId("runs-list").replaceChildren();
  }
}

function renderRuns() {
  const list = byId("runs-list");
  const status = byId("runs-status");
  list.replaceChildren();
  if (!ui.runs.length) {
    status.hidden = false;
    status.textContent = "还没有运行记录。";
    return;
  }
  status.hidden = true;
  ui.runs.forEach(run => {
    const button = element("button", "run-item");
    button.type = "button";
    button.dataset.runId = run.run_id;
    button.classList.toggle("is-selected", run.run_id === ui.currentRunId);
    button.setAttribute("aria-pressed", String(run.run_id === ui.currentRunId));
    const top = element("div", "run-item-meta");
    top.append(statusBadge(run.status), element("span", "", formatTime(run.updated_at, true)));
    const title = element("strong", "run-item-title", run.task || "未命名任务");
    const bottom = element("div", "run-item-meta");
    bottom.append(element("span", "", shortId(run.run_id, 12)), element("span", "", run.parent_run_id ? "分支/回放" : "原始运行"));
    button.append(top, title, bottom);
    button.addEventListener("click", () => selectRun(run.run_id));
    list.append(button);
  });
}

function closeEventSource() {
  if (ui.source) ui.source.close();
  ui.source = null;
}

async function selectRun(runId) {
  if (ui.currentRunId !== runId) {
    closeEventSource();
    ui.currentRunId = runId;
    ui.current = null;
    ui.history = [];
    ui.selectedCheckpoint = null;
    ui.events = [];
    ui.eventIds = new Set();
    ui.lastEventId = 0;
    byId("empty-selection").hidden = true;
    byId("run-view").hidden = false;
    renderRuns();
    renderLoadingDetail();
  }
  const selectedId = runId;
  try {
    const [current, history, events] = await Promise.all([
      api(`/api/runs/${encodeURIComponent(runId)}`),
      api(`/api/runs/${encodeURIComponent(runId)}/history`),
      api(`/api/runs/${encodeURIComponent(runId)}/events-log`)
    ]);
    if (ui.currentRunId !== selectedId) return;
    ui.current = current;
    ui.history = history;
    ui.events = [];
    ui.eventIds = new Set();
    events.forEach(appendEvent);
    if (ui.selectedCheckpoint) {
      ui.selectedCheckpoint = history.find(item => item.checkpoint_id === ui.selectedCheckpoint.checkpoint_id) || null;
    }
    renderAll();
    if (!isStreamTerminal(current.status)) connectEvents();
    else setStreamStatus("idle", "运行已停止");
  } catch (error) {
    if (ui.currentRunId !== selectedId) return;
    renderDetailError(error);
  }
}

function renderLoadingDetail() {
  byId("run-title").textContent = "正在加载…";
  byId("top-trace-id").textContent = "正在加载";
  byId("copy-top-trace").disabled = true;
  byId("output").textContent = "正在读取运行详情…";
  byId("events-list").replaceChildren();
  byId("events-empty").hidden = false;
  setStreamStatus("idle", "未连接");
}

function renderDetailError(error) {
  byId("problem-panel").hidden = false;
  byId("problem-panel").className = "problem-panel";
  byId("problem-panel").textContent = `运行详情加载失败：${error.message}`;
  byId("output").textContent = "无法读取输出。";
  setStreamStatus("error", "加载失败");
  byId("retry-stream").hidden = false;
}

function appendEvent(event) {
  const id = Number(event.id || 0);
  if (id && ui.eventIds.has(id)) return false;
  const normalized = {
    id,
    event: event.event || "message",
    data: event.data || {},
    created_at: event.created_at || event.data?._created_at || null
  };
  if (normalized.data && Object.prototype.hasOwnProperty.call(normalized.data, "_created_at")) {
    normalized.data = {...normalized.data};
    delete normalized.data._created_at;
  }
  if (id) {
    ui.eventIds.add(id);
    ui.lastEventId = Math.max(ui.lastEventId, id);
  }
  ui.events.push(normalized);
  ui.events.sort((a, b) => a.id - b.id);
  return true;
}

function connectEvents() {
  closeEventSource();
  if (!ui.currentRunId || (ui.current && isStreamTerminal(ui.current.status))) return;
  const runId = ui.currentRunId;
  const query = ui.lastEventId ? `?last_event_id=${ui.lastEventId}` : "";
  const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events${query}`);
  ui.source = source;
  setStreamStatus("idle", "正在连接");
  byId("retry-stream").hidden = true;
  source.onopen = () => setStreamStatus("live", "实时连接");
  EVENT_TYPES.forEach(type => source.addEventListener(type, message => {
    if (ui.currentRunId !== runId || ui.source !== source) return;
    let data = {};
    try { data = JSON.parse(message.data || "{}"); }
    catch { data = {raw: message.data}; }
    const added = appendEvent({id: message.lastEventId, event: type, data});
    if (added) renderEvents();
    scheduleDetailRefresh();
    if (["run_finished", "paused", "error"].includes(type)) {
      setStreamStatus("idle", "正在确认状态");
    }
  }));
  source.onerror = () => {
    if (ui.currentRunId !== runId || ui.source !== source) return;
    setStreamStatus("error", "连接中断");
    byId("retry-stream").hidden = false;
    scheduleDetailRefresh();
  };
}

function setStreamStatus(kind, text) {
  const chip = byId("stream-status");
  chip.className = `stream-chip is-${kind}`;
  chip.textContent = text;
}

function scheduleDetailRefresh() {
  clearTimeout(ui.detailTimer);
  ui.detailTimer = setTimeout(refreshSelectedDetail, 140);
}

async function refreshSelectedDetail() {
  const runId = ui.currentRunId;
  if (!runId) return;
  try {
    const [current, history] = await Promise.all([
      api(`/api/runs/${encodeURIComponent(runId)}`),
      api(`/api/runs/${encodeURIComponent(runId)}/history`)
    ]);
    if (ui.currentRunId !== runId) return;
    ui.current = current;
    ui.history = history;
    if (ui.selectedCheckpoint) {
      ui.selectedCheckpoint = history.find(item => item.checkpoint_id === ui.selectedCheckpoint.checkpoint_id) || null;
    }
    renderAll();
    await refreshRuns({quiet: true});
    if (isStreamTerminal(current.status)) {
      closeEventSource();
      setStreamStatus("idle", current.status === "paused" ? "已暂停" : "运行已停止");
      byId("retry-stream").hidden = true;
    }
  } catch (error) {
    showToast(`刷新详情失败：${error.message}`);
  }
}

function renderAll() {
  if (!ui.current) return;
  renderOverview();
  renderFlow();
  renderOutput();
  renderEvents();
  renderEvidence();
  renderCheckpoints();
  renderState();
  updateControls();
}

function renderOverview() {
  const current = ui.current;
  const state = current.state || {};
  const run = current.run || ui.runs.find(item => item.run_id === ui.currentRunId) || {};
  byId("run-title").textContent = run.task || state.task || "未命名任务";
  byId("run-status").replaceChildren(statusBadge(current.status));
  byId("current-node").textContent = currentNodeLabel();
  byId("step-count").textContent = String(state.step_count ?? 0);
  byId("updated-at").textContent = formatTime(run.updated_at, true);
  byId("trace-id").textContent = current.trace_id || "等待首个 checkpoint…";
  byId("copy-trace").disabled = !current.trace_id;
  byId("top-trace-id").textContent = current.trace_id || "暂无";
  byId("copy-top-trace").disabled = !current.trace_id;
  const lineage = [];
  if (run.parent_run_id) lineage.push(`来源运行 ${shortId(run.parent_run_id, 16)}`);
  if (run.source_checkpoint_id) lineage.push(`来源 checkpoint ${shortId(run.source_checkpoint_id, 16)}`);
  lineage.push(`Run ${shortId(run.run_id, 16)}`, `Branch ${shortId(run.branch_id, 16)}`);
  byId("lineage").textContent = lineage.join(" · ");
}

function latestNodeFromEvents() {
  for (let index = ui.events.length - 1; index >= 0; index -= 1) {
    const event = ui.events[index];
    if (event.event === "node_started" && event.data?.node) return event.data.node;
  }
  return null;
}

function currentNodeName() {
  const state = ui.current?.state || {};
  const status = ui.current?.status;
  if (status === "pausing") return "pause_gate";
  if (status === "completed" || status === "partial") return "finalizer";
  if (status === "paused") return state.next_agent || "pause_gate";
  return state.next_agent || latestNodeFromEvents();
}

function currentNodeLabel() {
  const status = ui.current?.status;
  if (status === "queued") return "等待开始";
  if (status === "interrupted") return "进程已中断";
  if (status === "failed" && !currentNodeName()) return "失败位置未知";
  const name = currentNodeName();
  return NODE_LABELS[name] || name || "等待 checkpoint";
}

function flowGroup(nodeName) {
  if (["web_surfer", "file_reader", "coder", "pause_gate"].includes(nodeName)) return "worker";
  return nodeName;
}

function renderFlow() {
  const activeName = currentNodeName();
  const activeGroup = flowGroup(activeName);
  const completed = new Set(ui.events.filter(item => item.event === "node_finished").map(item => flowGroup(item.data?.node)));
  document.querySelectorAll("[data-flow-node]").forEach(node => {
    const name = node.dataset.flowNode;
    node.classList.remove("is-active", "is-complete", "is-failed");
    if (completed.has(name)) node.classList.add("is-complete");
    if (activeGroup === name && isActive(ui.current.status)) node.classList.add("is-active");
    if (ui.current.status === "failed" && activeGroup === name) node.classList.add("is-failed");
  });
  const workerName = ["web_surfer", "file_reader", "coder"].includes(activeName)
    ? activeName
    : [...ui.events].reverse().find(item => ["web_surfer", "file_reader", "coder"].includes(item.data?.node))?.data?.node;
  const workerText = NODE_LABELS[workerName] || "Worker 按任务动态选择";
  byId("worker-name").textContent = workerText;
  byId("worker-detail").textContent = workerText;
}

function renderOutput() {
  const state = ui.current.state || {};
  const messages = state.messages || [];
  const progress = state.progress_ledger || [];
  const fallback = messages.length ? messages[messages.length - 1].content : progress.length ? progress[progress.length - 1].summary : "尚无输出。";
  byId("output").textContent = state.final_answer || fallback || "尚无输出。";
  const errors = [];
  if (ui.current.run?.error) errors.push(ui.current.run.error);
  (state.errors || []).forEach(item => errors.push(item.message || item.error || JSON.stringify(item)));
  const panel = byId("problem-panel");
  if (ui.current.status === "partial") {
    panel.hidden = false;
    panel.className = "problem-panel is-partial";
    panel.textContent = errors.length ? `部分完成：${errors.join("\n")}` : "任务仅部分完成，请检查未解决项与证据。";
  } else if (ui.current.status === "failed" || ui.current.status === "interrupted" || errors.length) {
    panel.hidden = false;
    panel.className = "problem-panel";
    panel.textContent = errors.length ? errors.join("\n") : statusMeta(ui.current.status).label;
  } else {
    panel.hidden = true;
    panel.textContent = "";
  }
}

function renderEvents() {
  const list = byId("events-list");
  list.replaceChildren();
  byId("events-empty").hidden = ui.events.length > 0;
  byId("event-count").textContent = String(ui.events.length);
  ui.events.forEach(event => {
    const item = element("li", "event-item");
    if (event.event === "error") item.classList.add("is-error");
    if (["node_started", "checkpoint_saved"].includes(event.event)) item.classList.add("is-progress");
    item.append(
      element("time", "event-time", formatTime(event.created_at)),
      element("span", "event-name", `#${event.id || "—"} ${event.event}`),
      element("pre", "event-data", JSON.stringify(event.data || {}, null, 2))
    );
    list.append(item);
  });
}

function safeExternalUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

function renderEvidence() {
  const state = ui.current.state || {};
  const citations = [...new Set(state.citations || [])];
  const artifacts = state.artifacts || [];
  const citationsRoot = byId("citations");
  citationsRoot.replaceChildren();
  if (!citations.length) citationsRoot.append(element("div", "panel-empty", "暂无引用。"));
  citations.forEach(value => {
    const safe = safeExternalUrl(value);
    const row = element("div", "link-item");
    if (safe) {
      const link = element("a", "", value);
      link.href = safe;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      row.append(link);
    } else row.textContent = `已忽略非 HTTP(S) 引用：${value}`;
    citationsRoot.append(row);
  });
  const artifactRoot = byId("artifacts");
  artifactRoot.replaceChildren();
  if (!artifacts.length) artifactRoot.append(element("div", "panel-empty", "暂无截图或产物。"));
  artifacts.forEach(artifact => {
    const url = `/api/runs/${encodeURIComponent(ui.currentRunId)}/artifacts/${encodeURIComponent(artifact.id)}`;
    const card = element("article", "artifact-card");
    if (artifact.type === "screenshot") {
      const button = element("button");
      button.type = "button";
      button.setAttribute("aria-label", `打开截图 ${shortId(artifact.id)}`);
      const image = element("img");
      image.src = url;
      image.alt = `运行截图 ${shortId(artifact.id)}`;
      image.loading = "lazy";
      button.append(image);
      button.addEventListener("click", () => openScreenshot(url, artifact.id));
      card.append(button);
    }
    const meta = element("div", "artifact-meta");
    meta.append(element("strong", "", artifact.type || "artifact"));
    const link = element("a", "", `打开产物 · ${shortId(artifact.id, 14)}`);
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    meta.append(link);
    card.append(meta);
    artifactRoot.append(card);
  });
  byId("evidence-count").textContent = String(citations.length + artifacts.length);
}

function openScreenshot(url, artifactId) {
  byId("screenshot-title").textContent = `截图 ${shortId(artifactId, 18)}`;
  byId("screenshot-full").src = url;
  byId("screenshot-dialog").showModal();
}

function selectCheckpoint(checkpoint) {
  ui.selectedCheckpoint = checkpoint;
  renderCheckpoints();
  updateControls();
}

function renderCheckpoints() {
  const list = byId("checkpoint-list");
  list.replaceChildren();
  byId("checkpoint-count").textContent = String(ui.history.length);
  if (!ui.history.length) list.append(element("div", "panel-empty", "暂无 checkpoint。"));
  ui.history.forEach((checkpoint, index) => {
    const button = element("button", "checkpoint-item");
    button.type = "button";
    button.classList.toggle("is-selected", checkpoint.checkpoint_id === ui.selectedCheckpoint?.checkpoint_id);
    button.setAttribute("aria-pressed", String(checkpoint.checkpoint_id === ui.selectedCheckpoint?.checkpoint_id));
    button.append(
      element("strong", "", checkpoint.checkpoint_id),
      element("span", "", `${formatTime(checkpoint.created_at, true)} · ${checkpoint.next?.length ? `下一节点 ${checkpoint.next.join(", ")}` : "终结 checkpoint"}`)
    );
    button.addEventListener("click", () => selectCheckpoint(checkpoint));
    if (index === 0) button.dataset.head = "true";
    list.append(button);
  });
  renderCheckpointDetail();
}

function renderCheckpointDetail() {
  const root = byId("checkpoint-detail");
  root.replaceChildren();
  const checkpoint = ui.selectedCheckpoint;
  byId("copy-checkpoint").disabled = !checkpoint;
  if (!checkpoint) {
    root.className = "panel-empty";
    root.textContent = "选择左侧 checkpoint 查看详情。";
    return;
  }
  root.className = "";
  const fields = element("dl", "checkpoint-fields");
  const pairs = [
    ["checkpoint_id", checkpoint.checkpoint_id],
    ["创建时间", checkpoint.created_at || "—"],
    ["next", checkpoint.next?.join(", ") || "无（终结）"],
    ["状态", checkpoint.state?.status || "—"],
    ["步骤", checkpoint.state?.step_count ?? "—"],
    ["节点来源", checkpoint.metadata?.source || "—"],
    ["中断", checkpoint.interrupts?.join("\n") || "无"]
  ];
  pairs.forEach(([label, value]) => { fields.append(element("dt", "", String(label)), element("dd", "", String(value))); });
  root.append(fields);
  if (!checkpoint.next?.length) root.append(element("div", "terminal-note", "这是终结 checkpoint，后端不会从这里 replay；请选择更早的 checkpoint。"));
}

function renderState() {
  byId("state-json").textContent = JSON.stringify(ui.current, null, 2);
}

const ATTRIBUTION_STATUS_LABELS = {
  correct: "正确",
  agent_error: "Agent 错误",
  step_error: "Step 错误",
  joint_error: "Agent + Step 错误",
  abstain: "Abstain",
  unscorable: "不可评价"
};

function displayValue(value) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

function formatNumber(value) {
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  return Number.isInteger(number) ? String(number) : number.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
}

function metricRatio(metric) {
  if (!metric || metric.denominator === 0 || metric.numerator === null) return "—";
  return `${formatNumber(metric.numerator)}/${metric.denominator}`;
}

function scoreChip(value, trueLabel = "命中", falseLabel = "未命中") {
  if (value === null || value === undefined) return element("span", "result-chip is-na", "不可评价");
  return element("span", value === 1 ? "result-chip is-pass" : "result-chip is-fail", value === 1 ? trueLabel : falseLabel);
}

function safeLocalPhoenixUrl(value) {
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol)) return null;
    if (!["127.0.0.1", "localhost", "[::1]", "::1"].includes(url.hostname)) return null;
    return url.href;
  } catch { return null; }
}

function setAttributionShell(state, message = "") {
  byId("attribution-loading").hidden = state !== "loading";
  byId("attribution-error").hidden = state !== "error";
  byId("attribution-content").hidden = state !== "content";
  if (state === "error") byId("attribution-error-message").textContent = message || "请确认 Phoenix 和归因 Experiment 可用。";
}

async function loadAttribution({force = false} = {}) {
  if (ui.attribution.loaded && !force) return;
  ui.attribution.summaryController?.abort();
  const controller = new AbortController();
  ui.attribution.summaryController = controller;
  const token = ++ui.attribution.summaryToken;
  ui.attribution.loaded = false;
  setAttributionShell("loading");
  try {
    const summary = await api("/api/attribution/summary", {signal: controller.signal});
    if (token !== ui.attribution.summaryToken) return;
    ui.attribution.summary = summary;
    ui.attribution.loaded = true;
    renderAttributionSummary();
    setAttributionShell("content");
    await loadAttributionCases();
  } catch (error) {
    if (error.name === "AbortError" || token !== ui.attribution.summaryToken) return;
    setAttributionShell("error", `无法读取错误归因概览：${error.message}`);
  }
}

function renderAttributionSummary() {
  const summary = ui.attribution.summary;
  if (!summary) return;
  byId("attribution-dataset").textContent = `${summary.dataset_name} · ${shortId(summary.dataset_id, 18)}`;
  byId("attribution-experiment").textContent = `${summary.experiment_name} · ${shortId(summary.experiment_id, 18)}`;
  byId("attribution-counts").textContent = `${summary.example_count} 个样本 · ${summary.methods.length} 种方法`;
  byId("attribution-phoenix-status").textContent = summary.phoenix_available ? "可用 · 只读" : "不可用";
  byId("attribution-disclaimer").textContent = summary.disclaimer;
  const phoenix = safeLocalPhoenixUrl(summary.phoenix_url);
  const link = byId("attribution-phoenix-link");
  if (phoenix) {
    link.href = phoenix;
    link.hidden = false;
  } else {
    link.removeAttribute("href");
    link.hidden = true;
  }
  byId("copy-experiment-id").disabled = !summary.experiment_id;
  const body = byId("attribution-metrics-body");
  body.replaceChildren();
  summary.method_metrics.forEach(item => {
    const row = element("tr");
    const distance = item.mean_step_distance.denominator
      ? `${formatNumber(item.mean_step_distance.value)} (n=${item.mean_step_distance.denominator})`
      : "—";
    const denominators = `A ${item.agent_exact_match.denominator} · S ${item.step_exact_match.denominator} · J ${item.joint_exact_match.denominator} · D ${item.mean_step_distance.denominator}`;
    [
      item.method,
      metricRatio(item.agent_exact_match),
      metricRatio(item.step_exact_match),
      metricRatio(item.joint_exact_match),
      distance,
      metricRatio(item.prediction_coverage),
      denominators
    ].forEach(value => row.append(element("td", "", value)));
    body.append(row);
  });
}

function attributionFilterQuery() {
  const params = new URLSearchParams();
  const filters = [
    ["source", byId("attribution-source-filter").value],
    ["method", byId("attribution-method-filter").value],
    ["result_status", byId("attribution-status-filter").value],
    ["trace_truncated", byId("attribution-truncated-filter").value]
  ];
  filters.forEach(([name, value]) => { if (value) params.set(name, value); });
  return params.toString();
}

function setCasesState(state, message = "") {
  byId("attribution-cases-loading").hidden = state !== "loading";
  byId("attribution-cases-error").hidden = state !== "error";
  byId("attribution-cases-empty").hidden = state !== "empty";
  byId("attribution-cases-table").hidden = !["content"].includes(state);
  if (state === "error") byId("attribution-cases-error").textContent = message;
}

async function loadAttributionCases() {
  ui.attribution.casesController?.abort();
  ui.attribution.detailController?.abort();
  const controller = new AbortController();
  ui.attribution.casesController = controller;
  const token = ++ui.attribution.casesToken;
  ui.attribution.selectedKey = null;
  clearAttributionDetail();
  setCasesState("loading");
  const query = attributionFilterQuery();
  try {
    const result = await api(`/api/attribution/cases${query ? `?${query}` : ""}`, {signal: controller.signal});
    if (token !== ui.attribution.casesToken) return;
    ui.attribution.cases = result.items || [];
    renderAttributionCases();
    setCasesState(ui.attribution.cases.length ? "content" : "empty");
  } catch (error) {
    if (error.name === "AbortError" || token !== ui.attribution.casesToken) return;
    ui.attribution.cases = [];
    byId("attribution-case-count").textContent = "加载失败";
    setCasesState("error", `样本加载失败：${error.message}`);
  }
}

function renderAttributionCases() {
  const body = byId("attribution-cases-body");
  body.replaceChildren();
  byId("attribution-case-count").textContent = `${ui.attribution.cases.length} 条`;
  ui.attribution.cases.forEach(item => {
    const key = `${item.dataset_case_id}|${item.method}`;
    const row = element("tr", "case-row");
    row.dataset.caseKey = key;
    row.classList.toggle("is-selected", ui.attribution.selectedKey === key);
    const caseCell = element("td");
    const button = element("button", "case-select", shortId(item.dataset_case_id, 24));
    button.type = "button";
    button.title = item.dataset_case_id;
    button.setAttribute("aria-label", `查看 ${item.dataset_case_id} 的 ${item.method} 详情`);
    button.addEventListener("click", () => selectAttributionCase(item));
    caseCell.append(button);
    const source = element("td", "", `${item.source}\n${item.split}`);
    source.style.whiteSpace = "pre-wrap";
    const predicted = element("td", "", `${displayValue(item.predicted_agent)} / ${displayValue(item.predicted_step)}`);
    const truth = element("td", "", `${displayValue(item.responsible_agent)} / ${displayValue(item.decisive_step)}`);
    const coverage = scoreChip(item.prediction_coverage, "已预测", "Abstain");
    const coverageCell = element("td"); coverageCell.append(coverage);
    const traceCell = element("td");
    traceCell.append(element("span", item.trace_truncated ? "result-chip is-warning" : "result-chip is-na", item.trace_truncated ? "已截断" : "完整保留"));
    const agentCell = element("td"); agentCell.append(scoreChip(item.agent_exact_match));
    const stepCell = element("td"); stepCell.append(scoreChip(item.step_exact_match));
    row.append(caseCell, source, element("td", "", item.method), predicted, truth, agentCell, stepCell, coverageCell, traceCell);
    body.append(row);
  });
}

function clearAttributionDetail() {
  byId("attribution-detail-empty").hidden = false;
  byId("attribution-detail-loading").hidden = true;
  byId("attribution-detail-error").hidden = true;
  byId("attribution-detail-content").hidden = true;
}

async function selectAttributionCase(item) {
  ui.attribution.detailController?.abort();
  const controller = new AbortController();
  ui.attribution.detailController = controller;
  const token = ++ui.attribution.detailToken;
  ui.attribution.selectedKey = `${item.dataset_case_id}|${item.method}`;
  renderAttributionCases();
  byId("attribution-detail-empty").hidden = true;
  byId("attribution-detail-loading").hidden = false;
  byId("attribution-detail-error").hidden = true;
  byId("attribution-detail-content").hidden = true;
  try {
    const detail = await api(`/api/attribution/cases/${encodeURIComponent(item.dataset_case_id)}?method=${encodeURIComponent(item.method)}`, {signal: controller.signal});
    if (token !== ui.attribution.detailToken) return;
    renderAttributionDetail(detail);
    byId("attribution-detail-loading").hidden = true;
    byId("attribution-detail-content").hidden = false;
  } catch (error) {
    if (error.name === "AbortError" || token !== ui.attribution.detailToken) return;
    byId("attribution-detail-loading").hidden = true;
    byId("attribution-detail-error").hidden = false;
    byId("attribution-detail-error").textContent = `详情加载失败：${error.message}`;
  }
}

function appendDefinition(root, label, value) {
  root.append(element("dt", "", label), element("dd", "", displayValue(value)));
}

function renderAttributionDetail(detail) {
  byId("attribution-detail-title").textContent = `${shortId(detail.dataset_case_id, 32)} · ${detail.method}`;
  byId("attribution-detail-provenance").textContent = `${detail.provenance.trace_origin} · 本项目未执行`;
  byId("attribution-task-summary").textContent = detail.task_summary;
  const truncation = byId("attribution-truncation-note");
  truncation.hidden = !detail.trace_truncated;
  truncation.textContent = detail.trace_truncated
    ? `轨迹已截断：原始 ${detail.trace_original_step_count} 步，保留 ${detail.trace_retained_step_count} 步；真实决定步骤${detail.decisive_step_present_in_trace ? "仍在保留范围内" : "未被保留，因此 Step 不可评价"}。`
    : "";
  const prediction = byId("prediction-fields"); prediction.replaceChildren();
  appendDefinition(prediction, "方法", detail.method);
  appendDefinition(prediction, "预测 Agent", detail.predicted_agent);
  appendDefinition(prediction, "预测 Step", detail.predicted_step);
  appendDefinition(prediction, "状态", detail.prediction_status);
  appendDefinition(prediction, "Confidence", formatNumber(detail.confidence));
  appendDefinition(prediction, "Reason", detail.prediction_reason);
  appendDefinition(prediction, "Evidence", detail.evidence_step_ids.length ? detail.evidence_step_ids.join(", ") : "无");
  const truth = byId("truth-fields"); truth.replaceChildren();
  appendDefinition(truth, "责任 Agent", detail.responsible_agent);
  appendDefinition(truth, "决定 Step", detail.decisive_step);
  appendDefinition(truth, "失败原因", detail.failure_reason);
  appendDefinition(truth, "Agent 可评价", detail.agent_scorable ? "是" : "否");
  appendDefinition(truth, "Step 可评价", detail.step_scorable ? "是" : "否");
  const evaluation = byId("evaluation-fields"); evaluation.replaceChildren();
  appendDefinition(evaluation, "结果状态", ATTRIBUTION_STATUS_LABELS[detail.result_status] || detail.result_status);
  appendDefinition(evaluation, "Agent exact", detail.agent_exact_match);
  appendDefinition(evaluation, "Step exact", detail.step_exact_match);
  appendDefinition(evaluation, "Joint exact", detail.joint_exact_match);
  appendDefinition(evaluation, "Step distance", detail.step_distance);
  appendDefinition(evaluation, "Coverage", detail.prediction_coverage);
  appendDefinition(evaluation, "不可评价原因", detail.unscorable_reason);
  const timeline = byId("attribution-timeline"); timeline.replaceChildren();
  const evidenceSteps = new Set(detail.evidence_step_ids.map(String));
  detail.steps.forEach(step => {
    const predicted = detail.predicted_step !== null && String(step.step_number) === String(detail.predicted_step);
    const truthStep = detail.decisive_step !== null && String(step.step_number) === String(detail.decisive_step);
    const match = predicted && truthStep;
    const item = element("li", "timeline-step");
    if (match) item.classList.add("is-match");
    else {
      if (predicted) item.classList.add("is-predicted");
      if (truthStep) item.classList.add("is-truth");
    }
    if (step.explicit_error) item.classList.add("is-error");
    const head = element("div", "timeline-step-head");
    head.append(element("strong", "", `Step ${step.step_number}`), element("span", "", displayValue(step.agent_name)), element("span", "", step.message_type));
    item.append(head, element("p", "timeline-step-summary", step.content_summary));
    const tags = element("div", "timeline-tags");
    if (match) tags.append(element("span", "timeline-tag is-match", "预测与真实一致"));
    else {
      if (predicted) tags.append(element("span", "timeline-tag is-predicted", "预测步骤"));
      if (truthStep) tags.append(element("span", "timeline-tag is-truth", "真实步骤"));
    }
    if (step.explicit_error) tags.append(element("span", "timeline-tag is-error", "明确错误事件"));
    if (evidenceSteps.has(String(step.step_number))) tags.append(element("span", "timeline-tag is-evidence", "Evidence"));
    if (tags.childNodes.length) item.append(tags);
    timeline.append(item);
  });
}

function resetAttributionFilters() {
  ["attribution-source-filter", "attribution-method-filter", "attribution-status-filter", "attribution-truncated-filter"].forEach(id => { byId(id).value = ""; });
  loadAttributionCases();
}

function updateControls() {
  if (!ui.current) return;
  const status = ui.current.status;
  const checkpoint = ui.selectedCheckpoint;
  const canTravel = Boolean(checkpoint?.next?.length);
  byId("pause-run").disabled = status !== "running";
  byId("resume-run").disabled = !ui.capabilities.resume_statuses.includes(status) || anyActiveRun();
  byId("replay-run").disabled = !canTravel || anyActiveRun();
  byId("fork-run").disabled = !canTravel;
  if (!checkpoint) byId("action-help").textContent = "请先选择一个非终结 checkpoint，才能回放或分叉。";
  else if (!checkpoint.next?.length) byId("action-help").textContent = "已选择终结 checkpoint：后端不允许 replay/fork，请选择更早的 checkpoint。";
  else byId("action-help").textContent = `已选择 ${shortId(checkpoint.checkpoint_id, 18)}；回放会立即重新执行，分叉创建后保持暂停。`;
}

async function startRun(event) {
  event.preventDefault();
  if (ui.submitting) return;
  const task = byId("task-input").value.trim();
  if (!task) {
    byId("task-validation").textContent = "请输入任务描述。";
    byId("task-input").focus();
    return;
  }
  if (anyActiveRun()) {
    byId("task-validation").textContent = "当前已有活跃任务；请等待其暂停或结束。";
    return;
  }
  if (!window.confirm("开始运行可能调用模型并产生费用。确定继续吗？")) return;
  byId("task-validation").textContent = "";
  setSubmitting(true);
  try {
    const allowedFiles = byId("files-input").value.split(",").map(value => value.trim()).filter(Boolean);
    const result = await api("/api/runs", {method: "POST", body: JSON.stringify({task, allowed_files: allowedFiles})});
    await refreshRuns({quiet: true});
    await selectRun(result.run_id);
    showToast("任务已创建，正在执行。");
  } catch (error) {
    byId("task-validation").textContent = `创建失败：${error.message}`;
  } finally {
    setSubmitting(false);
  }
}

async function requestPause() {
  if (!ui.currentRunId || byId("pause-run").disabled) return;
  if (!window.confirm("暂停将在下一个安全、已持久化的节点边界生效。确定请求暂停吗？")) return;
  try {
    await api(`/api/runs/${encodeURIComponent(ui.currentRunId)}/pause`, {method: "POST", body: "{}"});
    showToast("正在暂停：将在下一个安全边界生效。");
    await refreshSelectedDetail();
  } catch (error) { showToast(`暂停失败：${error.message}`, 5000); }
}

async function requestResume() {
  if (!ui.currentRunId || byId("resume-run").disabled) return;
  if (!window.confirm("恢复可能重新调用模型或重新访问网页，并产生费用。确定继续吗？")) return;
  try {
    await api(`/api/runs/${encodeURIComponent(ui.currentRunId)}/resume`, {method: "POST", body: "{}"});
    showToast("恢复已请求。");
    if (ui.current) ui.current.status = "queued";
    renderAll();
    connectEvents();
    scheduleDetailRefresh();
  } catch (error) { showToast(`恢复失败：${error.message}`, 5000); }
}

async function requestReplay() {
  const checkpoint = ui.selectedCheckpoint;
  if (!ui.currentRunId || !checkpoint?.next?.length || byId("replay-run").disabled) return;
  if (!window.confirm("回放会从所选历史 checkpoint 重新执行后续节点，可能再次调用模型并产生费用。确定继续吗？")) return;
  try {
    const result = await api(`/api/runs/${encodeURIComponent(ui.currentRunId)}/replay`, {
      method: "POST", body: JSON.stringify({checkpoint_id: checkpoint.checkpoint_id})
    });
    await refreshRuns({quiet: true});
    await selectRun(result.run_id);
    showToast("已创建回放运行。");
  } catch (error) { showToast(`回放失败：${error.message}`, 5000); }
}

function openForkDialog() {
  if (!ui.selectedCheckpoint?.next?.length || byId("fork-run").disabled) return;
  byId("fork-instruction").value = "";
  byId("fork-plan").value = "";
  byId("fork-dialog").showModal();
  byId("fork-instruction").focus();
}

async function confirmFork() {
  const checkpoint = ui.selectedCheckpoint;
  if (!checkpoint?.next?.length || !ui.currentRunId) return;
  const instruction = byId("fork-instruction").value.trim();
  const plan = byId("fork-plan").value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
  const changes = {};
  if (instruction) changes.worker_instruction = instruction;
  if (plan.length) changes.plan = plan;
  if (!window.confirm("创建分叉会保留旧分支；之后恢复新分支可能产生模型费用。确定创建吗？")) return;
  byId("confirm-fork").disabled = true;
  try {
    const result = await api(`/api/runs/${encodeURIComponent(ui.currentRunId)}/fork`, {
      method: "POST", body: JSON.stringify({checkpoint_id: checkpoint.checkpoint_id, changes})
    });
    byId("fork-dialog").close();
    await refreshRuns({quiet: true});
    await selectRun(result.run_id);
    showToast("分叉已创建并保持暂停，来源关系已保存。");
  } catch (error) { showToast(`分叉失败：${error.message}`, 5000); }
  finally { byId("confirm-fork").disabled = false; }
}

function activateTab(tab) {
  const tablist = tab.closest('[role="tablist"]');
  if (!tablist) return;
  tablist.querySelectorAll('[role="tab"]').forEach(button => {
    const active = button === tab;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
    byId(button.getAttribute("aria-controls")).hidden = !active;
  });
  if (tab.id === "tab-attribution-main") loadAttribution();
}

function setupTabs() {
  document.querySelectorAll('[role="tablist"]').forEach(tablist => {
    const tabs = [...tablist.querySelectorAll('[role="tab"]')];
    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => activateTab(tab));
      tab.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        let next = index;
        if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
        if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = tabs.length - 1;
        activateTab(tabs[next]);
        tabs[next].focus();
      });
    });
  });
}

function setupDialogs() {
  document.querySelectorAll("[data-close-dialog]").forEach(button => button.addEventListener("click", () => byId(button.dataset.closeDialog).close()));
  document.querySelectorAll("dialog").forEach(dialog => dialog.addEventListener("click", event => {
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
  }));
}

function bindActions() {
  byId("task-form").addEventListener("submit", startRun);
  byId("new-task").addEventListener("click", () => { byId("composer").scrollIntoView({behavior: "smooth"}); byId("task-input").focus(); });
  byId("refresh-runs").addEventListener("click", () => refreshRuns());
  byId("pause-run").addEventListener("click", requestPause);
  byId("resume-run").addEventListener("click", requestResume);
  byId("replay-run").addEventListener("click", requestReplay);
  byId("fork-run").addEventListener("click", openForkDialog);
  byId("confirm-fork").addEventListener("click", confirmFork);
  byId("retry-stream").addEventListener("click", connectEvents);
  byId("copy-trace").addEventListener("click", () => copyText(ui.current?.trace_id, "Trace ID"));
  byId("copy-top-trace").addEventListener("click", () => copyText(ui.current?.trace_id, "Trace ID"));
  byId("copy-output").addEventListener("click", () => copyText(byId("output").textContent, "输出"));
  byId("copy-state").addEventListener("click", () => copyText(byId("state-json").textContent, "State JSON"));
  byId("copy-checkpoint").addEventListener("click", () => copyText(ui.selectedCheckpoint?.checkpoint_id, "Checkpoint ID"));
  byId("retry-attribution").addEventListener("click", () => loadAttribution({force: true}));
  byId("copy-experiment-id").addEventListener("click", () => copyText(ui.attribution.summary?.experiment_id, "Experiment ID"));
  ["attribution-source-filter", "attribution-method-filter", "attribution-status-filter", "attribution-truncated-filter"].forEach(id => byId(id).addEventListener("change", loadAttributionCases));
  byId("reset-attribution-filters").addEventListener("click", resetAttributionFilters);
  window.addEventListener("beforeunload", closeEventSource);
  setupTabs();
  setupDialogs();
}

async function initialize() {
  bindActions();
  try {
    const session = await api("/api/session");
    ui.csrf = session.csrf_token;
    ui.capabilities = {...ui.capabilities, ...(session.capabilities || {})};
    byId("phoenix-link").href = session.phoenix_url;
    if (session.tracing_warning) byId("phoenix-health").title = session.tracing_warning;
    await Promise.all([loadHealth(), refreshRuns()]);
    window.setInterval(loadHealth, 30000);
  } catch (error) {
    setHealthChip("app-health", false, "应用正常", "初始化失败");
    byId("runs-status").textContent = `初始化失败：${error.message}`;
  }
}

window.m1Console = {
  selectRun,
  reconnectEvents: connectEvents,
  snapshot: () => ({runId: ui.currentRunId, lastEventId: ui.lastEventId, eventCount: ui.events.length, sourceOpen: Boolean(ui.source)}),
  attributionSnapshot: () => ({
    loaded: ui.attribution.loaded,
    caseCount: ui.attribution.cases.length,
    selectedKey: ui.attribution.selectedKey,
    experimentId: ui.attribution.summary?.experiment_id || null
  }),
  reloadAttribution: () => loadAttribution({force: true})
};

initialize();
