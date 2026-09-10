# Attribution benchmark protocol v1

记录日期：2026-09-10（America/New_York）。结论：PASS。当前版本把 Who&When 30 样本、三种既有确定性基线、指标分母和 Phoenix 写入契约固化为 `attribution-eval-v1`，没有修改归因算法、LangGraph、Agent、API 或网页。

## 固定来源与许可边界

- 官方代码仓库：`mingyin1/Agents_Failure_Attribution`，commit `b2bae5c5b06d681d04ea5e9b63b7a30525c04925`。
- 官方数据入口：`Kevin355/Who_and_When`，revision `59b9fcba1aaed7bbf206b5f4d3c68b8face2f49c`。
- 该 revision 含 Hand-Crafted 58 行、Algorithm-Generated 126 行，共 184 行。下载器仅获取两个 Parquet、数据卡、固定 commit 的 README/LICENSE，共 `1,903,897` bytes；累计硬上限为 25 MiB。
- 两个 Parquet 覆盖该 revision 的完整 184 行，所以 `full_who_when_downloaded=true`；只选取 30 行进入本项目基准并执行 90 次零费用预测。没有克隆仓库、执行上游代码或运行上游实验。
- 仓库 LICENSE 是 MIT，但 Hugging Face 数据卡没有独立 `license` 字段。本地策略固定为 `local research use only; redistribution not authorized`；原始 Parquet、抽取 JSON 和详细逐例报告均在被 Git 忽略的 `data/` 下，不进入提交。
- 文件大小与 SHA-256 写入本地 `data/external/who_when/download-receipt.json`；受控清单只保留固定 URL、大小、哈希和 revision，不包含短期签名 URL。

## 固定选择、划分与清单

协议配置为 `configs/attribution_eval_v1.json`，清单为 `datasets/manifests/who_when_benchmark_v1.json`。当前 manifest SHA-256 是：

```text
c18401f0605f50ec8d422edcdcaca8d64ee77173324511a975889d0b88c6cb2b
```

选择前先对 184 行执行与预测结果无关的 schema 校验。选择键严格为：

```text
SHA256("m1-local-lab-attribution-benchmark-v1" + source_split + original_case_id)
```

每个 source split 按键升序，取 Hand-Crafted 10 条、Algorithm-Generated 20 条。Dev/Frozen Test 使用独立 domain-separated hash 排序：Hand-Crafted 7/3、Algorithm-Generated 13/7，合计 Dev 20、Frozen Test 10。跨 source split 重复的原始问题 ID 不被错误剔除，稳定 ID 中加入 source split。越界或未保留的 step 标签也不用于选择过滤，而是在评价时标为 unscorable。

本次 schema exclusion 为 0。30 条中没有 trace 被 adapter 截断；1 条 Hand-Crafted 的决定性 step 不在其历史范围内，因此 agent 仍可评分，而 step exact、±1、±2、joint、distance 对三个方法都排除分母。受控 manifest 只含版本、种子、选择/划分 hash、原始与稳定 ID、行号、payload hash 和截断元数据，不含 question、history、答案或失败文本。

## 冻结基线与隔离

基线版本固定为 `deterministic-baseline-v1`，来源提交固定为 `da3ef193bdd73474eec5191f1e267ee163c308a7`：

- `last_active_agent`
- `first_explicit_error`
- `rule_based`

verifier 逐字节比较当前 `src/m1_local/attribution.py` 与冻结提交中的同一文件；当前校验通过。新增实现只位于 `attribution_protocol.py` 和 `attribution_benchmark.py`，没有 case 特判、Frozen Test 调参或 Ground Truth 驱动的 Agent alias。预测仍只接收递归 label guard 保护的 `AttributionInput`；真实 who/when/reason 只在预测结束后进入 evaluator。

Agent 比较只做 Unicode NFKC、首尾 trim、连续空白折叠和 casefold。原值与 canonical 值分别保留；例如 `Orchestrator (thought)` 不会被语义映射成 `Orchestrator`。Step 只接受十进制非负整数字符串；Who&When 的 `history`/`mistake_step` 均按 0-based 位置解释，不搜索 `+1/-1` 的最优匹配。无法映射到保留轨迹的位置即 unscorable。

## 指标协议

保留原有 agent/step/joint exact、step distance、coverage、agent/step scorable 和 provenance guard；新增 step within 1/2、selective agent/step accuracy 与 abstain rate。所有聚合均给出 numerator、denominator、value；step distance 另给 mean 和 median。

- 在标签可评分时，abstain 的 overall exact/within/joint 为 0，coverage 为 0，distance 为 `None`。
- selective 指标的分母排除 abstain；overall 指标的分母包含可评分的 abstain。
- step 不可评分时，step exact/within/joint/distance/selective step 均不进分母；agent 指标可独立保留。
- distance 只统计 step 可评分且预测 step 可解析的非 abstain 记录。

## 实测结果

总体 30 cases × 3 methods = 90 runs：

