# AI Ops v2 测试集完整运行手册

本文是一套从环境准备到正式 90 轮评分、对比报告的完整命令流。默认在本机 Windows PowerShell 中执行，仓库路径为：

```powershell
cd C:\1Project\project_web\mini-drop
```

## 一、需要的环境

必须具备：

```text
1. Windows 本机
   - Python 3.9+
   - 能 SSH 到三台 VM
   - 仓库在 C:\1Project\project_web\mini-drop

2. 三节点 VM
   - control: 172.18.88.237 / user: control
   - worker1: 172.18.90.144 / user: worker1 / agent_id: linux-worker-1
   - worker2: 172.18.87.120 / user: worker2 / agent_id: linux-worker-2

3. control 节点
   - mini-drop-server 正常运行
   - 本机 MINI_DROP_API_KEY 已设置，或 control 节点 env 文件存在 MINI_DROP_API_KEY
   - /api/healthz 可访问

4. worker 节点
   - mini-drop-agent 正常运行
   - agent 已注册到 control
   - Docker / perf / bpftrace 等基础采集能力可用
   - 工业采集器 sidecar 按需要可用：
     - Fluent Bit
     - Blackbox Exporter
     - Redis Exporter

5. Online Boutique
   - 12 个服务全部健康
   - frontend 首页 HTTP 200
```

安装本机 Python 依赖：

```powershell
python -m pip install paramiko
```

## 二、先做本地代码回归

```powershell
cd C:\1Project\project_web\mini-drop

python -m pytest -q
```

预期：

```text
全部测试通过
```

如果这里不过，先不要跑 VM 测试集。后面的 VM 故障注入会把问题放大，排查成本会更高。

### 冻结证据包 Watch 流程

如果测试输入来自已经结束的 Persistent Watch 异常窗口，使用
`POST /api/v1/watch-incidents/{incident_id}/analyze` 分析保存的
`structured_evidence`。该入口属于 `frozen_evidence` 模式：

- 复用原 `evidence_cohort_id`、`rolling_snapshot` 和 `same_window`；
- 允许 AI 树报告包内已存在的证据，缺少的 evidence family 输出
  `missing_evidence` 和 `evidence_package_exhausted`；
- 不创建新的实时 collector、approval item 或 delayed follow-up；
- 通过 `GET /api/v1/diagnoses/{analysis_id}/audit-bundle` 导出审计包；
- 普通诊断请求仍默认 `live_collection`，不支持 `hybrid`。

冻结分析的正确验收重点是证据复用、缺口边界和 `probe_count=0`，不能用
“是否创建了实时采集任务”作为冻结分析成功条件。

## 三、确认测试集路径

当前测试集已经整理为脚本友好的英文目录，不需要再做中文目录到英文目录的复制。

关键路径：

```text
docs\ai_ops_v2_test\scripts\run_ai_ops_v2_vm.py
docs\ai_ops_v2_test\scripts\check_readiness_gate.py
docs\ai_ops_v2_test\scripts\evaluate_diagnosis_bundles.py
docs\ai_ops_v2_test\scripts\compare_diagnosis_evaluations.py

docs\ai_ops_v2_test\benchmarks\ai_ops_v2
docs\ai_ops_v2_test\bundles
docs\ai_ops_v2_test\evaluation_results
docs\ai_ops_v2_test\run_records
```

检查测试集核心文件：

```powershell
Test-Path docs\ai_ops_v2_test\benchmarks\ai_ops_v2\public\cases.json
Test-Path docs\ai_ops_v2_test\benchmarks\ai_ops_v2\private\oracles.json
Test-Path docs\ai_ops_v2_test\benchmarks\ai_ops_v2\vm_faultctl.sh
```

三个都应该输出：

```text
True
True
True
```

## 四、配置 VM 密码

评测脚本通过 SSH 登录三台 VM，需要设置：

```powershell
$env:MINI_DROP_VM_PASSWORD = '<你的 VM 密码>'
```

注意：

```text
不要把密码写进仓库文件。
不要提交任何真实密钥、Token 或密码。
```

检查本机是否能连 VM：

```powershell
ssh control@172.18.88.237 "hostname"
ssh worker1@172.18.90.144 "hostname"
ssh worker2@172.18.87.120 "hostname"
```

## 五、检查 control 服务

```powershell
ssh control@172.18.88.237 "curl -kfsS https://127.0.0.1/api/healthz && echo OK"
```

预期有：

```text
OK
```

检查 API Key 是否存在。只输出 `FOUND` / `MISSING`，不要打印 key 内容：

```powershell
ssh control@172.18.88.237 'bash -lc "for f in /home/control/mini-drop-active/deploy/env/control-native.env /home/control/mini-drop-active/deploy/env/control.env /home/control/mini-drop/deploy/env/control.env; do [ -f \"$f\" ] && grep -q \"^MINI_DROP_API_KEY=.\" \"$f\" && echo FOUND && exit 0; done; echo MISSING"'
```

