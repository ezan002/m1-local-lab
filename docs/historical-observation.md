# 本地外部历史观测：能力、映射、使用与验收

日期：2026-10-10。基点 `a808a04ca60d35e6e2d35ea46441d3ba6e662597`，分支 `feature/full-project-audit-optimization`，本轮尚未提交。最新范围见[产品方向](product-direction.md)。本文的历史样本不是本项目新执行，也不是归因效果实验。

第 1–6 节记录首轮两样本验收；后续“同格式多样本接入”的增量与新证据见第 7 节，不倒写旧验收结果。

## 1. 开始时实际状态与本轮改动

开始时无已跟踪文件修改；有七份既有未跟踪文档，以及一份 `src/m1_local/historical_observation.py` 草稿。草稿与方向文档原内容备份至被 Git 忽略的 `data/test-output/historical-observation/baseline/`；在草稿上补安全校验和读回字段验证，没有覆盖重建。仓库、src、web、tests、scripts 及适用父目录未发现 AGENTS.md。

| 开始时能力 | 状态及依据 | 本轮处理 |
|---|---|---|
| 外部样本解析、来源区分、Dataset/Experiment | 已有代码：`external_datasets.py`、`external_export.py`、`attribution_read_model.py` | 冻结研究路径不改，新建独立历史 Trace 展示路径 |
| 历史 Trace 规范化/导入 | 部分：未跟踪 `historical_observation.py` 草稿，未接网页或 CLI；旧 PASS 不作证据 | 补测试、API/UI、身份/属性/时间读回 |
| 无 Key 且后端禁止执行的主界面 | 未实现：旧 `api.create_app` 无条件初始化 runtime | 新 `cli observe` 与独立 app factory |
| 历史步骤全文和源记录定位 | 部分：旧归因视图只读摘要，部分结构化内容省略 | 按需读取完整脱敏记录；JSON Pointer、hash、原编号及 Span ID |
| Phoenix 本地服务、执行控制 | 已有代码和历史证据；不是本轮的重新验收 | 保留原运行系统，不为历史伪造 checkpoint |

## 2. 当前安装版本与 Phoenix 能力

实际读取两个虚拟环境的 distribution metadata：Python 3.11.9 / Windows；Phoenix server **11.35.0**、client **3.3.0**、OTel API/SDK **1.44.0**、OpenInference semantic conventions **0.1.33**、应用 OpenInference instrumentation **0.1.59**。应用 FastAPI 0.141.1 / Playwright 1.54.0。没有升级或重装。

| 能力 | 当前接口与边界 |
|---|---|
| 历史 Trace 导入 | 服务端公开 `POST /v1/projects/{project_identifier}/spans` 接收完整 Span JSON，返回 202 和 total_queued。202 只是入队；本轮必须分页读回再写 verified |
| Trace/Span 读取 | `GET /v1/projects/{project_identifier}/spans?limit=100&cursor=...`。11.35.0 只有时间及游标过滤，没有新 client 中的 trace_ids 过滤；本轮不传未知过滤参数，分页后按完整 trace_id 匹配 |
| Project | `GET /v1/projects/{project_identifier}`、`POST /v1/projects`。独立 Project `m1-imported-history-v1`，不修改默认实时 Project、Dataset 或 Experiment |
| Span 层级/输入输出 | 公共字段 context/parent_id/span_kind/attributes。输入输出使用带来源的 input.value/output.value；普通消息仅写 historical.message。不存在的关系不重建 |
| 搜索/筛选/错误/来源 | Phoenix 原界面与 attributes 提供查看入口；本项目步骤列表严格按原数组索引。数据集失败不转成 ERROR；当前两种适配的 Span status 为 UNSET |
| 本地持久化 | Phoenix 通过自身服务持久化；本项目仅通过公共 API 验证重启后数据，绝不读写其数据库 |
| SDK/OTLP | client 有 spans.log_spans / get_spans 及 ATIF helper；服务端支持 HTTP OTLP。为匹配现有版本且保留全文，本轮使用公开 JSON Span API，无新增 exporter 全局配置 |
| ATIF 候选 | client 内存在 `phoenix.client.helpers.atif.upload_atif_trajectories_as_spans`，支持 ATIF 1.x 至 1.7 的校验；本地样本不是 ATIF。该版 `_convert.py` 缺时间时用 now/上一步及 +1 秒推导，不能直接当真实时间，因此本轮未使用 |
| 执行恢复 | Trace 不包含可执行状态；Phoenix 不负责原 Agent 恢复，也不会自动得到 LangGraph checkpoint |

