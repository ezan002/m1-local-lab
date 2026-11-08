# Agent Studio 使用说明

Agent Studio 是一个本地运行的智能体调试工作台。它可以接入正在运行的 LangGraph 智能体，查看每一次运行的结构、
时间线、span 和编号步骤（Steps），并从任一检查点重放（replay）或修改后分支（fork），再把分支与原运行对比。
别处产生的运行记录（OpenTelemetry trace、Codex 会话）也可以导入查看。

所有程序只监听本机 `127.0.0.1`，数据保存在本地 SQLite 文件里。

---

## 1. 部署

### 1.1 最快：看预置的演示数据（任意系统，不需要模型 key，不需要 Node.js）

需要 Python 3.11 和 Git。

```bash
git clone https://github.com/ezan002/m1-local-lab.git
cd m1-local-lab
python3.11 -m venv .venv                 # Windows: py -3.11 -m venv .venv
. .venv/bin/activate                     # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e .
python -m m1_local.cli studio --demo
```

浏览器打开 <http://127.0.0.1:8100>。`--demo` 会把仓库里的 `demo-data/` 复制到 `data/studio-demo/` 再打开，
仓库文件不会被改动。演示数据包括：

| 类型 | 内容 |
|---|---|
| Connected agents | M1 多智能体（真实模型 qwen3:8b）的运行及其分支；第三方 local-deep-researcher 的运行 |
| External traces | 示例研究智能体的 OpenTelemetry 轨迹（含一次工具报错）；一份由 Codex 会话转换来的轨迹（合成样本） |
| Studio 工作流（默认隐藏） | Research → Review 工作流的运行、检查点和分支 |

想重新从演示开始：删除 `data/studio-demo/` 后再启动。

### 1.2 日常使用（Windows）

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1   # 只需一次
.\scripts\start-studio.ps1                                                      # 自己的数据：data/studio/
.\scripts\start-studio.ps1 -Demo                                                # 演示数据
```

`install.ps1` 还会安装 Playwright 浏览器和 Phoenix 等旧工具，耗时较长。只用 Studio 时，1.1 的 `pip install -e .`
就够了。

### 1.3 使用真实模型（可选）

Studio 和智能体都通过 OpenAI 兼容接口调用模型。

- **OpenAI**：启动前设置 `OPENAI_API_KEY`（可选 `STUDIO_DEFAULT_MODEL`，默认 `gpt-4.1-mini`）。
- **本地 Ollama（免费，数据不出本机）**：安装 [Ollama](https://ollama.com) 并 `ollama pull qwen3:8b`，然后在启动智能体的
  终端里设置：

  ```powershell
  $env:OPENAI_BASE_URL = "http://localhost:11434/v1"
  $env:OPENAI_API_KEY  = "ollama"          # 占位值，Ollama 不校验
  $env:ORCHESTRATOR_MODEL = "qwen3:8b"
  $env:MODEL_TIMEOUT_SECONDS = "600"
  $env:RUN_TIMEOUT_SECONDS = "1800"
  ```

不设置 key 时，所有组件都可以用 **deterministic（确定性）** 模式运行：回答由脚本生成，界面上会标明，不算模型结果。

### 1.4 修改前端后重新打包

运行不需要 Node.js，打包好的界面已提交在 `web/studio/`。改了 `frontend/` 才需要：

```powershell
cd frontend; npm install; npm run build
```

---

## 2. 界面总览

左侧有三个入口：

| 页面 | 用途 |
|---|---|
| **Connected agents** | 接入并控制正在运行的智能体，查看它们的运行 |
| **External traces** | 导入或接收别处产生的运行记录（只读） |
| **Settings** | Studio 自带工作流使用的模型地址和默认模型（不影响接入的智能体） |

Workflows、Runs、Integrations 三个页面暂时隐藏（代码保留）。在浏览器控制台执行
`localStorage.setItem("studio.showHidden", "1")` 后刷新即可显示。

---

## 3. 接入智能体（Connected agents）

智能体在**自己的进程**里运行，并开一个本机 HTTP 端口；Studio 通过这个端口让它运行、读取检查点、fork 和 replay。
智能体的代码和数据库都留在它自己那边。

点 **Connect executor**，有两种方式：

- **The Studio starts it（launch）**：由 Studio 启动 M1。选端口和模型（Deterministic 离线可用），点 **Start executor**。
- **I start it myself（attach）**：自己先启动智能体，再填它的地址和启动时打印的 token。

常用的几个智能体：

| 智能体 | 启动命令 | Studio 里选择 |
|---|---|---|
| M1（离线） | Studio 直接启动 | M1 executor，模型 Deterministic |
| M1（真实模型） | 先按 1.3 设好环境变量，再运行 `python -m m1_local.studio.connectors.m1_legacy_host --port 8011 --data-dir data\studio\m1-qwen --model openai --otlp-endpoint http://127.0.0.1:8100/v1/traces --studio-url http://127.0.0.1:8100` | M1 executor，attach，`http://127.0.0.1:8011` |
| 示例客服智能体 | `python examples/langgraph_agent/support_agent.py --port 8020` | Any LangGraph agent，`http://127.0.0.1:8020` + 打印的 token |
| local-deep-researcher（第三方） | 用它自己的虚拟环境运行 `examples/third_party/local_deep_researcher_studio.py --repo <它的目录> --port 8030` | Any LangGraph agent，`http://127.0.0.1:8030` + token |

