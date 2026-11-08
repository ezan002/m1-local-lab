# 设计目标符合性验收（2026-10-12）

依据：[最新产品方向](product-direction.md)、用户本轮最终目标。本文是现状验收，不启动功能开发，不改写历史实验结论。

## 1. 结论与“初步完成”的范围

**A. 外部历史轨迹观测 MVP：数据处理核心已初步完成；日常产品闭环判定为「部分完成」。** 本次在正在使用的 8000/6006 服务中，实际验证了三条既有公开样本、两种格式，66 条源记录完整保留为 66 个记录 Span，加上 3 个明确标注的展示分组，共 69 个 Span。选样、预览、导入、逐项读回、步骤全文、原文件定位、刷新及重复导入均通过。独立启动也证明观测功能不依赖模型 Key。

未给予整体「已验证完成」的原因是：**日常 8000 服务仍生成编码有误的 Phoenix Project/Trace 地址，点击出现 `projectId not found in path`。** 当前磁盘代码已有修正，新启动的临时观测进程返回正确地址，但旧服务行为尚未同步；不能以代码测试通过替代日常网页验收。本文没有重启用户日常服务，也没有修复业务代码。

**B. 最终平台目标：部分完成。** 已有可信历史展示、有限格式适配、独立本地观测、旧执行系统的 checkpoint/Replay/Fork 以及研究评价管线。尚未形成通用外部采集、完整调用因果链、经过独立验证的归因方法，以及与外部执行系统对接的恢复/调试能力。

**可以向导师作有说明的阶段演示。** 主网页可以演示真实历史资料及来源核对；Phoenix 使用下文已实际打开的备用地址。应称为“外部历史观测研究原型”，不能称为已完成的自动错误诊断平台、第三方 Agent 通用调试器，或已通过全部日常验收的产品。

这里的“初步完成”仅指：支持的本地历史文件 → 校验与来源登记 → 脱敏 → 独立 Phoenix Project → 内容读回 → 本地步骤查看与追溯。它不包括证明第三方历史记录捕获完整、重新执行其任务、推断未记录的调用关系或证明归因有效。

## 2. 审查版本、环境与证据边界

| 项目 | 本次核实 |
| --- | --- |
| 工作区 | `C:\projects\m1-local-lab` |
| 分支 / HEAD | `feature/full-project-audit-optimization` / `ae4923d792c9593b673ee166a3301d146e34904e` |
| 初始 Git | clean；已有 origin `https://ezan002@github.com/ezan002/m1-local-lab.git`；本次不提交、不 push、不改引用 |
| 适用指令 | 工作区及相关上级目录未发现适用的 `AGENTS.md` |
| 系统 / Python | Windows 11，系统版本 `10.0.26200`；Python 3.11.9 |
| 已安装版本 | Phoenix 11.35.0；Phoenix client 3.3.0；OTel SDK 1.44.0；OpenInference semconv 0.1.33 / instrumentation 0.1.59；LangGraph 1.2.11；Playwright 1.54.0 |
| 浏览器 | 项目 Playwright 的真实 Chromium 139.0.7258.5，headless，1440×1000；不是仅静态检查 |
| 日常服务 | 既有应用 `http://127.0.0.1:8000` 为 observation；既有 Phoenix `http://127.0.0.1:6006`；两者健康检查 200 |
| 运行版本限制 | HEAD 是磁盘审查版本。旧服务没有可核验的 build SHA，不能断言其内存代码与 HEAD 相同；URL 行为已证明不一致 |
| 资料范围 | 阅读 README、direction、architecture、acceptance、historical-observation、归因协议与结果；对照 historical_observation、observation_api、网页、CLI、runtime、observability、归因输入/评价代码及相关测试 |

本次日常验收证据目录（被 Git 忽略）：

`data/test-output/design-goal-acceptance/20261012T192907Z-4d8fc8bd/`

