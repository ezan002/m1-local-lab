# 全仓工程审查与审慎优化

审查日期：2026-09-12。结论：已完成有证据支持的局部修复与完整离线回归；仍存在 P1 部署和进程所有权问题，不能据测试通过宣称整个系统已具备严格隔离或可直接交付生产。

## 范围与原始基线

- 根目录：`C:\projects\m1-local-lab`，Windows / Python 3.11，无 Docker。
- 开始分支：`feature/attribution-benchmark-v1`；提交 `1c1441c91cd6f4066d00658f628ed78e33e53f68`。开始时工作树干净，未发现适用的 `AGENTS.md`。
- 工作分支：`feature/full-project-audit-optimization`。未提交、未增加 remote、未 push、未新建或移动 tag。
- 原有 tag：`m1-local-lab-v1.0-smoke`、`m1-local-lab-v1.1-attribution-ui`。
- 阅读 README、架构、验收、三个归因文档、代码与测试；三个子代理分别只读审查执行恢复、遥测/部署、归因协议，主代理统一实施代码变更并复核。
- 原离线基线：`65 passed, 2 skipped in 32.06s`；两环境 `pip check` 通过；41 个原有 Python 文件 AST 解析通过。
- 测试运行使用临时 SQLite、临时产物、RuleBasedGateway、MockTransport 或内存 exporter。未运行会写现存 Phoenix/结果文件的历史验收脚本。子代理早期定向测试使用了新建独立 `data/test-output/audit-protocol-subagent`；其余使用系统临时目录，均未覆盖既有运行或研究数据。

## P0

本次审查没有确认 P0 缺陷。这不等于不存在尚未覆盖的缺陷。

## P1：已修复并验证

| 问题与实现位置 | 原复现证据 | 修复与语义影响 | 回归证据 |
|---|---|---|---|
| `runtime.py:51,255,291,320`：恢复/Replay 准入跨 await，Fork 可并发改同 thread 最新 head | 两个并发 resume 均成功；在 Replay 提交与读取之间插入 Fork，Replay 的 state/run ID 与 Fork 相同 | 所有 checkpoint 写入入口先预留准入，准备失败释放。保持单运行模型；并发请求立即拒绝，不排队重做 | `test_control_admission_covers_awaits_and_all_checkpoint_writers` 三种入口×四类竞争调用 |
| `runtime.py:169`：暂停通知覆盖上一节点的 execution link | `__interrupt__` 复用 orchestrator execution ID，`INSERT OR REPLACE` 把真实关联替换成 input=output 的通知记录 | 通知不创建节点 commit link；实际节点 input ID 来自 snapshot.parent_config。未修改已有数据库行 | `test_pause_preserves_real_execution_links_and_input_checkpoints` 对照真实 snapshot/execution/parent |
| `telemetry_export.py:21,70`、`observability.py:128`：第三方异常与序列化摘要泄漏 | MockTransport 模拟 401，SDK exception event 与 status 包含合成凭据；双层 JSON、resource/link/event-name 也可漏出 | 应用 provider 的唯一出口增加快照脱敏；移除异常 message/stack，保留类型、父子 ID、kind、时间、计量；配置完成前不注册为全局 provider。冻结归因预处理不变 | `test_export_boundary_hides_sdk_exception_and_preserves_parentage`、`test_export_boundary_covers_resource_links_and_event_names`、`test_initialize_replaces_phoenix_exporter_before_instrumentation` |
| `runtime.py` 异常处理：错误原文进入元数据和 SSE | 合成配置密钥与 JSON api_key 原样出现在 run.error/events | 在错误持久化前脱敏并限长；保留错误类型和安全说明 | `test_exception_credentials_do_not_enter_current_error_or_sse` |
| `scripts/install.ps1:1,9,27`：个人路径、原生命令失败后继续、Windows 引号 | 原版依赖作者用户目录；PowerShell 5.1 的 ErrorActionPreference 不检查原生退出码；初版修复的双引号在真实 -c 传参时丢失 | Python Launcher/PATH/显式 PythonExe 发现、3.11 x64 校验、逐条检查 LASTEXITCODE。保留已有环境，安装失败即停。未改依赖版本或模型 | `test_installer_uses_supplied_python_and_stops_before_later_commands` 空格路径/退出码17/版本拒绝；`test_version_probe_works_with_real_python_and_windows_argument_passing` 使用真实解释器 |
| `attribution_protocol.py:23,101`：冻结协议可漂移 | 6/4、14/6 划分及 abstain、normalization、license 修改仍能通过旧校验 | 以现有 canonical JSON digest 拒绝漂移；格式空白/键顺序不影响；不改任何冻结参数 | `test_protocol_rejects_semantic_drift` 五类漂移 |

