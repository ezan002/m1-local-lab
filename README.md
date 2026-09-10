# M1 Local Lab

这是一个仅监听本机回环地址的单用户 MVP：LangGraph 负责规划、调度、重规划和持久化；微软官方 AutoGen `MultimodalWebSurfer` 使用真实 Playwright Chromium 充当浏览 worker；Phoenix 独立保存可观测轨迹；FastAPI 页面通过 SSE 展示运行与 checkpoint。

它不是 Magentic-One 的逐行复刻，也不会执行模型生成的代码。

## 从准备好的 Windows 电脑开始

要求 Windows 11 x64 与 Python 3.11。本机基准解释器为：

```powershell
& 'C:/Users/wangy/AppData/Local/Programs/Python/Python311/python.exe' --version
```

在项目根目录执行安装脚本。若本机 PowerShell 策略不允许直接运行 `.ps1`，下面的 `Bypass` 只影响这个新进程，不修改全局策略：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

安装会创建相互隔离的 `.venv` 与 `.venv-phoenix`，安装锁定依赖、项目 editable 包和与当前 Playwright 配套的 Chromium，并分别运行 `pip check`。不需要 ChromeDriver。

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

脚本会检查端口；停止脚本只接受本项目 PID 文件且核对可执行文件路径，不会结束其他 Python 或 Chrome 进程。

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

脚本会安全复用或临时启动 Phoenix，复用 `m1-local-lab-external-trace-smoke` Dataset，并写入 `m1-local-lab-failure-attribution-mvp` Experiment。该命令不调用付费模型、不运行真实 WebSurfer、不执行外部任务，也不下载数据。接口、指标、实测结果和限制见 [docs/failure-attribution-mvp.md](docs/failure-attribution-mvp.md)。

## 错误归因网页

启动 Phoenix 和应用后，首页一级标签“错误归因”会通过 FastAPI 只读 API 展示最新的同名归因 Experiment。页面包含 Dataset/Experiment 概览、三种基线方法及有效分母、无刷新筛选、prediction/ground truth/evaluation 分区、截断提示和规范化步骤时间线。历史轨迹明确标为 `imported_historical`，不会被表示为本项目真实执行。

读取层只使用 Phoenix 11.35.0 公共 Python API，不读取 Phoenix SQLite，也不抓取 Phoenix HTML。Experiment 默认按名称元数据选择最新实例；`ATTRIBUTION_EXPERIMENT_ID` 仅是可选覆盖项，不在生产代码中固定 opaque ID。Phoenix 不可用时，任务运行页仍可使用，归因页显示可重试的脱敏错误。

真实浏览器验收使用项目现有 Chromium 和离线 `RuleBasedGateway`，不会触发模型或 WebSurfer：

```powershell
& '.\.venv\Scripts\python.exe' '.\scripts\verify_attribution_ui.py'
```

脚本检查端口后只管理自己启动的 Phoenix/Uvicorn，覆盖 1366×768、1920×1080 和 Playwright 125% 缩放模拟，并将未跟踪截图写入 `data/test-output/attribution-ui/`。接口、颜色/文字标签语义及验收细节见 [docs/failure-attribution-ui.md](docs/failure-attribution-ui.md)。

## 数据与安全边界

- 服务默认只绑定 `127.0.0.1`，API 校验 Host、Origin 和 CSRF；不建立公网隧道。
- WebSurfer 只允许配置白名单中的公开站点，阻止本机、内网、`file://`、登录、上传、支付和下载执行；网页文字按不可信数据处理。
- 文件 worker 只读显式允许目录下的 `.txt`、`.md`、`.csv`，并检查解析后的真实路径和大小。
- coder 只写 `unexecuted_code` 产物，`ENABLE_CODE_EXECUTION=false` 是强制默认值。
- `.env`、SQLite 数据库、浏览器调试目录、截图和 PID/日志都被 `.gitignore` 排除。Phoenix 不接收模型 Key。
- replay/恢复可能重新访问网页并再次产生模型费用。项目不承诺 exactly-once，也不恢复 DOM、JS 内存、登录态、表单或外部副作用。

## 排错

- `NotImplementedError` 或浏览器子进程错误：只能从 `m1_local.cli`/`start-app.ps1` 启动；入口在 Windows 上显式使用 Proactor event loop，Uvicorn 固定单 worker、无 reload。
- Chromium 不存在：运行 `& '.\.venv\Scripts\python.exe' -m playwright install chromium`。
- Phoenix 端口被占用：启动脚本会拒绝覆盖。先确认占用者；不要结束所有 Python 进程。
- Phoenix 暂不可达：主任务继续，但 UI/`/api/session` 会显示“未确认轨迹接收”的警告。本地事件不冒充 Phoenix span。
- `OPENAI_API_KEY` 缺失：离线测试和离线 doctor 仍可运行；正式 WebSurfer 不会静默回退 mock。
- 完整测试与实机证据见 [docs/acceptance.md](docs/acceptance.md)，语义说明见 [docs/architecture.md](docs/architecture.md)。
