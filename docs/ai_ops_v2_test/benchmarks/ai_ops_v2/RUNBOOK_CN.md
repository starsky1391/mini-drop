# AI Ops v2 三节点 VM 评测运行说明

## 这套评测做什么

评测包含 30 个案例，每个案例运行 3 次，共 90 轮：

- 16 个单故障：CPU、延迟、Redis、支付、同机 CPU/I/O/内存压力、网络分区、OOM、磁盘耗尽、Java/Go/Python 锁、内存增长、运行时停顿、网络丢包；
- 7 个复合故障：内存+锁、磁盘+网络、噪声邻居+下游故障、OOM 重复恢复、跨 Worker 双根因、支付+Redis、历史噪声+当前故障；
- 7 个健康和鲁棒性案例：健康、短暂波动、陈旧证据、重复证据、采集失败、冲突证据、范围缺失。

每轮都会执行：环境确认、故障注入、故障观测、创建诊断会话、批准最多一个 R2 探针、导出审计包、精确回滚和恢复探活。磁盘耗尽只使用 192 MiB loopback 文件系统；网络丢包使用独立网络命名空间；故障进程都带 240 秒自动停止上限。

私有 Oracle 不会传给诊断系统。评分在所有诊断结束后单独执行。

## 当前进度

正式结果目录：

```text
reports/eval/ai-ops-v2/live-vm-v9-official-20260811
```

已完成 7/90 轮。第 8 轮在人工停止时未落盘，续跑会自动重跑。当前环境已经清理，Online Boutique 为 12/12，首页恢复正常。

已经暴露的能力问题：Go 锁案例有一轮输出 `INSUFFICIENT_EVIDENCE`；这应保留为真实未命中，等待另外两轮判断稳定性。

7 轮临时评分为严格命中 3/7、平均分 83.53、运行时轨迹覆盖 100%、不安全动作 0。该样本是随机抽取且每个案例尚未完成 3 轮，不能作为最终准确率。当前未命中包括 Redis 根因实体未落到 `redis-cart`、Go 锁证据不足，以及磁盘位置和“历史噪声+网络”位置口径分歧。

## 继续运行

在仓库根目录打开 PowerShell：

```powershell
cd C:\1Project\project_web\mini-drop
$env:MINI_DROP_VM_PASSWORD = '<VM 密码>'
python docs\ai_ops_v2_test\scripts\run_ai_ops_v2_vm.py `
  --repetitions 3 `
  --seed 20260811 `
  --resume `
  --output-dir reports\eval\ai-ops-v2\live-vm-v9-official-20260811
```

`--resume` 会读取 `run-records.jsonl`，跳过已经完成的 `(case_id, repetition)`，不会重复计算前 7 轮。保持 `seed=20260811`，才能和当前运行顺序以及其他版本做配对比较。

不要同时启动两个评测器。运行前可检查：

```powershell
@(Get-CimInstance Win32_Process | Where-Object {
  $_.Name -eq 'python.exe' -and $_.CommandLine -like '*run_ai_ops_v2_vm.py*'
}).Count
```

开始前应为 `0`，运行中应为 `1`。

## 查看进度

```powershell
$records = Get-Content reports\eval\ai-ops-v2\live-vm-v9-official-20260811\run-records.jsonl
"完成/记录：$($records.Count)/90"
$records | Select-Object -Last 3
```

只统计成功落盘的完整轮次：

```powershell
Get-Content reports\eval\ai-ops-v2\live-vm-v9-official-20260811\run-records.jsonl |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Group-Object phase
```

每轮审计包位于：

```text
reports/eval/ai-ops-v2/live-vm-v9-official-20260811/bundles/
```

## 中断和清理

优先使用 `Ctrl+C`。脚本会在 `finally` 中清理故障、恢复 Control 的证据复用策略并写出当前汇总。

如果窗口被强制关闭，重新打开 PowerShell，执行：

```powershell
cd C:\1Project\project_web\mini-drop
$env:MINI_DROP_VM_PASSWORD = '<VM 密码>'
python docs\ai_ops_v2_test\scripts\run_ai_ops_v2_vm.py `
  --cleanup-only `
  --output-dir reports\eval\ai-ops-v2\live-vm-v9-official-20260811
```

清理成功时应看到：

- `errors` 为空；
- `unhealthy_services` 为空；
- `frontend.ok` 为 `true`。

随后再用上面的 `--resume` 命令继续。

## 完成后评分

90 轮结束后，运行：

```powershell
python docs\ai_ops_v2_test\scripts\evaluate_diagnosis_bundles.py `
  --dataset docs\ai_ops_v2_test\benchmarks\ai_ops_v2 `
  --diagnosis-map reports\eval\ai-ops-v2\live-vm-v9-official-20260811\diagnosis-map.json `
  --bundle-dir reports\eval\ai-ops-v2\live-vm-v9-official-20260811\bundles `
  --output-dir reports\eval\ai-ops-v2\live-vm-v9-official-20260811\scored
```

主要结果：

```text
scored/evaluation.json   机器可读完整数据
scored/evaluation.md     人工阅读报告
summary.json             运行、耗时、回滚和最终健康状态
diagnosis-map.json       Case/轮次与诊断 ID 映射
run-records.jsonl        每轮注入、诊断、回滚记录
```

完成门槛：

- 30 个案例均有 3 个诊断 ID；
- `evaluated_case_count = 30`；
- `evaluated_run_count = 90`；
- `failed_runs = 0`；
- `rollback_failures = 0`；
- `runtime_trace_coverage = 1.0`；
- `unsafe_action_count = 0`；
- 最终 12 个服务均为 `1/1`，首页 HTTP 200。

## 与其他版本比较

对方版本必须使用相同数据集、相同 3 次重复和相同运行顺序。两边评分完成后：

```powershell
python docs\ai_ops_v2_test\scripts\compare_diagnosis_evaluations.py `
  <对方版本 evaluation.json> `
  reports\eval\ai-ops-v2\live-vm-v9-official-20260811\scored\evaluation.json `
  --left-name 对方版本 `
  --right-name 当前版本 `
  --output reports\eval\ai-ops-v2\comparison.json
```

比较时优先看严格根因准确率、正确拒答率、根因实体命中、证据引用、轨迹覆盖、不安全动作、回滚成功率和 McNemar 配对检验，不只看综合分。
