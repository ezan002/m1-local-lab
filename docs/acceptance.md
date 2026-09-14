# 实机验收记录

2026-09-12 全仓工程审查的离线结果、环境阻塞及残余风险见 [审查报告](full-project-audit-2026-09-12.md)。以下各节保留其历史验收日期及当时结果，不代表本次重新运行了真实模型、浏览器或 Phoenix 验收。

## Attribution benchmark protocol v1（2026-09-10）

- PASS：固定 HF revision `59b9fcba1aaed7bbf206b5f4d3c68b8face2f49c` 与仓库 revision `b2bae5c5b06d681d04ea5e9b63b7a30525c04925`；下载 1,903,897 bytes，低于 25 MiB 上限。两个 Parquet 覆盖完整 184 行，但仅 30 行进入基准。
- PASS：manifest `c18401f0605f50ec8d422edcdcaca8d64ee77173324511a975889d0b88c6cb2b`；Hand-Crafted/Algorithm-Generated=10/20，Dev/Frozen Test=20/10，schema exclusion=0，固定顺序二次运行结果完全一致。
- PASS：`src/m1_local/attribution.py` 与冻结提交 `da3ef193bdd73474eec5191f1e267ee163c308a7` 一致；没有增加方法、case 特判或 Frozen Test 调参。
- PASS：label guard 覆盖所有 30 个 inference/Dataset input；真实 who/when/reason 只在 Dataset output 和 evaluator 阶段。`paid_model_calls=0`、`real_websurfer_calls=0`、`external_task_executed=false`。
- PASS：总体 agent exact 16/90、step exact 6/87、step ±1 16/87、step ±2 20/87、coverage 76/90；Dev agent/step=9/60、2/57，Frozen Test agent/step=7/30、4/30。一个越界 step 按协议排除三个方法的 step 分母但保留 agent 分母。
- PASS：Phoenix 逻辑 Dataset `m1-local-lab-who-when-benchmark-v1` 为 30 examples；因本机早期中间 manifest 已占用逻辑名，最终 canonical manifest 按冲突规则写入 `m1-local-lab-who-when-benchmark-v1-c18401f0605f`。Experiment `m1-local-lab-attribution-benchmark-v1` 为 90 runs、1170 evaluations、13 个 `attribution.*` 名称。仅使用公共 API。
- PASS：新增定向测试 13 passed；最终完整回归 `65 passed, 2 skipped in 26.71s`；两个 `pip check` 均无损坏依赖；旧 external smoke 与 failure attribution MVP verifier 均 PASS。
- PASS：验收结束后 6006/8000/8001 均关闭；没有停止任何预先存在的服务。API/config 未改变，因此按条件不重复执行 UI E2E。
- LIMIT：数据卡没有独立数据许可，固定策略为仅本地研究、禁止再分发；本结果仅是 30 样本冻结基线，不证明算法有效。完整证据见 [attribution-benchmark-v1.md](attribution-benchmark-v1.md)。

## 错误归因网页可视化与真实浏览器验收（2026-09-10）