- `receipt.json`：2026-10-12 19:29:07–19:29:57 UTC 的日常验证收据，含全部 66 条映射、浏览器错误、原文件哈希及导入前后数量。
- `*-detail.png`、`*-phoenix-trace.png`：三种样本的真实网页截图；`*-link-error.png` 保留跳转失败证据；`phoenix-project-all-time.png` 显示三个 Trace。
- `no-key-startup-v2/receipt.json`：独立无 Key 启动结果、命令、PID、限制及清理结果。

这些本地证据没有加入 Git。旧报告中的 166 passed / 3 skipped、双服务重启等记录只作为**历史证据**，不是本轮重新执行的结果。

## 3. 目标 → 实现 → 证据 → 本次验证 → 限制

判定仅使用「已验证完成／部分完成／未实现／环境阻塞」。未执行的范围不凭空记为环境阻塞。

| 目标 / 判定 | 实现代码 | 已有证据 | 本次实际验证 | 限制 |
| --- | --- | --- | --- | --- |
| 无 Key 本地观测：**已验证完成**（当前环境） | `src/m1_local/cli.py:90`；`observation_api.py:116` | 工厂与执行系统分离；历史无 Key 测试 | 新进程空 Key 环境启动；样本可用、预览 6/31 记录；执行接口 403；24 项定向测试含工厂隔离 | 不是另一台电脑安装验收；未查看日常服务秘密环境变量 |
| 两类真实历史导入：**已验证完成**（限定格式） | `historical_observation.py:141`、`:245`、`:400`；`observation_api.py:78` | 两个默认样本及登记式扩展；旧隔离验收 | 日常 Who 6、TE 31、Who Hand 29 条逐项核对；Phoenix 7/32/30 Span；源文件 SHA 不变 | 公开来源的历史样本，不是本项目执行；本次未重新从发布端下载认证 |
| 同格式扩展：**已验证完成**（登记范围） | `historical_observation.py:79`；`cli.py:90` | `--catalog`、本地文件白名单及 fixture 测试 | 已有第三条 Hand-Crafted 文件首次加入当前日常导入记录；缺 `name` 的 29 条均保留原 role | 不是新格式支持证明；当前仅 Who&When / TraceElephant 显式 schema；每文件 2 MiB、最多 500 条记录等边界 |
| 任务级可视化和步骤检查：**已验证完成**（列表式） | `web/observation.js:39`、`:63`、`:76`；`observation_api.py:90` | 历史浏览器与安全 DOM 实现 | 三个任务各自步骤列表、首条展开、刷新保留选中；API 查全体 66 条 | 没有按真实因果关系画出的 Agent 交互图；主页面不做跨运行比较 |
| Agent I/O 分析：**部分完成** | `historical_observation.py:199`、`:271`；`web/observation.js:76` | 带 JSON Pointer 的 request / response / message 区分 | TE 原 input/output 与 API、Phoenix 属性一致；Who 原消息一致；展开内容未被预览截断 | Who 不含完整原始请求/响应；手工样本只有 role 而非独立 Agent ID；目前主要是查看与人工分析 |
| 原文追溯和脱敏：**已验证完成**（当前规则与样本） | `historical_observation.py:58`、`:148`、`:264` | 全文脱敏、文件 hash、原编号、Pointer、内容 hash | 每条原 JSON 记录经脱敏后与规范化记录一致，再与 Phoenix 公共 API 逐项比较；TE 两条记录命中脱敏 | 正则/字段名保护不是任意个人信息识别保证；不代表所有第三方日志可直接安全公开 |
| 工具追踪：**部分完成** | `historical_observation.py:196`；`:282` | 保存工具声明；旧本地执行有 Worker/OTel Span | TE 第 4、15、25 步（数组 3、14、24）保留工具声明；未虚构 TOOL 执行 Span | 当前样本未规范化出可验证的 tool invocation/result 配对、耗时、独立执行状态；消息中的终端输出不等于完整工具调用链 |
| Phoenix 导入和读取：**已验证完成**（限定服务版本） | `historical_observation.py:288`、`:303`、`:323`、`:371` | 当前版本公共 REST Span API | 写入后逐项读回身份、全文属性、parent、kind、status 和时间；All Time 显示三条 Trace | 不依赖内部数据库；按 Trace 查询需分页读取 Project 后筛选，规模扩大后有成本 |
| 主网页直达 Phoenix：**部分完成** | `observation_api.py:46`；`web/observation.js:19` | 磁盘代码已保留 Project ID 的 `=`；边界回归测试 | 日常三个 Trace 按钮和 Project 按钮均失败；正确地址四处均实测可用；新进程返回正确 URL | 当前运行服务仍输出 `%3D%3D`；必须承认日常闭环未通过 |
| 重复导入和部分失败：**部分完成** | `historical_observation.py:332`、`:371`、`:400` | 稳定身份、写前意图、目录锁、读回核对 | 日常三条各重复一次，数量与 imported_at 不变；定向 fixture 验证部分确认、未尝试片段补入 | 未在日常数据库故意制造损坏；已尝试但永久丢失的写入仍保守标 uncertain，强制恢复契约未实现；不承诺多台客户端全局恰好一次 |
| 持久化和恢复可用性：**部分完成**（本轮证据） | `historical_observation.py:347`、`:356`；Phoenix 公共 API | 历史隔离双服务重启验收 | 日常刷新后仍可查看，重新读回所有 Span | 本轮未重启日常应用/Phoenix；刷新不能替代进程重启验收 |
| 第三方实时采集：**未实现**（项目产品入口） | 本地运行插桩 `observability.py:128`；历史导入独立 | 旧本项目运行能发 OTel；Phoenix 本身支持接收 | 本轮只查本地历史导入路径，未发现通用外部 Agent 连接、身份/会话适配产品入口 | Phoenix 接收 OTLP 的底层能力不等于项目已经完成第三方实时接入 |
| 错误步骤定位与归因：**部分完成** | `attribution.py:252`、`:329`、`:346`、`:373`；`attribution_read_model.py:225` | 三种确定性基线、研究页、冻结 30×3 结果 | 阅读 predictor/evaluator 实现与真实汇总；验证 label guard、abstain、截断分母等 | 新历史页面未自动运行归因；没有自动诊断当前三条 Trace；有效性尚未证明 |
| 真实数据与可复现实验：**部分完成** | `attribution_protocol.py:131`；`attribution_benchmark.py:376`、`:419`、`:514` | 固定源版本、manifest、方法源码及结果 | 检查资产未改；manifest/协议/评价定向测试通过 | 本次未重跑 90 次预测或重查 Phoenix Experiment；冻结测试集已被查看；工程正确不等于算法有效 |
| 回放、恢复与交互调试：**部分完成**（仅旧本地执行器） | `runtime.py:290`、`:298`、`:314`、`:334`、`:363` | LangGraph checkpoint、branch/head 存储和历史测试 | 两个临时 RuleBasedGateway 测试验证旧 head 不动、Replay/Fork 血缘与允许修改字段；观测模式恢复接口均 403 | 外部导入无 checkpoint，不能恢复；未验证真实模型重新执行；外部浏览器状态/副作用不可回滚 |

