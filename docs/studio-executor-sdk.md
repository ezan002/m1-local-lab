# studio-executor SDK：让任意 LangGraph 智能体接入 Agent Studio

日期：2026-10-15 · 包：`src/studio_executor/`（不依赖 Agent Studio / m1_local，只依赖 LangGraph；`serve` 另需 uvicorn）

## 1. 解决什么问题

路线 3 第一步只对接了本项目的旧执行系统。这个 SDK 把同一套控制能力做成通用包装：**任何编译好的 LangGraph 图 + 它自己的 checkpointer**，加几行代码就能在 Studio 的 *Connected agents* 里运行、暂停、恢复、失败重试、从任意检查点 fork（编辑声明过的字段）和 replay，并与原运行对比。

智能体仍在**自己的进程**里运行：自己的依赖、模型凭据和 checkpoint 数据库（Memory / SQLite / Postgres 均可）。Studio 只通过 HTTP 控制接口与它交互。

## 2. 接入步骤

```python
from studio_executor import ForkField, StudioExecutor, serve_async

graph = builder.compile(checkpointer=saver)            # 你原有的图和 checkpointer，不需要改

executor = StudioExecutor(
    graph, name="Support answer agent",
    input_fields=[{"name": "question", "label": "Customer question", "default": ""}],
    make_input=lambda inputs: {"question": inputs["question"]},   # Studio 输入 -> 图的输入
    fork_fields=[ForkField("category", "string", "Edit after classify"),
                 ForkField("review", "string", "APPROVED or REVISE: <reason>")],
    node_types={"classify": "agent", "lookup": "tool"},           # 可选，只影响 Studio 图标
    registry_path="data/my-agent/runs.sqlite",                    # 可选；运行记录在重启后保留
)
await serve_async(executor, port=8020)                            # 或同步版本 serve(executor, port=8020)
```

启动时会打印 URL 和 token（或读取环境变量 `STUDIO_EXECUTOR_TOKEN`）。然后在 Studio：**Connected agents → Connect executor → Agent type: Any LangGraph agent (studio-executor SDK)**，填 URL 和 token。

完整示例：`examples/langgraph_agent/support_agent.py`。这是一个独立编写的客服答复智能体（classify → lookup → draft → review ⟲ → finalize，使用 AsyncSqliteSaver），只有文件末尾约 15 行与 Studio 有关：

```powershell
.\.venv\Scripts\python.exe examples\langgraph_agent\support_agent.py --port 8020                 # 离线确定性
$env:OPENAI_API_KEY = '<key>'; .\.venv\Scripts\python.exe examples\langgraph_agent\support_agent.py --port 8020 --mode openai
```

## 3. 语义

| 操作 | SDK 的做法 |
|---|---|
| 运行 | 每次运行使用新的 LangGraph thread；用 `stream_mode="checkpoints"` 跟踪本运行的分支头 |
| 暂停 | 在下一个检查点提交后停止流（下一步尚未开始，已用测试验证），状态 `paused`；恢复时从分支头继续 |
| 图内 `interrupt()` | 识别为 `paused`；Resume 时以 `Command(resume=value)` 继续（Studio 按钮传 `None`；HTTP 接口可传 `value`） |
| 失败与重试 | 异常记为 `failed`（含类型、消息、traceback）；Resume 从最后提交的检查点重新执行失败的步骤 |
| Fork | `aupdate_state(源检查点, 修改)` 在同一 thread 中创建新分支，新运行状态为 `paused`，Resume 后执行（Studio 会自动 Resume） |
| Replay | 从源检查点以不修改的状态继续执行，形成新分支 |
| 源运行 | 分支只在源检查点之后追加新检查点；源运行的头和历史不变（测试断言） |

**可编辑字段的限制**：只允许顶层、替换语义（`LastValue`）的状态键。带 reducer 的键（如 `Annotated[list, operator.add]`、`add_messages`）在构造时就被拒绝，因为对它们的"编辑"会被合并而不是替换。字段类型：`string`、`string_list`、`number`、`boolean`、`json`；SDK 和 Studio 都会校验。

## 4. 协议（studio-executor/1）

前缀 `/studio/v1`，JSON：`GET /describe`、`GET /health`、`GET|POST /runs`、`GET /runs/{id}`、`GET /runs/{id}/history`、`POST /runs/{id}/pause|resume|fork|replay`。history 最新在前，每项包含 `checkpoint_id / parent_checkpoint_id / created_at / step / source / next / values / interrupted / writes`。`writes` 是产生这个检查点的那一步中，各节点写入的状态键，格式为 `{node: [keys]}`；Studio 据此只允许修改"这一步写入的字段"（方案 A）。

安全：每个请求必须带 `x-studio-executor-token`；带浏览器 `Origin` 头的请求一律拒绝；Host 必须是监听的 loopback 地址；`serve` 只绑定 127.0.0.1。Studio 只接受 loopback URL，token 存在 Studio 数据库中，API 返回时打码。

## 5. 限制

- 默认 `max_active_runs=1`：同一时刻一个运行在执行（fork/replay 与源运行共用 thread；更高并发未测试）。
- 暂停粒度是"步"（一个 superstep）。并行分支的图会在整步结束后停止。
- 运行登记默认在内存中；需要重启后保留请设置 `registry_path`。checkpoint 是否持久取决于你的 checkpointer。
- Studio 中的节点耗时由检查点提交时间推算；SDK 不导出模型调用 span。如需 LLM 级轨迹，智能体可以另外向 Studio 的 `/v1/traces` 导出 OTLP（与连接器互不冲突）。
- 状态值以 JSON 形式展示：消息对象、pydantic 模型、dataclass 会被转换，无法转换的显示为 `repr`。

## 6. 验证（2026-10-15，云端 Linux）

| 内容 | 文件 | 结果 |
|---|---|---|
| SDK 单独测试（真实 LangGraph、真实 checkpointer） | `tests/test_studio_executor_sdk.py` | 5 passed：reducer 字段拒绝；运行/历史/修订循环；fork 改 review 后跳过修订且源运行不变；replay 输出一致；在步边界暂停；失败后重试；`interrupt()` 人工确认；重启后标记 interrupted；HTTP token / Origin / Host 校验 |
| Studio ↔ SDK（进程内传输） | `tests/studio/test_connected_sdk.py` | 连接校验与 token 打码；运行、fork（review→APPROVED、category→technical）、replay、继承节点、源运行不变 |
| 三进程端到端 | 同上 | 示例智能体进程 + Studio 服务器 + HTTP：运行、fork、Compare（classify inherited、finalize differs）、智能体重启后运行与 checkpoint 仍在 |
| 浏览器 | `scripts/verify_sdk_ui.py` → `docs/evidence/connected/sdk-ui/` | 6/6 PASS，0 浏览器错误 |
| 全量 | `pytest -q` | 211 passed，10 skipped |

未在 Windows 上运行，未调用真实模型。
