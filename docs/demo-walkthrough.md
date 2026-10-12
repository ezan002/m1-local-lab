# 导师现场演示指南

核查日期：2026-09-14；代码基点 `a808a04ca60d35e6e2d35ea46441d3ba6e662597`。配套阅读：[项目汇报](advisor-project-status.md)、[Windows 部署指南](windows-deployment-guide.md)。本轮只编写和核对脚本说明，**没有执行下述服务、数据库写入或浏览器演示命令**。

开场可以这样说：“目前完成的是本地工作流与研究评价原型。工作流由 LangGraph 调度，Phoenix 保存观测和评价。今天用离线替身展示工程行为，用外部历史展示归因管线，不把这些演示当作真实模型能力或算法有效性验证。”

## 1. 演示前检查与模式选择

提前完成安装，避免在 5 分钟展示中下载依赖。主界面地址为 `http://127.0.0.1:8000`，高级观测入口为 `http://127.0.0.1:6006`，没有统一启动器。

| 演示 | 运行方式 | 数据 | 模型费用与限制 |
|---|---|---|---|
| A：本地任务、检查点和回放 | 现有验收脚本 `--serve-offline` 显式注入 RuleBasedGateway | 新临时目录的 CSV、运行 SQLite、产物和 Phoenix 数据 | 规定的 FileReader/Coder 路径为零费用；禁止临时改成浏览任务 |
| B：错误归因 | 在已准备好的研究实例上只浏览已有 Dataset/Experiment | 既有获准本地展示的 Who&When/TraceElephant 历史 | 浏览已有结果不调用 predictor、模型或外部任务；不要点击新建任务、Resume、Replay |
| 无既有研究实例时的备用 | 阅读固定结果文档，或提前运行隔离合成验收 | 本地合成 fixture / 聚合文档 | 必须写明合成或历史引用，不能当成真实样本已就绪 |

先在项目根目录记录服务状态。演示 A 仅在四个端口没有监听时开始；若已有研究服务，先进行 B 的只读展示或由其拥有者处理，不能覆盖或终止未知服务。

```powershell
Set-Location -LiteralPath 'C:\projects\m1-local-lab'
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in @(4317, 6006, 8000, 8001) } |
    Select-Object LocalAddress, LocalPort, OwningProcess
```

展示前隐藏凭据编辑器、本机日志和未经审阅的 State/截图。原始研究数据仅限获准本地研究用途，不上传演示材料或打包数据库给他人。API Token、Cookie、个人路径不应出现在投影中。

## 2. 演示 A：本地任务运行与回放

### A0. 前置条件与启动

需要两套已安装环境、空闲标准端口。使用当前 [verify_advisor_release.py](../scripts/verify_advisor_release.py) 中已有但未在 `--help` 显示的 `--serve-offline` 验收入口；这是显式测试入口，不是正式产品的离线启动选项。普通 `start-app.ps1` 需要 Key，不能替代下面命令。

在终端 A 创建唯一的新目录并启动**新的** Phoenix 存储：

```powershell
Set-Location -LiteralPath 'C:\projects\m1-local-lab'
$DemoRoot = Join-Path $env:TEMP ('m1-advisor-demo-' + [guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $DemoRoot) { throw 'Demo directory must be new' }
New-Item -ItemType Directory -Path $DemoRoot | Out-Null
Write-Output "Demo directory for terminal B: $DemoRoot"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-phoenix.ps1 -DataDirectory "$DemoRoot\phoenix"
if ($LASTEXITCODE -ne 0) { throw 'Phoenix launch failed' }
```

