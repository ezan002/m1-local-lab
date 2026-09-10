# 离线错误归因后端 MVP

> 2026-09-10：三样本 smoke 的算法与边界保持不变；其上新增的固定 30 样本评估协议见 [attribution-benchmark-v1.md](attribution-benchmark-v1.md)。三种 baseline 仍是同一冻结实现，新增层只负责规范化、分母、分组、manifest 和 Phoenix benchmark 写入。

> 2026-09-10 更新：后端算法和评价边界保持不变；本文件末尾原有“没有 UI”限制已由只读可视化阶段解除。页面实现与新限制见 [failure-attribution-ui.md](failure-attribution-ui.md)。

记录日期：2026-09-02（America/New_York）。本阶段只验证三条既有外部历史样本的数据流、隔离边界、指标和 Phoenix 集成，不证明归因算法有效性。

## 架构位置与隔离边界

该模块是 `external_datasets.py` 之后、Phoenix Experiment 之前的离线旁路，不进入 LangGraph state，也不新增 Agent、节点或调度器：

```text
ExternalTraceRecord
  ├─ deepcopy + recursive guard → AttributionInput → deterministic method → AttributionPrediction
  └─ separate object            → AttributionGroundTruth ───────────────────→ evaluator
                                                                            ↓
                                                               metrics / Phoenix Experiment
```

`AttributionInput` 只保留 source/revision/split/case、task、可用的 expected answer、来源专用的规范化步骤、脱敏摘要和截断/provenance 字段。`AttributionGroundTruth` 单独保存责任 Agent、决定性步骤、失败原因、标签来源和可评价性。三种 predictor 的函数签名只接收 `AttributionInput`；它们不能查询 Phoenix，也没有闭包或全局 case 缓存。

在构造 inference view 前先深拷贝原始 trace 与 expected answer，随后递归检查字典、列表、metadata 和 Pydantic 对象。发现 `responsible_agent`、`decisive_step`、`failure_reason`、`ground_truth`、`label`、对应 expected/mistake 字段等任一禁用 key 会立即失败，不会静默删除。Pydantic 模型采用 `extra="forbid"` 和 frozen 配置；evaluator 在预测完成后才独立接入 Ground Truth，并检查 prediction 未被修改。

TraceElephant 与 Who&When 分别由独立 normalizer 读取各自 step/agent/event 结构，统一成最小推理 step，但不改变原始 Dataset schema、Agent 名称和编号。

## 三种零费用基线

- `last_active_agent`：选择最后一个同时具有 Agent 和有效消息、动作、工具事件或错误的步骤。
- `first_explicit_error`：选择最早出现异常字段、失败/阻断状态或明确失败表达的 Agent 步骤。
- `rule_based`：按工具失败、明确错误、重试/循环/停滞、final-answer 不一致、最后有效动作的通用固定权重排序；同分时稳定选择较早步骤。

三者均不调用模型，相同输入产生相同结果，证据不足时返回 `abstained`，不伪造 Agent 或 step；prediction 含 reason、0–1 confidence、method、status、evidence IDs 和 warnings。

## 指标、状态和分母

每个 case/method 输出 `agent_exact_match`、`step_exact_match`、`joint_exact_match`、`step_distance`、`prediction_coverage`、`agent_scorable`、`step_scorable` 和 `provenance_guard`。评估状态区分 `scored`、`abstained`、`label_missing`、`trace_truncated`、`evaluation_failed`。

- exact-match 只把对应标签可评价的记录放入分母；可评价样本上的 abstain 为 0。
- joint 只在 Agent 与 step 均可评价时计算。
- distance 只纳入 step 可评价且预测/标签均可转成整数的记录；abstain 和非数字 step 为 `None`。
- coverage 与 provenance/scorable 标志以全部方法运行数为分母。
- `None` 永不转换成 0；每项汇总同时保存 numerator、denominator 和 value。

每个 inference view 都保存 `trace_original_step_count`、`trace_retained_step_count`、`trace_truncated`。Ground Truth join 再计算 `decisive_step_present_in_trace`。若决定性步骤未保留，step/joint/distance 不进入分母，Agent 标签仍可单独评价；不会猜测被截断内容。本次 TraceElephant 样本从 31 步保留 20 步，但真实步骤 5 仍在输入内，所以 step 可评价。

## 三样本实测

预测结果（case ID 为显示方便做了缩写）：

