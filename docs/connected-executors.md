# Connected agents：接管外部 LangGraph 执行器的快照（路线 3）

日期：2026-10-15 · 分支 `feature/agent-studio-v1` 上的后续工作 · 对应进展汇报 P12 的"先做路线 3"。

## 1. 做了什么

Studio 现在可以调试**不在 Studio 里搭建、在自己进程里运行**的 LangGraph 智能体。首个对象是原有的 M1 执行系统（`m1_local.runtime.AgentRuntime` + `m1_local.api`）：

- 执行器保留自己的进程、环境变量、模型凭据和 checkpoint 数据库。Studio **不打开**它的数据库，只调用它的 HTTP 控制接口。
- 在 Studio 中发起运行、浏览执行器已有的运行并导入；运行以 `origin=connected` 镜像到 Studio，图、时间线、Span、检查点、Compare 页面都可用。
- 暂停、恢复、失败重试、fork（按执行器声明的字段编辑）和 replay 都转发给执行器。分支由**执行器自己**在原 LangGraph thread 中创建；Studio 记录父运行、来源检查点和修改字段。
- 执行器自身导出的 OTLP 轨迹（`m1.run`、`m1.node.*`、`m1.model.*`）按 `application_run_id` 归到对应运行，不再作为孤立的外部轨迹出现。

## 2. 为什么选"独立进程 + 控制接口"

| 考虑 | 进程内加载 | 独立进程（本实现） |
|---|---|---|
| 对方改动 | 需要复刻对方的启动/依赖组装 | 只需暴露控制接口；旧系统仅新增只读 `GET /api/graph` |
| 依赖冲突 | 必须与 Studio 同 Python / LangGraph 版本 | 互不影响 |
| 全局副作用 | 对方的 OTel 全局插桩、Playwright 会进入 Studio | 隔离；对方崩溃只显示"离线" |
| checkpoint 归属 | 旧系统数据库有进程排他锁，需停掉原应用 | 原系统继续持有数据库 |
| 忠实性 | 同代码、不同环境 | 原执行器、原环境 |

代价：执行器必须在线；协议要处理不可达和冲突（见 §5）。

## 3. 结构

```
Studio (FastAPI, :8100)                         原 M1 执行器（独立进程）
  studio/connected.py   ConnectedService  ──HTTP──▶  m1_local.api: /api/session /api/graph /api/runs
  studio/connectors/base.py   执行器控制协议          /api/runs/{id} /history /pause /resume /fork /replay
  studio/connectors/m1_legacy.py  旧系统适配器         AgentRuntime + LangGraph + AsyncSqliteSaver
  studio/connectors/m1_legacy_host.py  Studio 启动执行器时使用的宿主（独立数据目录）
  studio/otlp.py   ◀──OTLP/HTTP──  执行器自己的 OpenTelemetry exporter（指向 Studio /v1/traces）
```

**控制协议**（`studio-executor/1`，`connectors/base.py`）：`describe`（图、能力、可编辑字段、输入字段、模型类型）、`health`、`list_runs`、`start`、`run`、`history`（最新在前，含 `checkpoint_id / parent / next / values / source / interrupted`）、`pause`、`resume`、`fork`、`replay`。新执行器类型只需再实现一个 connector。

**从检查点重建可观测信息**（`connected.py`）：

- 每个 `source=loop` 的检查点 C，以其父检查点 P 的 `next` 作为执行的节点，生成一个节点 span；输出为 P→C 的状态差（列表只取追加部分），去掉 `execution_id` 等每次都会变的簿记字段，方便 Compare。时间取两次提交时间（属性 `timing=checkpoint commit times`）。
- `source=update`：执行器为 fork/replay 写入的种子检查点，生成 `fork edit` / `replay seed` span。
- `source=fork`：LangGraph 恢复失败或中断运行时复制出的检查点，不算节点执行。
- 头部状态：运行中为 running 占位 span，暂停为 pause span，失败为下一节点的 error span（重试后失败记录仍保留，与 Studio 工作流一致）。
- fork/replay 子运行只记录来源检查点之后的步骤；之前的节点记为 inherited。

**fork 可编辑字段**：旧系统声明 `plan`（字符串列表）、`worker_instruction`（字符串）和 `next_agent`（Orchestrator 的路由决定，只能选 web_surfer / file_reader / coder / finalizer，2026-11-07 新增）。在 after Orchestrator 修改 `next_agent` 后，LangGraph 会按新值重新计算路由，运行会改走另一条路径。Studio 先按 schema 校验，执行器会再校验一次。其它字段返回 409。

**fork 规则（方案 A，2026-11-07）**：在"某节点 X 之后"的检查点，只能修改 **X 在这一步写入的**声明字段，例如 after Planner 只能改 `plan`，after Orchestrator 只能改 `worker_instruction`；想改更早的步骤，就从那一步的检查点 fork。执行从该检查点的 `next` 继续，之前的节点不会重跑，在 Compare 中显示为 inherited。

