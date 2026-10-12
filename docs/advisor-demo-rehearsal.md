# 导师演示排练：场景、讲解与现场问题

准备日期：2026-09-20。代码：`a808a04ca60d35e6e2d35ea46441d3ba6e662597`，分支：`feature/full-project-audit-optimization`。

本文件补充而不覆盖 [原演示指南](demo-walkthrough.md)。排练最初回看9月14日的销售CSV任务，随后已切换正常完整UI，并由用户完成新的真实网页任务和一次Replay。汇报以紧接下方的最终讲稿为准；后文保留阶段性证据和排查历史，不应把早期临时限制当成当前产品状态。

## 导师汇报用最终流程（2026-09-20，约8分钟）

开场：

> 老师，目前已经搭建出一个Windows本地研究原型。LangGraph负责多智能体工作流和执行状态，Phoenix负责观测与评价，网页提供任务控制和历史结果查看。今天展示三部分：真实任务如何执行、如何从checkpoint回放、外部历史轨迹和错误归因结果如何展示。

### 1. 真实任务执行与观测（约3分钟）

在独立Chrome打开 `http://127.0.0.1:8000`，选择原运行 `75be4940-dc1a-4fb3-b65a-e9b545e5aaed`。先说明新建任务表单，再依次看最终答案、事件、引用和截图。本次准备已实际提交并完成；汇报时回看记录不能说成正在现场新执行。如需再提交，须另行确认费用和任务范围。

> 用户从网页输入问题，LangGraph调度Planner制定计划，由Orchestrator选择Worker。这个例子通过官方AutoGen WebSurfer访问Playwright文档，最后由Finalizer总结。实际事件显示WebSurfer执行两次，答案说明了Windows上ProactorEventLoop的要求及原因，并提供了文档来源。

实际节点：planner → orchestrator → pause_gate → web_surfer → orchestrator → pause_gate → web_surfer → orchestrator → finalizer。本任务没有FileReader/Coder；文件读取由此前独立CSV演示验证，不能混进这条执行链。

