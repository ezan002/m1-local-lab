# 架构与执行语义

## 错误归因只读展示层

`attribution_read_model.py` 位于离线 predictor/evaluator 和 Web 之间，不进入 `AgentRuntime`、LangGraph State 或 checkpoint 路径：

```text
label-free inference → deterministic predictor ┐
ground truth ───────→ evaluator ───────────────┴→ Phoenix Experiment
                                                    ↓ public client APIs only
                                           AttributionReadModel
                                                    ↓ typed GET JSON
                                              attribution UI
```

读取层仅通过 Phoenix 11.35.0 公共 Dataset/Experiment Python API 查询最新同名 Experiment；不访问 Phoenix SQLite schema、不解析 Phoenix HTML，也不固定 opaque ID。60 秒进程内缓存复用同一历史快照，避免概览、筛选和详情重复拉取 72 条 evaluations；服务重启或缓存过期后重新按名称选择最新实例。可选 `ATTRIBUTION_EXPERIMENT_ID` 只用于显式运维覆盖。

所有 response 都是 frozen/extra-forbid Pydantic model。结构化历史 payload、完整 prompt、网页正文和 HTML 不进入浏览器；步骤只保留 step/agent/event flags 与脱敏限长摘要。prediction、ground truth 和 evaluation 在 response/UI 中是明确不同区域，合并只发生在预测完成后的 read/evaluation 层，因此 predictor 的签名、输入和标签隔离 guard 均未改变。

归因 API 是三个只读 GET，Phoenix/Dataset/Experiment/case 故障映射为稳定脱敏 404/503。前端没有新增 EventSource；筛选和详情请求使用 AbortController 与单调 token 防止过期响应覆盖。完整接口与颜色/文字语义见 [failure-attribution-ui.md](failure-attribution-ui.md)。

## 外部历史数据边界

`external_datasets.py` 是独立于现有 JSONL runner 的只读适配边界。TraceElephant 与 Who&When 分别解析为统一 `ExternalTraceRecord`，但保留各自 source、revision、split、case ID、原 Agent 名称和 ground truth，不进入 LangGraph state，也不改变 LangGraph 的唯一调度器地位。

外部历史轨迹只写入 Phoenix Dataset example，标记 `trace_origin=imported_historical`、`executed_by_m1_local=false`、`external_task_executed=false`，不会转换成应用 OpenTelemetry spans。离线 Experiment 可引用单独的 RuleBasedGateway 本地验证 run/trace；该本地 trace 只证明适配器和管线可工作，不代表重跑了外部 task。完整 schema、版本与验收证据见 [external-dataset-smoke.md](external-dataset-smoke.md)。

### 离线错误归因旁路

`attribution.py` 与 `attribution_evaluation.py` 位于外部数据适配器之后，是完全独立于在线 LangGraph 的离线旁路。它不进入 runtime/state，不增加 Agent 或节点，不改变 Planner、Orchestrator、WebSurfer、FileReader、Coder、Finalizer 与 checkpoint/replay/fork 行为。

```text
ExternalTraceRecord
  ├─ independent inference view → deterministic attribution → prediction
  └─ ground-truth view ─────────────────────────────────────→ evaluator → Phoenix Experiment
```

inference view 在深拷贝后递归拒绝所有标签 key，预测函数只接受该 frozen Pydantic 对象；Ground Truth 只在 prediction 产生后交给 evaluator。TraceElephant 与 Who&When 使用独立 step normalizer。Phoenix task result 仅含 prediction 与 inference 可知的 source/revision/truncation/provenance metadata；label-derived scorable 字段只写入 evaluation metadata，真实标签仅位于 Dataset expected/output 和 evaluation 阶段。外部历史仍不生成 OTel spans。完整规则、分母语义和实测见 [failure-attribution-mvp.md](failure-attribution-mvp.md)。

## 组件关系

```text
浏览器页面 ──HTTP/SSE──> FastAPI（单进程、单活跃任务）
                         │
                         ▼
                  AgentRuntime / LangGraph
                  planner → orchestrator
                     ▲       │
                     │       ├→ pause_gate → official WebSurfer → orchestrator
                     │       ├→ pause_gate → file_reader       → orchestrator
                     │       ├→ pause_gate → coder             → orchestrator
                     └───────┴→ finalizer → END
                         │             │
        AsyncSqliteSaver │             ├─ 产物目录（run/branch 隔离）
        AppStore         │             └─ SSE 事件与 execution/checkpoint 关联表
                         ▼
                OpenTelemetry / Phoenix（独立进程与 SQLite）
```

