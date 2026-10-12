# Windows 部署指南：m1-local-lab

适用代码：`a808a04ca60d35e6e2d35ea46441d3ba6e662597`，核查日期 2026-09-14。当前有标准双环境安装和两个独立启动入口，**统一启动尚未完成**。本指南中的安装、配置和服务命令供部署时执行，本轮文档核查没有执行这些有写入的操作。

## 1. 支持范围与准备

目标环境为 Windows 11、Python **3.11 x64**、Windows PowerShell、无 Docker、单机单用户。应用绑定 `127.0.0.1:8000`，Phoenix 绑定 `127.0.0.1:6006`；不要改成局域网共享入口。无需本地 GPU：普通模式使用远程模型，离线验证使用规则替身，不是本地大模型推理。

建议准备 16 GB 内存、至少 10 GB 可用磁盘，并为研究数据另留空间；这些是部署余量建议，**不是测得的最低配置**。首次安装需要访问 Python 包索引及 Playwright Chromium 下载源。离线 fixture 演示不需要模型服务；普通模型/网页任务需要相应网络和凭据。项目尚无一键离线 wheelhouse 安装流程。

提前安装 Git（Git 方式交付时需要）和 Python 3.11 x64。使用 PowerShell 检查：

```powershell
py -3.11 --version
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 is required' }
py -3.11 -c 'import sys; print(64 if sys.maxsize > 2**32 else 32)'
git --version
```

第二条 Python 输出应为 64。没有 `py.exe` 时，可将真实 Python 3.11 可执行文件路径传入安装脚本的 `-PythonExe`，不要把路径写死为开发者个人目录。

## 2. 获取源码：当前没有远程 URL

当前 `git remote -v` 为空，**目前没有可直接复制执行的网络 git clone 地址**。不能假定已有公开或私人仓库。具备权限的本地 Git 仓库路径可以 clone；未来若另行建立私人仓库，由交付人提供真实 URL。本指南不创建 remote 或上传内容。

### 方式 A：未来私人仓库，或获授权的本地 Git 仓库

目标目录必须不存在，不能覆盖已有工作区。在任意 PowerShell 目录执行：

```powershell
$Source = Read-Host 'Enter the authorized repository URL or local Git repository path'
$Checkout = 'C:\projects\m1-local-lab'
if (Test-Path -LiteralPath $Checkout) { throw 'Destination already exists; preserve it and choose an empty location' }
git clone --branch feature/full-project-audit-optimization "$Source" "$Checkout"
if ($LASTEXITCODE -ne 0) { throw 'git clone failed' }
Set-Location -LiteralPath $Checkout
git branch --show-current
git rev-parse HEAD
git status --short
```

本报告核查的是 a808a04。交付人确认新的纯文档提交后，HEAD 可以是其后继，但应说明交付版本；本轮尚未提交的三份指南不会自动包含在 a808a04 的 clone 中。不要为匹配文档而重置一个有改动的仓库。

### 方式 B：本地源码 ZIP

由交付人在已核实的源仓库根目录导出**跟踪源码**，不要直接压缩整个项目文件夹。下面命令导出当前已审代码，不包含尚未提交的三份新指南；指南需要审阅后另附，或在未来获准提交后随新提交导出。

```powershell
$Package = Join-Path $env:TEMP ('m1-local-lab-source-' + [guid]::NewGuid().ToString('N') + '.zip')
git archive --format=zip --output "$Package" a808a04ca60d35e6e2d35ea46441d3ba6e662597
if ($LASTEXITCODE -ne 0) { throw 'Source archive failed' }
Get-FileHash -LiteralPath $Package -Algorithm SHA256
Write-Output $Package
```

只通过获准的本地方式交付源码和哈希，不附 `.env`、虚拟环境、`data/`、原始 Who&When/TraceElephant、数据库、截图、日志或缓存。Who&When 不因项目代码采用 MIT 而获得再分发许可。

导师先比较交付哈希，再解压到新目录：

```powershell
$Archive = Read-Host 'Enter the received source ZIP path'
Get-FileHash -LiteralPath $Archive -Algorithm SHA256
$Checkout = 'C:\projects\m1-local-lab'
if (Test-Path -LiteralPath $Checkout) { throw 'Destination already exists; do not overwrite it' }
Expand-Archive -LiteralPath $Archive -DestinationPath $Checkout
Set-Location -LiteralPath $Checkout
```

