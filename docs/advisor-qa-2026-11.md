# 导师疑问核对答复（2026-10-19 进展核对会）

日期：2026-11-07。依据：代码、实际运行结果、自动化测试。每条后面的"依据"可以直接打开代码或运行测试核对。

---

## 一、Connected agents（接入外部 LangGraph 智能体）

### 1. 端口上运行的是什么？Studio 和智能体怎么通信？

智能体在**自己的进程**里运行，同时在本机端口（如 8020、8030）开一个 HTTP 控制服务。Studio 通过这个端口发指令："开始运行""暂停""从某检查点分叉"等；智能体在自己的进程里执行，执行过程写进它自己的检查点数据库。Studio 再读取这些检查点，在界面上显示。

为什么不把智能体代码直接放进 Studio 项目：
- 对方的依赖（Python 版本、LangGraph 版本、模型 SDK）可能和 Studio 冲突；
- 对方崩溃不会拖垮 Studio；
- 对方继续使用自己的数据库和凭据，不需要交出来；
- 执行环境就是原来的环境，结果更可信。

依据：`src/studio_executor/server.py`（智能体侧的控制服务），`src/m1_local/studio/connectors/generic.py`（Studio 侧的调用），协议说明见 `docs/studio-executor-sdk.md` §4。

### 2. token 是什么？

是一个**访问口令**，不是智能体的标识。控制接口能让智能体执行任务，如果没有口令，本机上任何程序、甚至浏览器里的恶意网页都可能向这个端口发指令。所以每个请求都必须在 `x-studio-executor-token` 头里带上口令。此外还有两道限制：拒绝带浏览器 `Origin` 头的请求；只接受本机地址。

口令可以在启动智能体前用环境变量 `STUDIO_EXECUTOR_TOKEN` 指定；不指定时智能体会随机生成一个并打印出来。Studio 把它存在本地数据库里，界面上打码显示。

依据：`src/studio_executor/server.py` `create_asgi_app`；测试 `tests/test_studio_executor_sdk.py`（token、Origin、Host 校验）。

### 3. 能显示多少信息、能否 fork / replay，取决于插桩吗？

**不取决于插桩。** fork / replay 读写的是 LangGraph 自带的**检查点**：图在编译时带上 checkpointer（`builder.compile(checkpointer=...)`）后，LangGraph 每执行完一步就自动保存一次完整状态。不需要在智能体代码里埋点，也不需要 OpenTelemetry。

接入一个 LangGraph 智能体需要的只有两点：
- 用带 checkpointer 的方式编译它的图；如果原图没带，在包装脚本里加上就行，不改对方代码。
- 在包装脚本里声明三件事：
  - 输入字段；
  - **哪些状态字段允许在 fork 时修改**；
  - 节点图标类型（可选）。

能改哪些字段由包装脚本声明，这是唯一需要"按需配置"的地方。

以 local-deep-researcher 为例：对方代码一行没改。包装脚本 `examples/third_party/local_deep_researcher_studio.py` 共约 100 行（含说明和启动代码），其中描述输入和可编辑字段的配置约 30 行。我们自己写的客服示例智能体，与 Studio 相关的部分只有约 15 行。

依据：`src/studio_executor/core.py`（`stream_mode="checkpoints"`、`aget_state_history`、`aupdate_state`）。

### 4. 检查点的数量由什么决定？

由 LangGraph 的执行步数决定：
- 开始时有一个输入检查点；
- 之后**每执行完一步（一个 super-step）保存一个**。如果同一步里有多个节点并行，它们共用一个检查点。
- 节点在循环里执行几次，就有几个对应的检查点。

这与插桩无关。

### 5. 会上的更正

| 会上的说法 | 实际情况 |
|---|---|
| "OpenTelemetry 是所有 LangGraph 都会有的" | **不对。** LangGraph 默认不产生 OpenTelemetry 数据，需要另外安装插桩（如 OpenInference 的 LangChain instrumentor）或使用 LangSmith。我们接入 LangGraph 智能体用的是检查点，不依赖 OTel。 |
| "左边的 M1 没有接大模型" | M1 当时是用 **Deterministic 模式**启动的（`RuleBasedGateway`，按规则生成回答）。M1 本身支持真实模型：连接时把 Model 选成 Real model 即可。导师的印象"M1 有确定性版本和真实版本"是对的。 |
| "右边的 local-deep-researcher 是 LangGraph 官网上的智能体" | 准确地说，它是 LangChain 官方组织在 GitHub 上开源的项目 `langchain-ai/local-deep-researcher`，用 LangGraph 编写。它用的模型是本机 Ollama 上的 qwen3:8b。 |

