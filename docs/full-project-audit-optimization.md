# 全仓工程审查与审慎优化

## Advisor release blocker resolution：当前交付结论

本节更新于 2026-09-13（美国东部时间；核验回执跨至 UTC 2026-09-14），覆盖后文上一阶段快照中的状态。沿用 `feature/full-project-audit-optimization`，没有重新开展全仓审查。开始时的22个变更路径已逐文件备份和保存 SHA-256；在其基础上增量修改，没有撤销先前修复。备份位于本机临时目录 `m1-advisor-release-ahez3xgz/before/`。

| 判定项 | 状态 | 证据与范围 |
|---|---|---|
| 工程回归 | **PASS** | 本阶段一次完整离线回归：130 passed、2 skipped，退出0；51个 Python 文件 AST 无语法错误，PowerShell 解析测试通过 |
| 安全 | **PARTIAL** | 三个已证实的 P1 阻塞项已修复并验证；WebSurfer 动作约束仍是风险建议，既有数据库未重新脱敏，凭据过滤不等于全部个人信息检测 |
| 新电脑部署 | **PASS（隔离 Windows 模拟）** | 新应用130/130、新 Phoenix 77/77锁依赖匹配，两环境 pip check 通过；新临时数据库完成健康检查、零费用运行及重启持久化。未在导师另一台物理电脑执行 |
| 浏览器验收 | **PARTIAL** | 项目 Chromium 已真实运行；应用页面 PASS，Phoenix 跳转可用，但其 GitHub 星标请求被隔离策略阻止后出现 console/page/request 错误，整体严格验收退出1 |
| 导师可部署性 | **PARTIAL** | 标准安装、项目启动入口和隔离演示已具备证据；普通模型模式启动需非空 API Key，真实模型/WebSurfer未验收，Phoenix 页面仍有外部资源问题 |

### 全部 P0/P1 关闭情况

| 等级/项目 | 当前分类 | 验证或残余边界 |
|---|---|---|
| P0 | 未确认缺陷 | 没有把未覆盖区域写成安全保证 |
| P1：进程内恢复/Replay/Fork 并发准入 | 已修复且有测试证据 | `test_control_admission_covers_awaits_and_all_checkpoint_writers`；完整回归通过 |
| P1：暂停通知覆盖 execution/checkpoint link | 已修复且有测试证据 | `test_pause_preserves_real_execution_links_and_input_checkpoints` |
| P1：OTel 异常/凭据导出泄漏 | 已修复且有测试证据 | `tests/test_observability.py` 的 exporter 边界测试；本轮另补引号密码与 JSON 键名用例 |
| P1：运行错误原文进入元数据/SSE | 已修复且有测试证据 | `test_exception_credentials_do_not_enter_current_error_or_sse` |
| P1：Windows 安装路径/退出码/引号 | 已修复且有测试证据 | `tests/test_windows_install.py`；新增四种 `PIP_NO_INDEX` 值的失败即停与环境保留测试 |
| P1：冻结协议可漂移 | 已修复且有测试证据 | `test_protocol_rejects_semantic_drift`；本轮冻结配置与结果未变 |
| P1：跨进程运行所有权 | **本轮关闭，已修复且有测试证据** | `src/m1_local/runtime_ownership.py`、`runtime.py`、`api.py`；`tests/test_runtime_ownership.py` 六项测试 |
| P1：Phoenix gRPC 通配监听 | **本轮关闭，已修复且有测试证据** | `scripts/serve_phoenix.py`、start/stop脚本和两个旧验收启动旁路；6项 launcher 测试及真实监听/HTTP写入/重启验证 |
| P1：外部历史导出脱敏不足 | **本轮关闭，已修复且有测试证据** | `external_export.py`、`external_smoke.py`、两个归因日志模块及调用脚本；5项安全视图测试和2项预测导出测试 |
| P1：WebSurfer 行为约束弱于文档原声明 | **仅为风险建议，未修复** | 未运行越权场景；HTTP method、DNS变化、service worker、表单/上传等边界仍需独立设计和验收 |

三个优先项的实现与语义影响：

- **进程所有权**：metadata/checkpoint 的规范化路径分别持有操作系统文件锁，按固定顺序获取；恢复旧状态前必须获得两把锁。正常退出、初始化失败及进程死亡释放锁，锁文件保留而不删除。第二个 runtime 无法改写活跃记录；未持有所有权的 pause 也拒绝。API lifespan 异常使用 finally 释放资源，关闭等待已准入的 checkpoint 写入完成。测试覆盖共享单个/两个数据库、失败启动、真实子进程互斥、强制终止后重新获取以及 lifespan 异常。保留单运行与 LangGraph 调度语义；API 启动期间同库 CLI 会明确失败。该协议要求参与进程使用本版本，不是阻止任意程序直接编辑 SQLite 的安全沙箱。
- **Phoenix gRPC**：锁定版本11.35.0没有独立 gRPC host/disable 配置。项目 wrapper 核对版本、类绑定和构造签名，在进程内强制禁用未使用的 gRPC 上下文，保持 HTTP 非 read-only。没有修改第三方安装文件或依赖版本；不兼容时拒绝启动。两个旧 `python -m phoenix.server.main serve` 旁路已改用 wrapper。停止脚本核对 PID、启动时间、解释器及入口脚本后结束该进程树。真实服务只出现 `127.0.0.1:6006`；静态上游横幅列出的4317不作为监听证据。独立 gRPC 客户端不再受支持，项目现有出口均是 HTTP OTLP。
- **导出安全视图**：`safe-export-v1` 复制数据后清理任务、历史展示文本与预测解释；标签、身份、revision、split、hash、step标识保持原值，敏感命中则在写入前拒绝。Dataset create/add、benchmark Dataset、SDK `run_experiment` 的内存 Dataset 副本、两个预计算归因日志出口均接入。SDK 自动实验 spans 的内容也是安全副本，不能解释为重跑外部任务。原始 record/inference/labels/predictions不被修改，原数据库内容不回填。新的 export metadata 明确区分展示视图与冻结研究输入。