ZIP 没有 `.git`，Git 历史核验和依赖 Git 的冻结源码核验不能直接在这个副本执行。安装、网页及零费用服务验收使用完整源码目录；需要完整研究复现时优先交付本地 Git clone。

## 3. 安装与两套虚拟环境

以下均在 `C:\projects\m1-local-lab` 执行。先检查端口，记录已有服务；不要终止未知进程：

```powershell
Set-Location -LiteralPath 'C:\projects\m1-local-lab'
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in @(4317, 6006, 8000, 8001) } |
    Select-Object LocalAddress, LocalPort, OwningProcess
```

使用现有 [install.ps1](../scripts/install.ps1)。下列环境变量只作用于当前终端及其子进程，使项目 Chromium 位于可明确定位的 `browsers/`。执行策略 Bypass 仅限启动的 PowerShell 进程，不修改系统策略；若组织策略禁止，请由管理员处理，不绕过组织规则。

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location).Path 'browsers'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
if ($LASTEXITCODE -ne 0) { throw 'Installation failed; inspect the first error before continuing' }
& '.\.venv\Scripts\python.exe' -B -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Application dependency check failed' }
& '.\.venv-phoenix\Scripts\python.exe' -B -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Phoenix dependency check failed' }
```

如需显式选择 Python，将第一条安装调用替换为：

```powershell
$Python311 = Read-Host 'Enter the full Python 3.11 x64 python.exe path'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -PythonExe "$Python311"
if ($LASTEXITCODE -ne 0) { throw 'Installation failed' }
```

安装顺序是创建/检查两个 Python 3.11 x64 环境 → 应用 lock → editable 源码（不重新解依赖、不另建 build isolation）→ Chromium → Phoenix lock → 两次 pip check。已有错误版本环境会被拒绝，不会自动删除重建。[应用 lock](../requirements-app.lock.txt)、[Phoenix lock](../requirements-phoenix.lock.txt)

应用锁 130 项，Phoenix 锁 77 项；关键版本为 LangGraph 1.2.11、AutoGen 0.7.5、Playwright 1.54.0、Phoenix 11.35.0。不要手工升级“修复”安装。`pip check` 只检查已安装包的一致性，**不能证明全部 lock 条目已经安装**；安装脚本必须成功跑到末尾。

如果安装器报 `PIP_NO_INDEX disables dependency index access`，表示用户当前禁止索引访问，脚本会保留该配置并停止。仅在明确允许本次安装访问索引后，改用以下**子进程限定**命令；不执行用户/系统级 SetEnvironmentVariable：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command '$env:PIP_NO_INDEX="0"; & .\scripts\install.ps1'
if ($LASTEXITCODE -ne 0) { throw 'Installation failed' }
```

引用既有验收结果：同机隔离 Windows 目录曾完成新双环境、无原缓存/数据库安装；Phoenix 补齐阶段 84.375 秒，退出 0，77/77 匹配。此前遇到过网络代理错误和监督超时，不能将虚拟环境目录存在视为安装成功；本轮未重新下载依赖或在导师物理电脑安装。

## 4. 本机配置与零费用模式的区别

仅在 `.env` 不存在时复制模板，在编辑器本地填写，不将凭据放进终端命令或汇报：

```powershell
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
notepad.exe .env
```

| 配置 | 是否需填写 | 本版本建议 |
|---|---|---|
| `OPENAI_API_KEY` | 普通模型入口必需 | 本机填入自己的非空凭据；本轮没有验证有效性 |
| `OPENAI_BASE_URL` | 可选 | 保留模板地址，除非已有获准的兼容服务 |
| `ORCHESTRATOR_MODEL`、`WEBSURFER_MODEL` | 不必修改 | 保留项目现有 `gpt-4.1`；编写代码所用 Codex 档位与运行时配置无关 |
| `APP_HOST`、`APP_PORT` | 不必修改 | 保留 `127.0.0.1`、8000；启动脚本按标准端口管理 |
| `PHOENIX_BASE_URL`、`PHOENIX_OTLP_ENDPOINT` | 不必修改 | 保留本机 6006 和 `/v1/traces` |
| `PHOENIX_PROJECT_NAME` | 可选 | 默认 `m1-local-lab` |
| `ATTRIBUTION_DATASET_NAME`、`ATTRIBUTION_EXPERIMENT_NAME`、可选实验 ID | 只在读取特定既有数据时配置 | 默认三案例 MVP；不填虚构 ID，空实例不会自动导入历史 |
| `ALLOWED_FILE_DIRS`、`ALLOWED_WEB_HOSTS` | 可选且需审慎 | 保留模板白名单，不开放整个磁盘或任意地址 |
| 预算、`HEADLESS`、代码执行开关 | 不必修改 | 保留已有值；Coder 当前仅生成未执行产物 |