版本依据来自本机安装包公开路由模型和 SDK 签名。官方概览：[导入与导出](https://arize.com/docs/phoenix/tracing/how-to-tracing/save-and-load-traces)、[Tracing/OTLP](https://arize.com/docs/phoenix/learn/tracing/how-tracing-works)。网页新版本示例不能取代本机 11.35.0 的接口验证。安装包仅只读检查，没有修改 Phoenix 源码。

## 3. 两个真实样本与映射契约

沿用已有资料及历史获取记录 `external-dataset-smoke.md` 的固定 revision；本轮不联网重下载校验远端版本，以实际文件 SHA256 标识当前字节。Who&When 仅本地研究，未推定其数据继承代码仓库 MIT 许可。

| 样本 | case / 来源版本 | 原文件 / hash |
|---|---|---|
| Who&When `who-short` | `4d51c4bf-4b0e-4f3d-897b-3f6687a7d9f2`；`59b9fcba1aaed7bbf206b5f4d3c68b8face2f49c` | `data/external/who-and-when/algorithm-generated-1.json`；`9a3295bd60713bf8603ad91b2ef3e563cefca448195cf7a19a046fa0b6ba3408` |
| TraceElephant `traceelephant-7` | `assistant_bench_task_7_gpt_4o_wylm9r1ppoet`；`a78a57cdcdf74a080b1bec0f56f85228d86acbac` | `trace_metadata.json` hash `58c10ec846a7c65d20739335f56672d64ecc3388c599036424e3a06ebed854c5`；`step_records.json` hash `5f5f0a5e22cfb24f121479e749e19a4dfca8f58e6619ed225e20bbd5519d1808`；目录 `data/external/traceelephant/assistant-bench-task-7/` |

Who&When 有 6 条消息并以 TERMINATE 结束，文件内记录全部保留，但没有逐次模型请求、时间或可恢复环境。TraceElephant 有 31 条结构化记录，保留 input/output、模型/usage 原字段及工具声明；未提供完整调用父子关系和真实耗时。**两者均不称为完整 runtime 捕获**，也不以合成 fixture 补齐真实样本。

| 原字段 | 规范化与展示 | Phoenix 对应 |
|---|---|---|
| Who history[i] | 编号为明确标注的零基数组索引；定位 `/history/i`，角色/name/content 保留 | 一个 UNKNOWN 消息 Span，不是一次 LLM 调用；historical.message 与脱敏完整 raw_record |
| TE step_records[i] | 保留原 step_id（样本为 1..31）及独立 index、定位 `/i` | output.object=chat.completion 且 choices 是数组时才标 LLM，否则为 Agent 活动；不是仅因 Agent 发言推定 LLM |
| TE input / output | 分别标明 `/i/input`、`/i/output`，原始请求/响应，未拼接系统提示重建 prompt | input.value / output.value 的完整脱敏 JSON |
| tool_logs | 保留声明及原记录位置；当前无对应执行结果证据 | 零个额外 TOOL Span；不能把声明当已执行。请求中的历史工具消息保留在原请求内，不推断独立调用树 |
| agent/role/step | Actor 缺失写未提供；身份含敏感值则整条拒绝导入 | agent_name、step_number、historical.record_kind、source_pointer |
| 数据集标签 | 单独折叠源元数据；不参与分类、父子边和异常推断 | source_metadata；仅明确 is_correct=false 才记录 task_failed=true，其余未知，不推断单步 ERROR |
| created | TE 响应 Unix 秒转 UTC；只是响应创建时刻，不是请求开始或耗时 | 已知时刻原样保留，start=end 为展示坐标，time_is_display_only=true |
| 无时间 | real time / duration 为未知；不用导入时间充当执行时间 | 固定 2000-01-01 UTC，零宽度仅满足必填字段；同时间兄弟 Span 排序不代表原顺序，以 step 编号为准 |
| 无 token / cost | 无则 null/原记录未提供；已提供 usage 保留在原响应供核对 | 不设置会触发估价的模型/token 计费属性；Phoenix 的空值/零宽度不能作为实测费用或耗时 |
| 无父级 | 所有记录放入一个显式的导入分组；不是原调用关系 | 额外一个 CHAIN 根 Span，group_generated=true，所有 parent_relation 标为 generated grouping |

因此预计是 **6+1=7** 和 **31+1=32** 个 Span，不强制“原步骤数=Span 总数”。每个 Span 的来源文件/hash、case、adapter、JSON Pointer、脱敏内容 hash 可与应用记录逐项对应。无 checkpoint；Replay/Fork 不可用。

所有记录带 `trace_origin=imported_historical`、`executed_by_m1_local=false`、`external_task_executed=false`。导入器自身不创建运行 Trace。未知项、主动截断和 UI 折叠分别表达：目前整文件最大 2 MiB、最大 500 条、最大递归深度 30；超限整体拒绝，绝不静默裁剪。界面展开的内容不是短摘要。现有凭据模式基础上仅在此适配器额外脱敏邮箱/PEM 私钥；不承诺识别全部个人信息。

## 4. 重复导入与部分失败

稳定身份含 adapter 版本、Project、来源、revision、case、原文件 SHA256。预览后文件变化会拒绝写入，要求重新预览。跨 case/source 不共用身份。目录内进程锁串行写入；预先分页读取已有 Span，再发送缺失且未尝试的部分，最后核对全部属性、父级、kind、名字、状态和时间。**不是假定稳定 ID 就会自动去重**。

日志原子保存并 fsync，写入前保存 attempted。超时/崩溃/未知回执时，页面显示 uncertain/partial 和已接收/缺失 ID，再点“确认导入 / 核对已有记录”仅核对，不重发已尝试但尚未读到的 ID；服务队列完成后可收敛为 verified。只有从未尝试的缺失记录可自动补写。

若队列已永久丢失，本轮没有自动判断或强制重发 UI：需人工确认服务队列及导入日志，后续增加明确的恢复协议；不删除日志或 Project 来获得干净结果。不同导入目录/不同电脑同时写同一 Phoenix 的全局 exactly-once 未保证；本机应固定一个导入目录。大量历史时当前分页扫描 Project 开销较大，本轮只限两个小样本。

## 5. 无 Key 使用与导师演示

安装是单独步骤，沿用现有双环境；安装可能联网，本轮没有安装/下载。已有环境下在项目根目录运行：

```powershell
Set-Location 'C:\projects\m1-local-lab'
# 只有6006未运行时启动；已有服务不重复启动、不误停
& '.\scripts\start-phoenix.ps1'
# 新终端；不必复制.env或填写模型Key
& '.\scripts\start-observation.ps1' -PhoenixUrl 'http://127.0.0.1:6006' -Port 8000
```

打开 `http://127.0.0.1:8000`。若端口占用，脚本拒绝，不强制结束。停止观测窗口用 Ctrl+C；停止自己用项目脚本启动且有所有权回执的 Phoenix 才用 `stop-phoenix.ps1`。独立启动不写旧运行库，但常规导入会向指定本机 Phoenix 的独立 Project 追加脱敏记录。

1. 指出“本地观测模式，无需 API Key，后端禁止执行”。解释这是查看第三方历史，而不是重新运行 Agent。
2. 选择 Who&When → 预览：6条保留、0排除、7 Span、时间/请求缺失。先说明它是完整保留的消息历史，不是完整模型调用捕获。
3. 确认导入，等待“已逐项读回核对”。展示来源、导入时间、Trace ID，不把健康检查绿灯当接收成功。
4. 点步骤，展开历史消息和脱敏原记录。根据 `data/external/... #/history/0` 在本地编辑器只读定位，再用 hash 校对文件版本。无需把原数据发给聊天或第三方。
5. 优先点“打开本条 Phoenix Trace”。链接由公开 API 返回的 Project ID 加本条 Trace ID 组成；不写死数据库 ID。备用路径是对应 Project → Last 7 Days → Custom → Clear start date and time → Clear end date and time → Apply；此时才显示 All Time。11.35.0 预设菜单里没有 All Time 项。筛选框输入 `context.trace_id == "完整TraceID"` 后回车。普通消息不是 LLM，分组不是原始 Agent 调用；未知时间在2000年，而非最近7天。Phoenix 原生总费用可能显示 $0、占位 Span 显示零宽度，分组可能显示首末响应时间跨度，均不是实际费用或执行耗时的测量值。浏览器会转成本地时区（UTC 2000-01-01 在本次浏览器显示为1999-12-31 19:00）。
6. 用 TraceElephant 验证第二种结构：步骤1对应原数组 `/0`；展开原始 input/output，查看其中真实记录的模型/usage。工具声明不等于执行结果；没有记录就不创建调用 Span。
7. 再次导入同一预览，数量应不增长。刷新页面可继续查看；没有历史重跑按钮，后端也不能通过 API 执行历史。

默认主导航没有归因入口且不请求归因接口。需使用旧功能时，另以原执行模式 `start-app.ps1` 启动，并访问 `/execution`；明确承担真实执行的 Key/费用条件。`/execution?research=1` 恢复研究 UI，仅在执行模式服务可用。

## 6. 本轮验收与证据

本轮使用 Windows / Python 3.11.9 / 项目自带 Playwright Chromium，隔离测试命令：

```powershell
& '.\.venv\Scripts\python.exe' -B scripts/verify_historical_observation.py --app-port 18000 --phoenix-port 16006
```

脚本创建唯一的 `data/test-output/historical-observation/<UTC>-<suffix>/`，只读真实源文件；使用全新 Phoenix 工作目录和导入日志；不访问原 Phoenix 数据库。子 Python 的 socket connect/connect_ex/getaddrinfo/sendto 禁止非回环，Chromium context 阻止外部请求和 service worker。覆盖范围不是整机防火墙，也不保证第三方 native code 绕过 Python socket 的行为。网页外部请求及阻断原因单独记录，不凭域名就断言轨迹泄漏。

### 判定与证据范围

| 项目 | 判定 | 本轮证据与限制 |
|---|---|---|
| 两种真实历史的规范化、脱敏、导入、读回 | PASS | Who 6/6 消息保留，7 Span；TE 31/31 保留，32 Span；均 0 排除、0 截断。逐项核对身份、属性、时间、父级、kind、status；来源文件 SHA 未变 |
| 不虚构调用与性能 | PASS | Who 不设为 LLM；TE 仅有响应证据才设 LLM；工具声明无新增 TOOL Span；生成分组明确标注；未知时间、耗时、费用不作为测量值 |
| 完整还原第三方执行环境 | PARTIAL | 源文件缺原始调用链或完整请求/时间信息；只能忠实呈现已有内容，不能承诺完整 runtime 捕获或可执行 checkpoint |
| 重复导入 | PASS | 两个真实样本重复导入后仍为 7/32 Span；不依赖服务端自动去重 |
| 部分失败 | PASS / PARTIAL | 工程测试验证 write-ahead、未知回执不盲重发、延迟到达核对、未尝试部分补写；真实服务永久丢失已尝试队列的人工恢复协议仍未实现 |
| 无 Key 观测与执行隔离 | PASS | 独立子进程无 runtime/models/WebSurfer 初始化；无 Key 启动，后端拒绝执行端点；只读历史不能 Replay/Fork；没有模型替身冒充真实执行 |
| 主网页 | PASS | 真浏览器选样、预览、导入、详情展开、刷新；Console/page error/request failure/意外 HTTP 错误均 0；无默认归因或任务请求 |
| Phoenix 详情与筛选 | PASS（功能） | Project All Time 可见两个 Trace；按完整 Trace ID 筛选并进入详情。最近七天为空是原始时间/未知时间占位的预期行为 |
| Phoenix 严格离线零错误 | PARTIAL | 原生 GitHub 星标请求 `https://api.github.com/repos/Arize-ai/phoenix` 被浏览器拦截，引起 Failed to fetch；不影响本地 Trace 查询。不据此称为轨迹泄漏，也不宣称完全无外联尝试 |
| 持久化、服务不可用 | PASS | 关闭 Phoenix 时主页面健康状态报告不可用，已保存记录仍可读；应用与 Phoenix 同时重启后 journal 和全部 Span 读回一致 |
| 离线边界 | PASS（所述范围） | Python socket 与 Chromium 请求均限制本地；不是整机防火墙，第三方 native code 不在证明范围 |

### 真实身份与逐步核对

Project 为 `m1-imported-history-v1`。Who Trace 为 `6a33077cf60d674ab78c4041cd08409a`；TE Trace 为 `cb661869f02717318effa413ce2ab9e4`。Project 的服务端 ID 随安装变化，不能移用隔离环境 ID 到导师电脑。

Who 文件没有独立 step ID，以下编号明确为零基数组索引，均为 message/UNKNOWN；不是工具执行证明：

| 原字段位置 | Actor | Phoenix Span ID |
|---|---|---|
| `/history/0` | Excel_Expert | `97024ef74db34094` |
| `/history/1` | Computer_terminal | `f9bbf95002615b85` |
| `/history/2` | BusinessLogic_Expert | `4f4d431366473032` |
| `/history/3` | Computer_terminal | `0222fffcd036f2be` |
| `/history/4` | DataVerification_Expert | `7b02b0ad08d32472` |
| `/history/5` | DataVerification_Expert | `3b1f3a86de28936b` |

TE `step_records.json` 的 `/0`…`/30` 分别保留原 `step_id=1`…`31`，31 条完整映射在验收 receipt 的 `samples[1].mapping`，页面每一步也可核对。工具声明出现在数组索引 3、14、24，仅保留为声明。不要把它们再算成三个已验证工具执行。

### 回归与保护边界

- 完整离线回归：**147 passed, 2 skipped in 98.96s**。2 个 skip 是需要显式授权的真实模型/WebSurfer 测试，未冒充通过；本轮未运行 benchmark 或归因实验。
- 最新定向 `tests/test_historical_observation.py tests/test_api.py`：**29 passed in 39.66s**；Node 前端行为测试 **4 passed**。最终截图等待调整仅涉及验收脚本，不重复完整回归。
- 两个既有虚拟环境 `python -m pip check` 均为 `No broken requirements found.`；未安装或升级依赖。
- `final-boundary.json` 记录 10 个受保护文件与 HEAD 比较：归因实现、外部研究适配器、协议、manifest、冻结结果、配置、图、模型入口和两个 lock 均未改动。变更 Python AST 可解析；大文件/密钥模式扫描无发现，但不等于穷尽所有个人信息识别。
- 真实模型、真实 WebSurfer、外部任务执行次数均 **0**。仅官方资料查阅涉及公开网站；样本/数据库未上传，无新数据下载。测试输出、截图、原始数据和数据库均位于被忽略目录，不进入 Git。

完整回归命令（仅记录已执行，不要求反复运行）：

```powershell
$env:RUN_REAL_MODEL_TESTS='0'; $env:OPENAI_API_KEY=''; $env:LANGSMITH_TRACING='false'
& '.\.venv\Scripts\python.exe' -B -m pytest -q -p no:cacheprovider --tb=short
```

### 残余问题与使用边界

Phoenix 原生界面仍可能显示 `$0`、零宽度 Span，TE 分组的首末响应时刻间隔也不是实测运行耗时。同时间戳消息在 Phoenix 树中可能逆序；应以主网页原编号或 source_pointer 为准。没有修改 Phoenix 源码来掩盖这些限制。脱敏是规则保护，无法保证识别任意自然语言个人信息。仅支持两种明确登记格式；没有任意路径上传、智能识别、实时接入或通用恢复功能。

### 最终浏览器证据与清理

最终证据目录：`data/test-output/historical-observation/20261010T204005Z-ebd55b81/`。`receipt.json` 记录公开 API、37 条逐步映射、39 个 Span 核对、浏览器事件、重启和端口。最终脚本退出码 0，耗时 **104.61 秒**；`core_result=PASS`，整体 `result=PARTIAL`，原因是已记录的 Phoenix 原生外联错误，不是忽略错误后宣称全通过。

| 证据文件（相对上述目录） | 可证明的内容 |
|---|---|
| `who-short-detail.png` | 无 Key 观测、6 条历史消息、原位置、完整脱敏原记录展开；不是新执行 |
| `traceelephant-7-detail.png` | 第二种结构、31 条保留记录、原始请求/响应入口 |
| `phoenix-te-direct-trace.png` | 从主网页真实点击直达所选 TE Trace，已出现实际模型调用 Span 树；原生 `$0`/227 秒跨度不作实测费用/耗时 |
| `phoenix-all-history.png` | 去除时间界限后两个历史 Trace 可见 |
| `phoenix-who-trace.png` | 按完整 Trace ID 筛选进入 Who 消息历史；同时间戳下原生树顺序不替代原编号 |
| `receipt.json` | 两个样本 identity、source hash、逐步 span mapping，重复检查、重启、网络与清理结果 |

截图已实际打开复核。先前 `20261010T090520Z-56ead4c3` 的直达截图停在加载状态，不能独立证明详情已呈现；最终脚本已改为等待实际分组 Span 出现。早期测试脚本的 CSP 等待方式和时间菜单定位问题均记录于各自隔离目录，未通过关闭 CSP 修复，也未将这些失败运行标为成功。

测试自建 Phoenix/app 的启动 PID 为 27820/22048，重启后为 7872/41184；仅关闭脚本持有的进程树，`cleanup_errors=[]`。最终 **4317、8000、8001、16006、18000 均无监听**；**6006 在开始前已运行，结束后仍运行**。本轮真实导入发生在新的 16006 测试存储，未向原 6006 数据库写入样本。测试已停，因此截图里的临时链接不是当前在线演示服务；按第 5 节启动并在指定本地 Phoenix 导入后即可查看。

Git 保持原分支和 HEAD，未暂存、未提交、未添加 remote、未 push、未移动 tag。最终工作区有 **9 个已跟踪修改、17 个未跟踪路径**；17 个中 8 个本来已存在，本轮只修改其中的方向文档和导入器草稿，另新增 9 个文件。已跟踪 diff 为 **+71/-5**，不包含未跟踪内容。`git diff --check` 通过；remote 仍空，两个 tag 对象 SHA 保持 `5a0fe38cdd7445f0a72638d1bbc04ccd300e14ef` 与 `74de81bf81ca1f284c7f48c006285e414d82ec2c`。

本轮触及文件：README；`docs/{product-direction,historical-observation,architecture,acceptance}.md`；`src/m1_local/{historical_observation,observation_api,api,cli}.py`；`web/{observation.html,observation.js,observation.css,observation-icon.svg,index.html,app.js}`；`scripts/{start-observation.ps1,verify_historical_observation.py,verify_attribution_ui.py}`；`tests/{test_historical_observation,test_api}.py`。其余六份既有未跟踪文档未修改。

## 7. 同格式多样本接入（后续迭代）

目的：新增同格式样本不必修改 Python 源码。没有扩大到任意格式自动识别，也没有新增外部执行、归因算法或回放能力。

在本机已有环境中，确认本地 Phoenix 已运行后执行：

```powershell
Set-Location 'C:\projects\m1-local-lab'
& '.\scripts\start-observation.ps1' -PhoenixUrl 'http://127.0.0.1:6006' -Port 8000 -Catalog '.\configs\observation-catalog.example.json'
```

该示例仅登记原来就位于 `data/external/who-and-when/hand-crafted-1.json` 的第三条样本。打开 `http://127.0.0.1:8000`，选择 Hand-Crafted 历史样本 → 预览 → 确认导入 → 展开步骤 → 核对来源。缺文件会显示不可用，程序不会下载。确认导入会向指定本地 Phoenix 的独立 Project 追加记录；不是自动在启动时导入。

登记其他获准资料时，将原文件放入被 Git 忽略的 `data/external/` 子目录，另建自己的本地清单，例如 `data/observation-catalog.json`，使用相同启动参数指定。不要把原始日志、真实凭据或私有资料加入 Git。清单结构：

```json
{
  "schema_version": 1,
  "samples": [{
    "sample": "my-case",
    "label": "供使用者辨认的名称",
    "source": "Who&When",
    "version": "明确的来源版本；无法核实时写 unknown",
    "url": "https://example.org/dataset",
    "license": "获准使用的范围或 unknown",
    "files": ["my-dataset/case.json"]
  }]
}
```

- `source` 只能为 `Who&When` 或 `TraceElephant`，与真实格式一致。前者 files 只有一个 JSON；后者固定为 `["目录/metadata.json", "目录/steps.json"]`，顺序不可颠倒。目录名和文件名无需与内置样本相同。
- 路径相对 `data/external`，不相对清单位置。禁止绝对路径、`..`、反斜线、ADS、解析后的软链接越界。注册并不免除实际内容校验。
- sample 是不超过 80 字符的小写英数字、短横线或下划线 ID；不能与内置 ID 或其他登记重复。清单最大 64 KiB、额外样本最多 32 条；正文仍有 2 MiB/文件、500 步限制，超限拒绝而不裁剪。
- 清单只在进程启动时读取，更改后重启；网页不能改清单或传任意路径。只读查看旧导入记录不依赖清单继续存在。
- 来源链接只记录不请求；版本和许可是提供者声明，不等于本项目独立认证。预览实际计算文件 SHA256。只有格式有效并不证明执行真实性或具备再分发许可。
- 预览后源文件变化会拒绝原确认；相同登记和文件内容重复导入仍核对而不增加 Span。更改文件路径、版本或内容会产生新的导入身份，未承诺跨不同路径的内容去重。
- 原两个内置样本的 Trace ID 不变；未改适配器 v2 的原步骤语义、冻结研究适配器、基线或原数据。没有 name 的消息用“原记录角色”显示原 role，普通消息不改成模型调用。

新增 `tests/test_observation_catalog.py` 使用临时合成 fixture 验证注册、路径/来源约束、预览变更、重启去重、不同 case 隔离和观测模式执行拒绝。新增测试先运行，得到未实现 `load_catalog` 的预期失败，再实现并验证。

本迭代完整离线回归 **166 passed, 3 skipped in 77.32s**；定向 `test_observation_catalog.py test_historical_observation.py test_api.py` 为 **48 passed, 1 skipped in 20.66s**。三个完整套件 skip 为两个真实模型测试与一个系统不允许 fixture 软链接创建的检查；后者没有冒充 PASS，普通路径越界测试通过。依赖未变，本轮未重新安装或重复 pip check。最后两处界面修复（选中高亮随详情同步、长 ID 换行）使用加强的真浏览器验收，不再次重复完整 Python 回归。

第三条真实样本为 `who-and-when/hand-crafted-1.json`，case `6e3be83d1949fa52cba03fb1ce4b5b3bf7e37a83fd7d67694b10b2e439d90cf8`，29 条历史消息 → 30 Span；Trace ID `6502eb71143024c1c03d396143d70092`。文件 SHA256 为 `ceb4cad005df8f45b779e4cc12ff943252e3574443e2f69ba4128c9255ed2df7`。使用之前获准的本机资料，本迭代未重新下载或独立认证来源版本；没有把缺 name 的记录归为真实 LLM 调用。

第一次三样本浏览器核对目录 `data/test-output/historical-observation/20261011T033503Z-dbd12a18/`，核心 PASS，Phoenix 原生外联 PARTIAL。截图复核发现列表高亮未随选中详情同步、长 case ID 溢出；已修正并在最终验收中要求高亮与当前 case 一致。最终证据记录见下文。

最终浏览器证据目录为 `data/test-output/historical-observation/20261011T033827Z-0fe4138d/`（目录时间为 UTC）。`receipt.json` 包含 3 条真实样本的逐步映射；`who-hand-detail.png` 已打开复核，角色、选中高亮和长 ID 换行正常。脚本退出 0，耗时 46.55 秒。主网页 Console/page error/request failure/意外 HTTP 错误均 0；三条样本共 **66 条源记录、69 个 Span** 全部读回核对，重复导入数量不变，刷新及双服务重启通过，原文件 hash 未变。主网页直达所选 TE Trace 通过。核心 PASS；Phoenix 两次 GitHub 星标请求被拦截，整体保持 PARTIAL，不宣称全无外联。

测试子进程 46868/46408，重启后 36028/47456 已全部按所有权清理，`cleanup_errors=[]`。16006/18000、4317、8000/8001 均无监听；原有 6006 保持运行。没有向原 6006 导入，使用说明中的导入由用户另行操作。模型/WebSurfer/外部任务调用均 0；无新数据下载、无依赖升级。

本增量新增 `configs/observation-catalog.example.json`、`tests/test_observation_catalog.py`，修改启动层、历史适配/接口、观测页面、验收脚本及上述文档，保留开始时所有未提交成果。结束时总工作区为 9 个已跟踪修改、19 个未跟踪路径；相对 HEAD 的已跟踪 diff 为 +84/-5，不包含未跟踪文件；不是说这些改动都由本增量产生。10 个受保护文件仍与 HEAD 一致，新增测试/示例语法及敏感模式检查通过，`git diff --check` 通过。未暂存、未提交、未添加 remote、未修改 tag 或 push。

仍未交付：任意格式上传/自动识别、批量无确认导入、跨路径内容去重、实时外部 Agent 接入、完整工具调用链、归因有效性验证或通用 Replay/Fork。当前提高的是已有格式的接入能力，不能将本轮工程测试解释为这些最终能力已经实现。

## 8. 2026-10-12 现场 Phoenix Project 链接修正

用户现场打开 Project 遇到 `projectId not found in path`。原 11.35.0 页面代码将解码后的路由参数与原始 pathname 片段比较；本地公共 API 返回 ID `UHJvamVjdDoxNw==`，观测接口将填充 `==` 编码成 `%3D%3D`，触发此错误。之前隔离环境的 ID 无填充，原浏览器验收未覆盖这一边界，不能据此前 PASS 推断所有 Project ID 都能跳转。

修正 `observation_api.py` 的链接生成，保留路径中合法的 `=`，其他字符仍转义；未修改 Phoenix 源码、数据库、Span 或导入身份。新增三种 ID（无填充、单填充、双填充）的离线临时数据测试；修改前 1 passed / 2 failed，修改后定向命令 `.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider tests/test_historical_observation.py tests/test_observation_catalog.py --tb=short` 得到 **39 passed, 1 skipped in 44.81s**。跳过项仍为系统不允许 fixture 软链接创建，未重跑完整套件。

使用现有 Chromium，只读访问原 6006 的现有 Project 和 Who&When Trace；没有重新导入。错误地址真实复现，修正地址正常打开 Project 及 Trace `6a33077cf60d674ab78c4041cd08409a`，截图中可见生成分组和 6 条消息。证据保存于 Git 忽略目录 `data/test-output/phoenix-project-link-check/` 的 `receipt.json`、`reported.png`、`candidate.png`、`candidate-trace.png`。浏览器阻断非回环请求，两次 Phoenix GitHub 星标请求被拦截并产生 `Failed to fetch`，不宣称 Console 全部无错误；此阻断范围是浏览器层，不代表系统或后端网络隔离。

未停止现有应用/Phoenix 服务；当前已运行应用仍使用旧的 Python 链接生成代码，修正将在应用下次重启后生效。现场可先直接打开保留 `==` 的地址继续查看，不需要删除数据或重新导入。浏览器测试退出并关闭自身 Chromium。模型、WebSurfer、外部任务调用均为 0；本次未提交 Git。

## 9. 2026-10-12 日常服务加载修正与运行版本信息

**问题与原因。** 日常 8000 观测进程于 2026-10-11 21:25（本地时间）启动，早于第 8 节的链接修正，修复前其 `/api/observation/health` 仍返回 `.../projects/UHJvamVjdDoxNw%3D%3D/traces`。磁盘代码无需再改链接逻辑；问题是运行进程未加载当前代码。

**受控重启。** 确认该进程命令行为本项目 `.venv` 的 `m1_local.cli observe`（观测模式没有执行任务可中断）后，只结束该 PID 树；Phoenix 用 `stop-phoenix.ps1`（核对所有权回执）和 `start-phoenix.ps1` 重启。观测应用以原参数（`--catalog .\configs\observation-catalog.example.json`）后台启动，PID 写入 `data/pids/observation.pid`，日志位于 `data/logs/observation.{stdout,stderr}.log`。未删除或重建任何数据库、导入记录或 Project。

**运行版本。** 主网页标题下显示“运行版本（进程启动时记录）”，`/api/observation/session` 与 `/api/health` 返回 `build`：

| 字段 | 含义 |
| --- | --- |
| `commit` | 进程启动时 `git rev-parse HEAD`；无 Git 或失败为 `unknown` |
| `uncommitted_changes` | 启动时 `git status --porcelain` 是否非空（含未跟踪文件）；未知为 `unknown` |
| `started_at` | 进程创建应用的 UTC 时间 |
| `mode` | `observation` 或 `execution` |

这些值只在启动时取一次（`src/m1_local/build_info.py`），运行期间切换分支或提交不会改变显示，因此“页面版本 ≠ 当前 `git rev-parse HEAD`”即说明需要重启。`uncommitted_changes=true` 时 commit 只能说明基线，不能单独确定加载的代码。不返回环境变量或密钥。

**检查与停止。**

```powershell
# 看进程加载的版本与当前磁盘 HEAD 是否一致
(Invoke-RestMethod http://127.0.0.1:8000/api/health).build
git rev-parse HEAD
# 停止本次后台启动的观测应用（先核对 PID 是本项目 observe 进程）
$p = [int](Get-Content .\data\pids\observation.pid -Raw)
(Get-CimInstance Win32_Process -Filter "ProcessId = $p").CommandLine   # 应含 m1_local.cli observe
taskkill /PID $p /T /F
# 停止 Phoenix（有所有权回执）
& '.\scripts\stop-phoenix.ps1'
```

之后可按第 5、7 节用 `start-observation.ps1`（前台）重新启动。

**验收（日常 8000/6006，真实 Chromium，两次受控重启后各一次）。** 证据位于 Git 忽略目录 `data/test-output/daily-closure/`（`*-restart1`、`*-restart2`、`no-key-*`），验收器为同目录的 `verify_daily_observation_closure.py`（由先前 `verify_design_goal_acceptance.py` 派生，改为必须通过主网页按钮完成跳转）。

- 三条样本（Who 短样本 6→7、TE 31→32、Who Hand-Crafted 29→30）预览、重复导入、逐项原文/API/Phoenix 比对、刷新保持选择均通过；Project Span 总数前后均为 69，导入时间未被改写。
- 主网页“打开本条 Phoenix Trace”三次与“打开对应 Phoenix Project”一次均直接打开 `.../projects/UHJvamVjdDoxNw==/...`，渲染 Trace Details，未出现 `projectId not found in path`；Project All Time 显示 3 条 Trace。
- 观测模式 `POST /api/runs`、`/resume`、`/replay`、`/fork` 与 `GET /execution` 均为 403。
- 独立无 Key 启动（18002，子进程模型 Key 均为空）通过，临时进程已结束。
- 主网页 Console/page error/失败请求/HTTP 错误均为 0。每个 Phoenix 页面仍各有 1 条 `Failed to fetch`：来自 Phoenix 侧栏 “Star on GitHub” 对 `https://api.github.com/repos/Arize-ai/phoenix` 的请求，被验收浏览器的非回环拦截阻断；这与已修复的 Project ID 编码错误是两个独立原因，不影响本地 Trace 显示。