按 [部署指南第 5 节](windows-deployment-guide.md#5-普通模式双服务启动) 的健康等待代码等待 6006 `/healthz` 返回 200。这里数据库在新目录；项目启动脚本仍把所有权回执和服务日志写入项目 `data/pids`、`data/logs`，所以不应与另一场 Phoenix 会话共用同一 checkout。

在终端 B 输入终端 A 刚显示的目录，启动离线应用；不更改 `.env`：

```powershell
Set-Location -LiteralPath 'C:\projects\m1-local-lab'
$DemoRoot = Read-Host 'Paste the NEW demo directory printed by terminal A'
if (-not (Test-Path -LiteralPath $DemoRoot -PathType Container)) { throw 'Demo directory not found' }
$env:OPENAI_API_KEY = ''
$env:RUN_REAL_MODEL_TESTS = '0'
$env:LANGSMITH_TRACING = 'false'
$env:PYTHONDONTWRITEBYTECODE = '1'
& '.\.venv\Scripts\python.exe' -B .\scripts\verify_advisor_release.py --serve-offline "$DemoRoot"
```

该入口 `_env_file=None`、显式空 Key、显式 RuleBasedGateway，运行存储在临时目录。启动时写入两行合成 CSV，并完成一次本地 FileReader smoke，随后保持应用前台运行。**仅使用下面明确给出的文件/代码任务**：RuleBased 的默认路由仍可能选择 WebSurfer，它不是任意任务的安全离线沙箱。

在终端 A 检查并打开主页面：

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 10 | ConvertTo-Json -Depth 6
Write-Output (Join-Path $DemoRoot 'fixture.csv')
Start-Process 'http://127.0.0.1:8000'
```

期望 app/phoenix healthy，任务历史至少有一条已完成的文件任务。若应用刚启动尚未就绪，等待终端 B 后重试健康检查。失败时不改成普通真实模型入口“救场”。

### A1. 新建文件任务、观察执行

| 操作 | 应看到什么 | 向导师解释 |
|---|---|---|
| 选择“任务运行”，查看启动时创建的历史任务 | completed、事件、检查点和 Trace ID | 这是本机本次执行产生的轨迹 |
| 新建任务，文本填写 `Read the release fixture file` | 新 run 进入列表 | 本演示明示使用测试 gateway |
| 在允许文件输入框填写终端 A 输出的 fixture.csv **完整路径**，提交 | Planner → Orchestrator → Worker → Finalizer 的状态与事件 | Worker 为真实本地 FileReader，其他模型输出由规则替代 |
| 打开事件页签，观察 node 事件与终态 | SSE 事件序列；完成后停止持续接收 | 数据库事件游标支持补传和去重，不依赖手工刷新 |
| 刷新页面，再选择该历史运行 | 同一运行仍存在，事件和状态可读 | SQLite 保存状态，浏览器不是状态所有者 |

离线过程可能非常快，四组节点动画不保证停留足够久；完成后的事件序列是可信备用，不为放慢演示而临时改代码或调用模型。输出可能带 `Offline deterministic result:`，它不是实际语言模型总结。

### A2. 证据、引用、截图和产物

打开证据页签，展示真实输出。FileReader 路径只读取允许文件，**不生成网页引用或浏览器截图**；应解释对应空态。不要用无关图片填充截图栏，也不把终端截图当成 Agent 浏览证据。

若需演示可下载产物，再创建一个任务：`Draft code for a UUID validation function`，允许文件保持空白。它会选 Coder，展示 Markdown 产物和未执行标记；RuleBased 模式只返回确定性文本，不能承诺生成可工作的 UUID 实现。Coder 的生成内容从未执行，也没有 ComputerTerminal。

仅在事先审阅过安全的既有真实运行时，可额外展示它自己的截图/引用，并明确这是**历史证据，非今天重新浏览**；无安全历史时保留空态即可。

### A3. Checkpoint、State、Pause 与 Resume

打开 Checkpoint，观察 next、step、状态等摘要；打开 State 查看当前安全合成任务的 JSON。历史检查点列表与“当前 State JSON”是不同视图，不宣称点击任意历史行就展示该时刻全部原始 State。

Pause 必须在运行中请求，只有到 Pause Gate 等安全控制边界后才暂停；不能撤销已经发生的工具动作。暂停后 Resume 应继续同一 run；completed 任务不能 Resume。RuleBased 任务很快，**现场点击暂停不稳定**，应在准备阶段确认能否操作，不能把未点击成功说成测试失败或伪造暂停状态。

稳定备用：展示本轮已通过的确定性测试 `test_background_admission_is_atomic_and_pause_survives_restart`。如现场确需重新演示该行为，以下定向测试使用可控 GatePlanningGateway、临时数据库，不调用 WebSurfer/模型，也不需要启动新的 Phoenix：

```powershell
Set-Location -LiteralPath 'C:\projects\m1-local-lab'
$env:RUN_REAL_MODEL_TESTS = '0'
$env:OPENAI_API_KEY = ''
$env:LANGSMITH_TRACING = 'false'
$env:PYTHONDONTWRITEBYTECODE = '1'
& '.\.venv\Scripts\python.exe' -B -m pytest -q -p no:cacheprovider tests/test_runtime_offline.py::test_background_admission_is_atomic_and_pause_survives_restart
```

说明：“这里证明暂停请求、进程重启与恢复语义，属于确定性测试，不是慢速真实模型现场任务。”本轮已经通过，无需为了汇报重复执行。

### A4. Replay 与 Fork

| 操作 | 预期 | 注意 |
|---|---|---|
| 在已完成任务历史中选一个已初始化、next 为 pause_gate/file_reader/coder 的可继续检查点，点击 Replay | 新 run、新 branch，同一 thread；旧 run/head 保留 | 不选择最初未初始化检查点或终态；服务器会拒绝非法来源 |
| 回到原任务，选择同类检查点，点击 Fork | 新分支先 paused，包含 parent 血缘 | Fork 不立即执行 |
| 在 Coder Worker 前的检查点修改 worker_instruction，例如 `Review the artifact without execution` | 白名单改动记录在新分支 | 只有 plan 与 worker_instruction 可改；选择过早检查点可能被后续规划覆盖 |
| 对 Fork 分支 Resume，查看新产物与历史 | 新分支继续执行，原任务仍可查看 | FileReader 不依赖该指令生成代码；想展示指令效果应选 Coder 边界 |

强调：“Replay/Fork 使用 LangGraph checkpoint 和 runtime，Phoenix 展示其观测血缘。Replay 不是播放旧视频，而是重新执行后续节点。离线这里零费用；普通模型模式可能再次计费。”

若 UI 出现 409/422，记录检查点 next 和运行状态，选择合法来源重试，不改数据库或关闭并发保护。没有合适 checkpoint 时使用已通过的 [Replay/Fork 回归](../tests/test_runtime_offline.py) 和主报告解释。

### A5. Trace 与职责边界

复制当前运行 Trace ID，使用 Phoenix 按钮打开 6006；选择 `advisor-release-smoke` 项目，在 Phoenix 中按对应 Trace/运行关联定位。主页面按钮目前是 Phoenix 入口，不保证自动跳到该 Trace 的详情。

展示根 `m1.run`、控制/工具节点、`m1.checkpoint.commit` 及关联字段。同一 run 恢复后可能出现多个 Trace，UI 提供的是最新关联 ID。“LangGraph 保存执行状态并决定下一步；Phoenix 保存观察记录，不执行回滚。离线测试出现 LLM 类型 Span 是语义分类，不是发生了付费模型调用。”

若 Phoenix 暂时不可达，任务与检查点仍可展示；健康正常也不代表 Span 已经持久化。既有隔离回执验证过 27 个 Span 重启后保存，本次若没有重做该验证，只说“引用既有验收结果”。Phoenix GitHub 请求错误是已知限制，展示其影响范围，不宣称页面零错误。

### A6. 结束与保留证据

终端 B 按 Ctrl+C，等待应用退出。终端 A 执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-phoenix.ps1
if ($LASTEXITCODE -ne 0) { throw 'Stop/ownership check failed; do not kill unknown processes' }
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in @(4317, 6006, 8000, 8001) } |
    Select-Object LocalAddress, LocalPort, OwningProcess
```

确认恢复初始无监听状态。保留本次临时目录便于核对，不执行递归清理命令，不覆盖原研究数据库。费用为 0 的前提是使用以上显式 RuleBased 文件/代码任务，没有切换普通模型模式或执行浏览任务。

## 3. 演示 B：错误归因数据集

### B0. 前置条件

优先使用已存在且获准本地展示的研究实例：Phoenix 和应用已由拥有者启动，主页面读取的 Dataset/Experiment 对应既有三案例 MVP。需要在演示前只读确认实例中确有一个 Who&When 和一个 TraceElephant 案例；本轮没有查询研究数据库，故不能保证某个现存实例此刻包含它们。

**新 clone/源码 ZIP 不包含研究数据库或原始数据，演示 A 的新 Phoenix 也不含真实研究案例。** 若实例未准备好，B 的“真实历史交互演示”标记 BLOCKED，转用下面的文档/合成备用，不自动下载、导入或重跑 benchmark。A、B 使用不同数据来源，不能把 A 的临时实例改指向现有库后继续无差别写入。

### B1. 操作、预期和讲解

| 步骤 | 页面操作与预期 | 讲解要点 |
|---|---|---|
| 1 | 打开 8000，切换“错误归因”；查看 Dataset/Experiment 名称和案例数量 | 默认是 MVP，不是自动生成的完整 benchmark 仪表盘；9 行是三例×三方法 |
| 2 | 来源筛选 Who&When，方法选择 last_active_agent，打开手工样本详情 | 对比 Prediction、Ground Truth、Evaluation；展示 agent 正确但步骤错误的区别 |
| 3 | 切换同样本的 first_explicit_error | 既有该例为 abstain；没有显式错误不能硬猜结果。弃答不是正确预测 |
| 4 | 切换 rule_based，查看解释和时间线 | 解释来自固定规则，不是新模型诊断；confidence 不是校准概率 |
| 5 | 来源切换 TraceElephant，选择截断筛选，打开详情 | 原 31 步、保留 20 步；展示标签/预测位置及截断提示 |
| 6 | 比较同例三种方法的 agent 与 decisive step | 正确 Agent 不等于正确错误步骤；该例标签仍保留，所以不能仅因截断一律排除评分 |
| 7 | 展示 provenance 和版本信息 | imported_historical；executed_by_m1_local=false；external_task_executed=false |
| 8 | 清空筛选，再制造一个无匹配组合后恢复 | 空结果不是接口失败；若服务 503，使用重试和清晰错误提示 |
| 9 | 如需完整实验记录，另开 Phoenix 6006 的 Dataset/Experiment | 展示已有预测和 Evaluation；本地页面不执行归因算法 |

以下预期来自 [MVP 既有验收](failure-attribution-mvp.md)，不是本轮重新运行：Who&When 手工例标签为 WebSurfer/12，last_active_agent 为 WebSurfer/28，first_explicit_error 弃答，rule_based 为 Orchestrator (thought)/5。TraceElephant 例标签步骤为 5；last_active_agent/rule_based 指向 20，first_explicit_error 指向 6，Agent 均指向 WebSurfer。应先按实际实例核对，不按过时数字强行解释另一版本样本。

页面上的 Step 标识遵循原来源：Who&When 是零基历史索引，TraceElephant 是原 step_id，不为了对齐标签而整体加减 1。标签不进入 predictor 的 inference 参数，UI 是预测结束后把二者分区展示。

### B2. 30 样本研究进展如何讲

打开 [固定 benchmark 聚合](results/attribution-benchmark-v1-summary.json) 或 [项目汇报第 9 节](advisor-project-status.md#9-数据集与错误归因进展)，不点击运行 benchmark：

- 30 个独立案例，三种冻结方法产生 90 个预测；Dev 20、Frozen Test 10，后者结果已查看，不能称为严格盲测。
- 合并 Agent exact 为 16/90，Step exact 为 6/87，coverage 为 76/90；缺失/不可评分步骤与弃答必须分开处理。
- 一个步骤标签不在保留历史中，导致 step 分母每种方法少 1；abstain 在可评分 accuracy 中计错，distance/选择性 accuracy 才使用另一分母。
- MVP 的 9 个 run/72 条评价，与 benchmark 的 90 个 run/1,170 条评价不同；“合成演示有 9 行”也不代表九个真实案例。
- 这些结果证明数据处理、固定预测和评价展示可复现；小样本、简单规则和已查看的测试结果不足以证明算法有效或泛化。

### B3. 失败备用方案与结束

| 问题 | 备用方案 |
|---|---|
| 全新电脑没有真实数据集 | 明确真实数据演示 BLOCKED；使用上述既有汇总文档，不复制原始数据或数据库补齐 |
| Phoenix 未运行、503、默认实验不存在 | 展示只读错误状态；请实例拥有者核对配置，不当场执行下载/导入脚本 |
| 只需展示 UI 交互且允许新临时数据 | 提前按部署指南运行 `verify_advisor_release.py --output`；它使用三例本地合成 fixture，并生成临时截图/回执，结束后自动停服 |
| 合成验收可用而真实样本不可用 | 展示合成回执或合成截图，标注 synthetic-release-fixture；其两步时间线与标签 99 不等于真实样本步骤 |
| 页面出现历史结果差异 | 停止报出固定样本数值，先核对 dataset/experiment/source revision；不改标签或指标 |
| Phoenix popup 报 GitHub fetch 错误 | 保留 PARTIAL，解释本地 API 与上游 UI 外部请求的范围，不临时放开全部网络 |

只浏览已有归因 GET 页面不会运行 predictor 或模型；不点击真实任务控制，不执行第三方 Agent，也不创建实验。B 若借用已运行研究服务，结束时仅关闭自己的浏览器页，不停止别人启动的服务。若为此另行启动服务，由启动者按部署指南核对所有权后关闭；不删除研究数据。

## 4. 五分钟快速演示

服务与数据必须提前准备；五分钟内不切换数据库、不安装依赖。若只有 A 或 B 已就绪，另一部分使用对应文档说明。

| 时间 | 内容 |
|---|---|
| 0:00–0:40 | 用架构图说明目标、LangGraph/Phoenix 分工与“原型”定位 |
| 0:40–2:00 | A 已完成文件任务：历史、事件、证据空态、Checkpoint；说明 RuleBased 不收费 |
| 2:00–2:50 | 展示预先准备的 Replay/Fork 分支和 Trace ID；手动 Pause 不稳定时引用确定性回归 |
| 2:50–4:20 | B 已有两类样本或聚合文档：预测/GT、弃答、截断与 provenance |
| 4:20–5:00 | 总结边界：九项已证实 P1 已修复、工程回归通过；研究有效性、产品整合和导师物理电脑验收仍待完成 |

## 5. 十五分钟完整演示

| 时间 | 内容 |
|---|---|
| 0–2 分钟 | 架构、原始目标、当前 PASS/PARTIAL；说明今天没有真实模型调用 |
| 2–5 分钟 | A 新建文件任务，查看四组节点、SSE、历史、输出与空态 |
| 5–8 分钟 | Checkpoint、Replay、Fork/Resume；Pause 采用已预演成功的点击或定向测试备用 |
| 8–10 分钟 | Coder 未执行产物、Trace ID、Phoenix 关联与独立进程边界 |
| 10–13 分钟 | B Who&When 与 TraceElephant 的方法、标签、评价、时间线和截断 |
| 13–14 分钟 | 固定 30×3 分母、已查看 Frozen Test 和研究局限 |
| 14–15 分钟 | 下一阶段三件事，说明服务清理与源码交付方式 |

A/B 若需要两套存储实例，切换应在预演时规划；时间不足就用 B 文档备用，不在台上搬迁数据库。完整演示结束后执行 A6，并确认端口恢复初态。

## 6. 导师可能提出的十个问题

| 问题 | 准确回答 |
|---|---|
| 这是微软官方 Magentic-One 吗？ | 不是完整官方团队部署；采用其规划/协调风格，唯一调度器是 LangGraph，官方组件是 AutoGen MultimodalWebSurfer。 |
| 有哪些 Agent？ | 图有 Planner、Orchestrator、Pause Gate、WebSurfer、FileReader、Coder、Finalizer 七个节点；Pause Gate/FileReader 是控制或工具节点，不是七个官方 Agent。 |
| Coder 会执行代码吗？ | 不会，只写未执行 Markdown 产物；没有 ComputerTerminal。FileReader 也不是官方 FileSurfer。 |
| Phoenix 能让任务回滚吗？ | 不能。runtime 与 LangGraph checkpoint 实现 Pause/Resume/Replay/Fork；Phoenix 负责观察关联。外部副作用不自动回滚。 |
| 今天的任务证明模型能力了吗？ | 没有。RuleBasedGateway 只证明本地工程链路；本轮两项真实模型测试按条件跳过。 |
| 为什么还要打开 6006？ | 8000 是任务和归因主界面；完整 Trace/Span 仍在独立 Phoenix，统一启动和全部内嵌尚未完成。 |
| 这些 Who&When 轨迹是项目跑出来的吗？ | 不是，是带固定 provenance 的外部历史；本地只做适配、确定性预测和评价，没有重新执行原 Agent。 |
| 三种基线哪个最好，能证明有效吗？ | 固定样本中指标各有差异，但 30 例与简单规则不足以证明有效性；不能凭一个分数做泛化结论。 |
| Frozen Test 是严格盲测吗？ | 不是，基线结果已被查看。v1 保持冻结；新算法需要另行设计并版本化评估，不能为了分数调整旧协议。 |
| 可以直接交给我电脑运行吗？ | 有隔离 Windows 新环境验收证据和双服务指南，但没有现成 remote URL；可按获准本地源码交付安装。真实数据不随包附带，物理电脑与上游 Phoenix 页面问题仍需逐项确认。 |

演示证据引用：[离线运行测试](../tests/test_runtime_offline.py)、[API 测试](../tests/test_api.py)、[前端行为测试](../tests/frontend_behaviors.cjs)、[归因读取测试](../tests/test_attribution_read_model.py)、[历史服务与 Chromium 验收](full-project-audit-optimization.md)。未重新运行的浏览器、部署和研究结果均按“引用既有验收结果”处理。