- PASS：从 `da3ef193bdd73474eec5191f1e267ee163c308a7` 创建 `feature/failure-attribution-ui`；稳定 tag `m1-local-lab-v1.0-smoke` 仍指向 `71cc37e740be9a755261f1c9153aca22b19eec09`，remote 为空。
- PASS：新增三个 typed GET：`/api/attribution/summary`、`/api/attribution/cases`、`/api/attribution/cases/{dataset_case_id}`。仅调用 Phoenix 公共 `datasets`/`experiments` Python API，未查询内部 SQLite、未抓取网页 HTML、未在生产代码固定 Experiment ID。
- PASS：零费用后端复验后，Dataset 仍为 3 个唯一 examples；最新具名 Experiment 为 `RXhwZXJpbWVudDo2`，含 9 task runs、72 evaluations。输出为 paid model calls=0、real WebSurfer calls=0、external task executed=false、label leakage check=passed。
- PASS：页面显示 3 种方法及独立分母：`last_active_agent` 2/3、0/3、0/3、distance 12 (n=3)、coverage 3/3；`first_explicit_error` 1/3、0/3、0/3、distance 1 (n=1)、coverage 1/3；`rule_based` 1/3、0/3、0/3、distance 9 (n=3)、coverage 3/3。`None` 显示为“—”，不转成 0。
- PASS：归因一级 tab、概览、筛选、列表、prediction/ground truth/evaluation 分区及纵向时间线均完成。TraceElephant 显示 original=31/retained=20/truncated=true，真实 step 5 和预测 step 20 分别以颜色和文字标签显示；2 个真实 abstain 能被筛选并显示。实际三样本没有 unscorable case，该状态由 fixture/API/UI 自动化测试覆盖，不伪造页面数据。
- PASS：`scripts/verify_attribution_ui.py` 使用现有 Playwright Chromium 和 `RuleBasedGateway`，在 1366×768、1920×1080、Playwright 125% 缩放模拟（1536×864、device scale factor 1.25）均通过。每档打开 3 个唯一 case 并验证 20 步时间线；1366 档模拟 503 后重试恢复。
- PASS：三档 Console errors=0、page errors=0、request failures=0、意外 4xx/5xx=0；归因请求分别为 20/16/16，无请求风暴。键盘 End/Home、焦点 outline、空筛选、复制 Experiment ID、安全 Phoenix 链接和页面级无横向溢出均通过。
- PASS：六张真实截图位于被忽略的 `data/test-output/attribution-ui/`，未加入 Git。验收启动前后 6006、8000、8001 均未监听；临时 Phoenix PID 29904、Uvicorn PID 45280，只停止脚本自身进程。
- PASS：最终完整测试为 `52 passed, 2 skipped in 28.44s`；两个隔离环境的 `pip check` 都返回 `No broken requirements found.`。归因/API 定向测试此前为 `25 passed`。

详细数据边界、API、交互和限制见 [failure-attribution-ui.md](failure-attribution-ui.md)。

记录日期：2026-08-28。所有命令均在 `C:\projects\m1-local-lab` 的 Windows PowerShell 执行；没有把 Linux 结果作为 Windows 验收。报告不含 API Key。

## External dataset compatibility smoke（2026-09-01）

- PASS：TraceElephant 与 Who&When 按不同 schema、revision 和许可口径独立处理；实际抽取 1 + 2 = 3 条，未超过 5 条。
- PASS：Phoenix Dataset `m1-local-lab-external-trace-smoke` 最终为 3 个唯一 examples；Experiment `m1-local-lab-external-dataset-smoke`（`RXhwZXJpbWVudDox`）有 3 个 task runs，`provenance_guard` 3/3 score 1.0。
- PASS：三个本地验证 run 全部由 RuleBasedGateway + 临时 CSV 完成，0 paid model calls；外部 task 未执行，外部 history 未被导出成项目 OTel trace。
- PASS：新增测试覆盖两套解析、原始 ID/ground truth、缺字段、稳定 ID、超长截断、路径穿越、不可信附件、key/password/Cookie/Bearer 脱敏、Phoenix 幂等复用和 provenance。完整套件 `28 passed, 2 skipped`，原 `19 passed, 2 skipped` 基线未回归。
- PASS：TraceElephant 只通过 bounded HTTP Range 读取目录和一条运行的两个 JSON；累计 TraceElephant Range 1,038,147 bytes，Who&When payload 45,785 bytes，没有下载 596,610,649-byte 全量 ZIP，也没有运行第三方代码或修改环境依赖。
- NOTE：Phoenix 11.35.0 不支持自定义 example ID，稳定 `dataset_case_id` 保存在 input/metadata 并用于复用检查。完整证据与字段映射见 [external-dataset-smoke.md](external-dataset-smoke.md)。

## 平台与锁定版本

- Windows x64，系统版本字符串 `Windows-10-10.0.26200-SP0`（Windows 11 内核版本报告方式）。
- 系统 Python、`.venv`、`.venv-phoenix` 均为 CPython 3.11.9，64 位。
- 主应用：LangGraph 1.2.11、SQLite checkpointer 3.1.1、AutoGen core/agentchat/ext 0.7.5 同系列、Playwright Python 1.54.0、LangChain OpenAI 1.6.0、Phoenix OTEL 0.17.1、Phoenix client 3.3.0、FastAPI 0.141.1。
- Phoenix：arize-phoenix 11.35.0、arize-phoenix-evals 2.0.1、FastAPI 0.116.2、Starlette 0.48.0、Pandas 2.2.3。
- 当前 Playwright 1.54 驱动安装 Chromium build 1181；实际启动浏览器版本 `139.0.7258.5`。
- 两个环境最终 `pip check`：`No broken requirements found.`