| case | method | status | predicted Agent | predicted step | agent / step / joint | distance |
|---|---|---|---|---:|---|---:|
| Who&When hand `6e3…90cf8` | last_active_agent | predicted | WebSurfer | 28 | 1 / 0 / 0 | 16 |
| Who&When hand `6e3…90cf8` | first_explicit_error | abstained | — | — | 0 / 0 / 0 | — |
| Who&When hand `6e3…90cf8` | rule_based | predicted | Orchestrator (thought) | 5 | 0 / 0 / 0 | 7 |
| Who&When algorithm `4d51…9f2` | last_active_agent | predicted | DataVerification_Expert | 5 | 0 / 0 / 0 | 5 |
| Who&When algorithm `4d51…9f2` | first_explicit_error | abstained | — | — | 0 / 0 / 0 | — |
| Who&When algorithm `4d51…9f2` | rule_based | predicted | DataVerification_Expert | 5 | 0 / 0 / 0 | 5 |
| TraceElephant `assistant_bench_task_7…` | last_active_agent | predicted | WebSurfer | 20 | 1 / 0 / 0 | 15 |
| TraceElephant `assistant_bench_task_7…` | first_explicit_error | predicted | WebSurfer | 6 | 1 / 0 / 0 | 1 |
| TraceElephant `assistant_bench_task_7…` | rule_based | predicted | WebSurfer | 20 | 1 / 0 / 0 | 15 |

宏观汇总：

| method | Agent exact | Step exact | Joint exact | Mean step distance | Coverage | Provenance |
|---|---:|---:|---:|---:|---:|---:|
| last_active_agent | 2/3 = 0.667 | 0/3 | 0/3 | 36/3 = 12 | 3/3 | 3/3 |
| first_explicit_error | 1/3 = 0.333 | 0/3 | 0/3 | 1/1 = 1 | 1/3 | 3/3 |
| rule_based | 1/3 = 0.333 | 0/3 | 0/3 | 27/3 = 9 | 3/3 | 3/3 |

这只是管线输出。样本仅三条且规则没有调参求满分，因此不能据此宣称任何方法有效。

## Phoenix 与重复验证

复用 Dataset `m1-local-lab-external-trace-smoke`：ID `RGF0YXNldDox`，version ID `RGF0YXNldFZlcnNpb246MQ==`，只读回查仍为 3 个唯一 examples。2026-09-10 零费用复验生成的最新具名 Experiment `m1-local-lab-failure-attribution-mvp` ID 为 `RXhwZXJpbWVudDo2`，包含 3 methods × 3 cases = 9 task runs，以及 8 个 namespaced evaluator × 9 = 72 evaluations。ID 仅是本次验收证据；生产读取层按名称元数据选择最新实例，不硬编码该值。

Phoenix task result 保存 prediction 和仅由 inference 得到的 metadata：method、case、source、revision、步骤计数、truncated，以及 `trace_origin=imported_historical`、`executed_by_m1_local=false`、`external_task_executed=false`。`decisive_step_present_in_trace` 与 scorable 字段只写在 evaluation metadata；真实标签仍只存在 Dataset expected/output 与 evaluation 阶段。外部历史没有转换为新 OTel Trace。

查看时先确保 Phoenix 启动，打开 `http://127.0.0.1:6006`，在 Datasets 中选择上述 Dataset，在 Experiments 中按名称或 ID 查找实验。验证脚本使用 Phoenix 11.35.0 的 `experiments.create/log_run/log_evaluation/get_experiment` 实际接口并回读数量与 evaluator 名称。

```powershell
& '.\.venv\Scripts\python.exe' '.\scripts\verify_failure_attribution_mvp.py'
```

脚本复用健康的既有 Phoenix；否则只启动项目自己的 Phoenix 并在 finally 中只停止该进程。重复运行复用同一 Dataset，不增加 examples；每次保留一个新的 Experiment 作为可审计验证记录。实测输出：PASS、paid model calls 0、real WebSurfer calls 0、external tasks 0、full dataset downloaded false、label leakage check passed。

## 尚未实现

没有 LLM predictor/evaluator、学习型或因果归因算法、全量 benchmark、阈值调优、模型/网络/浏览器/安全策略/checkpoint 的根因分类。现有只读 UI 只展示本节既有预测与评价，不改变算法；见 [failure-attribution-ui.md](failure-attribution-ui.md)。未来若引入真实算法，继续只接收 `AttributionInput`；LLM evaluator 只能位于预测后的 evaluator 边界，并需另行审批费用、数据许可、隐私策略和全量数据下载。