如果 control 节点没有这些 env 文件，也可以在本机 PowerShell 直接设置：

```powershell
$env:MINI_DROP_API_KEY = '<你的 Mini-Drop API Key>'
```

runner 的读取优先级是：

```text
1. 本机环境变量 MINI_DROP_API_KEY
2. /home/control/mini-drop-active/deploy/env/control-native.env
3. /home/control/mini-drop-active/deploy/env/control.env
4. /home/control/mini-drop/deploy/env/control.env
```

## 六、检查 Agent 和服务状态

在 control 节点查看 Swarm 服务：

```powershell
ssh control@172.18.88.237 "docker service ls"
```

如果 Online Boutique 是 Swarm 服务，确认服务副本都是健康状态，类似：

```text
1/1
```

检查 Mini-Drop Server：

```powershell
ssh control@172.18.88.237 "systemctl status mini-drop-server --no-pager"
```

检查 Worker Agent：

```powershell
ssh worker1@172.18.90.144 "systemctl status mini-drop-agent --no-pager"
ssh worker2@172.18.87.120 "systemctl status mini-drop-agent --no-pager"
```

## 七、先跑 readiness gate 旧包回放

这一步是验证门禁，不是测当前准确率。

```powershell
cd C:\1Project\project_web\mini-drop

$ts = Get-Date -Format "yyyyMMdd-HHmmss"

python docs\ai_ops_v2_test\scripts\check_readiness_gate.py `
  --bundle-dir docs\ai_ops_v2_test\bundles `
  --output docs\ai_ops_v2_test\readiness_gate_replay_$ts.json
```

旧包大量失败是合理的，因为它缺新方案要求的字段：

```text
runtime_trace
child_task_ids
artifacts
structured_evidence
evidence_refs
collector_invocation
target_config
```

这一步的目的不是提分，而是确认 readiness gate 能拦住“证据链不完整”的审计包。

## 八、小批量真实 VM 冒烟测试

先不要直接跑 90 轮。建议先跑 6 个关键 case，每个 1 次：

```powershell
cd C:\1Project\project_web\mini-drop

$env:MINI_DROP_VM_PASSWORD = 'admin'
$runName = "live-vm-smoke-" + (Get-Date -Format "yyyyMMdd-HHmmss")
$out = "reports\eval\ai-ops-v2\$runName"

python docs\ai_ops_v2_test\scripts\run_ai_ops_v2_vm.py `
  --cases OB-SINGLE-REDIS-001,OB-SINGLE-PAYMENT-001,OB-COMPOUND-PAYMENT-REDIS-001,OB-COMPOUND-NOISY-DOWNSTREAM-001,OB-SINGLE-GO-LOCK-001,OB-SINGLE-RUNTIME-STALL-001 `
  --repetitions 1 `
  --seed 20260811 `
  --output-dir $out
```

这一步重点验证：

```text
1. 诊断能不能真实创建
2. collector_tasks 能不能下发
3. redis_check / dependency_check / log_scan 是否产生结构化 artifact
4. collector_invocation 是否带 target_config
5. audit-bundle 是否能导出
6. 故障能否自动回滚
```

## 九、查看小批量运行结果

查看 summary：

```powershell
Get-Content "$out\summary.json"
```

查看最近几条运行记录：

```powershell
Get-Content "$out\run-records.jsonl" | Select-Object -Last 6
```

检查 bundle 是否生成：

```powershell
Get-ChildItem "$out\bundles" | Select-Object Name,Length,LastWriteTime
```

## 十、小批量 readiness gate

```powershell
python docs\ai_ops_v2_test\scripts\check_readiness_gate.py `
  --bundle-dir "$out\bundles" `
  --output "$out\readiness_gate.json"
```

查看概要：

```powershell
Get-Content "$out\readiness_gate.json"
```

理想情况：

```text
新生成的 bundle 应该尽量 PASS
```

如果失败，优先看失败项，不要急着看准确率。

## 十一、小批量评分

```powershell
python docs\ai_ops_v2_test\scripts\evaluate_diagnosis_bundles.py `
  --dataset docs\ai_ops_v2_test\benchmarks\ai_ops_v2 `
  --diagnosis-map "$out\diagnosis-map.json" `
  --bundle-dir "$out\bundles" `
  --output-dir "$out\scored"
```

查看 Markdown 报告：

```powershell
Get-Content "$out\scored\evaluation.md"
```

查看机器可读结果：

```powershell
Get-Content "$out\scored\evaluation.json"
```

## 十二、如果中断，先清理

如果 VM 测试中断，不要直接重跑，先清理故障：

```powershell
python docs\ai_ops_v2_test\scripts\run_ai_ops_v2_vm.py `
  --cleanup-only `
  --output-dir $out
```

然后续跑：

