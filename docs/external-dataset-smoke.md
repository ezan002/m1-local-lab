# TraceElephant / Who&When 小样本兼容测试

记录日期：2026-09-01（America/New_York）。本次只验证外部历史轨迹的安全读取、统一表示、Phoenix Dataset/Experiment 可见性，以及与本项目离线验证 run/trace 的关联；没有重跑外部任务，也没有复现论文指标。

## 来源、版本与许可

| 来源 | 论文 / 仓库 / 数据入口 | 本次固定版本 | 官方规模口径 | 许可结论 |
|---|---|---|---|---|
| TraceElephant | 论文 `arXiv:2604.22708`；`https://github.com/TraceElephant/TraceElephant`；`https://huggingface.co/datasets/TraceElephant/TraceElephant` | 仓库 `0ce8abb2855de9f454f27f6b0795a4b7e6c8d5fc`；HF `a78a57cdcdf74a080b1bec0f56f85228d86acbac` | 380 次执行，其中 220 条失败轨迹带标注；Captain-Agent、Magentic-One、SWE-Agent | 仓库明确为 CC BY 4.0；使用时保留归属与许可声明 |
| Who&When | 论文 `arXiv:2505.00212v3`；论文链接的仓库 `https://github.com/mingyin1/Agents_Failure_Attribution`；`https://huggingface.co/datasets/Kevin355/Who_and_When` | 仓库 `b2bae5c5b06d681d04ea5e9b63b7a30525c04925`；HF `59b9fcba1aaed7bbf206b5f4d3c68b8face2f49c` | 当前仓库/HF 为 184 条：Algorithm-Generated 126、Hand-Crafted 58；论文 v3 摘要仍写 127，属于版本口径差异 | 仓库 LICENSE 为 MIT；HF dataset card 没有独立的 `license` 声明，因此正式再分发前应向数据发布者确认数据许可，不把代码仓库许可自动扩张为独立数据许可 |

TraceElephant 的 HF 入口是远端 `data.zip`，实测长度 `596,610,649` bytes。HF viewer 显示的 2.51k rows 来自 ZIP 内文件级展开且 viewer 因图片路径报错，不能替代仓库所述的“380 executions / 220 annotated failures”口径。Who&When 当前 HF 页面显示 184 rows、8.68 MB；本次只流式读取两个单独 JSON，没有下载 Parquet 或整个仓库。

## 两套 schema 的真实差异

TraceElephant 一次运行由目录组织。抽样目录包含：

- `trace_metadata.json`：`task_id`、`task_instruction`、`system_name`、`agent_configuration`、`run_id`、`mistake_agent`、`mistake_step`、`mistake_reason`、`ground_truth`、`agent_system_intro`。
- `step_records.json`：数组元素包含 `step_id`、`agent_id`、`agent_name`、`input`、`output`、`tool_logs`；input/output 可含完整模型消息、usage 和工具原始日志。
- 全量 ZIP 中还存在图片等运行资产。本次没有抽取图片，也不执行 tool log 中的任何内容。

Who&When 是单条 JSON 记录：

- Hand-Crafted 样本使用 `groundtruth`，history item 为 `content/role`，本条没有 `system_prompt`。
- Algorithm-Generated 样本使用 `ground_truth`，history item 为 `content/role/name`，并有按专家名组织的 `system_prompt`。
- 两种 split 都使用 `question`、`question_ID`、`history`、`mistake_agent`、`mistake_step`、`mistake_reason`；字段缺失时适配器输出 `null` 和 capability flag，不补造数据。

TraceElephant 的 Magentic-One 元数据明确列出 MagenticOneOrchestrator、MultimodalWebSurfer、FileSurfer、MagenticOneCoderAgent、MagenticOneGroupChat、CodeExecutor。本项目仍是 LangGraph 唯一调度器，下挂 Planner、Orchestrator、WebSurfer、FileReader、Coder、Finalizer。Who&When Algorithm-Generated 的 CaptainAgent 会动态形成领域 Expert；Hand-Crafted 历史则保留其原始 Orchestrator/WebSurfer 等 role。本适配器不把任何外部 Agent 名称强行改名成本项目 Agent。

## 字段映射

