# Quickstart: Evidence-to-Attribution Analyzer

这份教程用于验证新版 Analyzer 是否能正常工作，并且可以和旧版假设验证链路做对比。

## 1. 目标

你将完成下面 3 件事：

1. 启动项目并打开任务结果页。
2. 分别用旧链路和新链路做一次诊断。
3. 确认新版链路会生成事实、现象、定位边界和证据反问结果，而旧链路不会。

## 2. 前置条件

- 本地已经能启动后端和前端。
- 项目中至少有一个已经采集完成的任务。
- 该任务最好包含 `top_json`、`sys_metrics` 或 `ebpf_metrics` 其中一种证据。

## 3. 启动服务

在仓库根目录分别启动后端和前端。

后端：

```powershell
python dev.py
```

前端：

```powershell
cd web
npm run dev
```

如果你本地不是用 `dev.py` 启动，也可以按项目现有方式启动 FastAPI 服务，只要任务列表和任务结果页可访问即可。

## 4. 打开任务

1. 打开前端页面。
2. 进入任务列表或任务详情页。
3. 选择一个已经完成采集的任务。
4. 进入该任务的结果页。

你应该能在“智能归因”卡片里看到两个下拉框：

- `归因策略`
- `分析链路`

## 5. 先测旧链路

### 页面操作

1. 将 `归因策略` 保持为 `线性候选`。
2. 将 `分析链路` 切换为 `旧版：假设验证`。
3. 点击 `运行诊断`。

### 你应该看到

- 页面提示 `旧版假设验证诊断完成`。
- 诊断结果里主要是候选原因、置信度和原始证据引用。
- `analysis_result` 不会参与约束。
- 即使证据不够完整，旧链路也更容易给出一个候选结论。

### 适合观察的现象

- 是否仍然能看到原有候选归因输出。
- 是否没有“事实 / 现象 / 定位层级 / 证据反问”这些结构化边界。

## 6. 再测新链路

### 页面操作

1. 将 `归因策略` 保持为 `线性候选`，或者改成 `因果图（方案 B）`。
2. 将 `分析链路` 切换为 `新版：证据归因`。
3. 点击 `运行诊断`。

### 你应该看到

- 页面提示 `Evidence-to-Attribution 诊断完成`。
- 诊断结果会先经过证据处理，再进入 LLM 报告。
- 诊断快照中会带有 `analysis_result`。
- 报告中的原因必须来自 `allowed_cause_ids`。
- 如果证据不足，报告会标记为 `not_enough_evidence`。

### 你应该重点检查的内容

在诊断详情里确认这些字段：

- `analysis_strategy`
- `analysis_pipeline`
- `summary`
- `ranked_causes`
- `not_enough_evidence`

如果你从接口返回里看到：

```json
{
  "analysis_pipeline": "evidence_to_attribution"
}
```

说明新链路已经生效。

## 7. 用接口直接验证

如果你想绕过页面，直接测试接口，可以用诊断接口。

### 7.1 测试新版链路

```http
POST /api/tasks/{task_id}/diagnose?analysis_strategy=linear&analysis_pipeline=evidence_to_attribution
```

### 7.2 测试旧版链路

```http
POST /api/tasks/{task_id}/diagnose?analysis_strategy=linear&analysis_pipeline=legacy
```

### 7.3 方案 B

```http
POST /api/tasks/{task_id}/diagnose?analysis_strategy=graph&analysis_pipeline=evidence_to_attribution
```

### 7.4 查看返回值

返回结果里重点看这几个字段：

- `analysis_strategy`
- `analysis_pipeline`
- `validated`
- `summary`
- `ranked_causes`
- `facts`
- `not_enough_evidence`

## 8. 推荐验证样例

### 样例 A：CPU 热点

适用条件：

- 任务里有 `top_json`
- `top_json` 里存在明显热点函数
- `sys_metrics` 里 CPU 用户态偏高

预期：

- 新链路会识别出事实和现象。
- 结果里应该能看到函数级定位边界。
- 证据反问会说明缺少热点证据时结论会降级。

### 样例 B：IO 等待

适用条件：

- 任务里有 `ebpf_metrics`
- `ebpf_metrics` 里存在 IO 延迟分布

预期：

- 新链路会把 IO 延迟当作关键证据。
- 如果 IO wait 事实不足，结论会保守。

### 样例 C：证据不足

适用条件：

- 任务只有很少的采样数据
- 没有明显热点或等待证据

预期：

- 新链路不会强行输出明确根因。
- `not_enough_evidence` 应该为 `true`。

## 9. 前端验证点

在任务结果页确认：

- `归因策略` 可切换 `linear` / `graph`
- `分析链路` 可切换 `新版：证据归因` / `旧版：假设验证`
- 点击 `运行诊断` 后，返回结果会随选择变化

如果你选择旧版链路，再运行一次，就能直观看到：

- 旧链路偏“候选原因验证”
- 新链路偏“事实 -> 现象 -> 定位 -> 归因边界”

## 10. 常见问题

### 看不到分析链路下拉框

确认你打开的是任务结果页，而不是历史诊断列表页。

### 运行诊断后没有结果

先确认任务本身已经完成采集，并且至少有一种结构化证据产物。

### 新版链路总是证据不足

先检查任务是否真的有 `top_json`、`sys_metrics` 或 `ebpf_metrics`。
如果数据太少，Analyzer 会故意保守，这属于正常行为。

### 我只想验证旧链路

把 `analysis_pipeline` 选成 `legacy` 即可。

## 11. 回归建议

每次改动 Analyzer 之后，至少跑一次：

```powershell
python -m pytest tests/test_rca.py tests/test_rca_enhanced.py tests/test_rca_strategies.py tests/test_rca_attribution.py tests/test_server_api.py -q
```

如果你改了前端，再额外确认任务结果页可以正常打开，并且两个下拉框都能提交成功。