完整字段以 [.env.example](../.env.example) 和 [Settings](../src/m1_local/config.py) 为准。不要另设 `PHOENIX_SQL_DATABASE_URL` 来指向未知现有库；项目 Phoenix wrapper 对这种覆盖会拒绝启动。启动 Phoenix 的 PowerShell 脚本自行设置标准 host/port/storage，不是通过 `.env` 通用托管任意 Phoenix 配置。

普通 `start-app.ps1` 构造 `OpenAIModelGateway`，即使未创建任务，空 Key 也可能导致启动时 `Missing credentials`；它**不会自动切换成 RuleBasedGateway**。普通页面创建任务、Resume、Replay 都可能计费。本指南不要求执行任何真实任务来证明安装成功。

无 Key 时使用 [演示指南 A](demo-walkthrough.md) 的现有 `verify_advisor_release.py --serve-offline` 专用入口。它显式注入 RuleBasedGateway、忽略 `.env`、使用新临时运行存储；这不是正式 CLI 的 `--offline` 选项，也不是模型故障时的降级策略。

## 5. 普通模式双服务启动

前提：两环境安装成功，标准端口无未知监听，普通模式 `.env` 已配置。先在终端 A 启动 Phoenix：

```powershell
Set-Location -LiteralPath 'C:\projects\m1-local-lab'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-phoenix.ps1
if ($LASTEXITCODE -ne 0) { throw 'Phoenix launch failed' }
$PhoenixReady = $false
$PhoenixDeadline = (Get-Date).AddSeconds(180)
do {
    try {
        $Response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:6006/healthz' -TimeoutSec 3
        if ($Response.StatusCode -eq 200) { $PhoenixReady = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $PhoenixDeadline)
if (-not $PhoenixReady) { throw 'Phoenix health check timed out; inspect local logs' }
```

`start-phoenix.ps1` 返回“starting”不代表冷启动完成。首次初始化可能较慢，必须等待 `/healthz`。该入口创建本机 `data/phoenix`、PID/所有权回执和服务日志；不要指向要保留不变的研究库做试安装。隔离演示使用新 `-DataDirectory`，见演示指南。

在终端 B 启动应用（前台阻塞是正常行为）：

```powershell
Set-Location -LiteralPath 'C:\projects\m1-local-lab'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location).Path 'browsers'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-app.ps1
```

回到终端 A 验证并打开页面：

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 10 | ConvertTo-Json -Depth 6
Start-Process 'http://127.0.0.1:8000'
```

如果应用尚未就绪，等待终端 B 启动完成后再执行健康检查。响应应分别显示 app 与 phoenix healthy，不能只看 HTTP 200。主入口是 **8000**；**6006** 是 Phoenix 高级观测入口，需要看完整 Trace/Span 时打开 `http://127.0.0.1:6006`。`/api/health` 只验证服务可达，接收和持久化 Trace 需要专门的零费用验收。

## 6. 可选：零费用隔离服务验收

仅当标准四端口没有现存服务时，在根目录执行项目已有验收器，输出目录必须尚不存在。它会创建自己的临时 Phoenix 数据、三例**本地合成**归因 fixture 和一个 RuleBased 文件任务，真实启动项目 Chromium；不会加载研究原始数据或调用模型/WebSurfer。

```powershell
$Acceptance = Join-Path $env:TEMP ('m1-advisor-acceptance-' + [guid]::NewGuid().ToString('N'))
& '.\.venv\Scripts\python.exe' -B .\scripts\verify_advisor_release.py --output "$Acceptance"
$AcceptanceExit = $LASTEXITCODE
Get-Content -LiteralPath (Join-Path $Acceptance 'result.json') -Encoding UTF8
Write-Output "Acceptance exit code: $AcceptanceExit"
```

此脚本会写入**新临时数据库**，故本轮只读文档任务未执行。它在结束时停止自己启动的进程，保留回执用于检查。它将浏览器位置固定为 `<项目>/browsers`，所以本指南安装时显式使用该位置。

引用既有验收结果：Phoenix/API/Trace 重启持久化通过，应用页面通过；Phoenix GitHub 星标请求被阻止后整体 PARTIAL、退出 1。当前应逐项读 `result.json`，不能将所有退出 1 都归为安装失败或全部通过。缺浏览器、权限等使实际验收不能运行时应保持 BLOCKED。