| 要求字段 | 统一记录 / Phoenix 位置 | 说明 |
|---|---|---|
| `source_dataset` | record；Phoenix input/metadata | 严格为 `TraceElephant` 或 `Who&When` |
| `source_version` | record；Phoenix input/metadata | 固定 HF revision，另在本页记录代码仓库 commit |
| `source_split` | record；Phoenix metadata | 保留原始 split/目录族 |
| `source_case_id` | record；Phoenix input/metadata | 保留 `run_id` 或 `question_ID`；缺失时才用内容 SHA-256 并标记 capability |
| `dataset_case_id` | `traceelephant:<id>` / `who-and-when:<id>` | 稳定内部键；Phoenix 11 不支持调用方 example ID，因此键放在 input/metadata |
| `task` | record；Phoenix input | `task_instruction` 或 `question` |
| `expected_answer` | record；Phoenix expected/output | `ground_truth`、`groundtruth`；允许 null |
| `original_system` | record；Phoenix metadata | 原样保留 Magentic-One/CaptainAgent/metadata system name |
| `original_agent` | `ground_truth.responsible_agent` | 来自 `mistake_agent`，不做内部 Agent 映射 |
| `original_step` | `ground_truth.decisive_step` | 来自 `mistake_step`，以字符串保真 |
| `original_trace` | `trace`；Phoenix `historical_trace` | 只作导入历史数据，绝不作为 OTel spans 导出 |
| `responsible_agent` | Phoenix expected/output | 与原始 ground truth 同值 |
| `decisive_step` | Phoenix expected/output | 与原始 ground truth 同值 |
| `failure_reason` | Phoenix expected/output | 与原始 ground truth 同值并执行文本脱敏 |
| `attachments` | record metadata-only descriptors | 只允许相对 POSIX 路径和安全后缀；不打开、不执行；本次 3 条样本均无可验证 attachment manifest |
| `metadata` | record；Phoenix metadata 子集 | adapter version、原始/保留 step 数、大小、截断、capabilities、provenance |

适配器位于 `src/m1_local/external_datasets.py`。单个 JSON 最大 2 MiB，trace 最多 200 items / 100,000 JSON chars；每个文本再次按现有 key/password/Cookie/Bearer/OpenAI-key 规则脱敏。路径必须解析在明确 allowed root 内，只接受 UTF-8 JSON。附件只记描述，拒绝绝对路径、`..`、反斜杠和脚本/可执行后缀。

## 实际抽样

总计 3 条，两个来源均至少 1 条，没有超过 5 条：

| 来源 / split | 原始 case ID | 本地文件 | 责任 Agent / 决定步骤 |
|---|---|---|---|
| Who&When / Hand-Crafted | `6e3be83d1949fa52cba03fb1ce4b5b3bf7e37a83fd7d67694b10b2e439d90cf8` | `hand-crafted-1.json`，34,159 bytes | `WebSurfer` / `12` |
| Who&When / Algorithm-Generated | `4d51c4bf-4b0e-4f3d-897b-3f6687a7d9f2` | `algorithm-generated-1.json`，11,626 bytes | `Excel_Expert` / `0` |
| TraceElephant / magentic-runs-assistant-bench | `assistant_bench_task_7_gpt_4o_wylm9r1ppoet` | metadata 2,647 + steps 536,368 bytes | `WebSurfer` / `5` |

TraceElephant 样本原有 31 steps；因 100,000-character 上限保留 20 steps，并明确写入 `truncated=true`、`original_trace_items=31`、`retained_trace_items=20`。标注字段来自 metadata，未因 trace 截断而丢失。

下载量：Who&When 两条样本 payload 共 45,785 bytes。TraceElephant 第一次只读 ZIP central directory 为 499,972 bytes，第二次目录 + 两个精确成员 Range 为 538,175 bytes；TraceElephant 累计 Range 1,038,147 bytes。可计量的数据/Range 总传输为 1,083,932 bytes，另有很小的 HTTP/GitHub/HF 元数据响应。没有下载 596,610,649-byte 完整 ZIP。抽取数据位于 `data/external/` 且该目录已加入 `.gitignore`。

## Phoenix Dataset / Experiment

- Dataset：`m1-local-lab-external-trace-smoke`
- Dataset ID：`RGF0YXNldDox`
- Dataset version ID：`RGF0YXNldFZlcnNpb246MQ==`
- 唯一 examples：3
- Experiment：`m1-local-lab-external-dataset-smoke`
- Experiment ID：`RXhwZXJpbWVudDox`
- Experiment task runs：3；`provenance_guard` evaluations：3/3 score 1.0