## P1：仅提出方案，未修改

| 已证实问题/合理风险 | 位置及可复现证据 | 建议与未实施原因 |
|---|---|---|
| **已证实：跨 runtime 所有权冲突** | `runtime.startup()` 调 `storage.mark_interrupted_runs()`；`cli.py:29` 连 history 都会 startup。在同一临时 DB 中将 runtime A 的 planner 挂起，再 startup runtime B，A 的记录变 interrupted，但 A task 仍在运行，B 又能创建运行 | 需要跨进程文件锁/租约及 recovery ownership。当前只允许一个 owner：API 启动期间不要并发 CLI 操作相同数据库。这是机制变化，本轮未仓促实施 |
| **已证实启动行为：Phoenix gRPC 通配绑定；实际 LAN 可达性未验证** | 锁定 Phoenix 11.35.0 的 `phoenix/server/app.py` 创建 GrpcServer；`server/grpc_server.py:108` 使用 `[::]:{port}`。`start-phoenix.ps1` 只设 HTTP host，默认 gRPC 4317 | 需要受支持的 gRPC 回环启动方式或经验证的版本方案。未改第三方包/防火墙。端口0仍会开随机监听，read-only 会阻止研究写入，均不是等价修复。未为本次审查启动 Phoenix |
| **已证实：外部历史 task 导出未全面脱敏** | `external_datasets.py:179,238` 使用 task.strip()；合成 token 可进入 phoenix_example input。旧脱敏函数也不覆盖全部 quoted JSON 形态 | 建议单独定义 Dataset 导出安全视图及研究输入版本。直接改 adapter/共享脱敏会改变冻结输入，因此本轮保持现有预处理，未上传任何数据 |
| **合理风险：浏览器行为约束弱于文档原声明** | `websurfer.py:90` URL route 未限制 HTTP method；非导航公开资源可跨主机；登录/表单/上传/支付主要靠提示词。DNS 检查与实际解析有时间差，service worker 边界未测 | 独立设计严格只读网络/动作策略并用合成页面验收；不更改 Agent 职责，也不以未执行的越权场景冒充实证。本轮只修正文档边界 |

## P2：已修复并验证

| 问题 | 修复位置与可复现证据 | 语义影响 |
|---|---|---|
| 新分支 metadata 丢 dataset_case_id | `runtime.py:310,340` 从所选 snapshot 继承；`test_branch_metadata_and_uninitialized_checkpoint_rejection` | 统一新分支的 run/state/span provenance；不回填历史行 |
| 最初 __start__ 快照误允许 Replay/Fork | runtime 检查已初始化 task；同上测试验证 ValueError | 将原 LangGraph InvalidUpdateError 改为可解释的拒绝；终结快照仍不可回放 |
| 成功恢复后 UI 仍展示旧当前 error | `_execute` 开始时清当前 error；`test_resume_clears_current_error_but_keeps_historical_event` | 原始 error 事件保留，新尝试的当前状态不被旧错误污染 |
| 尚未启动的后台 task 取消后占住准入 | `shutdown` 收尾清理 task/reservation 并标 interrupted；`test_shutdown_before_background_task_starts_releases_admission` | 可正常释放运行资源；不自动重跑 |
| root span 在 flush 后才结束 | `_execute.finally` 统一 flush；`test_run_flush_happens_after_root_span_ends` | 完成/失败/取消都在根 span 结束后交给 exporter，不承诺远端已接收 |
| partial SSE 不关闭、历史只读500条、连接竞争 | `runtime.sse` 增 partial；`web/app.js:227,272,340,370` 分页、终态补读、传输错误过滤、选择 token | SSE ID/事件持久化定义不变；501事件服务器测试及4个 JS 行为用例通过 |
| API 空白任务/错型 Fork/无效游标/IPv6 Host | `api.py:30,46,253,267` 与 host 格式；`test_api.py` 参数化测试 | 无效请求返回422；包括未知 Fork 字段从409改为422。事件游标限制为 SQLite signed 64-bit 非负整数；有效接口保持 |
| manifest 声明与逐例划分不一致 | `attribution_benchmark.load_manifest` 核验真实计数、身份、hash、确定性划分；四种篡改重算 manifest hash 后仍拒绝 | 只增加拒绝条件，不重新选样，不修改 manifest |
| 错误 case/source/revision join 仍评分 | `evaluate_benchmark_prediction` 关联四对象身份及来源；五种错配测试 | 拒绝错误 join；正常分母和评分公式保持不变 |