**接入你自己的 LangGraph 智能体**：图编译时带上 checkpointer，再用 `studio_executor` SDK 包一层，声明输入字段和
fork 时允许修改的字段。对方代码不用改。详见 [studio-executor-sdk.md](docs/studio-executor-sdk.md)，例子见
`examples/langgraph_agent/support_agent.py`。

**卡片显示 offline**：智能体没在运行，或重启时换了 token。用原来的 `STUDIO_EXECUTOR_TOKEN` 重启，或删掉卡片重新添加。

卡片上填写任务，点 **Run on executor** 开始一次运行；下方列出这个智能体的历史运行。

---

## 4. 查看一次运行

点进任一运行，顶部显示状态、模型类型（real model / deterministic）和分支（branches）。下面是几个标签页：

| 标签页 | 显示什么 | 怎么用 |
|---|---|---|
| **Graph** | 智能体的结构图。绿色是这次走过的节点，变淡的是没走到的，虚线是条件边 | 点节点看它的 span |
| **Timeline** | 每一步的开始时间和耗时 | 找最慢的一步 |
| **Spans** | 所有 span 的树（运行 → 节点 → 模型 / 工具调用） | 按名称、类型、状态筛选；右侧看输入、输出、属性 |
| **Steps** | 运行拆成编号步骤 1..N：每次模型调用、工具调用、路由判断、无调用的控制节点各算一步（规则见下） | 顶部是统计（步数、模型调用、工具调用、token、报错数）；展开“How N spans became M steps”看每个 span 和步骤的对应关系；点一步看输入输出，可跳回对应 span |
| **Checkpoints & fork** | 每一步结束时的完整状态 | 重放或分支，见第 5 节 |
| **Compare** | 两个相关运行逐节点对比 | 见第 5 节 |
| **Run input/output** | 整次运行的输入和最终输出 | |

**step 和 span 的关系**：一个节点 span 可以包含多个 step。例如 Researcher 节点的一次执行产生 4 个 span：

```
Researcher（节点 span）            ← 外壳，不算步骤
 ├─ llm：模型决定调用工具          → 第 1 步
 ├─ tool：执行 word_count          → 第 2 步
 └─ llm：模型根据结果写结论        → 第 3 步
```

- 和节点比，step 更细：一个节点里有几次模型或工具调用，就拆成几步。
- 和全部 span 比，step 不会更多：最底层的每个调用 span 本身就是一步；其余 span 是外壳（节点、运行、chain 序列）、
  同一次调用的重复记录（两套埋点各记一次）或没有操作的连接件（提示词拼接、解析、保存检查点）。