打开本次原运行的 [Phoenix Trace](http://127.0.0.1:6006/redirects/traces/a21ffe4f63e09c8f91d66a3ca3d7a980)，展示根运行、节点和实际LLM调用。已查询54条span，其中7条实际SDK LLM记录，合计14,190 tokens。不要把所有span都当成模型请求。

> 事件记录发生顺序，checkpoint保存图状态和待执行节点，Trace展示执行与模型调用的观测信息。三者通过ID关联，各有用途。

### 2. Checkpoint回放（约2分钟）

在原运行的Checkpoint中选择 `1f1b4e54-31d6-697e-8008-ebaf65db1efe`，说明 `next=finalizer` 表示网页证据已取得、下一步是总结。然后从左侧切到已经生成的回放分支 `f69007ff-3883-4ac5-b800-74b17293d798`，查看父运行/来源checkpoint和事件。不必为了汇报再次点击回放。

> 我们从总结前的快照创建了新分支。原运行保留，新分支只重新执行Finalizer，没有再次访问网页。新结果有独立的运行ID、分支ID和Trace，因此可以区分原结果和重新执行的结果。

回放状态completed，6条事件，实际只执行finalizer；[回放Trace](http://127.0.0.1:6006/redirects/traces/b6da464257fcff2b9923c283d42b1b01)已查询7条span，其中1条实际SDK LLM记录，1,928 tokens。两次执行观察到的SDK token合计16,118，账单费用未核对。State累计gateway计数不能直接当作回放自身调用次数。

本次现场验证的是这个合法checkpoint的Replay。Pause/Resume/Fork有既有实现和测试依据，但没有在本次排练逐项真实执行；浏览器现场和外部副作用不会自动回滚，外部导入历史也不能直接使用本项目checkpoint回放。

### 3. 外部历史轨迹与归因展示（约2分钟）

同一8000页面切换“错误归因”，数据源选TraceElephant，方法选first_explicit_error，其余筛选全部。打开 `traceelephant:assistant_bench_task_7_gpt_4o_wylm9r1ppoet`，展示任务简介、provenance、预测/标签和时间线。

> 这是导师提供的数据来源中的历史执行，不是本项目重新执行了原任务。我们实现了三个确定性规则作为离线归因基线，预测结果与数据集标签分别保存，评价后写入Phoenix。网页当前读取此前计算的结果，不是打开页面时重新运行算法，也没有自动诊断刚才的新任务。

该例预测WebSurfer第6步，数据集标签为WebSurfer第5步，Agent一致但步骤不一致。第6步规范化结果含明确错误信号；第5步为数据集标注位置。当前仅保留原31步中的20步，页面省略了结构化原始payload，只能说明标注对比，不能独立据此证明完整因果链。

当前页面为3个案例×3种方法的9条已保存结果，不是固定30样本基准的完整展示。30×3冻结评价属于另一组既有研究资产；不重跑、不修改，也不宣称Frozen Test结果从未被查看过。

### 4. 收尾：完成范围与下一步（约1分钟）

> 目前已打通本地执行、checkpoint回放和Phoenix观测，并实现了外部历史数据的适配、离线规则归因及结果展示。现阶段主要成果是可运行、可追踪、可回放的研究基础和评价接口，还不能宣称能解决大多数benchmark问题或可靠定位所有根因。
>
> 下一步优先完善历史步骤的原始内容展示，提升演示与部署便利性，再根据您提供的方法接入更深入的归因算法。目前还没有形成新任务执行后自动归因、修改并重新验证的闭环。

边界说明：工作流受Magentic-One启发，调度器为LangGraph，不是官方MagenticOneGroupChat完整部署；Coder只生成代码；Phoenix仍是独立后台进程，完整Trace在6006查看，尚未统一为完整的一体化产品。此前同机隔离Windows部署通过，不等于另一台物理电脑已验收。130 passed、2 skipped为此前回归结果，本次排练没有重跑完整测试。

现场问题处理：使用独立Chrome。此次VS Code内嵌页面未出现回放确认框，Chrome操作成功，但没有修复或确认内嵌页的根因。回放按钮无反应先检查是否已选中非终结checkpoint；页面刷新后需重新选择运行及checkpoint。不要连续点击执行按钮；出现异常先查运行列表和事件，避免重复计费。引用列表可能混入资源链接，讲解时选具体官方文档段落。

本轮没有修改业务源码、冻结资产或原数据库，没有提交Git。仅更新本演示指南，并在被忽略的演示目录保存辅助诊断和只读核验回执。应用与Phoenix服务保留；仅在用户确认后，按进程所有权记录停止本轮启动的服务。

## 当前有效入口：完整原版 UI（用户要求撤销演示禁用）

用户随后明确要求展示正常部署后的完整交互。当前以本节为准；下文多端口、横幅、禁用按钮的记录均为此前排练历史，不再是当前服务行为。

- 唯一项目主入口：`http://127.0.0.1:8000`，顶部“任务运行”“错误归因”均为原有功能；Phoenix 为 `http://127.0.0.1:6006`。8001、6007的旧展示服务已关闭，4317无监听。
- 应用直接使用 `.venv/Scripts/python.exe -B -m m1_local.cli serve`，由原 `m1_local.api:create_app` 构造真实 `OpenAIModelGateway`；Phoenix 使用原 `scripts/serve_phoenix.py`。没有自定义 API 中间件、HTML 注入、模型替身或额外 Worker 拦截。新建、暂停、恢复、回放、分叉按产品原有状态规则工作。
- 原配置模型 gpt-4.1，本机 Key 非空（未输出）。原有30次协调步骤阈值、40次 gateway 调用阈值、2次重规划、600秒任务超时均未修改；这些不是严格费用上限。模型配置、白名单和 `.env` 未修改。未来新建/恢复/回放会按正常逻辑调用真实模型或工具，需要按演示约定确认实际执行范围。
- 为保护原记录，只有服务地址与存储路径通过子进程环境指向 `datasets/.cache/advisor-full-ui-20260920T103022Z-a48b1ff9`。原 `data/app.sqlite`、`data/checkpoints.sqlite`、`data/phoenix/phoenix.db` 经稳定快照和 SQLite backup 形成完整副本，并复制6个产物文件；原数据库及sidecar哈希核对不变。副本是可写的正常运行环境。
- 主页面原始 HTML 与 `web/index.html` 字节一致。真实 Chromium 检查新建表单可用；两条历史可读；选合法非终结checkpoint后 Replay/Fork 可用；已完成运行 Resume 灰显符合产品设计。相同8000页面可读取3案例×3方法的9条归因结果。验证期间没有发送写请求，没有新任务或模型调用，console/page/request/HTTP错误均0。
- 当前历史是原项目研究环境中两条8月28日（本地显示日期）的公开文档浏览任务，均completed；每条14个checkpoint、3个产物。其trace `614ffac08358fc7c5455bbe2b1561f71` 和 `64c6c119373ba5e4b426cd9750fa4765` 在Phoenix分别可查到50、56个span。不是新增运行，也不是销售CSV任务；销售原记录及之前展示副本仍保留。新clone只具有相同界面，不会自动携带这些研究/运行数据。
- 切换前对4个旧服务逐项验证PID、启动时间、解释器和入口后停止其进程树，没有结束未知进程。当前Phoenix启动PID42660、应用启动PID39360；所有权证据在新目录 `processes.json`，停止前须重新核对。健康接口均通过，未提交Git。

正常UI排练顺序：先刷新8000认识两类主标签和新建入口；选择第一条历史查看输出、引用/截图、事件、checkpoint；在非终结checkpoint观察回放/分叉可用（观察不等于执行）；切换错误归因查看来源与时间线。新真实任务提交与实际Replay/Fork作为后续独立阶段，由用户确认后操作，助手不擅自执行。

### 已完成一次用户提交的真实网页任务

用户明确授权一次真实模型和官方WebSurfer网页任务后，亲自在正常UI点击提交。助手只读查询，没有通过API创建第二条任务，没有自动重试，授权不包含后续Replay。

- run_id：`75be4940-dc1a-4fb3-b65a-e9b545e5aaed`；trace_id：`a21ffe4f63e09c8f91d66a3ca3d7a980`。状态completed，当前error为空。
- 问题：查阅 `https://playwright.dev/python/docs/library`，说明Windows上的asyncio事件循环要求及原因并引用来源。是新的现场执行，不是原两条历史记录回看。
- 实际顺序：planner → orchestrator → pause_gate → web_surfer → orchestrator → pause_gate → web_surfer → orchestrator → finalizer。没有FileReader/Coder节点；29条应用事件、11个checkpoint、2个截图产物。
- 最终答案的核心为ProactorEventLoop支持驱动所需的异步子进程，SelectorEventLoop不支持该能力；已独立对照官方文档对应段落核实，不仅凭completed判成功。没有执行生成代码来验证这一技术结论。
- Phoenix已查询到54个span，包括5条ChatOpenAI和2条ChatCompletion实际SDK LLM记录，返回模型为gpt-4.1-2025-04-14。另有5条手工模型包装span，不重复计为调用。普通gateway计数5，不包含2条WebSurfer内部调用。
- 上述7条SDK记录token合计输入13,214、输出976、总计14,190。实际账单费用未核对，不填写金额。
- 引用列表包含具体文档及段落，也混入图片资源和schema.org链接；讲解时使用具体官方文档段落，不把全部自动提取链接当作独立有效来源。引用清洗仍有改进空间，本次未修改业务代码。
- 只读核对回执保存在当前副本目录的 `new-web-task-observed.json` 和 `new-web-task-trace.json`。首次辅助读取脚本误用了事件字段名，随后按API实际 `id/event/data` 结构更正，29条原事件没有被修改。

当前教学进入 checkpoint 回放；截至下面的现场排查，尚未创建本任务的 Replay/Fork 分支，服务保持运行。

### 回放现场问题：VS Code 内嵌页面返回初始界面

用户已授权一次 Replay，并报告点击后回到“选择一个运行查看详情”。用户确认使用 VS Code 页面，未看到费用确认框。本次拟选 checkpoint 为 `1f1b4e54-31d6-697e-8008-ebaf65db1efe`，`next=["finalizer"]`，属于已有网页证据形成后、最终总结前的快照。

- 只读核查：服务访问日志未发现 Replay POST，运行列表仍只有3条已完成原始运行，没有回放子运行。因此本次点击没有可确认的回放执行，也没有因这次回放新增模型调用的证据。
- `web/app.js` 的 `requestReplay()` 使用 `window.confirm()`，确认后才发送 POST。独立 Chromium 定向诊断确认：取消确认不会发送请求；接受确认会尝试发送正确 run/checkpoint 的请求；模拟请求失败只显示错误提示，不跳转页面。诊断浏览器拦截全部写请求，并返回明确的诊断错误；服务实际收到的写请求为0，前后运行列表一致。这不是一次真实 Replay 成功验证，也没有限制用户正在使用的正常服务。
- 独立诊断同时复现了“刷新页面后丢失当前选择”：UI 选择只保存在内存中。服务日志也有页面重新加载记录；但尚无证据确定用户环境中是谁触发了重新加载。VS Code 内嵌页面的确认框兼容性属于待确认原因，不能写成已证实或已修复的根因。
- 下一步：用户在独立 Edge/Chrome 打开同一 `http://127.0.0.1:8000`，重新选择上述原运行和 checkpoint，再执行已授权的一次回放。保留所有功能、原端口和服务，不通过 API 代用户提交，不重复执行原任务。
- 定向诊断脚本和回执为当前忽略目录中的 `diagnose_replay_click.py`、`replay-click-diagnostic.json`。首次沙箱内启动 Chromium 遇到 WinError 5；授权沙箱外运行后上述检查通过，页面脚本错误0。业务源码未修改，完整测试未重跑；真实回放仍待用户操作和结果核验。

### 独立 Chrome 中已完成真实 Replay

用户切换独立 Chrome 后亲自点击并完成已授权的一次回放。助手只读核验，没有代为创建或重复执行。

- 新 run_id：`f69007ff-3883-4ac5-b800-74b17293d798`，状态 completed；父运行 `75be4940-dc1a-4fb3-b65a-e9b545e5aaed`，来源 checkpoint `1f1b4e54-31d6-697e-8008-ebaf65db1efe`。
- 保持同一 thread `d7d2e28d-8137-44c7-9536-3129b2474d5d`，新 branch 为 `6dd7abe0-71e3-4940-9285-cf416037c15c`。原运行仍 completed，保留原 trace、终结 checkpoint 和29条事件。
- 新分支6条事件：replay_created → run_started → node_started(finalizer) → node_finished(finalizer) → checkpoint_saved → run_finished。没有重新执行 Planner、Orchestrator 或任何 Worker；最终总结再次给出 ProactorEventLoop 和异步子进程支持的解释。此次证实从指定快照继续后续总结，不代表所有节点、浏览器现场或外部副作用都能完整回滚。
- 新终结 checkpoint：`1f1b4e82-7f5f-6b99-800a-fdfdf5b62ea4`，next 为空。事件中的输入 checkpoint 是创建分支后生成的新快照；来源 checkpoint 保存在血缘字段中，两者不应混为一谈。
- 新 trace：`b6da464257fcff2b9923c283d42b1b01`，Phoenix 实际查到7条 span，其中1条 ChatOpenAI SDK 调用，模型 `gpt-4.1-2025-04-14`，输入1,721、输出207、总计1,928 tokens。`m1.model.finalizer` 是包装 span，不另计一次请求；实际账单费用未核对。State 的 model_call_count=6 来自 gateway 累计计数，不能表述为这次 Replay 调用了6次模型。
- 页面入口：`http://127.0.0.1:6006/redirects/traces/b6da464257fcff2b9923c283d42b1b01`。只读证据在忽略目录的 `replay-observed.json`，采集脚本 `observe_replay.py`。Chrome 回放成功不等于已修复 VS Code 内嵌预览问题；本次未修改业务代码，服务继续保留。

## 演示重点修正：按主要功能组织，不以 Span 字段教学为主

用户随后明确：导师需要看的是“输入问题后如何执行”“执行后如何 checkpoint 回溯”“原始目标完成了多少”。后续以这三问组织演示。上面的历史回看准备仍作为安全备用，但不能代替现场提交、控制操作的实际展示。

| 主要功能 | 应演示的动作 | 当前证据和缺口 |
|---|---|---|
| 提交问题并执行 | 正常网页输入任务和必要的允许文件；观察实际规划、Worker、输出 | 已有真实 CSV 任务证据；当前临时只读页面故意禁用提交。再次真实执行需单独授权，不能把规则替身当真实模型 |
| 从 checkpoint 回溯 | 选择已初始化的非终结检查点；Replay 后续执行；或 Fork 修改允许字段、再 Resume；比较新旧运行 | 后端和 UI 已实现，有离线测试。当前尚未在本轮演示动态操作；真实模式会再次调用模型，原任务和旧分支保留 |
| 展示导师要求的成果 | 执行控制、Phoenix 观测、历史归因三部分分别展示，再说明缺口 | 基础链路已实现；完整官方 Magentic-One、任意 benchmark 求解、完整内嵌 Phoenix、自动归因后修复闭环不能宣称已完成 |

特别区分两个 benchmark 含义：当前固定 Who&When 30×3 是“读取已有历史，预测责任 Agent/步骤”的归因评价，不是重新完成 30 个原始任务。项目另有接受 task/allowed_files 的 JSONL 执行器，但没有证明多数 benchmark 题目可正确完成，也没有通用 benchmark 附件/运行环境重建与答案评分能力。文本可以提交，不代表所需浏览域名、文件格式、代码执行等能力齐全。

导入的 Who&When/TraceElephant 历史不带本项目 LangGraph 可执行 checkpoint，不能直接对这些历史使用 Replay/Fork。对本项目自己执行的任务，checkpoint 控制才适用。目前没有把“外部历史错误归因 → 自动回到错误前 → 修改策略并重新验证”串成自动闭环。

正常 UI 的回溯操作顺序：历史任务 → Checkpoint → 选择 next 非空且已初始化的快照 → 回放（创建新 run/branch 并执行），或分叉（仅修改 plan/worker_instruction，创建 paused 分支）→ 恢复。已完成原运行不能直接 Resume。浏览器外部副作用不会还原，模型重新执行也不保证相同输出。当前展示层会拒绝这些操作，不能直接让用户在只读服务上尝试。

## 最新进度：历史展示服务已就绪（2026-09-20）

### 导师原话对齐后的第二幕：TraceElephant 历史归因展示已就绪

根据用户提供的导师语音转写截图，主线调整为：本地 Phoenix 与真实运行观测 → 导师提供的外部历史轨迹展示 → 归因结果及额外可视化。Checkpoint 为补充能力；不将“覆盖大多数 benchmark 原题”或“完整内嵌 Phoenix UI”添加为截图中已经明确的要求。

本阶段保留销售展示 `8000/6006`，另启动 `http://127.0.0.1:8001`（项目原有归因页面）和 `http://127.0.0.1:6007`（独立 Phoenix 研究库副本）。四个健康接口最终均返回 200。

- 副本目录：`datasets/.cache/advisor-attribution-20260920T093929Z-8e0b8108`。对原 `data/phoenix/phoenix.db` 及 WAL/SHM 做稳定快照后，用 SQLite backup 生成独立库，完整性检查通过；展示启动及浏览之后，原文件哈希仍一致。
- 应用使用空的新 runtime 存储、禁止执行的 gateway 和 HTTP 写操作拦截，不加载真实模型客户端。没有下载数据、导入新案例、创建 Experiment、运行 predictor/evaluator、调用 WebSurfer 或执行原始任务。
- 启动故障按实记录：第一次 Phoenix CLI 参数顺序错误，进程退出；修正后发现 11.35 的原生 read-only 模式会把全部 REST API（包括 Dataset GET）禁用为403，导致归因接口503。核实本任务 PID 40744 的启动时间、解释器与入口后，仅停止该进程树，改用常规 Phoenix 模式服务**副本**；沿用项目 HTTP-only gRPC 防护。不能称6007为接口级只读服务，操作限定为查询，8001写接口继续拒绝。
- 当前归因应用启动 PID 43960；当前 Phoenix 副本启动 PID 37788，身份与启动时间见 `processes.json` 和 `phoenix-copy-process.json`。先前失败及退出日志保留；停止前须再次核验身份，不按端口强杀。
- 实际读取 Dataset `m1-local-lab-external-trace-smoke`，本次选择到既有 Experiment `RXhwZXJpbWVudDo5`，不是历史文档中的 Do6。共有3个案例、9条方法预测、72条评价。这是既有记录选择结果，不是本次新增实验。
- TraceElephant 案例：`traceelephant:assistant_bench_task_7_gpt_4o_wylm9r1ppoet`。任务为两位成人、5岁和2岁儿童一年去西雅图儿童博物馆4次时，年票相对单次票可节省多少。任务仅作为历史上下文，不重新查询现价或计算答案。
- 历史标签为 WebSurfer/5，失败原因记录为无法打开 WhichMuseum 的博物馆链接；本次没有重新验证这一历史诊断。`first_explicit_error` 预测 WebSurfer/6；另外两种方法预测 WebSurfer/20。Agent均命中，Step均未命中，不将演示顺畅写成算法正确。
- 原31步、保留20步，标签步骤5仍在保留范围内。时间线使用来源 step_id；这是外部规范化历史步骤，不是本地 LangGraph checkpoint 或本次新生成的原系统 Span。
- TraceElephant三个详情的显示文本经过项目安全导出函数比对无额外变化；邮箱、私钥标记、个人用户路径模式扫描无命中，不能据此宣称穷尽个人信息检测。
- 真实 Chromium 验证：全部9行 → 数据源TraceElephant筛选3行 → 方法first_explicit_error筛选1行 → 详情和20步时间线；真实Step5、预测Step6标记与API一致，console/page/request/HTTP错误均0。页面证据保存在 `page-check.json`、`traceelephant-detail.png`；API安全视图在 `attribution-observed.json`，均留在本机忽略目录。

用户操作：打开8001 → 错误归因 → 数据源TraceElephant → 方法first_explicit_error → 点击唯一案例编号 → 查看原任务、来源声明、31/20截断说明及时间线。看到后暂停，再讲第三幕的预测/标签/评价对比。页面大量步骤显示 `structured historical payload omitted`，表示当前只展示安全的类型/信号摘要，没有展开原始结构化内容；这限制了原始工具输入输出的细读，不能称为完整原日志浏览器。

以下为随后实际完成的准备，覆盖第 1 节初始服务未启动的状态：

- 独立目录：`datasets/.cache/advisor-rehearsal-20260920T073252Z-5d322863`，由稳定的原数据库及 sidecar 快照经 SQLite backup 创建三份展示数据库；完整性检查通过，启动后的原文件哈希核对仍一致。没有把原数据库交给 runtime 启动恢复。
- 临时 `serve_history.py` 复用现有 API、网页及 runtime 读取副本；不初始化模型客户端，不初始化遥测导出。HTTP 写方法、runtime 执行入口及 gateway 均拒绝执行，网页响应额外标注历史回看并禁用执行按钮。这是临时展示层，不是产品新增能力；副本仍可被服务做初始化维护，不能称其为 SQLite 文件级只读。
- 五个写接口在有效 CSRF 条件下均返回 403；一个 completed 运行及八个 checkpoint 可读，gateway 调用被拒绝。首次检查辅助命令因漏写 UTF-8 产生 UnicodeDecodeError；修正读取编码后通过，未改变任务或数据。
- 独立启动沿用项目 `scripts/serve_phoenix.py`，日志和进程回执写入展示目录，不覆盖原 `data/logs`。Phoenix 启动 PID 43048、监听 PID 3536；应用启动 PID 42764、监听 PID 42872。进程身份和启动时间另存 `processes.json`；未来停止前须重新核对，不能仅凭此处 PID 结束进程。
- Phoenix `/healthz` 和应用 `/api/health` 均健康。仅发现回环 6006、8000 监听，4317、8001 无监听。副本中最终答案逐字匹配，指定 Trace 的 37 个 Span 经只读客户端查询确认；尚未完成 Phoenix 网页逐 Span 教学。
- 使用项目 Playwright 和已安装 Chromium 1181 真实检查任务历史、答案、20 条事件、8 个 checkpoint、四个详情标签和按钮禁用；应用 console/page/request/HTTP 错误均为 0。最初沙箱 WinError 5，授权沙箱外运行后成功。未下载或升级浏览器。
- 实际页面的“证据 0”统计的是引用和产物，不包含 FileReader 的 progress_ledger 文本。本次没有引用/产物，因此不是读取失败。CSV 内容应从 State 的 progress_ledger（及相关消息）查看，节点执行从事件核对；这也是当前证据页展示范围的限制，不能把它称为完整工具证据浏览器。

本阶段新增模型调用和 WebSurfer 调用均为 0；服务保持运行，等待用户查看。证据回执为该展示目录内 `guard-check.json`、`services-ready.json`、`page-check.json` 和安全合成页面截图；它们不加入 Git。

随后已用真实 Chromium 验证 Phoenix 的直接定位入口：`http://127.0.0.1:6006/redirects/traces/4c7472de98389ed9672cf1b3b82058fb`，实际跳转到项目 `UHJvamVjdDoy` 下同一 Trace 详情。根 `m1.run`、节点树及四条 `ChatOpenAI` 在页面可见，根耗时 16.53 秒、总 token 2,263；Phoenix 页面显示 Total Cost `<$0.01`，这是观测平台显示的成本值，不是已核对的服务商账单。外部 GitHub 星标请求被预检网络策略阻止，产生各一条 console/page/request 错误，本地 HTTP 无 4xx/5xx；Trace 详情可用，仍不宣称 Phoenix 页面零错误。证据保存在 `trace-page-check.json` 和 `phoenix-trace.png`。

## 1. 本次准备核对

| 项目 | 9 月 20 日实际核对 | 下一步 |
|---|---|---|
| Git | HEAD 未变；开始时只有三份既有未跟踪指南 | 保留原文件，不提交 |
| 服务 | netstat 未发现 4317、6006、8000、8001 监听；两项健康请求均 ConnectError | 网页演示待启动和验证，不记为已就绪 |
| Python | 两个项目虚拟环境的解释器文件存在 | 未重新运行完整测试或安装；存在不等于重新验收通过 |
| 真实任务 | 安全合成任务的核对 JSON、应用/检查点/Phoenix 数据库文件仍存在 | 先准备独立副本，验证页面可读取后才作为现场入口 |
| 旧启动器 | `serve_sales_demo.py` 拒绝已存在的存储，且两项一次性 claim 文件存在 | 不删除 claim、不绕过原启动保护、不重新提交任务 |
| 原应用启动 | API lifespan 调用 runtime.startup；后者会处理未完成运行 | 不能把普通启动称为对旧存储完全只读 |
| 归因案例页面 | 本阶段未查询研究实例；销售任务的独立实例不能假定含有归因数据 | 单独核对可展示案例；未就绪时使用已有汇总文档 |

本阶段仅新增此 Markdown，读取安全演示回执和代码；未启动/停止服务、运行测试、调用模型或读写研究数据库。

## 2. 给导师的演示主线（约 12–15 分钟）

开场：“目前是本地研究原型：LangGraph 负责执行和状态控制，Phoenix 负责观测与评价。先看一条已经实际执行的模型任务，再看状态控制和错误归因。今天回看历史记录不等于重新调用模型。”

| 场景 | 时间 | 页面操作 | 希望导师看到的成果 | 证据边界 |
|---|---|---|---|---|
| A：任务如何得到答案 | 4 分钟 | 任务历史 → 销售任务 → 事件 → 证据 → 最终答案 | 模型规划、真实 FileReader 读取、模型总结串成完整链路 | 9 月 14 日真实执行，今天回看；不是现场新任务 |
| B：状态与观测分别保存什么 | 4 分钟 | Checkpoint → State → Trace ID → Phoenix 对应 Trace/Span | 执行状态和模型调用能分别定位并关联 | 完整 Span 仍需 6006；不能只打开 Phoenix 首页就算定位完成 |
| C：暂停、恢复、回放和分叉 | 2 分钟 | 展示合法非终结 checkpoint 和控制入口，先不点击 | 可从执行边界继续或建立分支 | 销售任务已完成，不能 Resume；本次未现场验证暂停/分支执行 |
| D：错误归因研究管线 | 3 分钟 | 归因列表 → 一个已核对案例 → 方法筛选 → 预测/标签/评价 → 时间线 | 已有历史数据适配、预测评价和可视化 | 数据来源须标明；页面就绪待核对，不临时重跑 benchmark |
| 收尾 | 1 分钟 | 项目汇报中的当前限制 | 明确已完成和后续工作 | 非官方完整 MagenticOneGroupChat；未统一启动；另一台物理电脑部署待验收 |

如果只有 5 分钟，保留 A、B 和一个归因案例；控制功能说明入口与既有测试依据即可，不为覆盖全部按钮临时执行任务。

## 3. 场景 A：真实任务讲解卡

本机安全回执：`datasets/.cache/live-sales-20260914T074330Z-400a8931/observed-run.json`。它是本次准备读取的历史证据，不是刚查询到的在线 API 响应，也不随 Git 交付。

- run_id：`2b34a30f-ec43-4ee6-9d7d-f99af05397f1`
- trace_id：`4c7472de98389ed9672cf1b3b82058fb`
- Phoenix 项目：`live-sales-20260914T074330Z-400a8931`
- 配置模型：`gpt-4.1`；实际 LLM Span 模型：`gpt-4.1-2025-04-14`。
- 实际顺序：Planner → Orchestrator → Pause Gate → FileReader → Orchestrator → Finalizer。
- 结果：Notebook 50，Pen 40，Backpack 90，总额 180，Backpack 占比 50%。状态 completed，结果也与独立验算一致。
- 真实文件读取匹配；没有 WebSurfer/Coder 节点。只有 FileReader 可实际执行的限制来自一次性演示启动层，不是原产品的 Worker 白名单能力。

现场依次说：

1. “这个任务先进入 LangGraph。Planner 把用户目标变成计划，Orchestrator 根据已有证据选择下一步。”
2. “这里确实执行了 FileReader。证据中的三行 CSV 与本次授权文件一致，不是模型自行编造的文件内容。”
3. “读取之后再次协调，再由 Finalizer 根据证据生成答案。计算由模型完成，本任务没有执行 Python 计算程序。”
4. “完成状态只说明流程结束；我还独立核对了金额和比例，两者都正确才说这次成功。”

FileReader 任务没有网页引用和浏览器截图，空栏属于正常结果。任务已经完成，现场主要按事件顺序回看；不把静态历史讲成 SSE 正在直播。

## 4. 场景 B：三类记录的讲解卡

| 记录 | 本次已有证据 | 一句话解释 |
|---|---|---|
| 事件 | 20 条 | 应用记录运行和节点发生了什么；运行时可经 SSE 推送，结束后可查询历史 |
| Checkpoint | 8 个，含初始化快照 | 保存图状态和待执行节点；`next` 是下一步，不是已经发生的步骤 |
| Phoenix Trace | 回执中有 37 个 Span | 展示执行及模型调用耗时、层级和关联信息；不是图状态恢复引擎 |

优先对比两个检查点：

- 读取前：`1f1b0598-bdf5-6465-8003-89a762b93891`，`next=file_reader`，证据数 0。
- 读取后：`1f1b0598-bf2f-6a9e-8004-a7bbef627af6`，`next=orchestrator`，证据数 1。

State 页展示当前状态，不等于选中任意历史 checkpoint 后就展示其完整旧状态。初始任务账本的 unknowns 可能仍保留“尚未读取”的文字；应对照后续读取事件和证据解释，不因此抹掉真实执行或修改旧记录。

Phoenix 应定位上述项目和 trace_id，再展开根运行、节点和真实 `ChatOpenAI` Span。已有回执显示 4 条真实 SDK LLM Span：规划一次、协调两次、总结一次；另有手工模型包装 Span，不能相加说成 8 次请求。手工埋点和框架自动埋点形成不同层级，不额外代表新的 Agent。

四条实际 SDK Span 合计输入 1,614 tokens、输出 649 tokens、总计 2,263 tokens。实际账单金额没有本次可用证据，不填估算值冒充账单。原始模型输入输出的隐藏属于脱敏策略，不能据此认定没有调用。

事件、checkpoint、Trace 通过 ID 关联，数量不同，不是一一对应，也不能互相替代。9 月 20 日尚未重新在 Phoenix 网页核对这些记录，页面定位属于下一排练阶段。

## 5. 场景 C、D 的准备边界

Resume 继续原运行；Replay 从合法历史 checkpoint 创建新运行/分支并重新执行后续节点；Fork 允许调整 plan 和 worker_instruction，建立暂停分支后再 Resume。外部副作用不会自动撤销。现场只展示含义和入口；如要实际演示，另用明确标注的离线合成场景，不能操作本次真实任务来暗中增加调用。

暂停依赖安全边界，快速任务不适合临场抢点。当前没有核对到可直接展示的暂停分支，因此先引用已有确定性测试，不宣称现场已操作成功。需要动态演示时，应在独立数据环境预演后加入讲稿。

归因场景只选一个获准本地展示、且已核对版本的案例，讲清“预测—真实标签—评价—对应步骤”。先确认数据来源和案例是否存在，再填写讲稿中的具体预测数值。合成 fixture 可以展示交互，但必须标注合成，不能替代真实研究案例的结论。

30 个固定样本 × 3 种基线是 90 条预测，不是 90 个独立样本。Frozen Test 结果已被查看；本阶段不改协议、标签、manifest 或结果，也不重新运行归因算法。现有默认归因页面不是完整 30 样本 benchmark 仪表盘。

## 6. 现场问题与处理顺序

| 现象 | 先判断什么 | 本次处理 |
|---|---|---|
| 8000/6006 打不开 | 端口、PID、各自健康接口 | 当前已确认无监听；后续由助手准备独立存储后启动，不能直接启动旧一次性入口 |
| 已有端口占用 | 当前进程身份及归属 | 保留未知服务，不按端口强杀 |
| 旧启动器拒绝启动 | 存储和 claim 是否已存在 | 这是一次性保护；不删除 claim，不指向旧运行进行启动恢复 |
| 需要保留原演示记录 | SQLite 是否仍有写入者、WAL 是否存在 | 后续采用 SQLite 一致性备份到唯一新目录，不能只复制主 db 文件；本阶段尚未执行备份 |
| 刷新后没有所需历史 | 启动配置指向哪个存储副本 | 核对 run_id；不重新提交销售任务补历史 |
| Phoenix 健康但看不到 Trace | 项目、Trace ID、时间筛选 | 此任务在 9 月 14 日，9 月 20 日默认近 24 小时可能过滤掉；扩大时间范围，再有限只读查询 |
| Phoenix GitHub 请求报错 | 失败请求是外部星标组件还是本地 Trace API | 区分已知上游组件问题与本地服务失败；不笼统宣布全页面通过 |
| 归因页为空或 503 | Dataset/Experiment 是否存在、配置是否匹配 | 销售实例没有义务包含研究数据；保留错误说明，转用已核对结果文档，不自动导入 |
| 暂停按钮点不到 | 当前状态及任务是否已结束 | completed 不可 Resume；快速执行结束后按历史讲解，另准备离线控制场景 |
| 提交或控制返回 409/422 | 是否为演示禁用、状态不合法、参数错误 | 不反复点提交，不解除保护来追求成功 |
| 新任务请求失败 | 网络、凭据、限额或模型响应 | 新真实调用另需授权；失败后不自动重跑，也不偷偷改成规则模型 |
| 中文变成问号 | 源文件编码和 PowerShell 管道编码 | 上次已发生并在提交前修正；核对 UTF-8 文件，避免用默认 ASCII 管道重建中文提示词 |

下一阶段准备目标：在不写入原数据库的前提下，为已有合成运行建立独立展示副本，核对展示启动方式是否阻止新执行，再验证 8000 与 6006 页面。此隔离展示准备尚未完成，不能照本文件宣称已具有产品级只读模式或统一启动器。

## 7. 排练验收卡

- [x] 代码版本、既有改动、服务初态已核对。
- [x] 真实运行回执、实际节点、结果及 token 记录已核对。
- [x] 独立展示存储和启动层准备完成，原始记录不变。
- [x] 8000 可查看指定运行、事件、证据、Checkpoint、State；证据标签为引用/产物空态，读取内容见 State。
- [x] 6006 已实际定位指定 Trace，根运行和四条 LLM 树项可见；LLM 字段逐项讲解仍待用户继续。
- [ ] 控制功能的动态离线场景已预演，或明确只展示入口与测试依据。
- [x] TraceElephant实际案例页面、来源、截断与评价已核对；用户逐步讲解仍在进行。
- [ ] 用户完成现场讲解排练后，记录本次实际操作和未解决问题。

本阶段新增模型调用 0、WebSurfer 调用 0；不提交、不创建 tag、不添加 remote、不 push。服务之后如由本任务启动，在用户确认看完并选择停止前保持可用；收尾只停止身份与所有权核实过的本任务服务。