---

## 二、fork 和 replay 的逻辑

### 6. 导师指出的问题：在第 5 个检查点 fork 为什么能改前面的节点？

**问题属实，已经修改（方案 A）。** 原先的实现允许在任意检查点修改所有已执行节点的输出，但执行只从这个检查点的下一个节点继续，前面的节点不会重跑。这样一来，"改前面节点的输出"实际上只是修补了保存的状态，不同检查点之间也几乎没有区别。导师说得对，这样确实容易混淆。

**现在的规则：**
- 在 "after X"（X 节点之后）的检查点，fork **只能修改 X 在这一步产生的结果**。
- 想改更早的节点，就从那个节点之后的检查点 fork。
- **Start 之后**的检查点：可以改运行输入，因为 Start 节点产生的就是输入。
- **条件节点之后**的检查点：没有可改的内容，条件结果是由其他值计算出来的；但可以 replay。
- **接入的智能体**：只能改这一步写入的、且被声明为可编辑的字段。"这一步写了哪些字段"由智能体从 LangGraph 的任务结果中准确报告，所以即使写入的值和之前一样，也能正确识别。
- 界面上明确写出"从哪个节点继续执行"。改了不允许的字段，接口会拒绝，并提示去对应的检查点 fork。

依据：
- 代码：`src/m1_local/studio/engine.py` 的 `_editable`、`_apply_changes`；`src/m1_local/studio/connected.py` 的 `written_keys`、`fork`；
- 测试：`test_fork_rejects_invalid_requests`、`test_sdk_agent_run_fork_replay_through_the_studio`。

### 7. fork / replay 是从头跑，还是从当前检查点跑？

**从当前检查点的下一个节点开始跑**，检查点之前的节点不重跑，直接继承原来的结果。fork 和 replay 都是这样。

两者的区别：

| | 改状态吗 | 结果 |
|---|---|---|
| replay | 不改 | 状态原样不动，继续执行 |
| fork | 改 | 先改这一步的结果，再继续执行 |

两者都会产生一个新的运行（分支），原运行和它的检查点不会被修改。

导师提到的另一种做法是"从头重跑前面的节点，到第 5 个检查点再用修改后的值"。我们没有采用：对调用大模型的节点来说，重跑前面的节点会得到不同的结果，就不再是"在同一个状态上只改一处"了。

### 8. Compare 中 inherited / same / differs 的含义

| 标记 | 含义 |
|---|---|
| inherited | 该节点在分叉点之前，没有重跑，直接继承 |
| same | 重跑了，结果和对比的运行相同 |
| differs | 重跑了，结果不同 |

例如在第 5 个检查点 fork：前 5 步都是 inherited；第 6、7 步如果结果没变就是 same；之后结果变了就是 differs。导师会上的理解完全正确。

依据：`src/m1_local/studio/app.py` 的 compare 接口。

---

## 三、span 的含义与显示

### 9. span 到底是什么？能不能包含多个 step？

**span 是 OpenTelemetry 的基本单位，表示"一段有名称、有起止时间的工作"。** span 之间有父子关系，组成一棵树。在我们的平台里有四层：

```
workflow 根 span（整次运行）
 └─ 节点 span（一个节点的一次执行，例如 Researcher 第 1 次）
     ├─ llm span（一次模型调用）
     ├─ tool span（一次工具调用）
     └─ llm span（又一次模型调用）
```

**一个节点 span 可以包含多个 step。** 实际运行中，Researcher 节点的一次执行就包含 3 步：
1. 模型决定调用工具；
2. 执行 word_count 工具；
3. 模型根据工具结果写出结论。

会上看的 Planner 和 Reviewer 都只调用一次模型，所以节点 span 和它下面的 llm span 看起来几乎一样。导师觉得"llm 没什么信息量"，原因就在这里。

两者记录的内容不同：

| | 节点 span（Agent） | llm span |
|---|---|---|
| 记录什么 | 这个智能体这次执行的整体输入、最终输出、工具调用汇总、总 token | 某一次模型调用：发送的完整消息、返回内容、模型名、延迟、本次 token |
| 什么时候有区别 | 只调一次模型、不用工具时，内容和 llm span 基本相同 | 有工具调用或多轮调用时，一个节点下会有多个 llm span，每个都不同 |