两份 lock 已扩展为本次实机环境的精确传递依赖闭包。官方 `autogen-ext[web-surfer]` 声明依赖 `markitdown[all]`，后者强制带入 `azure-ai-contentunderstanding` beta；这不是项目主动选择预发行核心组件，文件中已明确注释并固定实际通过版本。

Phoenix 20.3.0 在 Python 3.11 实测启动时触发 dataclass `mappingproxy` mutable-default 错误，因此没有把“已安装”当成成功；最终固定为能在该实机启动并持久化的 11.35.0 组合。两个环境的重建输入分别是 `requirements-app.lock.txt` 与 `requirements-phoenix.lock.txt`。

## 实际命令与结果

### 环境与 doctor

```powershell
& '.\.venv\Scripts\python.exe' -m pip check
& '.\.venv-phoenix\Scripts\python.exe' -m pip check
& '.\.venv\Scripts\python.exe' -m m1_local.cli doctor
& '.\.venv\Scripts\python.exe' -m m1_local.cli doctor --online
```

- PASS：依赖导入、SQLite 实际写入、Chromium 启动。
- PASS：Windows 浏览器进程实际运行于 `_ProactorEventLoop`。
- PASS：Phoenix 首页 HTTP 200。
- PASS：联网模型文字 `TEXT_OK`、人工 16×16 图片 `IMAGE_OK`、真实 tool call；严格共 3 次预检调用。

### 官方 WebSurfer 与真实 Chromium

```powershell
$env:HEADLESS='true'
$env:RUN_REAL_MODEL_TESTS='1'
& '.\.venv\Scripts\python.exe' -m pytest -q tests/test_real_websurfer.py
```

结果：`2 passed`。

- PASS：本地双页 fixture 中，官方 WebSurfer 读取第一页唯一标记、自主点击可见链接、读取第二页 `UNIQUE_MARKER_BETA_7394` 并保存真实截图。测试代码没有代替 agent 调 Playwright click。
- PASS：公开 `https://playwright.dev/python/docs/library` 测试返回 `Proactor` 要求与该页 URL。