Phoenix 11.35.0 不支持 client 3.3.0 的自定义 example ID（该能力要求 Phoenix >=15），所以稳定 `dataset_case_id` 放在每个 example 的 input/metadata，并用于幂等复用检查；没有修改 Phoenix 源码或升级环境。第一次兼容尝试产生的 6-entry 重复测试 Dataset 已按精确 dataset ID 删除（HTTP 204），随后重新创建并只读确认最终 Dataset 为 3 个唯一 IDs。

导入历史只存在于 Dataset example：`trace_origin=imported_historical`、`executed_by_m1_local=false`、`external_task_executed=false`。Experiment 没有执行外部 task，而是关联三个真实的本地离线验证 run；这些 run 只使用 RuleBasedGateway 读取临时 CSV：

| dataset_case_id | local run_id | local trace_id | status |
|---|---|---|---|
| `who-and-when:6e3...90cf8` | `b6816894-aec5-4188-a623-916b87dce596` | `d6b7b1640846039f745b5039bd86b139` | completed |
| `who-and-when:4d51...9f2` | `3a3ed168-2b04-49f2-b621-63cdbcc09c7` | `9e039ddb537ce2e714535bf9db5ebd74` | completed |
| `traceelephant:assistant_bench_task_7_gpt_4o_wylm9r1ppoet` | `567a8de5-824f-4867-aaa1-a55209901575` | `5382e1881a683ab995215ac9e28d873c` | completed |

因此 Phoenix 中可直接查看外部 task、历史 trace、expected answer、responsible agent、decisive step、failure reason、source/revision/split/case ID，以及 Experiment 输出中的本地 run/trace 关联。Phoenix 中的三个本地 OTel traces 是“适配器管线离线验证”，不是外部任务复跑；外部历史本身没有被伪造成 OTel trace。

## 命令与结果

关键命令：

```powershell
& '.venv/Scripts/python.exe' scripts/fetch_traceelephant_smoke.py --list-pattern 'assistant_bench_task_7_gpt_4o_wylm9r1ppoet' --max-list 10 --extract 'data/magentic-runs-assistant-bench/assistant_bench_task_7_gpt_4o_wylm9r1ppoet/trace_metadata.json' --extract 'data/magentic-runs-assistant-bench/assistant_bench_task_7_gpt_4o_wylm9r1ppoet/step_records.json' --target 'data/external/traceelephant/assistant-bench-task-7'
& '.venv/Scripts/python.exe' scripts/verify_external_dataset_smoke.py
& '.venv/Scripts/python.exe' -m pytest -q
git status --short
```

结果：fetch `member_count=2827`、精确匹配 2、`downloaded_bytes=538175`；Phoenix smoke PASS、3 samples、0 paid model calls、3 completed local runs；最终完整离线套件 `28 passed, 2 skipped in 26.63s`，原基线 `19 passed, 2 skipped` 未回归。`git status --short` 返回 exit 1：当前目录不是 Git repository，因此无法用 Git 状态证明忽略结果；仍按 `.gitignore` 和文件位置隔离外部数据。

## 未执行的操作与下一步建议

未克隆或运行任何第三方仓库代码、Shell 脚本、Agent 系统、中间件或推理程序；未安装任何新依赖；未修改 `.venv` / `.venv-phoenix`；未使用 Docker/Node；未调用 OpenAI 或其他付费模型；未重跑原始 WebSurfer task；未运行完整 Who&When/TraceElephant 实验；未下载 TraceElephant 全量 ZIP。

建议后续分工，而不是强行二选一：

- 正式错误归因算法的首要开发/调试集优先 TraceElephant，因为 input/output/tool logs/system configuration 更完整，更适合定位因果步骤；代价是约 600 MB、复杂资产和更严格的资源/安全治理，下载全量前必须再次获得用户批准。
- 快速回归、schema 稳定性、who/when 指标和低成本 CI 优先 Who&When，因为 184 条较小、字段直接；正式再分发前先确认 HF 数据本身的许可口径，并处理论文 127 与当前 184 的版本差异。

进入正式阶段前仍需批准：TraceElephant 全量下载；任何第三方推理/Agent 执行；额外依赖或隔离环境；真实模型调用；完整 benchmark；以及将外部 ground truth 与本项目新运行做误差归因对比的评价协议。