| 范围 | Agent exact | Step exact | Step ±1 | Step ±2 | Coverage | Selective agent | Selective step | Mean / median distance |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Overall | 16/90 | 6/87 | 16/87 | 20/87 | 76/90 | 16/76 | 6/74 | 851/74 = 11.50 / 4 |
| Dev | 9/60 | 2/57 | 12/57 | 16/57 | 51/60 | 9/51 | 2/49 | 619/49 = 12.63 / 4 |
| Frozen Test | 7/30 | 4/30 | 4/30 | 4/30 | 25/30 | 7/25 | 4/25 | 232/25 = 9.28 / 6 |

按方法：

| Method | Agent exact | Step exact | Step ±1 | Step ±2 | Coverage | Selective agent | Selective step | Mean / median distance |
|---|---:|---:|---:|---:|---:|---:|---:|
| last_active_agent | 10/30 | 0/29 | 1/29 | 3/29 | 30/30 | 10/30 | 0/29 | 440/29 = 15.17 / 5 |
| first_explicit_error | 3/30 | 3/29 | 8/29 | 8/29 | 16/30 | 3/16 | 3/16 | 158/16 = 9.88 / 2 |
| rule_based | 3/30 | 3/29 | 7/29 | 9/29 | 30/30 | 3/30 | 3/29 | 253/29 = 8.72 / 4 |

按来源：Algorithm-Generated 为 agent 12/60、step 6/60、±1 16/60、±2 17/60、coverage 51/60；Hand-Crafted 为 agent 4/30、step 0/27、±1 0/27、±2 3/27、coverage 25/30。完整 overall/method/benchmark split/source split/method×benchmark split/method×source split 聚合位于 `docs/results/attribution-benchmark-v1-summary.json`。

Confusion matrix 按 method、benchmark split、source split、ground-truth canonical agent、predicted canonical agent 计数；abstain 使用独立的 `__ABSTAIN__` 桶，不混入任何 Agent。详细 CSV 位于被忽略的 `data/reports/attribution-benchmark-v1/confusion_matrix.csv`，安全聚合副本在上述 tracked JSON 中。

这些结果只描述三种未调参的简单确定性基线，不能证明归因方法有效，也不能与论文中的 LLM 方法结果直接比较。Frozen Test 仅运行并报告冻结基线；后续开发只能用 Dev 调试。

## Phoenix Dataset / Experiment

- 逻辑 Dataset 名：`m1-local-lab-who-when-benchmark-v1`，30 个唯一 examples。最终 canonical manifest 验收时，本地 Phoenix 中该名称已被早期中间 manifest 占用；Phoenix 11 不支持原位声明式替换，因此按冲突规则新建有效名称 `m1-local-lab-who-when-benchmark-v1-c18401f0605f`。干净环境第一次执行仍使用逻辑名称。
- Experiment：`m1-local-lab-attribution-benchmark-v1`，90 task runs。
- 每个 run 写入 13 个 `attribution.*` evaluator，共 1170 evaluations；不适用值仍以 `score=None`、`label=not_applicable` 明示，聚合分母按本协议计算。
- 只使用 Phoenix 11.35 公共 client API；没有读取 SQLite 或抓取 HTML。

Phoenix 可直接查看：Dataset input 中的 case/source/revision/source split、benchmark/protocol、benchmark split、manifest hash、imported-history provenance；Dataset output 中的 evaluator-only expected answer、responsible agent、decisive step、failure reason；Experiment metadata 中的 source/baseline revision、method、样本/run/调用计数；run output 中的 prediction/status/confidence/evidence 和 label-free metadata；evaluation 中的 13 个指标及 method/source split/benchmark split/scorable/truncation/provenance metadata。

仍需后续错误归因阶段处理：失败类型 taxonomy 与质量评估、自然语言 failure reason 的因果充分性、外部历史与本项目 runtime/checkpoint/span 的因果映射、模型/网络/浏览器/安全策略类别归因，以及更强方法的 Dev 开发与独立 Frozen Test 发布。外部历史不会被伪装成新的 OTel trace。

## 复现与产物

```powershell
$env:PYTHONPATH='C:/projects/m1-local-lab/src'
& '.\.venv-phoenix\Scripts\python.exe' '.\scripts\fetch_who_when_benchmark_v1.py' --materialize-only
& '.\.venv\Scripts\python.exe' '.\scripts\verify_attribution_benchmark_v1.py'
```

下载时去掉 `--materialize-only` 并加 `--materialize`。下载器不使用 `trust_remote_code`，不执行第三方代码。详细逐例文件为 `predictions.json`、`evaluations.json`、`summary.json`、`confusion_matrix.csv`、`excluded_cases.json`，统一位于被忽略的 `data/reports/attribution-benchmark-v1/`。

验收实测：最终完整回归 `65 passed, 2 skipped in 26.71s`；应用与 Phoenix 两个 `pip check` 均为 `No broken requirements found.`；旧 `verify_external_dataset_smoke.py` 与 `verify_failure_attribution_mvp.py` 均 PASS；本 verifier PASS。没有运行真实 WebSurfer、外部任务、OpenAI 或其他付费模型。未修改 API/config，因此按约定不重复运行 UI E2E。