## 4. 日常真实数据核对与可展示内容

### 4.1 来源、身份及数量

所有记录位于独立 Project **`m1-imported-history-v1`**，公共 API Project ID 为 `UHJvamVjdDoxNw==`。

| 样本 | 原始 case ID | 本地源文件（相对 `data/external/`） | Phoenix Trace ID | 源记录 → Span |
| --- | --- | --- | --- | --- |
| Who&When Algorithm-Generated | `4d51c4bf-4b0e-4f3d-897b-3f6687a7d9f2` | `who-and-when/algorithm-generated-1.json` | `6a33077cf60d674ab78c4041cd08409a` | 6 → 7 |
| TraceElephant | `assistant_bench_task_7_gpt_4o_wylm9r1ppoet` | `traceelephant/assistant-bench-task-7/trace_metadata.json`、`step_records.json` | `cb661869f02717318effa413ce2ab9e4` | 31 → 32 |
| Who&When Hand-Crafted | `6e3be83d1949fa52cba03fb1ce4b5b3bf7e37a83fd7d67694b10b2e439d90cf8` | `who-and-when/hand-crafted-1.json` | `6502eb71143024c1c03d396143d70092` | 29 → 30 |

登记的固定来源：

- [Who&When 数据集](https://huggingface.co/datasets/Kevin355/Who_and_When/tree/59b9fcba1aaed7bbf206b5f4d3c68b8face2f49c)，revision `59b9fcba1aaed7bbf206b5f4d3c68b8face2f49c`；关联研究仓库 [Agents_Failure_Attribution](https://github.com/mingyin1/Agents_Failure_Attribution/tree/b2bae5c5b06d681d04ea5e9b63b7a30525c04925)。本地 JSON 是先前取得的单条样本，不是完整上游 Parquet。
- [TraceElephant 数据集](https://huggingface.co/datasets/TraceElephant/TraceElephant/tree/a78a57cdcdf74a080b1bec0f56f85228d86acbac)，revision `a78a57cdcdf74a080b1bec0f56f85228d86acbac`；本地两个 JSON 是先前提取的该 case 数据。

本轮没有重新下载、访问日志中的 URL 或运行第三方系统；来源版本来自已有获取说明/登记，当前验证的是本地字节、格式和展示身份。来源声明与独立复现原始任务是两种证据，不能混用。第三条是**本机早已存在、本次加入日常导入记录的样本**，并非新下载的未知格式，也不是自编 fixture。

本轮前后不变的 SHA256：

| 文件 | SHA256 |
| --- | --- |
| algorithm-generated-1.json | `9a3295bd60713bf8603ad91b2ef3e563cefca448195cf7a19a046fa0b6ba3408` |
| TE trace_metadata.json | `58c10ec846a7c65d20739335f56672d64ecc3388c599036424e3a06ebed854c5` |
| TE step_records.json | `5f5f0a5e22cfb24f121479e749e19a4dfca8f58e6619ed225e20bbd5519d1808` |
| hand-crafted-1.json | `ceb4cad005df8f45b779e4cc12ff943252e3574443e2f69ba4128c9255ed2df7` |

日常 Project 原有 7 Span，本轮只新增缺少的两条历史 Trace（32+30 Span），最终 69 Span。三条分别重复导入后仍为 7/32/30，导入时间未被重写。沒有删除 Project、Dataset、数据库或旧运行；未直接读写 Phoenix 内部数据库。

### 4.2 如何逐项核对

验收器直接读取本地 JSON，按每条 `source_pointer` 定位原记录；验证 `redact(原记录) == raw_record_redacted`，再比较步骤 API 与规范化记录，最后通过 Phoenix 公共 API 比较完整属性、Span ID、kind、parent、状态和时间。**不是仅核对数量，也不是仅比较两个摘要。** 全部 66 条映射在收据中，不把大段原始数据复制进本报告。

可当面演示的一个最小例子：

- Who 短样本的步骤 1，原字段 `/history/1/content`，保留 `exitcode: 0 (execution succeeded)` 和末尾 `4`。
- 对应原文件 `who-and-when/algorithm-generated-1.json`，Span `f9bbf95002615b85`，Trace 为上表 Who 短样本。
- 这是原记录中的终端结果消息；本轮没有执行其中的 Python、没有读取其 Excel 附件。退出码成功只证明历史消息声称程序成功，不证明任务答案正确，也不是本项目自动完成的归因。

### 4.3 不同记录的准确语义

| 类别 | 本次实际情况 | 不应宣称 |
| --- | --- | --- |
| 历史消息 | Who 共 35 条；Who 短样本有 name，Hand-Crafted 29 条缺 name、保留 role；Span kind UNKNOWN | 每条发言都是 LLM 请求；角色字符串证明独立进程或真实调用边 |
| 模型请求/响应 | TE 31 条有 `chat.completion` 结构证据，按 model_call / LLM 展示，保留原 input/output | 本轮调用了 31 次模型；数据格式本身能独立证明原系统完整运行 |
| 工具声明 | TE 数组 3、14、24 有声明；原始参数/记录完整保存 | 声明等于工具已执行或成功 |
| 工具实际执行 | 本次没有可逐项对应的规范化 TOOL 执行 Span；Who 有终端输出文本 | 仅凭 Agent 名、代码块、相邻文本补造 tool_call_id、父子关系与耗时 |
| 展示分组 | 每条 Trace 新增一个 `Imported history [generated grouping; not execution]` CHAIN，全部记录是其子级 | 分组代表原始 Orchestrator，或这棵展示树就是原始调用树 |
| 失败 | TE 的数据集任务失败元数据单独保存；本次所有导入 Span 状态 UNSET | 任务失败意味着每个 Span ERROR；已知标签等于本项目推断 |

输入输出不由模型补写，Who 消息不拼接成原始 prompt。66 条均无排除、无主动截断；完整保留文件内容不等于源捕获完整。TE 数组 19、24 命中现有脱敏规则；其余未命中不等于绝无个人信息。

Who 真实执行时间未知，用固定 `2000-01-01 UTC` 作展示坐标；TE 使用原响应的 UTC 秒级时间点，单次起止耗时仍未知。Phoenix 中 TE 根分组显示的 **227 秒**只是首末响应时间差，`$0`、零宽 Span 和百分位也不是实测费用/耗时。模型名与 usage 保留在原响应，不自动转成 Phoenix 费用估算。不得据此比较 Agent 性能。

### 4.4 Phoenix 现场入口

本次日常旧进程返回 `/projects/UHJvamVjdDoxNw%3D%3D/traces`，四次实际点击均触发页面错误。新无 Key 临时进程使用当前代码返回未编码 `==` 的地址，边界单测也通过。结论是**磁盘修正已存在，日常运行仍未完成交付验证**，不是“当前所有跳转都通过”。

已用真实浏览器打开的本机备用入口：

- Project：`http://127.0.0.1:6006/projects/UHJvamVjdDoxNw==/traces`
- Who 短样本：上述 Project 路径追加 `/6a33077cf60d674ab78c4041cd08409a`
- TE：追加 `/cb661869f02717318effa413ce2ab9e4`
- Who Hand：追加 `/6502eb71143024c1c03d396143d70092`

这些 ID 只适用于本机当前服务，不是安装到另一台电脑可硬编码的地址。从 Project 浏览时选 `Last 7 Days → Custom`，清空起止日期，再 `Apply`，才显示 `All Time`。本次真实截图显示三个 Trace；默认近七天为空不是丢失数据。

## 5. 归因与可复现实验：已经有什么，尚不能证明什么

代码确实在计算预测，不是把标签直接当预测展示：`build_attribution_views` 在 `attribution.py:252` 分开 predictor 输入和 Ground Truth，三个方法在 `:329`、`:346`、`:373` 使用末活跃 Agent、显式错误线索、规则加权。递归 guard 与评价分母本轮有定向测试证据。

但日常观测模式不运行这些方法，页面的源元数据中即使有标签，也只是来源标签。旧研究页面及 Dataset/Experiment 路径保留，未因此自动连接到新导入的 69 个 Span。

本轮只读检查 `configs/attribution_eval_v1.json`、`datasets/manifests/who_when_benchmark_v1.json`、`docs/results/attribution-benchmark-v1-summary.json` 和实现，未重跑基准：

| 既有冻结证据 | 实际含义 |
| --- | --- |
| 30 样本 × 3 方法 = 90 次预测；1170 evaluations | 30 个案例的三个确定性方法，不是 90 个独立案例，也不是新算法实验 |
| Hand-Crafted 10 / Algorithm-Generated 20；Dev 20 / Frozen Test 10 | 源版本、选样、划分与 manifest 冻结；本次未改 |
| last_active_agent：agent exact 10/30，step exact 0/29，coverage 30/30 | 简单基准，不证明最后发言者是因果责任人 |
| first_explicit_error：3/30，3/29，coverage 16/30 | 有弃答；整体分母不能偷换为已回答样本分母 |
| rule_based：3/30，3/29，coverage 30/30 | 规则输出不是校准的失败概率 |
| 三方法合计 agent 16/90，step 6/87，coverage 76/90 | 不能把合计当作某一个算法的准确率；一个越界标签使三个方法的 step 分母各少 1 |

局限：

1. **Frozen Test 结果已经被查看，不能宣称严格盲测。** 本轮不调整它来追求分数。
2. `expected_answer` 在协议中允许进入 predictor；隐藏的是负责 Agent、决定性步骤、失败原因。不能把它称为完全无答案辅助的在线诊断。
3. `external_datasets.py:120` 的旧研究预处理与新全文历史导入不同：保留步骤数不等于保留完整内容。历史审查记录 30 条中 297 个字符串超过旧 1000 字符限制；本轮确认裁剪代码仍在，未重新计算该历史计数。
4. confidence 是代码常数/规则档位，不是经校准置信概率。字段 guard 不能保证自然语言中永不包含答案或标签线索。
5. 现有结果没有证明泛化、因果充分性、相对强基线优势或实际调试收益；统计显著性、独立评估、失败类型覆盖仍欠缺。**测试通过只支持工程契约，不支持算法有效性。**

本次检查的 10 个关键文件与 HEAD 一致：attribution、external_datasets、config、graph、models、冻结协议、manifest、结果 JSON、两个 requirements lock；归因源码也与冻结提交 `da3ef193bdd73474eec5191f1e267ee163c308a7` 一致。

## 6. 历史查看与重新执行的边界

- 观测数据明确带 `trace_origin=imported_historical`、`executed_by_m1_local=false`、`external_task_executed=false`。每条规范化记录 checkpoint 为 null，Span 也声明 checkpoint 不可用。
- 日常服务实际请求 `/api/runs`、历史 ID 的 `/resume`、`/replay`、`/fork` 及 `/execution` 均为 403。此拒绝发生在后端，不只是按钮隐藏。
- 旧 LangGraph 执行系统依然存在：Resume 仅允许 paused/failed/interrupted；Replay 从本运行历史中的合法非终结 checkpoint 新建 run/branch，复用 thread；Fork 只允许 `plan`、`worker_instruction`，创建暂停分支。旧 head 不被新分支覆盖。本轮两条离线测试验证的是这些机制。
- 这些测试使用临时数据库与 RuleBasedGateway，不是真实模型验证，也没有用外部历史日志“恢复”任务。浏览器采用 reconstruct，外部网页、文件写入和其他副作用不会自动回滚。为第三方日志提供可执行恢复仍需原系统的状态、环境和恢复契约。

## 7. 本次有限验证记录

### 7.1 日常浏览器与 API

命令：`.\.venv\Scripts\python.exe -B data/test-output/verify_design_goal_acceptance.py`。验收器仅本地 HTTP，使用既有服务和本机样本；主网页实际执行选样、预览、导入、详情、重复导入和刷新。

已测接口：应用 `/api/health`、`/api/observation/{session,health,samples,imports}`、`preview/{sample}`、`POST import`、`imports/{id}`、`imports/{id}/steps`、`steps/{index}`；Phoenix `/healthz`、`/v1/projects`、对应 Project Span 查询和 Span 写入。普通步骤读取使用完整脱敏数据；Phoenix 仅公开 API。

结果：

- 主网页 Console error、page error、request failure、意外 4xx/5xx 均为 0；没有默认归因或执行请求。
- Phoenix 四个页面均记录上述路径错误；改用正确地址后 Project 和三个 Trace 可渲染。不能称 Phoenix 浏览器零错误。
- 浏览器拦截四次 `https://api.github.com/repos/Arize-ai/phoenix`，产生 `Failed to fetch`。这与 Phoenix 的 GitHub 项目入口/星标获取有关；未观察到上传轨迹正文的请求，但不能因此推断所有服务端均无外联。
- **阻断范围只覆盖此次 Chromium 的非本机请求**；既有日常后端/Phoenix 进程没有被注入 socket 防护。本轮不是整机防火墙验收，也不是日常后端完全离线的证明。

### 7.2 单独的无 Key 启动检查

命令：`.\.venv\Scripts\python.exe -B data/test-output/verify_acceptance_no_key.py`。它用当前已有 `scripts/verify_historical_observation.py --child app` 工厂入口，在空白独立 journal、18002 端口启动临时服务，只连接既有本机 Phoenix。

- 子进程环境仅继承必要系统路径，OpenAI/Anthropic/OpenRouter Key 均空；工厂不读取 `.env`，并断言 runtime/models/websurfer 模块未加载。
- 健康、样本列表、Who/TE 预览通过，执行 POST 403；没有向 Phoenix 导入数据。耗时 2.719 秒，退出码 0。
- 验证器及此临时应用 Python socket 限制为 loopback，未记录非本机尝试；不覆盖既有 Phoenix、原生扩展或整机。
- 仅停止所启动 PID 56020，18002 已关闭。初次临时检查因验收辅助脚本提供空扩展 catalog 被正确拒绝，保留失败日志；改为已有合法登记后通过，没有修改应用或测试源码使之通过。

### 7.3 24 项定向测试通过

目的：补足现实样本不易安全触发的部分失败、标签隔离、弃答分母、截断及本地分支独立性。执行前确认使用临时 fixture、临时 SQLite、Fake Remote 或 RuleBasedGateway，未访问真实运行库，不调用模型/公开网络。

```powershell
$env:RUN_REAL_MODEL_TESTS='0'
$env:OPENAI_API_KEY=''
$env:LANGSMITH_TRACING='false'
$cases = @(
  'tests/test_historical_observation.py::test_messages_are_not_llm_calls_and_content_is_not_clipped',
  'tests/test_historical_observation.py::test_call_evidence_numbering_time_and_labels_are_separate',
  'tests/test_historical_observation.py::test_partial_ack_never_blindly_resends_and_can_reconcile',
  'tests/test_historical_observation.py::test_unattempted_partial_records_are_completed',
  'tests/test_historical_observation.py::test_no_key_mode_has_no_runtime_and_blocks_execution',
  'tests/test_historical_observation.py::test_phoenix_project_link_preserves_base64_padding',
  'tests/test_attribution.py::test_views_separate_inference_from_ground_truth_recursively',
  'tests/test_attribution.py::test_recursive_label_guard_rejects_top_level_and_nested_fields',
  'tests/test_attribution_benchmark.py::test_abstain_overall_and_selective_denominators_are_distinct',
  'tests/test_attribution_benchmark.py::test_truncation_excludes_step_metrics_but_keeps_agent_scorable',
  'tests/test_attribution_benchmark.py::test_checked_in_manifest_hash_and_safe_metadata_are_valid',
  'tests/test_attribution_protocol.py::test_frozen_protocol_loads_with_exact_method_order_and_quotas',
  'tests/test_attribution_protocol.py::test_protocol_rejects_semantic_drift',
  'tests/test_runtime_offline.py::test_fork_preserves_old_head_and_whitelists_changes',
  'tests/test_runtime_offline.py::test_replay_creates_lineage_without_moving_original_head'
)
& ./.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider @cases --tb=short
```

结果：**24 passed in 25.39s**，无 skip。上述环境变量只设置在该命令进程，未永久更改用户配置。未运行完整套件、90 次 benchmark、真实模型/WebSurfer、安装升级或新数据下载；未重做旧的双服务重启、另一台电脑部署或归因研究 UI 验收。合成 fixture 与网页三条真实样本分开。

## 8. 主要缺口及下一阶段最小交付

阻碍最终核心目标的前三项：

1. **调用语义仍不完整。** 支持两个文件格式、查看真实消息和请求已成立，但尚缺外部系统完整工具执行配对、调用因果关系、稳定 Agent 身份和真实起止时间。现有文件缺失的事实不能由界面或模型补造。应先明确一个外部系统的可获取字段与缺失契约，再选择是否适配实时采集。
2. **归因尚无有效性证据，也未与当前观测流程形成闭环。** 三种简单规则及可复现评价是基础资产；需要独立于已看过 Frozen Test 的后续评估设计、适当对照及明确错误定义。暂不能承诺“自动找到真正出错的 Agent/步骤”。
3. **日常部署与运行状态尚未形成可靠交付闭环。** 本次磁盘修复通过、旧进程链接仍坏就是实证。另有永久未确认导入恢复、依赖本机已准备样本、Project 全量分页查询、Phoenix 原生占位费用/耗时误读等限制。历史隔离 PASS 不能覆盖这些日常问题。

较近的现场问题：

| 严重度 | 证据与影响 | 建议（本轮不实施） |
| --- | --- | --- |
| P1 | 8000 返回编码后的 Project ID；实际四处跳转失败，导师无法按标准按钮完成演示 | 安全确认服务所有权与版本后，使日常进程加载已有修正；随后按真实按钮重验，不再仅检查字符串单测 |
| P1 | 导入意图已写入但服务永久未接收时，禁止盲重发，现阶段只能 uncertain | 设计公开 API 读回与人工恢复确认契约；保留原记录，不以删除数据库恢复 |
| P2 | Phoenix 显示 $0、0 ms、227 s 分组跨度；相应缺失标记位于应用和属性 | 演示时明确解释；后续考虑更直接的未知/展示时间提示，而不是补造数字 |
| P2 | Hand-Crafted 无 name，Phoenix 子 Span 名为 unknown；日常登记 label 存在乱码 | role 原文可在主网页核对；后续改善显示名称/登记 UTF-8，保持原字段与推导字段分开 |
| P2 | 浏览器 Phoenix GitHub 外联被阻断后报错，日常后端未受离线防护 | 记录非必要资源行为与受测范围；不夸大为泄漏，也不宣称完全无外联 |

**下一阶段最小可交付任务建议：完成“日常环境可重复的历史观测演示闭环”。** 不先扩展算法或换平台。交付界限为：确认并展示当前运行版本；在获得服务所有权确认后加载现有链接修正；让 Who 短样本和 TE 的主网页按钮直接打开正确 Trace；预览、逐步追溯、重复导入、受控重启后读回均有同一日常环境证据；给用户一页固定演示步骤与来源/缺失说明。不要为此重建数据库、重新导入已验证记录或执行原任务。

其后的最小功能增量可以是“一个有完整 invocation/result 证据的工具调用适配”，但需先确定真实来源是否提供这些字段；没有数据时应保持缺失，不扩写为通用回放平台。本轮停在验收与交接，不实施上述建议。

## 9. 本轮变更与资源结清

- 业务代码、测试源码、配置、依赖、冻结协议/manifest/结果均未修改。可审查交付仅新增本报告。
- 本地被忽略目录新增两个验收辅助脚本、收据、截图与临时 journal；不纳入 Git，不上传样本或日志。
- 日常 Project 按本轮验收要求追加两条先前未导入的历史 Trace；原文件、旧 Trace、旧运行不删除不覆盖。
- 真实模型调用 **0**，真实 WebSurfer 调用 **0**，外部任务执行 **0**，新数据下载 **0**；没有本轮模型费用。
- 原有 8000/6006 服务保持运行；临时 18002 已关闭。结束时本机 TCP 检查：6006、8000 可连接；4317、8001、18002 不可连接。未停止未知进程。
- Git 分支、HEAD、remote、tag 均未修改；不暂存、不提交、不 push。结束检查 `git status --short` 唯一非忽略变更为 `?? docs/design-goal-acceptance-2026-10-12.md`；跟踪文件 diff 为空。报告无行尾空白、无失效的本地文档链接。