**span 数量的计算：** Research → Review 工作流的一次运行共 26 个 span：
- 1 个根 span；
- 13 次节点执行：Start、Planner、3×Researcher、3×Reviewer、3×条件判断、Final；
- 12 个模型或工具调用。

会上看到"比节点多一个"，多出的那个就是根 span。

**改进建议（待和导师确认）：** 当一个节点下只有一个 llm span 时，默认把它折叠起来，减少重复信息。

依据：实际运行打印的 span 树（`scripts/` 中任意 deterministic 运行均可复现）；`src/m1_local/studio/engine.py` 中节点执行与 `llm`、`tool` span 的创建。

### 10. 顶层 span 的父节点为什么不统一？

三类运行的根 span 来源不同：

| 运行类型 | 根 span | 图上是否画出 |
|---|---|---|
| Studio 工作流 | 引擎自动创建，名称是工作流名（如 "Research → Review loop"） | 不画。图来自工作流定义，根 span 不是节点 |
| Connected agents | Studio 为镜像运行生成一个根 span | 不画。图来自智能体自己的 LangGraph 图 |
| 外部轨迹 | 外部程序自己定义，例如示例里的 "research_session" | 画出来。外部轨迹的图就是 span 树本身 |

所以，会上看到工作流运行里 Brief 的父节点是工作流名称，而外部轨迹里是 research_session，这两者都是"这条轨迹的根"。

**改进建议：** 在 span 列表里给所有根 span 统一加上 "trace root" 标记。

### 11. 外部轨迹里的"并行"和显示顺序为什么奇怪？

两件事：

1. **那个示例其实不是并行的。** 示例程序是顺序执行的：research_session → analyst（内部依次调用模型、word_count）→ critic（内部调用模型）。图上 analyst 和 critic 排在同一列，是因为图按**调用树的层级**排列：x 轴是层级，同一层排在同一列。它表示"谁包含谁"，不表示"同时执行"。执行先后要看 Timeline。

2. **列表里子节点排在父节点前面，是一个显示 bug，已修复。** span 原先按写入数据库的先后排列；而 OpenTelemetry 导出器在 span **结束时**才发送，所以子 span 先到、先入库，父 span 反而排在后面。Studio 自己的工作流在 span 开始时写入，所以没有这个问题，只有外部轨迹会出现。现在改为按调用树排序：父节点在前，同一层按开始时间排列。

依据：`examples/external_agent/research_agent.py`；`src/m1_local/studio/app.py` 的 `tree_order`；测试 `test_external_spans_listed_parent_first_even_when_children_arrive_first`。

---

## 四、External traces（外部轨迹）

### 12. 外部轨迹的结构是从哪来的？任意智能体都能显示吗？

结构来自 OpenTelemetry span 自带的**父子关系**（每个 span 记录自己父 span 的 id）。节点类型来自 OpenInference 规范的属性 `openinference.span.kind`（AGENT / LLM / TOOL / CHAIN），名称来自 `agent.name`、`tool.name` 或 span 名。

所以，**任何框架只要按 OpenTelemetry 发送 span，就能显示**，和是不是 LangGraph 无关。导师的判断正确：结构信息是对方在埋点时就已经写进 span 里的，平台只负责读出来并展示。

### 13. 会上的更正

| 会上的说法 | 实际情况 |
|---|---|
| "因为我们是用 Phoenix 的代码去做的" | **不对。** 接收和解析是 Studio 自己写的（`src/m1_local/studio/otlp.py`）。我们只是沿用了 Phoenix 采用的 OpenInference 属性规范。 |
| "Integrations 右侧是导入历史记录" | **不准确。** 它是一个实时接收端点（`/v1/traces`），智能体运行中或运行后都可以向它发送。 |

### 14. 没有用 OpenTelemetry 的原始轨迹怎么办？为什么不能直接导入文件？

- **导入文件：已实现。** External traces 页面顶部新增了 "Import traces"，可以上传 OTLP JSON 文件（单个对象、数组或 JSON Lines）。实时端点也保留着，折叠在同一区域里。依据：接口 `POST /api/traces/import`，测试 `test_trace_file_import_json_jsonl_and_errors`。
- **非 OTel 的原始轨迹：已实现第一种格式（Codex 会话记录）。** 上传 Codex 的 `rollout-*.jsonl` 文件后，Studio 会自动识别并转换成 span，再走同一条导入流程（`src/m1_local/studio/converters.py`）。映射规则：
  - 整个会话 → 根 span（CHAIN）；
  - 每一轮任务 → AGENT span，输入是用户消息，输出是最终回复；
  - 每次模型响应 → LLM span，带 token 数；
  - 每次工具调用 → TOOL span；命令退出码非 0 时标为错误；
  - 文件修改 → 挂在发起修改的工具调用下面。

  系统提示词、账号 ID 等内容不导入。

  实测：一次 44 秒的 Codex 会话转换成 10 个 span，可以清楚地看到"模型 → 命令 → 模型 → 写文件 → 跑测试 → 总结"的过程。测试：`tests/studio/test_trace_converters.py`。