- 例：M1 的一次真实运行有 26 个 span，实际是 3 次模型调用和 1 次路由判断，所以是 4 步；26 个 span 里有 3 个重复记录、
  14 个外壳、5 个连接件。Steps 页的对照表会逐个列出。

结构图从哪来：接入的智能体，结构来自它运行时的 LangGraph 图对象，所以没走到的节点也会画出来；外部轨迹没有图对象，
结构由 span 的父子关系拼成，只包含实际执行过的部分。

---

## 5. 重放、分支与对比

在 **Checkpoints & fork** 里选一个检查点，例如 “#3 after Orchestrator”（Orchestrator 执行完之后的存档）：

- **Replay from here**：不改任何东西，从下一个节点继续跑。用来判断结果的变化是不是模型本身的随机性。
- **Fork with edits**：先改这一步产生的结果，再继续跑。

规则：在 “after X” 的检查点，**只能改 X 这一步写出的字段**。想改更早的节点，就去那个节点的检查点 fork。检查点之前的
节点不重跑，直接继承（Compare 里标为 inherited）。replay 和 fork 都生成一条新运行，原运行不变。

M1 在 after Orchestrator 可以改 `worker_instruction`（给下一个工作者的指令）和 `next_agent`（下一步交给谁），在
after Planner 可以改 `plan`。

**Compare** 的标记：

| 标记 | 含义 |
|---|---|
| inherited | 在分叉点之前，没有重跑 |
| same | 重跑了，结果相同 |
| differs | 重跑了，结果不同 |

建议同一个检查点同时做一次 fork 和一次 replay：只在 fork 里变、在 replay 里没变的部分，才更可能是修改带来的。

---

## 6. 导入外部运行记录（External traces）

页面上方是导入区，下方是已导入的列表。外部记录只能查看，不能 fork。

- **上传文件**：OTLP JSON 格式的 trace 文件（单个对象、数组，或一行一个）。
- **Codex 会话**：直接上传 Codex 的会话文件，Studio 会自动识别并转换（会话 → 每一轮 → 模型调用 / 工具调用 / 文件修改）。
  系统提示词和账号信息不会导入。会话文件位置：
  - Windows：`%USERPROFILE%\.codex\sessions\<年>\<月>\<日>\rollout-*.jsonl`
  - macOS / Linux：`~/.codex/sessions/<年>/<月>/<日>/rollout-*.jsonl`
- **实时接收**：任何用 OpenTelemetry 埋点的程序，把 OTLP/HTTP 发到 `http://127.0.0.1:8100/v1/traces` 即可。例子：
  `python examples/external_agent/research_agent.py --topic "tool failures" --mode deterministic`
  （加 `--fail-tool` 可以看到报错的显示方式）。

某一步报错但整体跑完的运行，会显示灰色提示“运行完成，但有 N 步报错”，而不是失败。

---

## 7. 常见问题

| 问题 | 处理 |
|---|---|
| 启动时提示端口被占用 | 已有一个 Studio 在运行，或换端口：`--port 8101` |
| 页面没有样式 / 是旧界面 | `web/studio/` 里的文件不是同一次打包的：在 `frontend/` 下重新 `npm run build`，浏览器按 Ctrl+F5 |
| 接入的智能体卡片 offline | 智能体没启动或 token 变了，见第 3 节 |
| 新增的 fork 字段没出现 | 重启智能体后，Studio 也要重启一次（或刷新运行页） |
| 本地模型很慢 | qwen3:8b 跑一次 M1 约 6 分钟；把超时调大（1.3 的两个环境变量） |

更多技术说明：[connected-executors.md](docs/connected-executors.md)（接入原理与 fork 规则）、
[studio-executor-sdk.md](docs/studio-executor-sdk.md)（SDK）、[advisor-qa-2026-11.md](docs/advisor-qa-2026-11.md)（问答）。