```powershell
python docs\ai_ops_v2_test\scripts\run_ai_ops_v2_vm.py `
  --cases OB-SINGLE-REDIS-001,OB-SINGLE-PAYMENT-001,OB-COMPOUND-PAYMENT-REDIS-001,OB-COMPOUND-NOISY-DOWNSTREAM-001,OB-SINGLE-GO-LOCK-001,OB-SINGLE-RUNTIME-STALL-001 `
  --repetitions 1 `
  --seed 20260811 `
  --resume `
  --output-dir $out
```

## 十三、小批量通过后，跑正式 90 轮

正式跑：

```powershell
cd C:\1Project\project_web\mini-drop

$env:MINI_DROP_VM_PASSWORD = '<你的 VM 密码>'
$runName = "live-vm-current-" + (Get-Date -Format "yyyyMMdd-HHmmss")
$fullOut = "reports\eval\ai-ops-v2\$runName"

python docs\ai_ops_v2_test\scripts\run_ai_ops_v2_vm.py `
  --repetitions 3 `
  --seed 20260811 `
  --output-dir $fullOut
```

中断续跑：

```powershell
python docs\ai_ops_v2_test\scripts\run_ai_ops_v2_vm.py `
  --repetitions 3 `
  --seed 20260811 `
  --resume `
  --output-dir $fullOut
```

查看进度：

```powershell
$records = Get-Content "$fullOut\run-records.jsonl"
"完成记录数：$($records.Count) / 90"
$records | Select-Object -Last 5
```

## 十四、正式 90 轮 readiness gate

```powershell
python docs\ai_ops_v2_test\scripts\check_readiness_gate.py `
  --bundle-dir "$fullOut\bundles" `
  --output "$fullOut\readiness_gate.json"
```

## 十五、正式 90 轮评分

```powershell
python docs\ai_ops_v2_test\scripts\evaluate_diagnosis_bundles.py `
  --dataset docs\ai_ops_v2_test\benchmarks\ai_ops_v2 `
  --diagnosis-map "$fullOut\diagnosis-map.json" `
  --bundle-dir "$fullOut\bundles" `
  --output-dir "$fullOut\scored"
```

输出文件：

```text
$fullOut\scored\evaluation.json
$fullOut\scored\evaluation.md
$fullOut\summary.json
$fullOut\diagnosis-map.json
$fullOut\run-records.jsonl
$fullOut\readiness_gate.json
$fullOut\bundles\
```

## 十六、和旧结果对比

旧结果在：

```text
docs\ai_ops_v2_test\evaluation_results\evaluation.json
```

对比命令：

```powershell
python docs\ai_ops_v2_test\scripts\compare_diagnosis_evaluations.py `
  docs\ai_ops_v2_test\evaluation_results\evaluation.json `
  "$fullOut\scored\evaluation.json" `
  --left-name old-baseline `
  --right-name current `
  --output "$fullOut\comparison_vs_old.json"
```

查看：

```powershell
Get-Content "$fullOut\comparison_vs_old.json"
```

## 十七、这次重点看什么

不要只看综合分。优先看：

```text
1. readiness_gate 是否通过
2. runtime_trace_coverage 是否 100%
3. unsafe_action_count 是否 0
4. evidence_citation_validity 是否接近 100%
5. REDIS / PAYMENT / DOWNSTREAM case 是否明显提升
6. root_entity 是否从 0/2 改善
7. repeat_output_consistency 是否高于旧结果 36.7%
8. delayed_followup 是否没有错误反证 same-window snapshot
```

## 十八、如果 Docker 拉镜像超时

如果遇到：

```text
failed to resolve source metadata for docker.io/library/nginx:1.27-alpine
failed to resolve source metadata for docker.io/library/python:3.11-slim
```

这是 Docker Hub 网络问题，不是代码问题。

可以先在对应机器上单独重试：

```bash
docker pull nginx:1.27-alpine
docker pull python:3.11-slim
docker pull node:20-alpine
```

如果仍然超时，需要：

```text
1. 配 Docker 镜像加速
2. 或使用离线镜像包
3. 或在网络正常环境提前 docker save / docker load
```

正式 VM 评测前，control / worker 服务必须已经起来，否则测试集脚本会失败在环境确认阶段。

## 十九、最建议的实际执行顺序

```text
1. python -m pytest -q
2. 确认 docs\ai_ops_v2_test\benchmarks\ai_ops_v2 已存在
3. 设置 MINI_DROP_VM_PASSWORD
4. 检查三台 VM SSH、control healthz、agent 状态
5. 跑 6 case × 1 repetition 冒烟
6. 对冒烟 bundle 跑 readiness gate
7. 对冒烟 bundle 评分
8. 确认采集器结构化证据进入报告
9. 再跑 30 case × 3 repetition 正式评测
10. 跑正式评分和旧结果对比
```

这套顺序比较稳，可以避免一上来跑 90 轮后才发现“采集器没接进去”。