## P2：仅提出方案与研究局限

- **已证实预算语义问题**：`graph.py:122` 使用 gateway 进程累计 calls，而非持久化每 run 预算。用 `max_model_calls=4` 连续运行两次 RuleBased file task，首次 completed、第二次 planner 后预算失败，重启计数又重置。WebSurfer 内部调用也不能据此当作完整费用硬上限。需先定义每 run/恢复/分支统一计量契约；本轮未改变调度和预算语义。
- **已证实 adapter 索引覆盖**：`external_datasets.py:131` 的 `{trace_index:index, **item}` 可被来源 trace_index 覆盖；单步 history 传 trace_index=99，会使 step0 不可评分。30 个冻结样本均无此来源字段。为保持预处理版本，列为下一版 adapter 修复，不动既有标签/结果。
- `safe_json_summary`、`redact_sensitive` 的历史文本裁剪仍保留。30/30 样本存在内容裁剪，manifest 的 trace_truncated 只反映步骤保留，不能作为内容无损指标。
- predictor 可用任务标准答案 expected_answer；责任 Agent、决定性 step、失败原因才是 evaluator-only 标签。结构化 guard 不能证明历史自然语言没有透露标签。
- Frozen Test 结果已经被查看。本次 30×3 内存重算是完整性核验，不是新的盲测、方法开发或调参。当前结果仅描述三种简单基线，不能证明算法有效或泛化。
- manifest 的 hash 是内部一致性校验，不是签名认证；固定来源/hash/既有选择逻辑仍是来源可信性的前提。
- 两个 lock 精确固定版本，但没有包内容哈希；build backend 未完全锁定。没有擅自升级库或增加依赖。
- 文件白名单检查解析后的真实路径能挡目录逃逸；resolve 后 is_symlink 不能实现字面上的“拒绝所有符号链接”。尚未确认目录白名单绕过。
- 代码样式、文件长度、命名和前端框架选择属于风格偏好，没有据此大规模重构。

## 功能—代码—测试对应

| 功能 | 实现 | 验证 |
|---|---|---|
| 唯一图调度、文件/coder worker、预算失败 | graph.py、models.py、security.py | test_runtime_offline.py、test_config_security.py |
| checkpoint、暂停、重启恢复、Replay/Fork | runtime.py、storage.py、state.py | test_runtime_offline.py、test_api.py |
| OTel 父子/kind/关联/脱敏/flush | observability.py、telemetry_export.py、graph.py | test_observability.py；内存 Phoenix exporter 集成 |
| API Host/Origin/CSRF、参数与 SSE | api.py、runtime.py | test_api.py、runtime 的501事件测试 |
| 网页分页/去重/终态/异步竞争 | web/app.js | frontend_behaviors.cjs，经 test_frontend.py 调用；不是浏览器视觉验收 |
| Windows 安装及参数传递 | scripts/install.ps1 | test_windows_install.py，空格目录、真实 Python、脚本语法 |
| 外部历史 provenance 与标签隔离 | external_datasets.py、attribution.py | test_external_datasets.py、test_attribution.py |
| 冻结选择、分母、abstain、截断、关联 | attribution_protocol.py、attribution_benchmark.py | test_attribution_protocol.py、test_attribution_benchmark.py |
| Phoenix 归因只读视图 | attribution_read_model.py | test_attribution_read_model.py（公共 API fake client），未写数据库 |

## 验证结果与复现

完整回归：**107 passed, 2 skipped in 76.04s**。两项 skip 是真实模型/WebSurfer。新增前端 Python 用例内执行4个 Node 行为用例，全部通过。

```powershell
$env:RUN_REAL_MODEL_TESTS='0'
$env:OPENAI_API_KEY=''
$env:LANGSMITH_TRACING='false'
& '.\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider --tb=short
& '.\.venv\Scripts\python.exe' -m pip check
& '.\.venv-phoenix\Scripts\python.exe' -m pip check
git diff --check
```

两环境 pip check 均为 `No broken requirements found.`。44 个 Python 文件 AST 解析、PowerShell Parser 和 git diff --check 通过。完整套件第一次受沙箱 Node `spawn EPERM` 影响（106通过、1失败、2跳过）；授权相同测试命令后整套通过，不把权限失败掩盖为应用修复。没有新增静态检查依赖，仓库未配置 Ruff/mypy。