### 离线单元/API/恢复测试

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q
```

覆盖项：动态选择 file/coder worker、代码产物未执行、稳定 ID reducer、文件边界与 URL 安全、停滞重规划和受控预算失败、模型异常持久化、原子单任务准入、节点边界暂停、关闭并重启 runtime 后恢复、history、replay、fork 及旧 head 不变、串行 JSONL 独立 thread、Host/Origin/CSRF/API。真实模型测试默认明确 skip，不作为 mock 通过冒充。

最终结果：`14 passed, 2 skipped`；两个 skip 正是需要显式 `RUN_REAL_MODEL_TESTS=1` 的真实模型测试。

### Phoenix 接收与重启持久化

```powershell
& '.\scripts\start-phoenix.ps1'
& '.\.venv\Scripts\python.exe' .\scripts\verify_phoenix.py
& '.\scripts\stop-phoenix.ps1'
& '.\scripts\start-phoenix.ps1'
& '.\.venv\Scripts\python.exe' .\scripts\verify_phoenix.py --query-only 6396f47e8511ea98d80d0f705731b246
```

- PASS：真实 OTLP/HTTP protobuf 被 Phoenix 查询到；测试 trace `6396f47e8511ea98d80d0f705731b246` 有父子 spans。
- PASS：停止并重启 Phoenix 后，同一 trace 仍可查询；服务存储为 `data/phoenix/phoenix.db`，与 LangGraph 数据库分离。

业务图关联验证：

```powershell
& '.\.venv\Scripts\python.exe' .\scripts\verify_runtime_trace.py
```

结果为 PASS：run `c685aedf-3900-4ef0-be5c-987830b18339`，trace `7bd9fc8cd7423f7a88bfa5a44311193a`；查询到 `m1.run`、planner、orchestrator、file_reader、finalizer，节点具有父 span，并有 5 条真实已提交 output checkpoint 关联。

最终插桩去重验收的真实完整图运行 `1d882fbb-ec86-416e-8e11-614e6b8c5b35` 对应 trace `614ffac08358fc7c5455bbe2b1561f71`。Phoenix 查询到同一父 trace 下的 run、图节点、5 个 LangChain `ChatOpenAI` LLM spans，以及 3 个 AutoGen WebSurfer `ChatCompletion` LLM spans。每次编排调用不再同时出现第二个 OpenAI span；WebSurfer span 仍完整保留。该次运行保存 3 张截图和终结 checkpoint。

AutoGen 0.7.5 的 `on_messages()` 在其 `on_messages_stream()` 最终响应处提前 return，会触发 `async generator ignored GeneratorExit` 清理警告。适配器改为完整消费公开 stream 并提取最终 `Response`；修改后的两项真实 WebSurfer 测试均通过且未再出现该警告。剩余的 pydub “未在 PATH 找到 ffmpeg”提示来自未使用的音频传递依赖，不影响 Chromium/WebSurfer，本项目也不调用音频转换。

### UI/API

- PASS：FastAPI TestClient 验证本机 Host、Origin、CSRF 与后台 run API。
- PASS：事件先入 SQLite，SSE 具有单调 ID 并支持 `Last-Event-ID` 参数/头补读；页面断线不拥有后台 task。
- PASS：页面仅用 `textContent`/安全 DOM API显示 state 与模型文本；产物只能按服务端 artifact ID 解析。
- PASS：实际运行 `scripts/start-app.ps1` 后，首页与 `/api/session` 均返回 HTTP 200，页面包含正确 UTF-8“新任务”，CSRF token 存在；随后 Uvicorn 完整执行 shutdown。
- NOT RUN：人工逐像素/多浏览器视觉验收。功能路径已通过 API 测试，仍建议演示前在目标显示缩放设置下人工查看一次。

### 第一版交互控制台增量验收（2026-08-30）

- PASS：页面升级为中文桌面控制台，包含健康/trace 顶栏、历史运行、任务提交、状态指标、流程图、输出/错误、事件、证据/截图、checkpoint、State、暂停/恢复/replay/fork。
- PASS：新增最小只读 `GET /api/health`、`GET /api/runs/{run_id}/events-log`，详情返回脱敏 run 元数据和真实 trace ID；`/api/session` 返回后端能力约束。列表不返回 `head_config`。
- PASS：离线测试更新为 `16 passed, 2 skipped`。新增覆盖首页/CSS/JS/UTF-8、空列表和 404、健康状态、真实详情字段、trace、事件时间、CSRF、暂停/恢复状态、终结 checkpoint、成功 replay/fork 血缘和 fork 白名单。
- PASS：实际服务 HTTP 检查：应用/Phoenix 均 healthy，首页、CSS、JS 均 200；历史数据共 2 个运行。选中真实历史运行后读取到 14 个 checkpoint、32 个事件、3 张截图、5 个引用和 trace；截图 artifact 返回 `200 image/png`、109613 bytes。
- PASS：静态安全检查确认前端没有 `innerHTML`，任务重复提交有同步 `ui.submitting` 防护，SSE 新连接携带最后 `last_event_id`，终结状态会关闭 EventSource。
- PASS：服务端请求日志中，所有按真实单一 run ID 发出的页面依赖接口均返回 200；本次没有创建新的真实模型/WebSurfer 运行。
- BLOCKED：真实浏览器控制运行时未发现任何可连接浏览器（浏览器列表为空），因此没有伪造 1366×768、1920×1080、控制台、网络面板、点击/焦点或截图验收。CSS 已为两种桌面宽度和 125% 缩放的等效逻辑宽度设置响应式布局，但仍需在浏览器连接可用后完成视觉与交互实测。
- NOT RUN：新版 UI 截图；由于真实浏览器项被阻塞，没有生成或引用示意截图。

### 第二阶段视觉验收与 Trace 语义（2026-08-30）

- BLOCKED：按浏览器控制流程再次连接 `http://127.0.0.1:8000/`，浏览器发现列表仍为空。无法真实执行 1366×768、1920×1080、125% 缩放、Console、网络面板、标签点击、截图弹窗、剪贴板和焦点检查；未使用独立 Playwright/源码渲染绕过，也未生成 UI 截图。完成该项仍需要连接一个可用浏览器实例。
- PASS（服务级，不冒充浏览器验收）：复用历史完成运行 `1d882fbb-ec86-416e-8e11-614e6b8c5b35`，读取到 32 个唯一事件 ID、14 个 checkpoint、3 个产物、5 个引用和 trace ID。首页/CSS/JS 均为 200，刻意请求的缺失页面为 404；已完成运行的 pause/resume 以及终结 checkpoint 的 replay/fork 均返回 409。没有创建任务或调用模型。
- NOTE：第一版 PowerShell 检查因顶层 JSON 数组被拼接，曾向一个由两个 run ID 拼成的无效路径发送请求并得到 404/422；服务没有改变状态。改用 Python HTTP 客户端显式解析数组后，上述真实单 run 结果全部通过。
- PASS（静态辅助，不冒充点击验收）：前端只有一个 `new EventSource` 创建点，每次连接和切换运行前调用 `closeEventSource()`，事件按单调 ID 去重；终态关闭连接。标签页具备 click、方向键、Home/End 逻辑，截图使用原生 dialog，复制按钮统一调用 Clipboard API，控制按钮同时检查 UI capability 与 checkpoint 终结状态。
- BASELINE：既有付费完整图 trace `614ffac08358fc7c5455bbe2b1561f71` 共 50 个 span；OpenInference kind 为 30 个 CHAIN、9 个 LLM、11 个缺失，`input.value` 39 个、`output.value` 36 个。这些历史 span 保持原样，不做追溯修改。
- PASS：新增语义层后，`m1.run`=`AGENT`，LangGraph 控制节点=`CHAIN`，Worker=`TOOL`，应用模型摘要=`LLM`；自动 LangChain/OpenAI span 保留原有父子位置并继承运行/节点关联字段。`m1.checkpoint.commit` 在同步提交后记录真实 checkpoint，不预猜 ID。
- PASS：第三方插桩配置隐藏原始输入、输出、消息、prompt、choice、工具定义和 invocation parameters；应用摘要限制长度并脱敏 secret/key/password/Cookie/Bearer/OpenAI key 形态。错误只记录异常类型，不记录异常正文中的潜在敏感内容。
- PASS：修复 Phoenix 11.x provider 的处理器注册语义，添加关联处理器时保留默认 BatchSpanProcessor；runtime 关闭前在根 span 结束后再次 flush。此前本地 trace ID 存在但未导出的回归已加入自动化测试。
- PASS：`scripts/verify_runtime_trace.py` 仅使用 `RuleBasedGateway` 和本地 CSV，在 Phoenix 中验证原始运行 `fd68d674-d23b-4ec9-b688-314b8389b544`、Replay `75e4d35f-34b5-49b1-8288-c0d6244004c9`、Fork `2ebd8f98-4946-46e8-95e1-b9443861dbeb`。对应 trace 为 `b407a8ede55ffa7588ab3dd921a67cfa`、`ba739ab434c95cfe04fe55a62fcc3fa6`、`d9f5d6ac195b426707e97cb183a8d288`；25 个应用 span 覆盖 AGENT/CHAIN/LLM/TOOL，8 个 checkpoint link 全部 committed，验证用假密钥未出现在 Phoenix。
- PASS：完整离线套件为 `19 passed, 2 skipped`。新增测试覆盖摘要脱敏、第三方原始内容隐藏、Phoenix 默认 exporter 保留、语义 kind、input/output、父子关系、全部关联字段、checkpoint commit，以及 Replay/Fork 血缘。
- DIRECT：Phoenix 可直接查看 `run_id`、`thread_id`、`branch_id`、`execution_id`、`langgraph_node`、`agent_name`、`step_number`、`status`；有值时可直接查看 `dataset_case_id`、`replay_of`、`fork_of`。精确 `checkpoint_id` 在 run 最终 span 和 `m1.checkpoint.commit` 上可见。
- DEFERRED：节点/模型执行时 output checkpoint 尚未产生，需通过相同 `execution_id` 连接 `m1.checkpoint.commit`，不会回填已结束 span。模型/网络/浏览器/安全策略/提交不确定性的自动错误分类与责任归因仍留待错误归因阶段；旧 trace 也不会被追溯改写。