### 隔离 Phoenix 安装结果

继续使用原隔离 `m1-audit-clean-7vv7te23/checkout with spaces`，没有重装应用130项依赖。

首次补齐命令为隔离 `.venv-phoenix/Scripts/python.exe -m pip install -r requirements-phoenix.lock.txt`：31.141秒、pip退出1、未达到监督上限，日志确认 `ProxyError/NewConnectionError`。随后有一次沙箱外申请在确认执行前被中止，没有将其记作安装运行或超时。

最终成功命令（在该隔离 checkout 中，使用其新 Phoenix Python）：

```powershell
& '.\.venv-phoenix\Scripts\python.exe' -m pip install --disable-pip-version-check --no-cache-dir -r '.\requirements-phoenix.lock.txt'
```

监督器只给安装子进程传入 `PIP_NO_INDEX=0`、`PIP_NO_CACHE_DIR=1`、`PIP_NO_COMPILE=1`，上限 **3600秒**；没有改用户/系统级环境变量。**实际84.375秒，pip退出0，timed_out=false；77项匹配，缺失0、错版0。** 两隔离环境随后各自 `pip check` 均退出0。setup脚本自身会检测禁用索引并明确报错，不会静默改为0。

准确绝对 argv、工作目录、起止UTC时间、PID、退出码、匹配清单保存在隔离父目录的 `phoenix-completion-root/started.json` 与 `result.json`；全日志和最后200行也保留。首次失败回执位于 `phoenix-completion-20260913T221402Z-aaf66474/`。没有把可能含索引配置的原始安装日志加入 Git；本次没有触及60分钟上限，也没有证据把成功安装误归因于版本冲突或构建失败。

### 实际服务、Chromium 与测试证据

使用 `scripts/verify_advisor_release.py --output <新的临时目录>`，从隔离 checkout 启动自己的 Phoenix/API，写入3个本地编写的合成归因案例、9条预计算预测和1条 RuleBasedGateway 文件任务。它们不是 Who&When 30样本结果，也没有读取原项目 data/。新服务的每次启动、重启、停止均使用项目入口，原有数据库和历史运行未触碰。

最终回执：临时目录 `m1-advisor-release-ahez3xgz/acceptance-3/result.json`。

- Phoenix `/healthz` 200；FastAPI `/api/health` 的 app/phoenix 均 healthy；RuleBasedGateway运行 completed。
- HTTP OTLP 接收到该运行的 **27个 span**，停止并重启 Phoenix 后 span数量与trace身份一致。运行ID、trace ID、监听PID在回执中，截图与日志均为合成数据。
- 项目隔离 Chromium 1181，1366×768，真实检查任务历史与checkpoint、归因9行/3种方法、来源/方法/状态/截断筛选、空结果、详情、2步时间线、预测/真实分区、键盘主标签、横向溢出及真实 Phoenix popup。
- 应用页面 console error/page error/request failure 均0；整体意外4xx/5xx为0。Phoenix popup：1条console error、1条page error、1个失败的GitHub请求。验收器阻止了 `https://api.github.com/repos/Arize-ai/phoenix`，未放行该外部请求。**整体PARTIAL，验收脚本退出1**，不是浏览器环境不可用。
- 证据截图：`task-history.png`、`attribution-detail.png`、`attribution-overview.png`；已查看归因详情截图，合成script文本显示为文本，未执行。
- 首次服务验收遇到 Windows PowerShell 子进程继承 PIPE 导致日志采集等待超时；改为文件输出后完成验证。第二次浏览器用例快速连续切换筛选造成一次预期取消，最终用例逐步等待响应，第三次仅余上述上游GitHub请求。所有失败回执保留，没有将失败写成PASS。
- 定向验证：所有权/安装13 passed；launcher 6 passed；导出/adapter/benchmark/OTel 38 passed；预测日志出口4 passed；补充所有权/API/launcher检查32 passed。以上集合部分重叠，不能相加作为独立测试数量。
- 代码完成后只运行一次完整套件：**130 passed, 2 skipped in 4709.14s，exit0**。这里记录pytest墙钟回执，不把它当作性能基准。两项skip仍是真实模型/WebSurfer。51个Python AST及4个PowerShell解析无错误。
- 10个受保护文件仍与HEAD一致；5个来源文件receipt通过；只读重算30 cases/90 predictions/90 evaluations，summaries及confusion matrix逐值等于既有JSON，30个export examples也通过。Frozen Test已被查看过，本次不是盲测或调分。

### 新增未关闭事项与导师命令

**P2，已证实但未修改：Phoenix GitHub星标组件。** 锁定包 `phoenix/server/static/assets/components-CdQiQTvs.js` 的 `X3()` 无条件fetch GitHub，未读取 `allow_external_resources`。当前严禁外部请求的浏览器验收会阻止它并报告错误；常规启动没有因此自动获得出站隔离。没有修改第三方压缩bundle。后续应验证上游修复版本或单独设计受测试的静态资源方案；目前保留PARTIAL。

普通 `start-app.ps1` 使用原有 `OpenAIModelGateway`，不是验收器的RuleBased模式。新应用环境只构造该gateway、没有请求模型时，空Key会报 `OpenAIError: Missing credentials`。这是必须明确的部署前提：导师正常启动需要在本机配置非空Key；本轮未验证Key有效性，未改模型代码/配置。无Key的零费用验证请使用隔离验收命令，不能把两种启动条件混写。全新clone不包含受忽略的研究数据，归因页的真实研究数据需另行按本地研究协议准备。

