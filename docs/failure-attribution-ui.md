# 错误归因网页与只读 API

记录日期：2026-09-10（America/New_York）。本页面只查询并显示已有离线归因结果，不改进归因算法，不执行外部任务，也不调用模型。

## 数据流与边界

```text
Phoenix Dataset + Experiment/Evaluations（公共 Python API）
  → AttributionReadModel（只读、脱敏、规范化、60 秒进程内缓存）
  → FastAPI Pydantic response models
  → 原生 HTML/CSS/JavaScript
```

读取层调用 `Client.datasets.list/get_dataset` 和 `Client.experiments.list/get_experiment/get_experiment_url`。它不查询 Phoenix 内部 SQLite，不解析 Phoenix 网页，也不把 Phoenix 客户端对象返回给浏览器。默认从 Experiment metadata 的 `experiment_name=m1-local-lab-failure-attribution-mvp` 选择最新实例；为兼容旧实例，可匹配已有 methods/provenance 元数据签名。`ATTRIBUTION_EXPERIMENT_ID` 可显式覆盖，但代码没有固定任何 opaque ID。

隔离顺序保持不变：label-free inference view 只进入 predictor；ground truth 只在 prediction 完成后进入 evaluator；read model 最后合并 prediction 与 evaluation 供 UI 显示。UI 的真实标签栏属于评价后读取层，不会反向传给 predictor。外部轨迹继续标记 `trace_origin=imported_historical`、`executed_by_m1_local=false`、`external_task_executed=false`。

## API

- `GET /api/attribution/summary`：Dataset/Experiment 名称和 ID、3/9/72 计数、source/method、每种方法五项指标的 value/numerator/denominator、更新时间、Phoenix 本地链接和 provenance。
- `GET /api/attribution/cases`：支持 `source`、`method`、`result_status`、`agent_correct`、`step_correct`、`trace_truncated` 和 `limit=1..100`；返回预测、真实标签、评价、截断、可评价和 provenance 摘要。
- `GET /api/attribution/cases/{dataset_case_id}?method=...`：返回脱敏任务摘要、prediction/ground truth/evaluation、规范化步骤、安全摘要、证据、截断和 Experiment/Trace 标识。

三个端点都是 GET。Dataset、Experiment 或 case 不存在返回稳定 404；Phoenix 不可用返回稳定 503，不包含上游 exception 正文。步骤只返回规范化字段；结构化历史 payload、完整 prompt、HTML 和网页正文会省略，普通文本也会脱敏并限长。本地 Phoenix 跳转只接受 `http/https` 和 loopback hostname。

## 页面区域与交互

“错误归因”是现有中文控制台的一级 ARIA tab，不创建第二个 SSE。页面包含：

- 概览：Dataset、Experiment、样本/方法数、Phoenix 只读状态、imported historical 和基线有效性声明；
- 方法指标：`last_active_agent`、`first_explicit_error`、`rule_based` 的 Agent/Step/Joint、Mean step distance、Coverage 和独立有效分母；`None` 显示为“—”；
- 筛选与列表：source、method、result status、truncation 无刷新筛选；快速切换通过 AbortController 和请求 token 防止旧响应覆盖；
- 详情：预测结果、真实标签（仅评价层）、评价结果三个明确分区；
- 时间线：灰色普通、蓝色预测、橙色真实、绿色重合、红色明确错误、紫色 Evidence，并同时显示文字标签；截断轨迹显示原始/保留步数和真实 decisive step 是否仍存在；
- Phoenix 入口：复制 Experiment ID，并仅在公共 API 生成安全本地 URL 时显示跳转按钮。

键盘支持点击、左右方向键、Home 和 End；焦点使用可见 outline。窄屏中的表格在自身容器横向滚动，不造成页面级横向溢出。动态内容全部通过 `textContent`/安全 DOM 节点写入，不使用 `innerHTML`、`outerHTML` 或 `insertAdjacentHTML`。

## 启动与验收

常规启动：

```powershell
& '.\scripts\start-phoenix.ps1'
& '.\scripts\start-app.ps1'
```

零费用浏览器验收：

```powershell
& '.\.venv\Scripts\python.exe' '.\scripts\verify_attribution_ui.py'
```

验收脚本把 `OPENAI_API_KEY` 置空，并以 `RuleBasedGateway` 启动临时应用。若 6006 已有健康 Phoenix 则复用且不停止；若 8000 已被占用则使用 8001。脚本只结束自己启动的 PID，并记录前后端口/PID。截图保存在被 `.gitignore` 排除的 `data/test-output/attribution-ui/`：三档各有 overview/detail 共六张。

2026-09-10 实测：1366×768、1920×1080、1536×864 + `device_scale_factor=1.25` 全部 PASS。第三项仅称为“Playwright 125% 缩放模拟”，不等同于修改或验证 Windows 原生显示缩放。每档选择 3 个唯一 case，验证 20 步 TraceElephant 时间线；Console、page error、request failure 和意外 4xx/5xx 均为 0。1366 档另模拟一次预期 503，并通过“重新加载”恢复。

## 当前数据与限制

当前页面读取 3 条样本、3 种规则基线、9 个 task runs 和 72 个 evaluations。实测汇总仍是：`last_active_agent` Agent 2/3、Step/Joint 0/3、distance 36/3、coverage 3/3；`first_explicit_error` Agent 1/3、Step/Joint 0/3、distance 1/1、coverage 1/3；`rule_based` Agent 1/3、Step/Joint 0/3、distance 27/3、coverage 3/3。这只证明管线和显示可工作，不证明方法有效。

真实三样本包含 2 个 abstain；TraceElephant 为 original=31、retained=20、truncated=true，真实 step 5 仍被保留，因此此次仍可评价 step。当前实际样本没有 unscorable case；unscorable 的 API/UI 语义由离线 fixture 和自动化测试覆盖，页面筛选项也保留，但不能伪造真实样本截图。

尚未实现人工标注、算法改进、学习型/因果归因、全量 benchmark 或全量数据下载。下一步应先扩充经许可且脱敏的标注样本，并单独评审隐私、费用和评价协议，再考虑算法比较。
