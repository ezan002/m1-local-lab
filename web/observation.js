"use strict";
const el = id => document.getElementById(id);
const observation = {csrf: "", preview: null, selected: null, traceId: null, traceReady: false, offset: 0, generation: 0, projectUrl: null};
const statusLabel = {verified: "已逐项读回核对", partial: "部分接收，需再次核对", uncertain: "写入回执不确定，禁止盲目重发", sending: "写入已发起，需核对"};
const kindLabel = {message: "历史消息（非 LLM 调用）", model_call: "模型调用（有请求/响应记录）", agent_activity: "Agent 活动（未证实为模型调用）"};
async function request(path, body) {
  const response = await fetch("/api/observation" + path, body ? {method:"POST", headers:{"Content-Type":"application/json", "x-m1-csrf":observation.csrf}, body:JSON.stringify(body)} : {});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "请求校验失败，请重新选择样本或刷新页面。");
  return result;
}
function fail(error) { el("error").textContent = error.message; el("error").hidden = false; }
async function action(fn) { el("error").hidden = true; try { await fn(); } catch(error) { fail(error); } }
function text(parent, tag, content) { const item = document.createElement(tag); item.textContent = content; parent.append(item); return item; }
function expanded(parent, title, value) {
  const detail = document.createElement("details"); text(detail, "summary", title + "（按需展开，内容未截断）");
  text(detail, "pre", value == null ? "原记录未提供" : typeof value === "string" ? value : JSON.stringify(value, null, 2)); parent.append(detail);
}
function traceLink() {
  el("trace-link").hidden = !(observation.projectUrl && observation.traceId && observation.traceReady);
  if (!el("trace-link").hidden) el("trace-link").href = observation.projectUrl + "/" + observation.traceId;
}
async function refresh() {
  const health = await request("/health");
  observation.projectUrl = health.project_url;
  el("health").textContent = `Phoenix：${health.phoenix === "healthy" ? "连接正常" : "不可用"} · ${health.endpoint}。${health.message || "接收状态以每条记录的读回核对为准。"}`;
  el("project").hidden = !health.project_url;
  if (health.project_url) el("project").href = health.project_url;
  traceLink();
  const items = await request("/imports"); el("imports").replaceChildren();
  el("list-note").textContent = items.length ? `${items.length} 条本地导入记录` : "尚无导入。先选择样本并预览。";
  for (const item of items) {
    const preview = item.trace.task.slice(0, 100) + (item.trace.task.length > 100 ? "…（详情可看全文）" : "");
    const button = text(el("imports"), "button", `${item.trace.source} · ${item.trace.case_id}\n任务预览：${preview}\n${statusLabel[item.status] || item.status}\n保留 ${item.trace.retained_steps}/${item.trace.original_steps} 步 · 捕获完整性未保证\n导入 ${item.imported_at}`);
    button.className = "obs-item"; button.setAttribute("aria-pressed", String(item.trace.import_id === observation.selected));
    button.dataset.identity = item.trace.import_id;
    button.addEventListener("click", () => action(() => select(item.trace.import_id)));
  }
}
async function select(identity) {
  const generation = ++observation.generation;
  const item = await request("/imports/" + identity);
  if (generation !== observation.generation) return;
  observation.selected = identity; observation.offset = 0; observation.traceId = item.trace.trace_id;
  for (const button of el("imports").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.identity === identity));
  }
  observation.traceReady = (item.present || []).includes(item.trace.root_span_id); traceLink();
  localStorage.setItem("m1-observation-selection", identity);
  const t = item.trace;
  el("empty").hidden = true; el("trace").hidden = false; el("step-detail").hidden = true;
  el("metadata-content").replaceChildren(); el("steps").replaceChildren();
  el("trace-title").textContent = `${t.source} · ${t.case_id}`; el("trace-task").textContent = t.task;
  el("trace-status").textContent = `${statusLabel[item.status] || item.status} · 已确认 ${item.present?.length || 0}/${t.expected_spans} spans。${item.message || ""}`;
  el("trace-time").textContent = `导入时间 ${item.imported_at}（不是原执行时间）；真实耗时、费用：原记录未提供。`;
  el("trace-counts").textContent = `原始 ${t.original_steps} 步 · 保留 ${t.retained_steps} · 排除 ${t.excluded_steps} · 截断：无。捕获完整性未保证，文件内保留完整。`;
  el("trace-identity").textContent = `Project：${t.project} · Trace ID：${t.trace_id}`;
  el("provenance").textContent = JSON.stringify({source:t.source, version:t.source_version, version_source:t.version_source, files:t.files, adapter:t.adapter_version,
    completeness:t.completeness, exclusions:t.exclusion_reasons, issues:t.issues, trace_origin:t.trace_origin, executed_by_m1_local:t.executed_by_m1_local, external_task_executed:t.external_task_executed}, null, 2);
  await loadSteps(generation);
}
async function loadSteps(generation = observation.generation) {
  const identity = observation.selected;
  const result = await request(`/imports/${identity}/steps?offset=${observation.offset}&limit=50`);
  if (generation !== observation.generation) return;
  for (const r of result.items) {
    const button = text(el("steps"), "button", `${r.original_step_id} · ${r.agent_name || (r.role ? "原记录角色：" + r.role : "执行者未提供")} · ${kindLabel[r.kind]} · ${r.source_pointer}`);
    button.className = "obs-item";
    button.addEventListener("click", () => action(() => showStep(identity, r.index, generation)));
  }
  observation.offset += result.items.length; el("more").hidden = observation.offset >= result.total;
}
async function showStep(identity, index, generation) {
  const r = await request(`/imports/${identity}/steps/${index}`);
  if (generation !== observation.generation) return;
  el("step-detail").hidden = false;
  el("step-title").textContent = `步骤 ${r.original_step_id} · ${r.agent_name || (r.role ? "原记录角色：" + r.role : "执行者未提供")}`;
  el("step-source").textContent = `原始记录：data/external/${r.source_file} #${r.source_pointer} · Span ${r.span_id} · ${r.numbering}`;
  el("step-note").textContent = `${kindLabel[r.kind]}。${r.redacted ? "已脱敏" : "未命中当前脱敏规则"}；预览仅折叠，未截断。原始时间：${r.original_time || "原记录未提供"}；${r.time_precision}。调用父子关系、真实耗时及费用未提供。`;
  const root = el("step-content"); root.replaceChildren();
  expanded(root, "收到什么：原始请求 " + (r.request_pointer || ""), r.request);
  expanded(root, "返回什么：原始响应 " + (r.response_pointer || ""), r.response);
  expanded(root, "历史消息（不是重建 prompt） " + (r.message_pointer || ""), r.historical_message);
  expanded(root, "工具调用声明（不证明工具已执行）", r.tool_declarations);
  expanded(root, "脱敏后的完整原始记录", r.raw_record_redacted);
  el("step-detail").scrollIntoView({block:"start"});
}
el("sample").addEventListener("change", () => { observation.preview = null; el("preview-result").replaceChildren(); el("import").disabled = true; });
el("preview").addEventListener("click", () => action(async () => {
  observation.preview = null; el("import").disabled = true;
  const sample = el("sample").value;
  const p = await request("/preview/" + encodeURIComponent(sample));
  if (sample !== el("sample").value) return;
  observation.preview = p; el("preview-result").replaceChildren();
  text(el("preview-result"), "p", `预计 ${p.expected_spans} spans（含1个导入分组）。原始 ${p.original_steps} 步，保留 ${p.retained_steps}，排除 ${p.excluded_steps}。无截断；真实捕获完整性未知。`);
  expanded(el("preview-result"), "预览问题、来源与缺失字段", p); el("import").disabled = false;
}));
el("import").addEventListener("click", () => action(async () => {
  const p = observation.preview; if (!p) return;
  el("import").disabled = true; el("progress").textContent = "正在导入并逐项读回核对，请勿重复点击…";
  try { const r = await request("/import", {sample:p.sample, import_id:p.import_id}); await refresh(); await select(r.trace.import_id); }
  finally { el("progress").textContent = ""; el("import").disabled = !observation.preview; }
}));
el("refresh").addEventListener("click", () => action(refresh));
el("more").addEventListener("click", () => action(() => loadSteps()));
el("copy-trace").addEventListener("click", () => action(async () => { await navigator.clipboard.writeText(observation.traceId); el("progress").textContent = "Trace ID 已复制"; }));
el("metadata").addEventListener("click", () => action(async () => {
  const id = observation.selected; const meta = await request(`/imports/${id}/metadata`);
  if (id !== observation.selected) return;
  el("metadata-content").replaceChildren(); expanded(el("metadata-content"), "来源元数据（标签不参与 Span 分类和异常判断）", meta);
}));
action(async () => {
  const session = await request("/session"); observation.csrf = session.csrf_token;
  el("mode").textContent = session.execution_enabled ? "执行模式服务 · 当前页面仅观测历史；本地任务在次级入口" : "本地观测模式 · 无需 API Key · 后端已禁止执行";
  el("execution").hidden = !session.execution_enabled;
  const b = session.build || {};
  const dirty = b.uncommitted_changes === true ? "有未提交改动" : b.uncommitted_changes === false ? "无未提交改动" : "改动状态未知";
  el("build").textContent = `运行版本（进程启动时记录）：commit ${b.commit && b.commit !== "unknown" ? b.commit.slice(0, 12) : "未知"} · ${dirty} · 启动 ${b.started_at || "未知"} · 模式 ${b.mode || "未知"}`;
  el("build").title = b.commit || "未知";
  for (const s of await request("/samples")) { const option = text(el("sample"), "option", `${s.source} · ${s.label} · ${s.available ? "本机已有" : "源文件缺失或越界"}`); option.value = s.sample; option.disabled = !s.available; }
  await refresh(); const selected = localStorage.getItem("m1-observation-selection"); if (selected) await select(selected);
});
