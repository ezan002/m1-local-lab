# M1 Local Lab：旧研究工具说明

> 这是 Agent Studio 之前的研究原型（本地历史轨迹观测、Who&When 归因基准、错误归因 MVP）的说明，从原 README 移到这里，内容未改动。
> 当前产品请看仓库首页的 README 和 [USER_GUIDE.md](../USER_GUIDE.md)。

## Legacy research tools (still available)


当前主线（2026-10-10）：**无 Key 的本地外部历史轨迹观测**。选择已有获准样本，预览、脱敏导入独立 Phoenix Project，再从步骤定位原记录。使用 `scripts/start-observation.ps1`；指南、版本能力及本轮证据见 [本地历史观测](historical-observation.md)。它不调用模型，后端禁止执行/Resume/Replay/Fork；原任务系统保留在显式 `serve` 模式的 `/execution`，旧归因界面通过 `/execution?research=1` 恢复。下文的旧执行与研究说明不代表观测模式需要 Key。

接入更多同格式记录可通过启动参数 `-Catalog .\configs\observation-catalog.example.json` 注册本机文件，不必修改适配器代码；示例添加本机已有 Hand-Crafted 样本，没有包含原始数据，也不会下载缺失文件。支持格式、路径边界与使用方法见[样本清单](historical-observation.md#7-同格式多样本接入后续迭代)。

工程审查、导师部署验证及尚未解决的边界见 [完整审查报告](full-project-audit-optimization.md)。

## Who&When 归因基准 v1

当前仓库已固化 `attribution-eval-v1`：固定 revision 的 Who&When 30 样本（Hand-Crafted 10、Algorithm-Generated 20），固定 Dev/Frozen Test 20/10，以及三种既有零费用确定性基线，共 90 次离线预测。详细协议、分母语义、Phoenix 结果和复现命令见 [docs/attribution-benchmark-v1.md](attribution-benchmark-v1.md)；安全聚合结果见 [docs/results/attribution-benchmark-v1-summary.json](results/attribution-benchmark-v1-summary.json)。该路径不调用模型、不运行真实 WebSurfer，也不改变 LangGraph 或现有 Agent。

这是一个仅监听本机回环地址的单用户 MVP：LangGraph 负责规划、调度、重规划和持久化；微软官方 AutoGen `MultimodalWebSurfer` 使用真实 Playwright Chromium 充当浏览 worker；Phoenix 独立保存可观测轨迹；FastAPI 页面通过 SSE 展示运行与 checkpoint。

它不是 Magentic-One 的逐行复刻，也不会执行模型生成的代码。

## 从准备好的 Windows 电脑开始

要求 Windows 11 x64 与 Python 3.11。先确认 Python Launcher 能找到对应解释器：

```powershell
py -3.11 --version
```

在项目根目录执行安装脚本。若本机 PowerShell 策略不允许直接运行 `.ps1`，下面的 `Bypass` 只影响这个新进程，不修改全局策略：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

安装会创建相互隔离的 `.venv` 与 `.venv-phoenix`，安装锁定依赖、项目 editable 包和与当前 Playwright 配套的 Chromium，并分别运行 `pip check`。不需要 ChromeDriver。

若 `PIP_NO_INDEX` 禁用索引，安装脚本会在调用 Python 前给出明确错误，不会覆盖该设置。只有操作者确认允许下载后，才应在安装子进程中显式启用索引；不会修改用户或系统级环境变量。导师从 clone 到启动网页的完整 PowerShell 命令见上述报告。

没有 Python Launcher 时尝试 PATH 中的 `python.exe`；也可给安装脚本传 `-PythonExe 'C:\Python311\python.exe'`（替换为实际路径）。脚本校验 Python 3.11 x64，任一安装命令失败就停止，保留已有环境。部署需保留完整 checkout 及 `web/`，目前不支持仅复制 wheel。依赖及 Chromium 首次安装需要网络。

只在 `.env` 不存在时复制模板，然后在本机编辑器中填写 Key；不要把 Key 粘贴到聊天、日志或截图里：

```powershell
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
```

## 自检

离线检查不调用模型：

```powershell
& '.\.venv\Scripts\python.exe' -m m1_local.cli doctor
```

显式联网检查最多调用模型 3 次，分别验证文字、人工生成的 16×16 图片和工具调用：

```powershell
& '.\.venv\Scripts\python.exe' -m m1_local.cli doctor --online
```

报告只显示 Key 是否已配置，不显示其值。

## 启动与停止

先启动 Phoenix，再在另一个 PowerShell 窗口以前台方式启动应用：

```powershell
& '.\scripts\start-phoenix.ps1'
& '.\scripts\start-app.ps1'
```

- 应用：http://127.0.0.1:8000
- Phoenix：http://127.0.0.1:6006

停止应用：在应用窗口按 `Ctrl+C`。停止 Phoenix：

```powershell
& '.\scripts\stop-phoenix.ps1'
```

项目启动入口 `scripts/serve_phoenix.py` 对锁定的 Phoenix 11.35.0 关闭未使用的 gRPC，保留回环 HTTP/OTLP 写入；版本或接口不符时拒绝启动。请使用项目启动脚本，直接运行 `phoenix serve` 会绕过该限制。Phoenix 自带启动横幅仍会列出默认 gRPC 地址，该静态文字不代表存在监听。

停止脚本核对解释器、启动脚本、PID 和启动时间后只结束该进程树。旧版本入口启动且没有所有权回执的进程会被拒绝停止，应从原终端退出。

应用对 metadata/checkpoint 两个 SQLite 路径持有进程生命周期锁；另一个 API/CLI 无法启动同一数据库的 runtime，也不会把活跃运行标为 interrupted。正常退出或进程终止后操作系统释放锁。API 运行期间请从网页查看历史；使用同库 CLI 前先停止 API。不要手工删除 `.owner.lock` 文件。

## 网页控制台

打开 http://127.0.0.1:8000 后，可以完成以下闭环：

1. 在左侧新建任务或选择历史运行；运行列表显示状态、更新时间和来源类型。
2. 主区域实时显示状态、当前节点、步骤数、更新时间以及 Planner → Orchestrator → Worker → Finalizer 流程。
3. 顶部可查看应用/Phoenix 健康状态、打开 Phoenix，并复制当前 trace ID。
4. “事件”标签按持久化事件 ID 和时间展示 SSE 进度；断线时可手动重试，重连从最后 ID 补读。
5. “证据”标签展示引用、产物和截图缩略图；点击截图可打开大图。
6. “Checkpoint”标签可选择历史快照、检查 `next` 和来源，并从非终结 checkpoint 回放或分叉。
7. “State”标签默认折叠完整 JSON，支持复制；所有任务、模型和错误文本都通过安全 DOM API显示。

暂停只在下一个安全边界生效。恢复和回放会先确认，因为可能重新调用模型并产生费用。分叉对话框只提供 `plan` 和 `worker_instruction` 两个后端白名单字段；终结 checkpoint 会禁用 replay/fork 并解释原因。页面以桌面端为主，在窄窗口自动收敛流程和详情布局。

## 第一条演示

在页面输入：

```text
Visit https://playwright.dev/python/docs/library and report the Windows asyncio event-loop requirement with the source URL.
```

也可用 CLI：

```powershell
$env:HEADLESS='true'
& '.\.venv\Scripts\python.exe' -m m1_local.cli run 'Visit https://playwright.dev/python/docs/library and report the Windows asyncio event-loop requirement with the source URL.'
```

页面可查看运行状态、SSE 事件、最终输出、引用、截图、完整 state 与 checkpoint，并可在非终结 checkpoint 上 replay/fork。暂停请求在下一个持久化安全边界生效，不会声称立即冻结。

## CLI 与数据集

```powershell
& '.\.venv\Scripts\python.exe' -m m1_local.cli history RUN_ID
& '.\.venv\Scripts\python.exe' -m m1_local.cli resume RUN_ID
& '.\.venv\Scripts\python.exe' -m m1_local.cli replay RUN_ID CHECKPOINT_ID
& '.\.venv\Scripts\python.exe' -m m1_local.cli fork RUN_ID CHECKPOINT_ID --changes '{"worker_instruction":"新的指令"}'
& '.\.venv\Scripts\python.exe' -m m1_local.cli dataset .\datasets\sample.jsonl --limit 1
```

JSONL 每行包含 `case_id`、`task`、可选 `allowed_files` 和 `metadata`。`--limit` 必填，用于明确控制串行真实模型调用的成本。每个案例有独立 thread；结果保留状态、答案、引用、耗时、模型调用次数、错误和产物。无法获得的 token 数写作 `unknown`。

## 离线错误归因 MVP

三条既有 TraceElephant/Who&When 历史样本可通过三种不调用模型的确定性方法预测“责任 Agent + 决定性步骤”，再由独立 evaluator 接入真实标签。预测输入经过深拷贝和递归禁用字段检查，Ground Truth 不会传给 predictor；截断导致决定性步骤未保留时，该 case 不进入 step 指标分母。

```powershell
& '.\.venv\Scripts\python.exe' '.\scripts\verify_failure_attribution_mvp.py'
```

脚本会安全复用或临时启动 Phoenix，复用 `m1-local-lab-external-trace-smoke` Dataset，并写入 `m1-local-lab-failure-attribution-mvp` Experiment。该命令不调用付费模型、不运行真实 WebSurfer、不执行外部任务，也不下载数据。接口、指标、实测结果和限制见 [docs/failure-attribution-mvp.md](failure-attribution-mvp.md)。

## 错误归因网页

启动 Phoenix 和应用后，首页一级标签“错误归因”会通过 FastAPI 只读 API 展示最新的同名归因 Experiment。页面包含 Dataset/Experiment 概览、三种基线方法及有效分母、无刷新筛选、prediction/ground truth/evaluation 分区、截断提示和规范化步骤时间线。历史轨迹明确标为 `imported_historical`，不会被表示为本项目真实执行。

读取层只使用 Phoenix 11.35.0 公共 Python API，不读取 Phoenix SQLite，也不抓取 Phoenix HTML。Experiment 默认按名称元数据选择最新实例；`ATTRIBUTION_EXPERIMENT_ID` 仅是可选覆盖项，不在生产代码中固定 opaque ID。Phoenix 不可用时，任务运行页仍可使用，归因页显示可重试的脱敏错误。

真实浏览器验收使用项目现有 Chromium 和离线 `RuleBasedGateway`，不会触发模型或 WebSurfer：

```powershell
& '.\.venv\Scripts\python.exe' '.\scripts\verify_attribution_ui.py'
```

脚本检查端口后只管理自己启动的 Phoenix/Uvicorn，覆盖 1366×768、1920×1080 和 Playwright 125% 缩放模拟，并将未跟踪截图写入 `data/test-output/attribution-ui/`。接口、颜色/文字标签语义及验收细节见 [docs/failure-attribution-ui.md](failure-attribution-ui.md)。

## 数据与安全边界

外部历史的 Phoenix 写入使用独立 `safe-export-v1` 展示副本，冻结 adapter、predictor、Ground Truth、manifest 和本地结果保持不变。SDK 实验重用旧 Dataset 时也先复制并脱敏；敏感内容出现在标签或身份字段时拒绝导出。该机制处理凭据形态和显式配置密钥，不承诺识别全部个人信息，也不会清理已有数据库内容。

隔离的项目 Chromium 验收已能运行。当前 Phoenix 11.35.0 的 GitHub 星标组件仍会发起外部请求；在阻止外部请求的严格验收中产生页面错误，整体网页验收为 PARTIAL，详情见审查报告。

- 应用和 Phoenix HTTP 默认绑定 `127.0.0.1`，API 校验 Host、Origin 和 CSRF；不建立公网隧道。项目 Phoenix 启动脚本已对锁定的11.35.0关闭未使用的 gRPC；不要用旧版本“未解决通配监听”的结论代替当前脚本。
- 同一组运行数据库只能由一个 runtime 进程使用，已有跨进程所有权锁；第二个 API/CLI 会被拒绝，不会误改活跃记录。观测模式不初始化 runtime，也不打开这些运行数据库。
- WebSurfer 对导航站点、公开 IP 和 URL scheme 做代码校验；公开静态资源可来自非导航白名单主机。登录、上传、支付等主要由提示词约束，尚无完整 HTTP 方法/表单动作强制隔离；网页文字按不可信数据处理。
- 文件 worker 只读显式允许目录下的 `.txt`、`.md`、`.csv`，并检查解析后的真实路径和大小。
- coder 只写 `unexecuted_code` 产物，`ENABLE_CODE_EXECUTION=false` 是强制默认值。
- `.env`、SQLite 数据库、浏览器调试目录、截图和 PID/日志都被 `.gitignore` 排除。应用 OTel 导出层隐藏模型输入/输出及 SDK 异常正文，并脱敏配置密钥和常见凭据形态；本地 checkpoint、产物及外部 Dataset 内容不具备全面脱敏保证。
- replay/恢复可能重新访问网页并再次产生模型费用。项目不承诺 exactly-once，也不恢复 DOM、JS 内存、登录态、表单或外部副作用。

## 排错

- `NotImplementedError` 或浏览器子进程错误：只能从 `m1_local.cli`/`start-app.ps1` 启动；入口在 Windows 上显式使用 Proactor event loop，Uvicorn 固定单 worker、无 reload。
- Chromium 不存在：运行 `& '.\.venv\Scripts\python.exe' -m playwright install chromium`。
- Phoenix 端口被占用：启动脚本会拒绝覆盖。先确认占用者；不要结束所有 Python 进程。
- Phoenix 暂不可达：主任务继续，但 UI/`/api/session` 会显示“未确认轨迹接收”的警告。本地事件不冒充 Phoenix span。
- `OPENAI_API_KEY` 缺失：离线测试和离线 doctor 仍可运行；正式 WebSurfer 不会静默回退 mock。
- 完整测试与实机证据见 [docs/acceptance.md](acceptance.md)，语义说明见 [docs/architecture.md](architecture.md)。
