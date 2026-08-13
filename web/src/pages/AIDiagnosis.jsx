import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Modal,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Timeline,
  Typography,
  message,
} from "antd";
import {
  CheckOutlined,
  CloseOutlined,
  ExperimentOutlined,
  MinusCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import {
  approveDiagnosisProbe,
  approveWaitingDiagnosisProbes,
  createDiagnosisSession,
  getDiagnosisSession,
  listAgents,
  listDiagnosisSessions,
  runAIValidation,
} from "../api/client";

const TERMINAL = new Set([
  "COMPLETED",
  "INSUFFICIENT_EVIDENCE",
  "PARTIAL_COMPLETED",
  "BUDGET_EXHAUSTED",
  "TOPOLOGY_UNAVAILABLE",
  "USER_CANCELED",
  "FAILED",
]);

const STATUS_COLORS = {
  COMPLETED: "green",
  PARTIAL_COMPLETED: "orange",
  INSUFFICIENT_EVIDENCE: "gold",
  FAILED: "red",
  BUDGET_EXHAUSTED: "red",
  WAITING_APPROVAL: "purple",
  COLLECTING: "blue",
  ANALYZING: "cyan",
  NEEDS_SCOPE_CONFIRMATION: "orange",
};

function Status({ value }) {
  return <Tag color={STATUS_COLORS[value] || "default"}>{value || "UNKNOWN"}</Tag>;
}

export default function AIDiagnosis() {
  const [form] = Form.useForm();
  const [agents, setAgents] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(false);
  const [validationRunning, setValidationRunning] = useState(false);
  const [error, setError] = useState("");
  const watchedInstances = Form.useWatch("instances", form) || [];

  async function refreshSessions() {
    try {
      setSessions(await listDiagnosisSessions({ limit: 50 }));
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    Promise.all([listAgents(), listDiagnosisSessions({ limit: 50 })])
      .then(([agentItems, sessionItems]) => {
        setAgents(agentItems);
        setSessions(sessionItems);
        const first = agentItems.find((item) => item.status === "ONLINE") || agentItems[0];
        if (first) {
          const instances = form.getFieldValue("instances") || [{}];
          if (!instances[0]?.agent_id) {
            form.setFieldsValue({
              instances: [{
                ...instances[0],
                agent_id: first.id,
                host_id: first.hostname || first.id,
              }, ...instances.slice(1)],
            });
          }
        }
      })
      .catch((err) => setError(err.message));
  }, [form]);

  useEffect(() => {
    if (!selected?.diagnosis_id || TERMINAL.has(selected.status)) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const detail = await getDiagnosisSession(selected.diagnosis_id);
        setSelected(detail);
        refreshSessions();
      } catch (err) {
        setError(err.message);
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [selected?.diagnosis_id, selected?.status]);

  async function submit(values) {
    setLoading(true);
    setError("");
    try {
      const instances = values.instances.map((item, index) => ({
        service_id: item.service_id,
        instance_id: item.instance_id || `${item.service_id}-${index + 1}`,
        host_id: item.host_id,
        agent_id: item.agent_id,
        pid: item.pid,
        environment: item.environment || values.environment,
      }));
      const detail = await createDiagnosisSession({
        query: values.query,
        context: {
          service_id: values.target_service,
          environment: values.environment,
          instances,
          dependencies: values.dependencies || [],
        },
        budget_profile: values.budget_profile,
        auto_execute_policy: values.auto_execute_policy,
      });
      setSelected(detail);
      await refreshSessions();
      message.success(values.auto_execute_policy === "all_registered"
        ? "诊断会话已创建；开发模式会自动批准 AI 树补证探针"
        : "诊断会话已创建；系统将先复用已有证据并运行低风险探针");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function openSession(id) {
    setLoading(true);
    setError("");
    try {
      setSelected(await getDiagnosisSession(id));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function decideProbe(stepId, decision) {
    if (!selected) return;
    setLoading(true);
    try {
      const detail = await approveDiagnosisProbe(selected.diagnosis_id, {
        step_id: stepId,
        decision,
        scope: "single_execution",
        approver_id: "demo_user",
      });
      setSelected(detail);
      message.success(decision === "approve" ? "已批准本次探针" : "已拒绝本次探针");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function approveWaitingProbes() {
    if (!selected) return;
    setLoading(true);
    try {
      const detail = await approveWaitingDiagnosisProbes(selected.diagnosis_id, {
        decision: "approve",
        scope: "all_waiting",
        approver_id: "demo_user",
      });
      setSelected(detail);
      message.success("已批准当前所有待采集探针");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function executeAIValidation() {
    setValidationRunning(true);
    try {
      const result = await runAIValidation();
      const columns = [
        { title: "层级", dataIndex: "layer", width: 130 },
        { title: "验证项", dataIndex: "name", width: 210 },
        {
          title: "结果",
          dataIndex: "status",
          width: 90,
          render: (value) => <Tag color={value === "PASS" ? "green" : "red"}>{value === "PASS" ? "通过" : "失败"}</Tag>,
        },
        { title: "耗时", dataIndex: "duration_ms", width: 100, render: (value) => `${value} ms` },
        { title: "说明", dataIndex: "detail" },
      ];
      const open = result.status === "PASSED" ? Modal.success : Modal.warning;
      open({
        title: `Drop AI 服务检测：${result.passed_count}/${result.total_count} 通过`,
        width: 1000,
        content: (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Alert
              type={result.status === "PASSED" ? "success" : "warning"}
              showIcon
              message={`${result.provider} / ${result.model} · 总耗时 ${result.duration_ms} ms`}
              description="结果不包含 AI Key、余额金额或原始思维链。"
            />
            <Table
              rowKey="check_id"
              columns={columns}
              dataSource={result.checks || []}
              pagination={false}
              size="small"
              scroll={{ x: 850 }}
            />
          </Space>
        ),
      });
    } catch (err) {
      setError(err.message);
    } finally {
      setValidationRunning(false);
    }
  }

  function requestAIValidation() {
    Modal.confirm({
      title: "运行 Drop AI 服务检测？",
      content: "将真实调用 Provider、NLP、集群意图、总结和 RCA，产生少量 Token 费用。",
      okText: "开始检测",
      cancelText: "取消",
      onOk: executeAIValidation,
    });
  }

  const agentOptions = agents.map((agent) => ({
    value: agent.id,
    label: `${agent.hostname || agent.id} · ${agent.status}`,
    disabled: agent.status !== "ONLINE",
  }));
  const serviceOptions = [...new Set(
    watchedInstances.map((item) => item?.service_id?.trim()).filter(Boolean),
  )].map((value) => ({ value, label: value }));

  function selectAgent(instanceIndex, agentId) {
    const agent = agents.find((item) => item.id === agentId);
    if (agent) {
      form.setFieldValue(["instances", instanceIndex, "host_id"], agent.hostname || agent.id);
    }
  }

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Space>
        <RobotOutlined style={{ fontSize: 22, color: "#722ed1" }} />
        <Typography.Title level={4} style={{ margin: 0 }}>AI 集群诊断</Typography.Title>
        <Tag color="purple">证据驱动</Tag>
        <Button
          size="small"
          icon={<ExperimentOutlined />}
          loading={validationRunning}
          onClick={requestAIValidation}
        >
          AI 服务检测
        </Button>
      </Space>

      <Alert
        type="info"
        showIcon
        message="诊断智能体只可选择已注册探针；R2 深度采样必须逐次审批，R3 变更仅生成建议。"
      />
      {error && <Alert type="error" showIcon closable message={error} onClose={() => setError("")} />}

      <Row gutter={[16, 16]}>
        <Col xs={24} xl={14}>
          <Card title="发起诊断" extra={<SafetyCertificateOutlined style={{ color: "#52c41a" }} />}>
            <Form
              form={form}
              layout="vertical"
              initialValues={{
                environment: "production",
                budget_profile: "production_safe",
                auto_execute_policy: "safe_only",
                target_service: "service-a",
                instances: [{
                  service_id: "service-a",
                  instance_id: "service-a-1",
                  environment: "production",
                }],
                dependencies: [],
              }}
              onFinish={submit}
            >
              <Form.Item name="query" label="问题描述" rules={[{ required: true, min: 3 }]}>
                <Input.TextArea rows={3} maxLength={2000} showCount placeholder="例如：service-a 从十点开始变慢，检查自身、同机服务和一跳下游" />
              </Form.Item>
              <Row gutter={12}>
                <Col xs={24} md={12}>
                  <Form.Item name="target_service" label="诊断入口服务" rules={[{ required: true }]}>
                    <Select showSearch options={serviceOptions} placeholder="先在下方添加服务实例" />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item name="environment" label="默认环境">
                    <Select options={["production", "staging", "development"].map((value) => ({ value }))} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item name="budget_profile" label="预算策略">
                    <Select options={[
                      { value: "production_safe", label: "生产安全" },
                      { value: "staging", label: "预发布" },
                      { value: "development", label: "开发" },
                    ]} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item name="auto_execute_policy" label="执行策略">
                    <Select options={[
                      { value: "safe_only", label: "仅自动执行低风险" },
                      { value: "all_registered", label: "自动批准 AI 树补证（开发）" },
                      { value: "manual", label: "全部人工审批" },
                    ]} />
                  </Form.Item>
                </Col>
              </Row>

              <Typography.Title level={5}>服务实例 / Worker</Typography.Title>
              <Form.List name="instances">
                {(fields, { add, remove }) => (
                  <Space direction="vertical" style={{ width: "100%" }}>
                    {fields.map((field, index) => (
                      <Card
                        key={field.key}
                        size="small"
                        title={`实例 ${index + 1}`}
                        extra={fields.length > 1 ? (
                          <Button danger type="text" icon={<MinusCircleOutlined />} onClick={() => remove(field.name)}>
                            删除
                          </Button>
                        ) : null}
                      >
                        <Row gutter={12}>
                          <Col xs={24} md={8}>
                            <Form.Item name={[field.name, "service_id"]} label="服务 ID" rules={[{ required: true }]}>
                              <Input placeholder="service-a" />
                            </Form.Item>
                          </Col>
                          <Col xs={24} md={8}>
                            <Form.Item name={[field.name, "instance_id"]} label="实例 ID" rules={[{ required: true }]}>
                              <Input placeholder="service-a-1" />
                            </Form.Item>
                          </Col>
                          <Col xs={24} md={8}>
                            <Form.Item name={[field.name, "agent_id"]} label="目标 Agent" rules={[{ required: true }]}>
                              <Select
                                options={agentOptions}
                                placeholder="选择在线 Agent"
                                onChange={(value) => selectAgent(field.name, value)}
                              />
                            </Form.Item>
                          </Col>
                          <Col xs={24} md={8}>
                            <Form.Item name={[field.name, "host_id"]} label="宿主机 ID" rules={[{ required: true }]}>
                              <Input placeholder="worker-1" />
                            </Form.Item>
                          </Col>
                          <Col xs={24} md={8}>
                            <Form.Item name={[field.name, "pid"]} label="目标 PID" rules={[{ required: true }]}>
                              <InputNumber min={1} max={4194304} style={{ width: "100%" }} />
                            </Form.Item>
                          </Col>
                          <Col xs={24} md={8}>
                            <Form.Item name={[field.name, "environment"]} label="实例环境">
                              <Select options={["production", "staging", "development"].map((value) => ({ value }))} />
                            </Form.Item>
                          </Col>
                        </Row>
                      </Card>
                    ))}
                    <Button
                      block
                      type="dashed"
                      icon={<PlusOutlined />}
                      onClick={() => add({ environment: form.getFieldValue("environment") })}
                    >
                      添加 Worker 实例
                    </Button>
                  </Space>
                )}
              </Form.List>

              <Typography.Title level={5} style={{ marginTop: 20 }}>服务依赖关系</Typography.Title>
              <Form.List name="dependencies">
                {(fields, { add, remove }) => (
                  <Space direction="vertical" style={{ width: "100%", marginBottom: 20 }}>
                    {fields.map((field, index) => (
                      <Row key={field.key} gutter={8} align="middle">
                        <Col xs={24} md={6}>
                          <Form.Item name={[field.name, "source_service"]} label={index === 0 ? "上游服务" : ""} rules={[{ required: true }]}>
                            <Select options={serviceOptions} placeholder="source" />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={6}>
                          <Form.Item name={[field.name, "target_service"]} label={index === 0 ? "下游服务" : ""} rules={[{ required: true }]}>
                            <Select options={serviceOptions} placeholder="target" />
                          </Form.Item>
                        </Col>
                        <Col xs={18} md={8}>
                          <Form.Item name={[field.name, "relation"]} label={index === 0 ? "关系" : ""} rules={[{ required: true }]}>
                            <Select options={[
                              "CALLS", "READS_FROM", "WRITES_TO", "PUBLISHES_TO", "CONSUMES_FROM", "SHARES_DEPENDENCY",
                            ].map((value) => ({ value }))} />
                          </Form.Item>
                        </Col>
                        <Col xs={6} md={4}>
                          <Button danger type="text" icon={<MinusCircleOutlined />} onClick={() => remove(field.name)}>删除</Button>
                        </Col>
                      </Row>
                    ))}
                    <Button
                      type="dashed"
                      icon={<PlusOutlined />}
                      disabled={serviceOptions.length < 2}
                      onClick={() => add({ relation: "CALLS", confidence: "high", source: "request_context" })}
                    >
                      添加依赖边
                    </Button>
                  </Space>
                )}
              </Form.List>
              <Button type="primary" htmlType="submit" loading={loading} icon={<RobotOutlined />}>
                创建诊断会话
              </Button>
            </Form>
          </Card>
        </Col>

        <Col xs={24} xl={10}>
          <Card
            title="最近会话"
            extra={<Button size="small" icon={<ReloadOutlined />} onClick={refreshSessions}>刷新</Button>}
            bodyStyle={{ maxHeight: 470, overflow: "auto" }}
          >
            <List
              dataSource={sessions}
              locale={{ emptyText: "暂无 AI 诊断会话" }}
              renderItem={(item) => (
                <List.Item actions={[<Button key="open" type="link" onClick={() => openSession(item.diagnosis_id)}>查看</Button>]}>
                  <List.Item.Meta
                    title={<Space><Typography.Text>{item.target_scope?.target_service || "未绑定服务"}</Typography.Text><Status value={item.status} /></Space>}
                    description={<Typography.Text type="secondary" ellipsis>{item.raw_query}</Typography.Text>}
                  />
                </List.Item>
              )}
            />
          </Card>
        </Col>
      </Row>

      <Spin spinning={loading}>
        {selected ? <DiagnosisDetail detail={selected} onDecision={decideProbe} onApproveWaiting={approveWaitingProbes} /> : <Card><Empty description="创建或打开一个诊断会话以查看假设、探针和证据" /></Card>}
      </Spin>
    </Space>
  );
}

function DiagnosisDetail({ detail, onDecision, onApproveWaiting }) {
  const conclusion = detail.latest_conclusion;
  const candidates = conclusion?.root_cause_candidates || [];
  const assessment = conclusion?.cluster_assessment;
  const commands = conclusion?.diagnostic_commands || [];
  const hypotheses = detail.hypothesis_graph?.hypotheses || [];
  const probes = detail.probes || [];
  const evidence = detail.evidence || [];
  const evidenceMap = useMemo(() => new Map(evidence.map((item) => [item.evidence_id, item])), [evidence]);
  const traceProfiles = useMemo(
    () => evidence
      .map((item) => item.observed_value || {})
      .filter((value) => value.collector_type === "trace_endpoint_profile" || value.trace_source || value.stack_source),
    [evidence],
  );
  const offCpuProfiles = useMemo(
    () => evidence
      .map((item) => item.observed_value || {})
      .filter((value) => value.collector_type === "off_cpu_wait_profile" || value.collector_family === "off_cpu_wait_profile"),
    [evidence],
  );
  const resourceBudget = detail.resource_budget || {};
  const budgetUsed = detail.budget_used || {};
  const totalBudget = Number(resourceBudget.max_total_probe_cpu_seconds || 180);
  const followUpReserve = Math.min(
    Number(resourceBudget.follow_up_reserve_seconds || 60),
    totalBudget,
  );
  const initialBudget = Math.max(0, totalBudget - followUpReserve);
  const usedBudget = Number(budgetUsed.probe_duration_seconds || 0);
  const isTerminal = TERMINAL.has(detail.status);
  const waitingProbeCount = probes.filter((item) => item.status === "WAITING_APPROVAL").length;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card title={<Space>诊断详情 <Status value={detail.status} /></Space>}>
          <Descriptions size="small" column={{ xs: 1, md: 3 }}>
          <Descriptions.Item label="诊断 ID"><Typography.Text copyable>{detail.diagnosis_id}</Typography.Text></Descriptions.Item>
          <Descriptions.Item label="目标服务">{detail.target_scope?.target_service || "未解析"}</Descriptions.Item>
          <Descriptions.Item label="拓扑快照">{detail.topology_snapshot_id}</Descriptions.Item>
          <Descriptions.Item label="症状">{detail.normalized_intent?.symptom}</Descriptions.Item>
          <Descriptions.Item label="模型">{detail.model_version}</Descriptions.Item>
          <Descriptions.Item label="规划器">{detail.planner_version}</Descriptions.Item>
          <Descriptions.Item label="采集预算">
            {usedBudget}s / {totalBudget}s
          </Descriptions.Item>
          <Descriptions.Item label="初始采集上限">{initialBudget}s</Descriptions.Item>
          <Descriptions.Item label="Follow-up 保留">{followUpReserve}s</Descriptions.Item>
        </Descriptions>
      </Card>

      {conclusion && (
        <Card title="最新结论">
          <Alert
            showIcon
            type={detail.status === "INSUFFICIENT_EVIDENCE" ? "warning" : "info"}
            message={conclusion.summary}
            description={`置信等级：${conclusion.confidence_level}`}
            style={{ marginBottom: 12 }}
          />
          {assessment && (
            <Descriptions
              size="small"
              bordered
              column={{ xs: 1, md: 3 }}
              style={{ marginBottom: 12 }}
            >
              <Descriptions.Item label="跨节点判断">{assessment.classification}</Descriptions.Item>
              <Descriptions.Item label="判断置信度">{assessment.confidence}</Descriptions.Item>
              <Descriptions.Item label="对比目标">{assessment.compared_targets?.length || 0}</Descriptions.Item>
              {assessment.supported_level && (
                <Descriptions.Item label="定位层级">{assessment.supported_level}</Descriptions.Item>
              )}
              {assessment.primary_anchor?.anchor && (
                <Descriptions.Item label="具体锚点">
                  <Typography.Text code>{assessment.primary_anchor.anchor}</Typography.Text>
                </Descriptions.Item>
              )}
              {assessment.primary_anchor?.wait_reason && (
                <Descriptions.Item label="等待原因">
                  <Typography.Text code>{assessment.primary_anchor.wait_reason}</Typography.Text>
                </Descriptions.Item>
              )}
              <Descriptions.Item label="证据引用" span={3}>
                <Space wrap>
                  {(assessment.evidence_refs || []).map((ref) => (
                    <Tag key={ref} color={evidenceMap.has(ref) ? "blue" : "red"}>{ref}</Tag>
                  ))}
                </Space>
              </Descriptions.Item>
            </Descriptions>
          )}
          <Table
            rowKey="candidate_id"
            size="small"
            pagination={false}
            dataSource={candidates}
            columns={[
              { title: "排名", dataIndex: "rank", width: 70 },
              { title: "候选", dataIndex: "candidate_id", width: 220 },
              { title: "置信等级", dataIndex: "confidence_level", width: 100, render: (value) => <Tag>{value}</Tag> },
              { title: "说明", dataIndex: "description" },
              {
                title: "证据",
                dataIndex: "evidence_refs",
                render: (refs = []) => <Space wrap>{refs.map((ref) => <Tag key={ref} color={evidenceMap.has(ref) ? "blue" : "red"}>{ref}</Tag>)}</Space>,
              },
            ]}
          />
          {conclusion.limitations?.length > 0 && (
            <Alert type="warning" message="限制与缺失证据" description={conclusion.limitations.join("；")} style={{ marginTop: 12 }} />
          )}
        </Card>
      )}

      {traceProfiles.length > 0 && (
        <Card title="Trace / 栈结构化证据">
          {traceProfiles.map((profile, index) => {
            const stack = profile.stack_source || {};
            const trace = profile.trace_source || {};
            const correlation = profile.correlation_status || {};
            const hotspots = profile.call_path_hotspots || [];
            const topFunctions = profile.top_functions || [];
            return (
              <Card key={`${profile.task_id || "trace"}-${index}`} size="small" type="inner" style={{ marginBottom: 12 }}>
                <Descriptions size="small" bordered column={{ xs: 1, md: 3 }}>
                  <Descriptions.Item label="栈来源">{stack.kind || "unknown"} / {stack.status || "unknown"}</Descriptions.Item>
                  <Descriptions.Item label="Trace 来源">{trace.kind || "unknown"} / {trace.status || "unknown"}</Descriptions.Item>
                  <Descriptions.Item label="关联状态">
                    <Tag color={correlation.status === "completed" ? "green" : correlation.status === "blocked" ? "red" : "orange"}>
                      {correlation.status || "unknown"}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="最高定位层级">{correlation.max_supported_level || "function"}</Descriptions.Item>
                  <Descriptions.Item label="阻断原因" span={2}>
                    {correlation.blocked_reason || "无"}
                  </Descriptions.Item>
                  {correlation.blocked_details?.repair_action && (
                    <Descriptions.Item label="修复动作" span={3}>
                      {correlation.blocked_details.repair_action}
                    </Descriptions.Item>
                  )}
                </Descriptions>
                {(hotspots.length > 0 || topFunctions.length > 0) && (
                  <Table
                    rowKey={(item, rowIndex) => item.evidence_ref || `${index}-${rowIndex}`}
                    size="small"
                    pagination={false}
                    style={{ marginTop: 12 }}
                    dataSource={hotspots.length > 0 ? hotspots : topFunctions}
                    columns={[
                      { title: "函数", dataIndex: "function", render: (value, item) => <Typography.Text code>{value || item.name || "unknown"}</Typography.Text> },
                      { title: "热点比例", dataIndex: "percent", render: (value) => `${value || 0}%` },
                      { title: "样本", dataIndex: "samples" },
                      { title: "Endpoint", dataIndex: "endpoint", render: (value) => value || "未回连" },
                      { title: "Call Path", dataIndex: "call_path", render: (value) => Array.isArray(value) && value.length > 0 ? value.join(" -> ") : "未回连" },
                      { title: "关联方式", dataIndex: "correlation_method", render: (value) => value || "function-only" },
                      { title: "证据引用", dataIndex: "evidence_ref", render: (value) => <Typography.Text copyable>{value || "-"}</Typography.Text> },
                    ]}
                  />
                )}
              </Card>
            );
          })}
        </Card>
      )}

      {offCpuProfiles.length > 0 && (
        <Card title="Off-CPU 工业化证据">
          {offCpuProfiles.map((profile, index) => {
            const summary = profile.summary || {};
            const eventSummary = profile.event_summary || {};
            const cause = profile.cause_summary || {};
            const quality = profile.stack_quality || {};
            const correlation = profile.correlation || {};
            const stacks = profile.top_wait_stacks || [];
            return (
              <Card key={`${profile.task_id || "offcpu"}-${index}`} size="small" type="inner" style={{ marginBottom: 12 }}>
                <Descriptions size="small" bordered column={{ xs: 1, md: 3 }}>
                  <Descriptions.Item label="采集状态">
                    <Tag color={profile.collector_status === "completed" ? "green" : profile.collector_status === "blocked" ? "red" : "orange"}>
                      {profile.collector_status || "unknown"}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="等待事件">{eventSummary.observed_wait_events || 0}</Descriptions.Item>
                  <Descriptions.Item label="总等待">{summary.total_wait_ms || 0} ms</Descriptions.Item>
                  <Descriptions.Item label="等待原因">{summary.top_wait_reason || "未识别"}</Descriptions.Item>
                  <Descriptions.Item label="原因层">{cause.top_cause || "未识别"}</Descriptions.Item>
                  <Descriptions.Item label="栈质量">{quality.stack_unwind_status || "unknown"}</Descriptions.Item>
                  <Descriptions.Item label="关联状态">
                    <Tag color={correlation.status === "confirmed" ? "green" : correlation.status === "unmatched" ? "orange" : "red"}>
                      {correlation.status || "unknown"}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="Endpoint">{correlation.endpoint || "未回连"}</Descriptions.Item>
                  <Descriptions.Item label="关联置信度">{correlation.confidence || 0}</Descriptions.Item>
                  <Descriptions.Item label="Call Path" span={3}>
                    {Array.isArray(correlation.call_path) && correlation.call_path.length > 0
                      ? correlation.call_path.join(" -> ")
                      : "未回连"}
                  </Descriptions.Item>
                </Descriptions>
                {stacks.length > 0 && (
                  <Table
                    rowKey={(item, rowIndex) => item.evidence_ref || `${index}-offcpu-${rowIndex}`}
                    size="small"
                    pagination={false}
                    style={{ marginTop: 12 }}
                    dataSource={stacks}
                    columns={[
                      { title: "Top Frame", dataIndex: "top_frame", render: (value) => <Typography.Text code>{value || "unknown"}</Typography.Text> },
                      { title: "等待原因", dataIndex: "wait_reason" },
                      { title: "样本", dataIndex: "samples" },
                      { title: "等待时长", dataIndex: "wait_ms", render: (value) => `${value || 0} ms` },
                      { title: "证据引用", dataIndex: "evidence_ref", render: (value) => <Typography.Text copyable>{value || "-"}</Typography.Text> },
                    ]}
                  />
                )}
              </Card>
            );
          })}
        </Card>
      )}

      {commands.length > 0 && (
        <Card title="可审核命令">
          <Alert
            showIcon
            type="warning"
            message="以下命令仅供人工审核，不会由 AI 自动执行；R2/R3 操作必须单次确认。"
            style={{ marginBottom: 12 }}
          />
          <Table
            rowKey="command_id"
            size="small"
            pagination={false}
            dataSource={commands}
            columns={[
              { title: "用途", dataIndex: "title", width: 180 },
              {
                title: "风险",
                dataIndex: "risk_level",
                width: 90,
                render: (value, record) => (
                  <Space>
                    <Tag color={value === "R2" || value === "R3" ? "orange" : "green"}>{value}</Tag>
                    {record.requires_approval && <Tag color="purple">需审批</Tag>}
                  </Space>
                ),
              },
              {
                title: "命令",
                dataIndex: "command",
                render: (value) => <Typography.Text copyable code>{value}</Typography.Text>,
              },
              { title: "审核注释", dataIndex: "comment" },
            ]}
          />
        </Card>
      )}

      <Row gutter={[16, 16]}>
        <Col xs={24} xl={12}>
          <Card title="候选假设">
            <List
              dataSource={hypotheses}
              renderItem={(item) => (
                <List.Item>
                  <List.Item.Meta
                    title={<Space><Typography.Text>{item.type}</Typography.Text><Tag color={item.status === "SUPPORTED" ? "green" : "default"}>{item.status}</Tag></Space>}
                    description={item.description}
                  />
                </List.Item>
              )}
            />
          </Card>
        </Col>
        <Col xs={24} xl={12}>
          <Card
            title="受控探针与审批"
            extra={!isTerminal && waitingProbeCount > 0 ? (
              <Button size="small" type="primary" icon={<CheckOutlined />} onClick={onApproveWaiting}>
                一键批准当前待采集
              </Button>
            ) : null}
          >
            {!isTerminal && waitingProbeCount > 0 && (
              <Alert
                type="info"
                showIcon
                message="只批准当前 WAITING_APPROVAL 的注册探针；仍受 R2 预算和并发预算限制，不执行任意命令。"
                style={{ marginBottom: 12 }}
              />
            )}
            {isTerminal && probes.some((item) => item.status === "WAITING_APPROVAL") && (
              <Alert
                type="warning"
                showIcon
                message={`当前诊断已进入终态 ${detail.status}，不能继续审批旧探针；需要补采时请新建一次开发模式诊断。`}
                style={{ marginBottom: 12 }}
              />
            )}
            <List
              dataSource={probes}
              locale={{ emptyText: "尚未规划探针" }}
              renderItem={(item) => (
                <List.Item
                  actions={!isTerminal && item.status === "WAITING_APPROVAL" ? [
                    <Button key="approve" size="small" type="primary" icon={<CheckOutlined />} onClick={() => onDecision(item.step_id, "approve")}>单次批准</Button>,
                    <Button key="reject" size="small" danger icon={<CloseOutlined />} onClick={() => onDecision(item.step_id, "reject")}>拒绝</Button>,
                  ] : []}
                >
                  <List.Item.Meta
                    title={<Space><Typography.Text>{item.probe_id}</Typography.Text><Tag color={item.risk_level === "R2" ? "orange" : "green"}>{item.risk_level}</Tag><Status value={item.status} /></Space>}
                    description={`${item.reason} · ${item.parameters?.duration_sec || 0}s`}
                  />
                </List.Item>
              )}
            />
          </Card>
        </Col>
      </Row>

      <Card title={`证据血缘 (${evidence.length})`}>
        <Table
          rowKey="evidence_id"
          size="small"
          pagination={{ pageSize: 6 }}
          scroll={{ x: 900 }}
          dataSource={evidence}
          columns={[
            { title: "Evidence ID", dataIndex: "evidence_id", width: 210, render: (value) => <Typography.Text copyable>{value}</Typography.Text> },
            { title: "来源", dataIndex: "source_system", width: 170 },
            { title: "类型", dataIndex: "source_type", width: 150 },
            { title: "探针/查询", dataIndex: "query_or_probe", width: 150 },
            { title: "完整性 Hash", dataIndex: "integrity_hash", ellipsis: true },
          ]}
        />
      </Card>

      <Card title="状态事件">
        <Timeline
          items={(detail.events || []).map((event) => ({
            color: event.to_status === "FAILED" ? "red" : "blue",
            children: <Space><Typography.Text>{event.event_type}</Typography.Text><Status value={event.to_status} /></Space>,
          }))}
        />
      </Card>
    </Space>
  );
}