"这一步写了哪些字段"的来源：
- SDK 执行器在 history 里报告 `writes`，来自 LangGraph 的任务结果，是准确值；
- 旧执行器没有这个字段，Studio 退而比较与父检查点的状态差。这种方式会漏掉"写入了但值没变"的情况。

Studio 工作流用同样的规则：after X 只能改 `nodes.X.output`；after Start 可以改输入；条件节点之后没有可编辑字段，只能重放。

## 4. 使用

1. `.\scripts\start-studio.ps1`，打开 http://127.0.0.1:8100 → **Connected agents** → **Connect executor**。
2. 选择：
   - *The Studio starts it*：选择端口和模型（`Deterministic` = RuleBasedGateway，离线，有标注；`Real model` 使用 Studio 环境里的 `OPENAI_API_KEY`）。数据在 `data/studio/connected/<connector id>/`，与你自己的 `data/app.sqlite` 分开。点 **Start executor**。
   - *I start it myself*：先运行 `m1-local serve`（默认 :8000），填 `http://127.0.0.1:8000`。可通过 **Browse the executor's own runs** 导入已有运行（含其分支关系）。
3. 填写 Task → **Run on executor**。离线模式下含 "code" 的任务走 Coder，提到文件且给出 Allowed files 的任务走 File reader，其它任务走 WebSurfer（需要真实浏览器和模型）。
4. 运行页 → **Checkpoints & fork**：选 *after Orchestrator → pause_gate* 修改 `worker_instruction`，或选 *after Planner* 修改 `plan` → **Create branch and run** → **Compare**。

## 5. 限制（如实说明）

- 执行器一次只运行一个任务（`MAX_ACTIVE_RUNS=1`）；冲突时返回 409，fork 会保留为 paused，空闲后再 Resume。
- 暂停只在执行器的 `pause_gate`（下一个 worker 之前）生效；执行器没有 cancel 接口。
- 节点耗时是检查点提交时间差，不是节点内部计时；更细的模型耗时/Token 看挂上来的 OTLP span（旧系统默认隐藏提示词与回复内容）。
- 可编辑字段由执行器决定：M1 是 `plan`、`worker_instruction` 和 `next_agent`（下一步交给哪个工作者）。
- 执行器离线时，Studio 只显示上次同步的状态；控制操作返回 502。
- 只支持 loopback 地址，无认证（与 Studio 其它部分一致）。
- 旧执行系统验证的是"Studio 之外的执行器"，对象仍是自有系统。任意第三方 LangGraph 智能体通过 **studio-executor SDK** 接入，见 [studio-executor-sdk.md](studio-executor-sdk.md)。
- 离线 RuleBasedGateway 会把提示词前 200 个字符写进回答，其中含 execution id，因此 Compare 中 Finalizer 常显示 differs，这是网关特性而非状态差异。

## 6. 验证（2026-10-15，云端 Linux 环境）

| 内容 | 方式 | 结果 |
|---|---|---|
| 全量测试 | `pytest -q` | 203 passed，10 skipped（8 个 Windows 安装器测试、2 个付费模型测试） |
| 连接器集成（真实 AgentRuntime + LangGraph + SQLite，进程内传输） | `tests/studio/test_connected_executor.py` | 7 passed：图与 schema 来自执行器；镜像运行；字段校验；fork 改 plan 导致路径变化，源运行检查点不变；replay；暂停/恢复；失败/重试；导入已有运行及分支关系；冲突；OTLP 关联 |
| 三进程端到端 | `tests/studio/test_connected_e2e.py` | passed：Studio 启动执行器进程 → 运行 → 改 `worker_instruction` 分叉 → Compare（planner inherited、coder differs）→ 源运行检查点不变 → 执行器 OTLP span 挂到运行 → 停止执行器后历史仍可读 |
| 浏览器 | `scripts/verify_connected_ui.py`（Chromium，`docs/evidence/connected/ui/`） | 10/10 PASS，console/page/network 错误 0 |
| V1 回归 | `scripts/verify_studio_ui.py` | 10/10 PASS |
| **Windows 复核（2026-10-15 23:05，用户机器，Python 3.11 `.venv`）** | `pytest -q tests/studio`；`scripts/verify_connected_ui.py` | **40 passed**（71 s）；**10/10 PASS**，console/page/network 错误 0 |

环境说明：云端无法访问 PyPI/npm，云端测试使用 `.venv` 的纯 Python 包 + Python 3.13，`ormsgpack / xxhash / orjson / jiter / uuid_utils / grpc / tiktoken` 用纯 Python 替身（仅云端测试环境）；前端用 esbuild 构建。上表最后一行是在 Windows 原环境（真实编译依赖）上的复核结果。**尚未调用真实模型**；尚未用 vite 重新构建前端。复核命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/studio
.\.venv\Scripts\python.exe scripts\verify_connected_ui.py
cd frontend; npm run build   # 用 vite 重新生成 web/studio（替换云端 esbuild 构建）
```