### 离线错误归因后端 MVP（2026-09-02）

- PASS：从稳定提交 `71cc37e740be9a755261f1c9153aca22b19eec09` 和未改变的 `m1-local-lab-v1.0-smoke` tag 创建 `feature/failure-attribution-mvp`；未新增 remote 或 push。
- PASS：三种确定性方法 `last_active_agent`、`first_explicit_error`、`rule_based` 均只接收 label-free `AttributionInput`，支持 abstain；未调用模型或 Phoenix 查询。
- PASS：inference/ground-truth 双视图由独立深拷贝构造。递归 guard 覆盖顶层、嵌套列表/字典、metadata 与 Pydantic 对象，发现 label/ground-truth/mistake 字段立即失败；predictor schema `extra=forbid`、frozen。自动化测试证明正常 view 可通过、泄漏被拒绝、evaluator 在 prediction 之后 join 且不修改 prediction。
- PASS：TraceElephant 与 Who&When 使用独立 normalizer；三条既有样本共运行 3×3=9 次。评估区分 scored/abstained/label missing/trace truncated/evaluation failed，`None` 不转成 0，宏观结果同时报告 numerator/denominator。
- PASS：截断字段均进入 evaluation 和 Phoenix metadata。TraceElephant original=31、retained=20、truncated=true，真实 decisive step=5 仍保留，因此本次 step_scorable=true；自动化 fixture 另验证“真实步骤已截断”时 step/joint/distance 排除分母、Agent 仍可评价。
- PASS：复用 Phoenix Dataset `m1-local-lab-external-trace-smoke`（ID `RGF0YXNldDox`，version `RGF0YXNldFZlcnNpb246MQ==`），回查为 3 个唯一 examples；未创建重复 Dataset。
- PASS：Phoenix Experiment `m1-local-lab-failure-attribution-mvp`，最终验收实例 ID `RXhwZXJpbWVudDo1`，回查 9 task runs、72 evaluations；8 个 evaluation 名称均带 `attribution.` 命名空间。prediction metadata 只含 method/case/source/revision 与 inference 可知的 truncation/provenance；label-derived scorable 字段只在 evaluation metadata，prediction 侧不含真实标签。
- PASS：验证脚本输出 paid model calls=0、real WebSurfer calls=0、external tasks executed=false、full dataset downloaded=false、label leakage check=passed。外部历史只作为 Dataset example，没有伪装为新 OTel Trace。
- PASS：定向 attribution 测试为 `17 passed`；完整离线回归为 `45 passed, 2 skipped in 24.33s`。原 28 passed, 2 skipped 能力均保留，未修改 runtime、LangGraph、Agent、API 或网页。
- PASS：验证前后直接健康检查均为应用 `/api/health` HTTP 200 与 Phoenix `/healthz` HTTP 200。两项服务在脚本前已存在，脚本未启动 FastAPI/Phoenix，也未停止既有进程。