LangGraph 是唯一调度器。planner 维护事实、未知项和计划；orchestrator 通过结构化模型输出选择一个 worker、判断进展/循环并决定重规划或结束。步数、模型调用数、重规划次数和超时由代码强制。官方 WebSurfer 只作为 worker 调用；项目没有把 `MagenticOneGroupChat` 塞进一个节点，也没有第二套团队调度器。

## State 与 reducer

State 只含可序列化数据：schema 版本、任务、带稳定 ID 的消息、任务账本、计划、进度账本、下一 worker/指令、产物描述、浏览器重建摘要、限额计数、答案、状态、错误，以及 thread/run/branch/execution/来源 ID。消息、进度、产物和错误使用稳定 ID 的幂等 reducer，恢复或分支不会把同一条历史重复追加。

Page、浏览器、模型客户端、锁、数据库连接和 generator 不进入 checkpoint。截图与代码写入 `data/artifacts/<run>/<branch>/`，State 只存相对路径、类型、哈希、大小和 artifact ID。

## WebSurfer 适配边界

每个活跃 run/branch 有独立的官方 `MultimodalWebSurfer`、Playwright context 和 Chromium。连续节点只向同一 agent 传“新增消息”。AutoGen 0.7.5 的该 agent 没有覆盖基类的有效状态持久化，因此不能把默认 `save_state` 当成浏览器恢复。

节点结束保存最后 URL、标题、摘要、证据、引用和截图。应用重启、历史 replay 或 fork 均新建浏览器并重新访问保存 URL，State 明示 `browser_restore_mode=reconstruct`。它不恢复 DOM、JS 内存、登录态、表单、下载或外部副作用；重建失败就是显式失败。

请求路由在代码层阻止非 HTTP(S)、内网/回环、应用管理端点和非白名单导航；只有真实测试 fixture 的精确临时基址可例外。公开页面所需的公开静态资源可以加载，但不能借此导航到任意内网站点。

## Checkpoint、暂停、恢复与时间旅行

- `history`：沿选中 branch head 的 `parent_config` 只读遍历，不调用模型/浏览器。
- `pause`：只设置持久化请求；`pause_gate` 在外部 worker 动作之前消费请求并执行 LangGraph `interrupt`。恢复时该 gate 可能重跑，但 interrupt 前没有外部动作。
- `resume`：若快照确有 interrupt，使用 `Command(resume=True)`；普通失败/中断使用普通空输入继续，二者不混用。
- `replay`：要求非终结 checkpoint，携带完整 `thread_id/checkpoint_ns/checkpoint_id`，从所选历史配置创建新的应用 run/branch/Trace 后执行。不会误用“线程最新状态”。
- `fork`：通过 `aupdate_state` 在所选完整 config 上创建新 head，只允许改 `plan`、`worker_instruction`；旧 head 与旧产物不变。fork 初始为 paused，由用户确认后 resume。

checkpoint 是 super-step 快照，WebSurfer 的回放粒度是一个 agent 节点回合，不是每次点击。外部访问和模型调用可能重做、再次收费，网页变化也可能改变结果。外部动作后、checkpoint 提交前失败会记录 `external_commit_uncertain`，不声称 exactly-once。

## Trace 与 checkpoint 关联

一个 `m1.run` span 包含人工 `m1.node.*`、`m1.model.*` 和 `m1.checkpoint.commit` spans。`m1.run` 使用 OpenInference `AGENT`；Planner、Orchestrator、Finalizer 等控制节点使用 `CHAIN`；`web_surfer`、`file_reader`、`coder` 节点使用 `TOOL`；应用模型摘要 span 使用 `LLM`。OpenInference LangChain instrumentor 仍覆盖编排模型底层调用，OpenAI instrumentor 仍覆盖 AutoGen WebSurfer 的底层调用。由于 ChatOpenAI 也走 OpenAI SDK，编排模型调用期间在单活跃任务约束下临时关闭 OpenAI wrapper，仅保留 LangChain LLM span；离开调用立即以同一脱敏配置恢复，因此 WebSurfer 保留 OpenAI LLM span而不会双计同一次编排请求。