真实浏览器验收：**BLOCKED**。本会话 Browser 初始化成功但选择返回 `No browser is available`，按诊断列举得到 `[]`。未生成或引用本次真实 UI 截图；未把 JS VM 测试当成 DOM、布局、焦点、剪贴板或 Chromium 验收。遵循 Browser 技能的连接与控制边界，未绕过不可用连接去控制其他浏览器。既有历史截图不算本次证据。

Phoenix：OTel 的实际公共 provider/processor/exporter 替换路径已以内存 sink 验证；SDK 401 来自 httpx.MockTransport。没有重跑真实 OTLP 接收、Phoenix 重启持久化或视觉验收，也没有因此触碰现存 Phoenix 数据库。

冻结核验可复现（只读原始文件、内存计算，不调用会写库的 verifier）：

```python
import json
from pathlib import Path
from m1_local.attribution_benchmark import (
    load_manifest, verify_source_receipt, verify_frozen_baseline,
    load_benchmark_records, run_benchmark,
)
from m1_local.attribution_protocol import load_protocol
root = Path.cwd()
protocol = root / 'configs/attribution_eval_v1.json'
load_protocol(protocol)
manifest = load_manifest(root / 'datasets/manifests/who_when_benchmark_v1.json', protocol_path=protocol)
verify_frozen_baseline(root)
verify_source_receipt(manifest, root / 'data/external/who_when')
records = load_benchmark_records(manifest, raw_root=root / 'data/external/who_when')
result = run_benchmark(records)
tracked = json.loads((root / 'docs/results/attribution-benchmark-v1-summary.json').read_text(encoding='utf-8'))
assert len(records) == 30 and len(result.predictions) == len(result.evaluations) == 90
assert [x.model_dump(mode='json') for x in result.summaries] == tracked['summaries']
assert list(result.confusion_matrix) == tracked['confusion_matrix']
```

该核验通过：5 个来源文件 hash/size 正确；30 cases、90 predictions/evaluations，全部 summary groups 和 confusion matrix 逐值等于 tracked JSON。冻结 attribution.py、external_datasets.py、协议配置、manifest、聚合结果，以及运行时 config.py/graph.py/models.py 和两份 lock 共10个文件与原提交内容一致。

## 跨电脑部署

使用 `git archive HEAD` 加本次待审源码变更模拟全新 checkout（不是添加 origin 的 git clone），在新的带空格系统临时目录验证；开始时无 `.env`、`data/`、`.venv`、`.venv-phoenix`。使用系统 Python 3.11 新建两个虚拟环境，设置独立 PLAYWRIGHT_BROWSERS_PATH 和禁用 pip cache，避免依赖旧环境与数据。完整安装结果将在本报告收尾时写入。

## 密钥、个人数据、数据文件与 Git

- 扫描当前跟踪文件及4个可达提交的87个唯一 blob，检查密钥形态、个人绝对路径、邮箱和运行/原始数据类路径；扫描只输出类别和位置，没有打印密钥值。
- 未发现可确认的真实密钥。唯一 credential-shape 命中是 `scripts/verify_runtime_trace.py` 的合成脱敏测试值，不是运行凭据。模式扫描不等于穷尽秘密发现。
- 原 README/安装脚本及历史存在个人用户路径；当前安装入口已去除。提交元数据包含1个作者身份（姓名/邮箱），值未打印。没有改写历史。
- 可达历史和当前跟踪文件未发现 `.env`、SQLite/Phoenix 数据库、原始数据目录、日志、PID、pyc 或虚拟环境；受控 manifest/安全聚合结果是已有研究元数据，不等于原始轨迹。
- 现存原始数据、数据库、缓存和产物仍留在被忽略目录，没有删除、覆盖或加入版本控制。Who&When 仅按本地研究处理，不把代码 MIT 许可解释为数据再分发授权。

## 费用、外部调用与交付状态

项目付费模型调用 **0**，真实 WebSurfer **0**，第三方 Agent/脚本执行 **0**，外部数据上传 **0**，数据下载 **0**。依赖/浏览器组件安装探测是部署验证的一部分，独立记录其网络结果。没有修改运行时模型配置、生成代码执行策略、Agent 职责或 LangGraph 图。

Git HEAD 与提交历史不变；两个既有 tag 不变；没有 remote。所有修复、测试和文档保留为未提交工作树差异，等待用户审阅后再决定是否提交。