三样本方法汇总：`last_active_agent` Agent exact 2/3、step/joint 0/3、distance 36/3、coverage 3/3；`first_explicit_error` Agent exact 1/3、step/joint 0/3、distance 1/1、coverage 1/3；`rule_based` Agent exact 1/3、step/joint 0/3、distance 27/3、coverage 3/3。该结果只证明管线可工作，不代表方法有效。逐 case 表和完整分母规则见 [failure-attribution-mvp.md](failure-attribution-mvp.md)。

## 当前未完全通过或需诚实说明

- PARTIAL：两次完整图演示都完成了真实浏览、截图、checkpoint 和追踪，但最终摘要只写出“SelectorEventLoop 不兼容”，没有把同页的正向结论“Windows 需要 ProactorEventLoop”完整复述；独立真实 WebSurfer 公开站点验收多次准确返回 `Proactor`。适配器已加强“读取完整正向要求”的通用指令，但为避免继续重复收费，没有再执行第三次完整图。这是模型/编排结果质量局限，不伪装为全部语义验收成功。
- NOT RUN：没有登录、表单提交、支付、上传、下载执行、任意内网和生成代码执行测试，因为 V1 明确禁止这些能力。
- token/cost：当前客户端链路未提供可信统一值，结果写 `unknown`，不造数。
- Phoenix trace 深链接未硬编码；当前版本只提供可靠首页和可复制 trace ID。
- 第三方公开站点可能变化、限流或出现 CAPTCHA；此类故障与代码失败应分别判断。