确认提交后，导师可从有权限的仓库URL或本地仓库副本执行以下命令。本工作树目前尚未提交，当前HEAD的clone不会包含这些修复。

```powershell
# 需要 Git、Python 3.11 x64；把源填写为有权限的仓库URL或本地仓库路径。
$Source = Read-Host 'Repository URL or local repository path'
git clone --branch feature/full-project-audit-optimization "$Source" 'C:\projects\m1-local-lab'
if ($LASTEXITCODE -ne 0) { throw 'git clone failed' }
Set-Location -LiteralPath 'C:\projects\m1-local-lab'
py -3.11 --version
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 is required' }
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location).Path 'browsers'

# 这里显式授权安装子进程访问索引；不改变当前终端或用户/系统级PIP_NO_INDEX。
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command '$env:PIP_NO_INDEX="0"; & .\scripts\install.ps1'
if ($LASTEXITCODE -ne 0) { throw 'installation failed' }
& '.\.venv\Scripts\python.exe' -m pip check
if ($LASTEXITCODE -ne 0) { throw 'app dependencies are inconsistent' }
& '.\.venv-phoenix\Scripts\python.exe' -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Phoenix dependencies are inconsistent' }

if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
# 在编辑器中填写自己的OPENAI_API_KEY；保留已有模型设置，不把密钥贴进命令行。
notepad.exe .env
# 保存后继续；若设置过PHOENIX_SQL_DATABASE_URL，需先显式取消该覆盖。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-phoenix.ps1
if ($LASTEXITCODE -ne 0) { throw 'Phoenix launch failed' }
Start-Process 'http://127.0.0.1:8000'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-app.ps1
```

最后一条命令以前台方式运行应用；浏览器如先打开而服务尚未就绪，待终端显示启动完成后刷新。停止应用按Ctrl+C，再执行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-phoenix.ps1`。打开网页和初始化客户端不等于调用模型；本轮没有代导师创建真实任务。

零费用隔离验收（仅在没有现存6006/8000/8001/4317监听时执行，必须给新目录；当前上游问题会得到PARTIAL/exit1）：

```powershell
$Acceptance = Join-Path $env:TEMP ('m1-advisor-acceptance-' + [guid]::NewGuid().ToString('N'))
& '.\.venv\Scripts\python.exe' -B .\scripts\verify_advisor_release.py --output "$Acceptance"
Get-Content -LiteralPath (Join-Path $Acceptance 'result.json') -Encoding UTF8
```

### 当前费用、清理与Git

项目付费模型调用0、真实WebSurfer0、外部Agent任务执行0、研究数据集下载0、原始数据外传0。执行的是已授权的包安装、项目脚本、Phoenix、Chromium和本地合成验证；Phoenix页面的一次GitHub请求尝试被浏览器隔离器阻止。Codex会话自身额度不属于项目模型调用统计。

最终验收回执 `remaining_listeners=[]`：4317、6006、8000、8001没有遗留监听；本任务启动的Phoenix/API/Chromium已关闭。保留隔离安装日志、合成数据库及截图供复查，没有删除原项目数据。

最终收尾扫描了80个当前跟踪/未跟踪文件，以及4个可达提交的112个唯一blob（本次重新枚举的实际数量；后文87是旧回执的记录）。未发现可确认的真实凭据；唯一凭据形态命中位于 `scripts/verify_runtime_trace.py:31`，已核实为合成脱敏测试值，报告不显示值。当前文件没有个人用户绝对路径/邮箱命中，也没有数据库、原始数据、日志、PID、缓存或虚拟环境路径被纳入版本控制。历史README/安装脚本仍含个人用户路径，提交元数据有1个作者身份，均未打印值或改写历史。该模式扫描不能证明不存在任何秘密或个人信息。结果保存在临时目录的 `final-safety-scan.json`。

22个起始变更路径的备份及当前文件全部存在，且仍在Git变更列表中；原日期版草稿逐字节等于起始备份。10个冻结/配置/结果/锁文件再次与HEAD比较一致。隔离 checkout 与当前41个运行源码、脚本和网页文件逐一比较（仅归一化换行），缺失0、差异0，确认实际服务验收运行的是当前实现。回执为 `final-preservation-check.json`。最后一次只读进程查询仅匹配该查询自身的短期PowerShell，没有隔离目录的Python/Node；四个目标端口的LISTENING结果仍为空。

当前HEAD仍为 `1c1441c91cd6f4066d00658f628ed78e33e53f68`，分支仍为 `feature/full-project-audit-optimization`。当前39个变更路径（26个已跟踪修改、13个未跟踪），暂存0；`git diff --check`通过。两个既有tag的引用未变，remote仍为空；未提交、未创建tag、未添加remote、未push，等待用户确认。

`git diff --stat` 的已跟踪部分如下；**849行新增、174行删除，不包括13个未跟踪文件**：

```text
 README.md                                  |  27 ++-
 docs/acceptance.md                          |   2 +
 docs/architecture.md                        |   6 +-
 docs/attribution-benchmark-v1.md             |  10 +-
 scripts/install.ps1                        |  50 ++++-
 scripts/start-phoenix.ps1                  |  14 +-
 scripts/stop-phoenix.ps1                   |  21 ++-
 scripts/verify_attribution_benchmark_v1.py |   8 +-
 scripts/verify_attribution_ui.py           |   4 +-
 scripts/verify_external_dataset_smoke.py   |   5 +-
 scripts/verify_failure_attribution_mvp.py  |   6 +-
 src/m1_local/api.py                       |  41 +++--
 src/m1_local/attribution_benchmark.py      |  49 ++++-
 src/m1_local/attribution_evaluation.py     |  19 +-
 src/m1_local/attribution_protocol.py       |   4 +
 src/m1_local/external_smoke.py             |   8 +-
 src/m1_local/observability.py              |  22 ++-
 src/m1_local/runtime.py                   | 285 +++++++++++++++++++----------
 tests/test_api.py                         |  44 ++++-
 tests/test_attribution.py                 |  12 ++
 tests/test_attribution_benchmark.py       |  50 +++++
 tests/test_attribution_protocol.py        |  18 ++
 tests/test_external_datasets.py           |   7 +-
 tests/test_observability.py               | 117 ++++++++++++
 tests/test_runtime_offline.py             | 153 ++++++++++++++++
 web/app.js                               |  41 ++++-
 26 files changed, 849 insertions(+), 174 deletions(-)