## 7. 停止与端口核对

普通应用终端 B 按 Ctrl+C，等待关闭完成。在项目根目录停止本项目拥有的 Phoenix：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-phoenix.ps1
if ($LASTEXITCODE -ne 0) { throw 'Phoenix ownership/stop check failed; inspect before any manual action' }
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in @(4317, 6006, 8000, 8001) } |
    Select-Object LocalAddress, LocalPort, OwningProcess
```

开始无监听且只启动本指南服务时，结束后也应无监听。8001 只是验收关注端口，不是第三个必需服务；4317 在项目 wrapper 下禁用。停止脚本核对 PID、创建时间、解释器与入口，不是“按端口全部杀掉”。未知监听由其拥有者处理，不手动删除数据库、锁文件或运行历史。

## 8. 常见故障

| 现象 | 先检查 | 处理边界 |
|---|---|---|
| 6006 无法访问 | `/healthz`、冷启动等待、本地 `data/logs/phoenix.stderr.log` | 启动命令返回不等于 ready；日志可能含本机信息，不原样公开 |
| 已有端口占用 | 上述端口列表与 PID 所属进程 | 不启动第二份，也不终止未知进程 |
| stale PID / 所有权拒绝 | 是否还有旧服务、回执是否对应当前入口 | 停止脚本仅处理可验证的所有者；不要盲删回执强行接管 |
| Python 版本或位数错误 | `py -3.11`、安装器错误 | 提供正确解释器；已有错误环境由用户单独备份处理，不在指南中递归删除 |
| PIP_NO_INDEX 报错 | 当前索引策略 | 如获准，仅用前述子进程命令临时开启；不永久修改用户变量 |
| ProxyError / 超时 | 首个 pip 错误、包索引和代理策略 | 区分网络与版本冲突，保留本地日志；不盲目升级 lock 或关闭全局安全软件 |
| Chromium executable 不存在 | 安装时与启动时的 `PLAYWRIGHT_BROWSERS_PATH` 是否一致 | 可按下方命令补装项目 Chromium；不因此运行 WebSurfer |
| 空 Key / Missing credentials | 使用的是普通入口还是显式 RuleBased 入口 | 本地填写 Key 或切换专用零费用演示；不要填入虚构 Key 后称正常模型可用 |
| Phoenix 健康但页面 Failed to fetch | 浏览器 Network 的失败目标是本地静态资源还是 GitHub | 已知 GitHub 星标请求可失败；不能用 `/healthz` 推断所有 UI 资源无错 |
| 回环请求经过代理或防火墙受阻 | 本机代理绕过规则、组织策略 | 仅让获准的回环请求直连，不对局域网开放端口，不全局关闭防火墙 |
| 归因页空 / 404 / 503 | Phoenix 健康、默认 Dataset/Experiment 是否存在 | 新安装无研究数据是预期；用合成验收或已有结果文档，不自动下载或重跑 benchmark |
| 同库第二应用启动失败 | 是否另有 CLI/API 正在使用同一数据库 | 单运行所有权保护是预期；停止已知自身服务后再启动 |

需要补装浏览器时，在项目根目录显式执行（需要网络；本轮未执行）：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location).Path 'browsers'
& '.\.venv\Scripts\python.exe' -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Chromium installation failed' }
```

## 9. 最小验收清单

- [ ] 核对来源、代码版本和交付哈希；源码中不带凭据、原始数据或数据库。
- [ ] Python 3.11 x64；安装器退出 0；两个环境 pip check 通过。
- [ ] Chromium 在项目 browsers 目录，启动终端使用相同路径。
- [ ] Phoenix `/healthz` 200；应用 `/api/health` 的两部分均 healthy。
- [ ] 8000 主页面可打开；6006 高级入口可访问；无额外 4317 通配监听。
- [ ] 零费用 fixture 有运行历史/检查点；需要完整服务验证时核对新临时验收回执中的 Trace 持久化。
- [ ] 浏览器问题逐项记录；既有 Phoenix 外部请求问题不误记为 PASS。
- [ ] 停止自己启动的进程，端口恢复初态；历史数据保留。
- [ ] 本轮不以真实模型任务作为必要验收；导师物理电脑结果另行记录。

工程与研究边界见 [项目进展汇报](advisor-project-status.md)，现场操作见 [演示指南](demo-walkthrough.md)。