- **限制：** 原始轨迹里没有记录的信息，转换后也无法凭空得到。例如 Codex 不记录模型调用的开始时间，只能按"输入准备好的时刻"推断。凡是推断出来的地方，都在 span 属性 `conversion.timing` 里写明。

---

## 五、其他

### 15. Workflow 里的 Agent / Tool / Condition 节点兼容什么？

| 节点 | 兼容范围 |
|---|---|
| Agent | 两种提供者：①**任何 OpenAI 兼容的 Chat Completions 接口**（OpenAI、本地 vLLM、Ollama 的 `/v1` 接口等，在 Settings 里配置地址）；②deterministic（脚本回答，离线）。支持工具调用，最多调用几轮由 Max tool rounds 限定。 |
| Tool | 三个内置工具：calculator（安全计算表达式）、http_get（抓取网页文本）、word_count（统计词数）。 |
| Condition | 7 种运算：equals、not_equals、contains、not_contains、greater_than、less_than、is_true；Max true visits 用于防止死循环。 |
| Output | 用模板拼接结果。 |
| Start | 定义运行时的输入字段。 |

依据：`src/m1_local/studio/schema.py`，`src/m1_local/studio/runtime_support.py`。

### 16. Settings 管哪些智能体？

| 对象 | 是否受 Settings 控制 |
|---|---|
| Studio 工作流里的 Agent 节点 | **是。** 使用 Settings 里的 Base URL 和 Default model；API key 只从服务器环境变量 `OPENAI_API_KEY` 读取。 |
| Integrations 测试智能体（openai 模式） | **部分。** 它是一个独立进程，只继承服务器的环境变量（key、`OPENAI_BASE_URL`、`STUDIO_DEFAULT_MODEL`），**不读取** Settings 页面里填写的值。会上说"也管"不准确。 |
| Connected agents | **否。** 各自使用自己的模型配置：M1 在连接时选择模型；local-deep-researcher 用它自己 `.env` 里的 qwen3:8b。 |

### 17. Integrations 左侧的测试智能体是什么？

是一个约 100 行的 Python 程序（`examples/external_agent/research_agent.py`），**不是 LangGraph**，只用 OpenTelemetry 埋点，用来演示外部轨迹的接入。它包含两个顺序执行的"智能体"：analyst（调模型、调 word_count 工具）和 critic（调模型）。

- 默认是 **deterministic**（脚本回答），不调用模型；
- 选 openai 模式时才调用云端模型，默认是 **gpt-4.1-mini**，不是会上说的 GPT-4。

最初没有直接用 M1 生成测试轨迹，是因为 M1 依赖多、运行慢，而这里只需要一个轻量的轨迹来源，用来验证接收和显示的链路。

### 18. Run input/output 里的"总体"是什么意思？能看到每一步的输入输出吗？

- **Run input**：整个运行的输入，也就是 Start 节点收到的字段（例如 research topic）。
- **Run output**：所有 Output 节点的最终结果（例如 Final brief）。

每一步的输入输出目前**分散在各个 span 里**：Span 详情里有每个节点和每次调用的输入、输出。

导师提到的"数据流卡片"可以在这个基础上做：把同一份数据经过哪些节点、在哪里被修改，串成一条线显示出来。这需要和俊杰老师讨论后再定。

---

## 本轮已完成的修改（对应会上要求）

1. **界面精简**：侧栏只保留 Connected agents、External traces、Settings。Workflows、Runs、Integrations 功能保留，前端隐藏。
2. **External traces 改为"上面导入、下面展示"**：支持上传文件，实时端点信息也放在这里。
3. **fork 逻辑改为方案 A**：见第 6 条。
4. **外部轨迹 span 排序修复**：见第 11 条。

验证：Studio 与 SDK 测试 50 个全部通过；三个浏览器验证脚本全部通过。

## 待办

- 用真实模型（本机 qwen3:8b）运行 M1，并测试 fork（阶段 4）。
- 两个界面改进建议，待导师确认：单个 llm span 默认折叠；根 span 统一标记。