```

13个未跟踪文件及用途：

- `docs/full-project-audit-2026-09-12.md`：保留起始草稿；`docs/full-project-audit-optimization.md`：当前报告及历史回执。
- `src/m1_local/runtime_ownership.py`、`tests/test_runtime_ownership.py`：跨进程数据库所有权与失败/崩溃恢复验证。
- `scripts/serve_phoenix.py`、`tests/test_phoenix_launcher.py`：只启用回环HTTP的受版本约束入口及拒绝不兼容配置的验证。
- `src/m1_local/external_export.py`、`tests/test_external_export.py`：外部历史导出副本及身份/标签不变的验证。
- `src/m1_local/telemetry_export.py`：保留此前OTel出口脱敏，并补强引号与JSON键的凭据处理。
- `tests/frontend_behaviors.cjs`、`tests/test_frontend.py`：保留此前分页、事件及异步竞争回归。
- `tests/test_windows_install.py`：安装路径、版本、退出码及本轮PIP_NO_INDEX验证。
- `scripts/verify_advisor_release.py`：可重现的临时服务、合成数据、实际Chromium、trace持久化及清理验收；不调用模型。

---

## 上一阶段审查快照（历史记录）

以下保留2026-09-12至第一轮续接的原始结论，包括当时的BLOCKED状态与22文件统计；**当前发布判定以上方Advisor release章节为准**。

这是续接后的最终交付报告。此前21个文件的工作树内容已逐文件保存 SHA-256 指纹，本轮不覆盖、不撤销它们；旧日期版报告作为原始草稿保留。本轮仅新增本文件，最终结论以本文件为准。

审查开始：2026-09-12；续接交付：2026-09-13。结论：已完成有证据支持的局部修复与完整离线回归；隔离新安装的应用部分可验证，Phoenix 依赖安装超时，整套新电脑部署为 **BLOCKED**。仍存在 P1 部署和进程所有权问题，不能据测试通过宣称整个系统已具备严格隔离或可直接交付生产。

## 范围与原始基线

- 根目录：`C:\projects\m1-local-lab`，Windows / Python 3.11，无 Docker。
- 开始分支：`feature/attribution-benchmark-v1`；提交 `1c1441c91cd6f4066d00658f628ed78e33e53f68`。开始时工作树干净，未发现适用的 `AGENTS.md`。
- 工作分支：`feature/full-project-audit-optimization`。未提交、未增加 remote、未 push、未新建或移动 tag。
- 原有 tag：`m1-local-lab-v1.0-smoke`、`m1-local-lab-v1.1-attribution-ui`。
- 阅读 README、架构、验收、三个归因文档、代码与测试；三个子代理分别只读审查执行恢复、遥测/部署、归因协议，主代理统一实施代码变更并复核。
- 原离线基线：`65 passed, 2 skipped in 32.06s`；两环境 `pip check` 通过；41 个原有 Python 文件 AST 解析通过。
- 测试运行使用临时 SQLite、临时产物、RuleBasedGateway、MockTransport 或内存 exporter。未运行会写现存 Phoenix/结果文件的历史验收脚本。子代理早期定向测试使用了新建独立 `data/test-output/audit-protocol-subagent`；其余使用系统临时目录，均未覆盖既有运行或研究数据。

## P0

本次审查没有确认 P0 缺陷。这不等于不存在尚未覆盖的缺陷。

## P1：已修复并验证

| 问题与实现位置 | 原复现证据 | 修复与语义影响 | 回归证据 |
|---|---|---|---|
| `runtime.py:51,255,291,320`：恢复/Replay 准入跨 await，Fork 可并发改同 thread 最新 head | 两个并发 resume 均成功；在 Replay 提交与读取之间插入 Fork，Replay 的 state/run ID 与 Fork 相同 | 所有 checkpoint 写入入口先预留准入，准备失败释放。保持单运行模型；并发请求立即拒绝，不排队重做 | `test_control_admission_covers_awaits_and_all_checkpoint_writers` 三种入口×四类竞争调用 |
| `runtime.py:169`：暂停通知覆盖上一节点的 execution link | `__interrupt__` 复用 orchestrator execution ID，`INSERT OR REPLACE` 把真实关联替换成 input=output 的通知记录 | 通知不创建节点 commit link；实际节点 input ID 来自 snapshot.parent_config。未修改已有数据库行 | `test_pause_preserves_real_execution_links_and_input_checkpoints` 对照真实 snapshot/execution/parent |
| `telemetry_export.py:21,70`、`observability.py:128`：第三方异常与序列化摘要泄漏 | MockTransport 模拟 401，SDK exception event 与 status 包含合成凭据；双层 JSON、resource/link/event-name 也可漏出 | 应用 provider 的唯一出口增加快照脱敏；移除异常 message/stack，保留类型、父子 ID、kind、时间、计量；配置完成前不注册为全局 provider。冻结归因预处理不变 | `test_export_boundary_hides_sdk_exception_and_preserves_parentage`、`test_export_boundary_covers_resource_links_and_event_names`、`test_initialize_replaces_phoenix_exporter_before_instrumentation` |
| `runtime.py` 异常处理：错误原文进入元数据和 SSE | 合成配置密钥与 JSON api_key 原样出现在 run.error/events | 在错误持久化前脱敏并限长；保留错误类型和安全说明 | `test_exception_credentials_do_not_enter_current_error_or_sse` |
| `scripts/install.ps1:1,9,27`：个人路径、原生命令失败后继续、Windows 引号 | 原版依赖作者用户目录；PowerShell 5.1 的 ErrorActionPreference 不检查原生退出码；初版修复的双引号在真实 -c 传参时丢失 | Python Launcher/PATH/显式 PythonExe 发现、3.11 x64 校验、逐条检查 LASTEXITCODE。保留已有环境，安装失败即停。未改依赖版本或模型 | `test_installer_uses_supplied_python_and_stops_before_later_commands` 空格路径/退出码17/版本拒绝；`test_version_probe_works_with_real_python_and_windows_argument_passing` 使用真实解释器 |
| `attribution_protocol.py:23,101`：冻结协议可漂移 | 6/4、14/6 划分及 abstain、normalization、license 修改仍能通过旧校验 | 以现有 canonical JSON digest 拒绝漂移；格式空白/键顺序不影响；不改任何冻结参数 | `test_protocol_rejects_semantic_drift` 五类漂移 |

## P1：仅提出方案，未修改

| 已证实问题/合理风险 | 位置及可复现证据 | 建议与未实施原因 |
|---|---|---|
| **已证实：跨 runtime 所有权冲突** | `runtime.startup()` 调 `storage.mark_interrupted_runs()`；`cli.py:29` 连 history 都会 startup。在同一临时 DB 中将 runtime A 的 planner 挂起，再 startup runtime B，A 的记录变 interrupted，但 A task 仍在运行，B 又能创建运行 | 需要跨进程文件锁/租约及 recovery ownership。当前只允许一个 owner：API 启动期间不要并发 CLI 操作相同数据库。这是机制变化，本轮未仓促实施 |
| **已证实启动行为：Phoenix gRPC 通配绑定；实际 LAN 可达性未验证** | 锁定 Phoenix 11.35.0 的 `phoenix/server/app.py` 创建 GrpcServer；`server/grpc_server.py:108` 使用 `[::]:{port}`。`start-phoenix.ps1` 只设 HTTP host，默认 gRPC 4317 | 需要受支持的 gRPC 回环启动方式或经验证的版本方案。未改第三方包/防火墙。端口0仍会开随机监听，read-only 会阻止研究写入，均不是等价修复。未为本次审查启动 Phoenix |
| **已证实：外部历史 task 导出未全面脱敏** | `external_datasets.py:179,238` 使用 task.strip()；合成 token 可进入 phoenix_example input。旧脱敏函数也不覆盖全部 quoted JSON 形态 | 建议单独定义 Dataset 导出安全视图及研究输入版本。直接改 adapter/共享脱敏会改变冻结输入，因此本轮保持现有预处理，未上传任何数据 |
| **合理风险：浏览器行为约束弱于文档原声明** | `websurfer.py:90` URL route 未限制 HTTP method；非导航公开资源可跨主机；登录/表单/上传/支付主要靠提示词。DNS 检查与实际解析有时间差，service worker 边界未测 | 独立设计严格只读网络/动作策略并用合成页面验收；不更改 Agent 职责，也不以未执行的越权场景冒充实证。本轮只修正文档边界 |

## P2：已修复并验证

| 问题 | 修复位置与可复现证据 | 语义影响 |
|---|---|---|
| 新分支 metadata 丢 dataset_case_id | `runtime.py:310,340` 从所选 snapshot 继承；`test_branch_metadata_and_uninitialized_checkpoint_rejection` | 统一新分支的 run/state/span provenance；不回填历史行 |
| 最初 __start__ 快照误允许 Replay/Fork | runtime 检查已初始化 task；同上测试验证 ValueError | 将原 LangGraph InvalidUpdateError 改为可解释的拒绝；终结快照仍不可回放 |
| 成功恢复后 UI 仍展示旧当前 error | `_execute` 开始时清当前 error；`test_resume_clears_current_error_but_keeps_historical_event` | 原始 error 事件保留，新尝试的当前状态不被旧错误污染 |
| 尚未启动的后台 task 取消后占住准入 | `shutdown` 收尾清理 task/reservation 并标 interrupted；`test_shutdown_before_background_task_starts_releases_admission` | 可正常释放运行资源；不自动重跑 |
| root span 在 flush 后才结束 | `_execute.finally` 统一 flush；`test_run_flush_happens_after_root_span_ends` | 完成/失败/取消都在根 span 结束后交给 exporter，不承诺远端已接收 |
| partial SSE 不关闭、历史只读500条、连接竞争 | `runtime.sse` 增 partial；`web/app.js:227,272,340,370` 分页、终态补读、传输错误过滤、选择 token | SSE ID/事件持久化定义不变；501事件服务器测试及4个 JS 行为用例通过 |
| API 空白任务/错型 Fork/无效游标/IPv6 Host | `api.py:30,46,253,267` 与 host 格式；`test_api.py` 参数化测试 | 无效请求返回422；包括未知 Fork 字段从409改为422。事件游标限制为 SQLite signed 64-bit 非负整数；有效接口保持 |
| manifest 声明与逐例划分不一致 | `attribution_benchmark.load_manifest` 核验真实计数、身份、hash、确定性划分；四种篡改重算 manifest hash 后仍拒绝 | 只增加拒绝条件，不重新选样，不修改 manifest |
| 错误 case/source/revision join 仍评分 | `evaluate_benchmark_prediction` 关联四对象身份及来源；五种错配测试 | 拒绝错误 join；正常分母和评分公式保持不变 |

## P2：仅提出方案与研究局限

- **已证实预算语义问题**：`graph.py:122` 使用 gateway 进程累计 calls，而非持久化每 run 预算。用 `max_model_calls=4` 连续运行两次 RuleBased file task，首次 completed、第二次 planner 后预算失败，重启计数又重置。WebSurfer 内部调用也不能据此当作完整费用硬上限。需先定义每 run/恢复/分支统一计量契约；本轮未改变调度和预算语义。
- **已证实 adapter 索引覆盖**：`external_datasets.py:131` 的 `{trace_index:index, **item}` 可被来源 trace_index 覆盖；单步 history 传 trace_index=99，会使 step0 不可评分。30 个冻结样本均无此来源字段。为保持预处理版本，列为下一版 adapter 修复，不动既有标签/结果。
- `safe_json_summary`、`redact_sensitive` 的历史文本裁剪仍保留。30/30 样本存在内容裁剪，manifest 的 trace_truncated 只反映步骤保留，不能作为内容无损指标。
- predictor 可用任务标准答案 expected_answer；责任 Agent、决定性 step、失败原因才是 evaluator-only 标签。结构化 guard 不能证明历史自然语言没有透露标签。
- Frozen Test 结果已经被查看。本次 30×3 内存重算是完整性核验，不是新的盲测、方法开发或调参。当前结果仅描述三种简单基线，不能证明算法有效或泛化。
- manifest 的 hash 是内部一致性校验，不是签名认证；固定来源/hash/既有选择逻辑仍是来源可信性的前提。
- 两个 lock 精确固定版本，但没有包内容哈希；build backend 未完全锁定。没有擅自升级库或增加依赖。
- 文件白名单检查解析后的真实路径能挡目录逃逸；resolve 后 is_symlink 不能实现字面上的“拒绝所有符号链接”。尚未确认目录白名单绕过。
- 代码样式、文件长度、命名和前端框架选择属于风格偏好，没有据此大规模重构。

## 功能—代码—测试对应

| 功能 | 实现 | 验证 |
|---|---|---|
| 唯一图调度、文件/coder worker、预算失败 | graph.py、models.py、security.py | test_runtime_offline.py、test_config_security.py |
| checkpoint、暂停、重启恢复、Replay/Fork | runtime.py、storage.py、state.py | test_runtime_offline.py、test_api.py |
| OTel 父子/kind/关联/脱敏/flush | observability.py、telemetry_export.py、graph.py | test_observability.py；内存 Phoenix exporter 集成 |
| API Host/Origin/CSRF、参数与 SSE | api.py、runtime.py | test_api.py、runtime 的501事件测试 |
| 网页分页/去重/终态/异步竞争 | web/app.js | frontend_behaviors.cjs，经 test_frontend.py 调用；不是浏览器视觉验收 |
| Windows 安装及参数传递 | scripts/install.ps1 | test_windows_install.py，空格目录、真实 Python、脚本语法 |
| 外部历史 provenance 与标签隔离 | external_datasets.py、attribution.py | test_external_datasets.py、test_attribution.py |
| 冻结选择、分母、abstain、截断、关联 | attribution_protocol.py、attribution_benchmark.py | test_attribution_protocol.py、test_attribution_benchmark.py |
| Phoenix 归因只读视图 | attribution_read_model.py | test_attribution_read_model.py（公共 API fake client），未写数据库 |

## 验证结果与复现

本轮重新执行完整回归：**107 passed, 2 skipped in 112.95s**，退出码0。此前一轮为相同107通过/2跳过、76.04秒。两项 skip 是真实模型/WebSurfer。新增前端 Python 用例内执行4个 Node 行为用例，全部通过。这是原仓库环境回归，不能替代下节的全新安装验收。

```powershell
$env:RUN_REAL_MODEL_TESTS='0'
$env:OPENAI_API_KEY=''
$env:LANGSMITH_TRACING='false'
& '.\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider --tb=short
& '.\.venv\Scripts\python.exe' -m pip check
& '.\.venv-phoenix\Scripts\python.exe' -m pip check
git diff --check
```

本轮两原环境 pip check 均为 `No broken requirements found.`，退出码0，耗时分别9.61秒和9.49秒。44 个 Python 文件 AST 解析、4个 PowerShell 文件 Parser（0错误）和 git diff --check 通过。此前完整套件第一次受沙箱 Node `spawn EPERM` 影响（106通过、1失败、2跳过）；授权相同测试命令后整套通过，不把权限失败掩盖为应用修复。没有新增静态检查依赖，仓库未配置 Ruff/mypy。

真实浏览器验收：**BLOCKED**。本会话 Browser 初始化成功但选择返回 `No browser is available`，按诊断列举得到 `[]`，9月13日续接时仍为空。隔离 Chromium 组件已下载完成，但这不代表本次执行了网页验收。未生成或引用本次真实 UI 截图；未把 JS VM 测试当成 DOM、布局、焦点、剪贴板或 Chromium 验收。既有历史截图不算本次证据。

Phoenix：OTel 的实际公共 provider/processor/exporter 替换路径已以内存 sink 验证；SDK 401 来自 httpx.MockTransport。没有重跑真实 OTLP 接收、Phoenix 重启持久化或视觉验收，也没有因此触碰现存 Phoenix 数据库。

冻结核验可复现（只读原始文件、内存计算，不调用会写库的 verifier）：

```python
import json
from pathlib import Path
from m1_local.attribution_benchmark import (
    load_manifest, verify_source_receipt, verify_frozen_baseline,
    load_benchmark_records, run_benchmark,
)
from m1_local.attribution_protocol import load_protocol
root = Path.cwd()
protocol = root / 'configs/attribution_eval_v1.json'
load_protocol(protocol)
manifest = load_manifest(root / 'datasets/manifests/who_when_benchmark_v1.json', protocol_path=protocol)
verify_frozen_baseline(root)
verify_source_receipt(manifest, root / 'data/external/who_when')
records = load_benchmark_records(manifest, raw_root=root / 'data/external/who_when')
result = run_benchmark(records)
tracked = json.loads((root / 'docs/results/attribution-benchmark-v1-summary.json').read_text(encoding='utf-8'))
assert len(records) == 30 and len(result.predictions) == len(result.evaluations) == 90
assert [x.model_dump(mode='json') for x in result.summaries] == tracked['summaries']
assert list(result.confusion_matrix) == tracked['confusion_matrix']
```

该核验通过：5 个来源文件 hash/size 正确；30 cases、90 predictions/evaluations，全部 summary groups 和 confusion matrix 逐值等于 tracked JSON。冻结 attribution.py、external_datasets.py、协议配置、manifest、聚合结果，以及运行时 config.py/graph.py/models.py 和两份 lock 共10个文件与原提交内容一致。

## 跨电脑部署

**整体状态：BLOCKED，部分组件验证通过。** 这次模拟发生在同一台 Windows 主机，证明的范围是干净源码目录与新虚拟环境，不是另一台物理电脑的完整验收。

使用 `git archive HEAD` 加本次待审源码变更模拟全新 checkout（不添加 origin），隔离目录为系统临时目录下的 `m1-audit-clean-7vv7te23/checkout with spaces`；开始时无 `.env`、`data/`、`.venv`、`.venv-phoenix`。由系统 Python 3.11.9 新建两个虚拟环境，两个 `pyvenv.cfg` 均为 `include-system-site-packages=false`。设置 `PIP_NO_CACHE_DIR=1`、独立 `PLAYWRIGHT_BROWSERS_PATH=<隔离 checkout>/browsers`，清空 `PYTHONPATH` 和 `OPENAI_API_KEY`，禁用真实模型及 LangSmith tracing。没有使用原项目的虚拟环境、数据、浏览器缓存或 Phoenix 数据库。

安装调用的是此次修改后的 `scripts/install.ps1`。下表记录安装过程结果：124是监督器记录的超时中止结果码，并非安装脚本正常返回；首次1为安装脚本失败退出。外层日志采集脚本的成功退出不等于安装成功。

| 阶段 | 可复现证据 | 结论 |
|---|---|---|
| 首次安装 | `install-attempt-1.log`：继承环境中的 `PIP_NO_INDEX=1`，首个 aiofiles 依赖不可获得，安装退出1 | 环境禁止访问索引导致失败，不能据此认定锁定版本不存在 |
| 解除索引禁用后的依赖探测 | `dependency-probe.log`：新应用 Python、无缓存、显式启用索引，成功下载 aiofiles==25.1.0，退出0 | 当次网络及该依赖可用；没有依据宣称持续断网 |
| 第二次安装 | `install-attempt-2-result.json`：约300秒上限，实际300.70秒、结果码124、`timed_out=true`；下载应用依赖后安装未结束 | 超时中止；该部分安装不能当作通过 |
| 第三次安装 | `install-attempt-3-result.json`：约1200秒上限，实际1200.73秒、结果码124、`timed_out=true`；对可能不完整的包启用强制重装，禁用字节码编译，仍无缓存 | 应用依赖和 editable 项目安装成功，Chromium 下载成功；Phoenix 阶段超时，未走到脚本末尾的两次 pip check |
| 新应用环境核对 | `sys.prefix` 恰为隔离 `.venv`，`m1_local.__file__` 位于隔离 `src/m1_local/__init__.py`；锁文件130项全部版本匹配，缺失0、错版0；独立运行 pip check 退出0，5.83秒 | 应用依赖完整性及一致性 PASS；完整测试结果见下文 |
| 新 Phoenix 环境核对 | 独立 pip check 退出0，但锁文件77项仅59项版本匹配，缺18项、错版0；实际61个 distribution，另2个是初始化工具 | **BLOCKED**。缺 Phoenix 主包、client、evals、otel 等；pip check 不能发现全部尚未安装的锁文件条目，不能把它写成安装 PASS |
| 浏览器组件 | 隔离 `browsers/` 有 Chromium 1181、headless shell 1181、FFmpeg 1011、winldd 1007 | 下载 PASS；真实 Chromium 网页验收仍 BLOCKED |

新应用环境首次完整离线测试：**106 passed, 2 skipped, 1 failed in 82.44s**，退出1；唯一失败为 `tests/test_frontend.py::test_frontend_behavior_without_browser` 的 Node `spawn EPERM`。授权在沙箱外运行相同离线套件后：**107 passed, 2 skipped in 62.80s，退出0**。两项 skip 仍是真实模型/WebSurfer，没有额外 skip，也没有以跳过前端测试来获得通过。完整 pytest 结果来自本次工具回执，未另存原始测试日志。

测试工作目录为隔离 checkout，临时测试数据为其外侧独立的 `fresh-pytest/` 或 `fresh-pytest-elevated/`，不读取原项目 `data/`。另对58个源码、测试、脚本、配置、网页与构建/依赖文件逐项比较（仅归一化 CRLF/LF）：缺失0、内容差异0；核对时隔离 checkout 仍无 `data/` 和 `.env`。

新应用环境完整回归使用以下命令（在隔离 checkout 执行；占位路径须换成自己的全新隔离目录）：

```powershell
$env:RUN_REAL_MODEL_TESTS='0'
$env:OPENAI_API_KEY=''
$env:LANGSMITH_TRACING='false'
$env:PYTHONPATH=''
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PLAYWRIGHT_BROWSERS_PATH=Join-Path (Get-Location).Path 'browsers'
& '.\.venv\Scripts\python.exe' -B -m pytest -q -p no:cacheprovider --tb=short --basetemp '<独立临时测试目录>'
& '.\.venv\Scripts\python.exe' -m pip check
& '.\.venv-phoenix\Scripts\python.exe' -m pip check
```

安装日志及 JSON 回执保留在上述隔离目录的父目录，没有将安装器可能包含索引地址的原始日志加入 Git。复现时使用新的空目录，先确认索引策略允许下载；运行 `scripts/install.ps1` 后必须分别检查两份 lock 的安装完整性、两个 pip check 及离线测试。继续该隔离环境的安装也需先处理其不完整状态，不能把已有 `.venv-phoenix` 目录视为成功凭据。本次没有无限重试安装，没有更改锁定版本；在有更充足安装时间的环境中完成 Phoenix 安装及启动验收仍是交付前待办。

部署还依赖整个源码 checkout：网页资源在 `web/`，不能从当前 Python wheel 单独推断网页资源齐全；包内容哈希和 build backend 锁定不足的风险见 P2。全新环境没有下载 Who&When 原始数据，因此不能在没有研究数据的情况下重跑那30个真实样本；该核验单独使用原仓库数据只读完成，不作为安装依赖。

进程与端口收尾：两次超时均只终止本次安装启动的进程树；第三次安装 PID 40976 已不存在。9月13日最终目录过滤未见对应 Python/Node 进程，只有读取进程列表的短期 PowerShell；没有为“清理”停止其他会话进程。`netstat -ano -p tcp` 核对 4317、6006、8000、8001，均无 LISTENING 项。未删除隔离安装日志、原项目数据库、历史轨迹或其他用户数据。

## 密钥、个人数据、数据文件与 Git

- 本轮重新扫描当前跟踪/新文件及4个可达提交的87个唯一 blob，检查密钥形态、个人绝对路径、邮箱和运行/原始数据类路径；扫描只输出类别和位置，没有打印密钥值。
- 续接新增的最终报告再次单独检查凭据形态、个人用户路径及邮箱，均0命中；原21个文件 SHA-256 无变化。10个受保护文件再次与 HEAD 比较均一致，暂存区文件数0，`git diff --check` 退出0。
- 未发现可确认的真实密钥。唯一 credential-shape 命中是 `scripts/verify_runtime_trace.py` 的合成脱敏测试值，不是运行凭据。模式扫描不等于穷尽秘密发现。
- 原 README/安装脚本及历史存在个人用户路径；当前文件的个人绝对路径/邮箱模式扫描无命中。提交元数据包含1个作者身份（姓名/邮箱），值未打印。没有改写历史。
- 可达历史和当前跟踪文件未发现 `.env`、SQLite/Phoenix 数据库、原始数据目录、日志、PID、pyc 或虚拟环境；受控 manifest/安全聚合结果是已有研究元数据，不等于原始轨迹。
- 现存原始数据、数据库、缓存和产物仍留在被忽略目录，没有删除、覆盖或加入版本控制。Who&When 仅按本地研究处理，不把代码 MIT 许可解释为数据再分发授权。

## 费用、外部调用与交付状态

项目付费模型调用 **0**，真实 WebSurfer **0**，第三方 Agent/任务脚本执行 **0**，外部数据上传 **0**，研究数据集下载 **0**。实际下载了 Python 依赖和独立 Chromium 组件，执行了项目安装器及依赖安装所需的构建步骤；这些是已授权的部署验证，不能描述为全程无网络。没有修改运行时模型配置、生成代码执行策略、Agent 职责或 LangGraph 图。

Git HEAD 与提交历史不变；两个既有 tag 不变；没有 remote。原有21个文件逐字节指纹核对无差异，本轮仅增加这份最终报告。当前共22个变更路径：16个已跟踪文件修改、6个未跟踪文件。`git diff --stat` 仅统计已跟踪部分：**16 files changed, 657 insertions(+), 115 deletions(-)**；不包含下面6个新增文件，因此不能直接与 IDE 的含新增文件统计比较。

- `docs/full-project-audit-2026-09-12.md`：保留上轮审查草稿，内容未改。
- `docs/full-project-audit-optimization.md`：本次要求的最终报告，补齐续接验证结果与残余风险。
- `src/m1_local/telemetry_export.py`：导出边界脱敏，对应 P1 遥测泄漏修复。
- `tests/frontend_behaviors.cjs`、`tests/test_frontend.py`：4个前端行为回归及其 pytest 入口。
- `tests/test_windows_install.py`：Windows 安装发现、版本、空格路径及失败即停的回归。

README、architecture、acceptance 和 attribution-benchmark 文档同步说明已验证行为、安装方式及研究局限；各代码/测试文件的修改原因、证据与语义影响见前述 P1/P2 表格。所有修复、测试和文档保留为未提交工作树差异；未暂存、未提交、未创建 tag、未添加 remote、未 push，等待用户审阅确认。
