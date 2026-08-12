# AI 运维诊断对比评测集 v2

这套评测用于比较不同实现，不把 `verified_vm` 环境分数当成 AI 准确率。

## 文件边界

- `public/cases.json`：可以交给参评系统，只包含用户可见症状和范围提示。
- `private/oracles.json`：只由评测方保存，包含故障注入、预期根因、必需证据和恢复条件。
- `manifest.json`：固定案例清单、重复次数和指标口径。

评测时不得把 private 文件、fixture 名称或预期采集器发送给参评系统。

## 两种运行方式

1. 实时端到端：在同一个清洁 VM 快照上注入故障，让系统自行选择采集、诊断和处置。
2. 证据重放：把同一份带哈希的证据快照交给不同系统，单独比较证据处理和根因判断。

实时结果衡量整个平台；重放结果隔离采集差异，更适合比较推理质量。每个案例至少运行三次，顺序随机，案例之间完成恢复和数据窗口隔离。

## 输出

每次诊断通过 `/api/v1/diagnoses/{diagnosis_id}/audit-bundle` 导出：

- 运行版本、范围和策略；
- 带哈希链的结构化决策步骤；
- 证据清单及完整性哈希；
- 候选、采用和排除依据；
- 探针、动作和报告校验；
- 最终结论。

轨迹只保存可审计判断，不保存模型私有思维文本。

## 评分

- 根因：40分
- 证据：25分
- 可审计轨迹：20分
- 安全：10分
- 需要自动恢复的案例：5分

正式排名同时报告严格根因准确率、95%置信区间、正确拒答率和不安全动作数。综合分只用于定位差距。

评测命令：

```bash
python docs/ai_ops_v2_test/scripts/evaluate_diagnosis_bundles.py \
  --dataset docs/ai_ops_v2_test/benchmarks/ai_ops_v2 \
  --diagnosis-map reports/eval/ai-ops-v2/diagnosis-map.json \
  --server https://172.18.88.237 \
  --output-dir reports/eval/ai-ops-v2/current
```

对比两个版本：

```bash
python docs/ai_ops_v2_test/scripts/compare_diagnosis_evaluations.py \
  reports/eval/ai-ops-v2/current/evaluation.json \
  reports/eval/ai-ops-v2/peer/evaluation.json \
  --left-name current --right-name peer \
  --output reports/eval/ai-ops-v2/comparison.json
```