关联处理器把当前执行上下文复制到第三方子 span。Phoenix 可直接筛选 `run_id`（同时保留兼容字段 `application_run_id`）、`thread_id`、`branch_id`、`execution_id`、`langgraph_node`、`agent_name`、`step_number`、`status`、`dataset_case_id`，以及条件字段 `replay_of`、`fork_of`。`session.id` 与 `thread_id` 相同。不存在 dataset/replay/fork 时对应字段不写空值。

第三方自动插桩不导出原始 prompt、网页文字、模型回复、工具定义或 invocation parameters；其 `input.value`/`output.value` 显示为 OpenInference 脱敏值。应用 span 只写结构化摘要，字符串限制长度并按密钥、Bearer token、密码、Cookie 等模式再次脱敏。模型名、token 计量和状态等非敏感遥测仍由插桩保留。

Phoenix 的 tracer provider 自带 OTLP batch processor；增加关联处理器时必须使用 `replace_default_processor=False`，否则 Phoenix 11.x 会移除默认导出器。关闭 runtime 前在根 span 已结束后再次 `force_flush`，确保完整父子树进入 exporter。

节点更新以 `durability="sync"` 流式提交后，runtime 再读取真实 `snapshot.config`，将真实 output checkpoint ID 与 execution ID 写入本地关联表，并创建同一根 trace 下的 `m1.checkpoint.commit` 子 span。该 span 直接包含 `checkpoint_id`、输入/输出 checkpoint、execution、节点和提交状态。节点或模型执行期间 output checkpoint 尚不存在，因此不会预猜 ID，也不会在已结束 span 上回填；从 Phoenix 以 execution ID 连接 node/model 与 commit，或使用本地关联表。SSE 事件先写 SQLite，再由单调 event ID 补读；Phoenix 是已导出的 span 视图，两者用途不同。

错误 span 使用 OpenTelemetry `ERROR` status，并保留安全的异常类型。当前阶段不把错误自动归因为模型、网络、浏览器、策略或 checkpoint 提交故障；尤其外部动作后、checkpoint 前失败仍只标记 `external_commit_uncertain`，详细根因分类属于后续错误归因阶段。

## 故障恢复

应用启动时把遗留 `queued/running/pausing` 标为 `interrupted`，不会自动重跑。用户可检查 head/history 后再恢复。Phoenix 不可达不阻止任务，但启动预检会显示明确警告；本地事件不会被描述为已进入 Phoenix。正常 shutdown 会关闭全部 run/branch 浏览器、flush tracer 并退出受管理的 `AsyncSqliteSaver` 生命周期。

## 网页控制台与 API

控制台仍是 FastAPI 托管的本地 `index.html`、`style.css`、`app.js`，没有 Node 构建链或远程 CDN。左侧运行列表来自 `GET /api/runs`；选中运行后并行读取详情、history 和持久化事件列表，再按最后 event ID 建立 SSE。切换运行会先关闭旧 EventSource；完成、失败、暂停或中断后主动关闭连接，手动重连通过 `last_event_id` 查询参数补读。浏览器原生自动重连仍可携带 `Last-Event-ID`。

为 UI 增加的最小只读接口/字段：

- `GET /api/health`：动态检查应用与 Phoenix 首页，不返回环境变量或凭据。
- `GET /api/runs/{run_id}/events-log`：读取已持久化、带时间和单调 ID 的事件，供历史选择和断线恢复使用。
- 运行详情增加脱敏 run 元数据和本地关联表中的最新 `trace_id`；列表不再暴露内部 `head_config`。
- `/api/session` 声明 resume/active/terminal 状态和 fork 字段白名单，使前端控制与后端约束一致。

任务、模型输出、事件、错误与 JSON 均通过 `textContent`/节点属性渲染，不使用 `innerHTML`。外部引用只接受前端再次校验后的 HTTP(S)，截图与产物仍只走带 run/artifact ID 的安全端点。截图使用原生 `dialog` 预览；标签页支持方向键，所有按钮有可见焦点和禁用状态。
