import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
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
  ExperimentOutlined,
  MinusCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import {
  createDiagnosisSession,
  getDiagnosisSession,
  getTask,
  listAgents,
  listDiagnosisSessions,
  runAIValidation,
} from "../api/client";
import ControlledAITreeGraph from "../components/diagnosis/ControlledAITreeGraph";
import { selectSessionMainTree } from "../components/diagnosis/aiTreeGraphModel";
import RootCauseClusters from "../components/diagnosis/RootCauseClusters";
import "./AIDiagnosis.css";

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

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function Status({ value }) {
  return <Tag color={STATUS_COLORS[value] || "default"}>{value || "UNKNOWN"}</Tag>;
}

function normalizeSourceContext(value = {}) {
  const sourcePaths = String(value.source_paths_text || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  const payload = {
    source_paths: sourcePaths,
    repo_revision: value.repo_revision || undefined,
    language: value.language || undefined,
    symbol_map_paths: [],
    build_id: value.build_id || undefined,
    container_workdir: value.container_workdir || undefined,
  };
  return Object.values(payload).some((item) => (Array.isArray(item) ? item.length > 0 : Boolean(item)))
    ? payload
    : null;
}

export default function AIDiagnosis() {
  const [form] = Form.useForm();
  const navigate = useNavigate();
  const { diagnosisId: routeDiagnosisId } = useParams();
  const [agents, setAgents] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [selected, setSelected] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [validationRunning, setValidationRunning] = useState(false);
  const [error, setError] = useState("");
  const requestSerial = useRef(0);
  const watchedInstances = asArray(Form.useWatch("instances", form));

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
          const instances = asArray(form.getFieldValue("instances"));
          const firstInstance = instances[0] || {};
          if (!firstInstance.agent_id) {
            form.setFieldsValue({
              instances: [{
                ...firstInstance,
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
    if (!routeDiagnosisId) return;
    openSession(routeDiagnosisId, false);
  }, [routeDiagnosisId]);

  useEffect(() => {
    if (!selected?.diagnosis_id || TERMINAL.has(selected.status)) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const detail = await getDiagnosisSession(selected.diagnosis_id);
        if (String(detail?.diagnosis_id || "") !== String(selected.diagnosis_id)) {
          return;
        }
        setSelected((current) => (
          current?.diagnosis_id === detail.diagnosis_id ? detail : current
        ));
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
      const instances = asArray(values.instances).map((item = {}, index) => ({
        service_id: item.service_id,
        instance_id: item.instance_id || `${item.service_id || values.target_service}-${index + 1}`,
        host_id: item.host_id,
        agent_id: item.agent_id,
        pid: item.pid,
        container_id: item.container_id || undefined,
        environment: item.environment || values.environment,
      }));
      const sourceContext = normalizeSourceContext(values.source_context);
      const detail = await createDiagnosisSession({
        query: values.query,
        context: {
          service_id: values.target_service,
          environment: values.environment,
          instances,
          dependencies: asArray(values.dependencies),
          source_context: sourceContext || undefined,
        },
        budget_profile: values.budget_profile,
      });
      setSelected(detail);
      setCreateOpen(false);
      navigate(`/ai-diagnosis/${detail.diagnosis_id}`, { replace: false });
      await refreshSessions();
      message.success("诊断会话已创建；系统会自动执行已注册采集器，无法自动完成的事项会单独提示");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function openSession(id, updateRoute = true) {
    const serial = requestSerial.current + 1;
    requestSerial.current = serial;
    setLoading(true);
    setError("");
    setSelected(null);
    try {
      const detail = await getDiagnosisSession(id);
      if (serial !== requestSerial.current || String(detail?.diagnosis_id || "") !== String(id)) {
        return;
      }
      setSelected(detail);
      if (updateRoute) navigate(`/ai-diagnosis/${id}`, { replace: false });
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
              dataSource={asArray(result.checks)}
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
    <Space className="ai-diagnosis-page" direction="vertical" size="large" style={{ width: "100%" }}>
      <Space wrap>
        <RobotOutlined style={{ fontSize: 22, color: "#722ed1" }} />
        <Typography.Title level={4} style={{ margin: 0 }}>AI 集群诊断</Typography.Title>
        <Tag color="purple">证据驱动</Tag>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
          发起诊断
        </Button>
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
        message="诊断会自动执行已注册采集器；任意 shell、宿主机 sysctl、服务变更和修复动作仍只生成建议，不会自动执行。"
      />
      {error && <Alert type="error" showIcon closable message={error} onClose={() => setError("")} />}

      <Modal
        title="发起 AI 集群诊断"
        open={createOpen}
        onCancel={() => {
          if (!loading) setCreateOpen(false);
        }}
        footer={null}
        width={960}
        maskClosable={!loading}
        styles={{ body: { maxHeight: "70vh", overflowY: "auto" } }}
      >
        <Card title="发起诊断" extra={<SafetyCertificateOutlined style={{ color: "#52c41a" }} />}>
          <Form
              form={form}
              layout="vertical"
              initialValues={{
                environment: "production",
                budget_profile: "production_safe",
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
              </Row>

              <Typography.Title level={5}>源码上下文（可选，用于代码行级定位）</Typography.Title>
              <Row gutter={12}>
                <Col xs={24} md={12}>
                  <Form.Item name={["source_context", "source_paths_text"]} label="源码路径">
                    <Input placeholder="/repo/service-a/src, /repo/common/src" />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item name={["source_context", "repo_revision"]} label="Repo Revision">
                    <Input placeholder="git commit / tag" />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name={["source_context", "language"]} label="语言">
                    <Input placeholder="java / go / python / node" />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name={["source_context", "build_id"]} label="Build ID">
                    <Input placeholder="镜像 digest / build id" />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name={["source_context", "container_workdir"]} label="容器工作目录">
                    <Input placeholder="/app" />
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
                            <Form.Item name={[field.name, "container_id"]} label="容器 ID（可选）">
                              <Input placeholder="用于 Docker 日志和进程回连" />
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
                开始诊断
              </Button>
          </Form>
        </Card>
      </Modal>

      <Card
        title="最近会话"
        extra={<Button size="small" icon={<ReloadOutlined />} onClick={refreshSessions}>刷新</Button>}
        styles={{ body: { maxHeight: 360, overflow: "auto" } }}
      >
            <List
              dataSource={sessions}
              locale={{ emptyText: "暂无 AI 诊断会话" }}
              renderItem={(item) => (
                <List.Item key={item.diagnosis_id} actions={[<Button key="open" type="link" onClick={() => openSession(item.diagnosis_id)}>查看</Button>]}>
                  <List.Item.Meta
                    title={<Space><Typography.Text>{item.target_scope?.target_service || "未绑定服务"}</Typography.Text><Status value={item.status} /></Space>}
                    description={<Typography.Text type="secondary" ellipsis>{item.raw_query}</Typography.Text>}
                  />
                </List.Item>
              )}
            />
          </Card>

      <Spin spinning={loading}>
        {selected ? <DiagnosisDetail detail={selected} /> : <Card><Empty description="发起或打开一个诊断会话，优先查看结论与 AI 树" /></Card>}
      </Spin>
    </Space>
  );
}

function DiagnosisDetail({ detail }) {
  const conclusion = detail.latest_conclusion;
  const candidates = asArray(conclusion?.root_cause_candidates);
  const possibleCauses = asArray(conclusion?.possible_root_causes);
  const rootCauseClusters = asArray(conclusion?.root_cause_clusters);
  const sessionMainTree = useMemo(
    () => selectSessionMainTree(conclusion || {}),
    [conclusion],
  );
  const hasEligiblePrimary = Boolean(
    !conclusion?.abstained
    && (
      asArray(sessionMainTree?.final_primary_causes).length
      || candidates.length
      || rootCauseClusters.some((cluster) => cluster.conclusion_eligible)
    ),
  );
  const displayedConfidence = hasEligiblePrimary
    ? conclusion?.confidence_level
    : possibleCauses.length
      ? "低（可能根因待验证）"
      : "不可判断";
  const assessment = conclusion?.cluster_assessment;
  const unifiedQualification = conclusion?.qualification
    || assessment?.unified_qualification
    || {};
  const attributionGraph = conclusion?.attribution_graph || {};
  const attributionFacts = attributionGraph.facts || {};
  const attributionSourceRelations = asArray(attributionGraph.source_relations);
  const attributionMechanismNodes = asArray(attributionGraph.nodes)
    .filter((item) => item.role === "mechanism");
  const commands = asArray(conclusion?.diagnostic_commands);
  const hypotheses = asArray(detail.hypothesis_graph?.hypotheses);
  const probes = asArray(detail.probes);
  const evidence = asArray(detail.evidence);
  const [highlightedTreeCandidates, setHighlightedTreeCandidates] = useState([]);
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
  const pythonScenarioProfiles = useMemo(
    () => {
      const families = new Set([
        "python_lock_wait_profile",
        "python_exception_profile",
        "python_queue_profile",
        "python_pool_profile",
        "python_retry_timeout_profile",
      ]);
      return evidence
        .map((item) => item.observed_value || {})
        .filter((value) => families.has(value.collector_type) || families.has(value.collector_family));
    },
    [evidence],
  );
  const pythonScenarioGates = useMemo(
    () => {
      const gatesByFamily = new Map();
      evidence.forEach((item) => {
        const summary = item.observed_value?.summary || {};
        const inputs = summary.confidence_inputs || {};
        const gates = inputs.python_scenario_gates || summary.python_scenario_gates || {};
        Object.entries(gates).forEach(([family, gate]) => {
          if (gate && typeof gate === "object") gatesByFamily.set(family, { family, ...gate });
        });
      });
      return Array.from(gatesByFamily.values());
    },
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
  const manualActionProbes = probes.filter((item) => (
    item.status === "WAITING_APPROVAL"
    || item.status === "UNAVAILABLE"
    || item.status === "REJECTED_POLICY"
    || item.status === "FAILED"
  ));
  const treeStats = useMemo(
    () => countControlledTreeBranches(sessionMainTree),
    [sessionMainTree],
  );
  const aiReviewStatus = conclusion?.ai_review_status || "fallback";
  const retainedConclusion = conclusion?.retained_conclusion || {};
  const formalRootCause = conclusion?.formal_root_cause || null;
  const qualificationBoundary = conclusion?.qualification_boundary || {};
  const candidateReview = conclusion?.candidate_review || {};
  const candidateGenerationOutput = conclusion?.candidate_generation_output || {};
  const candidateGenerationAttempts = asArray(candidateGenerationOutput.attempts).length
    ? asArray(candidateGenerationOutput.attempts)
    : asArray(candidateReview.candidate_generation_attempts);
  const gateFailures = asArray(conclusion?.gate_failures).length
    ? asArray(conclusion?.gate_failures)
    : asArray(conclusion?.ai_gate_failures);
  const retainedParentConclusions = asArray(conclusion?.retained_parent_conclusions);
  const conclusionBoundaries = asArray(conclusion?.boundaries);
  const controlledTree = sessionMainTree || {};
  const lineAnchorEligibility = controlledTree.line_anchor_eligibility || {};
  const heapProbeOutcome = controlledTree.heap_probe_outcome || {};
  const probeConflicts = asArray(controlledTree.probe_conflicts);
  const displayedConclusion = aiReviewStatus === "succeeded" && conclusion?.headline
    ? conclusion.headline
    : conclusion?.headline || retainedConclusion.claim || formalRootCause?.claim || conclusion?.summary;
  const displayedLevel = rootCauseClusters.length > 1
    ? null
    : rootCauseClusters[0]?.supported_level
      || retainedConclusion.supported_level
      || conclusion?.cluster_assessment?.supported_level;
  const displayedQualification = aiReviewStatus === "succeeded" && formalRootCause
    ? "formal_root_cause"
    : retainedConclusion.qualification || (formalRootCause ? "formal_root_cause" : "partial_localization");
  const integratedBranchDetails = asArray(conclusion?.causal_chain)
    .map((item) => item.statement)
    .filter((statement) => statement && !String(conclusion?.why_it_happened || "").includes(statement));

  function inspectClusterInTree(candidateIds) {
    setHighlightedTreeCandidates(candidateIds);
    requestAnimationFrame(() => {
      document.getElementById("controlled-ai-tree")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {conclusion && (
        <Card title="最新结论">
          <Alert
            showIcon
            type={!hasEligiblePrimary || aiReviewStatus !== "succeeded" ? "warning" : "info"}
            message={displayedConclusion}
            description={(
              <Space direction="vertical" size={6}>
                <Typography.Text>{conclusion.why_it_happened || conclusion.summary}</Typography.Text>
                {rootCauseClusters.length > 1 && integratedBranchDetails.length > 0 && (
                  <Typography.Text>
                    {integratedBranchDetails.join("；")}
                  </Typography.Text>
                )}
                {asArray(conclusion.localization_chain).length > 0 && (
                  <Typography.Text type="secondary">
                    同时定位到：{asArray(conclusion.localization_chain).map((item) => item.statement).join("；")}
                  </Typography.Text>
                )}
                <Space wrap>
                  <Tag color={hasEligiblePrimary ? "green" : "gold"}>根因置信等级 {displayedConfidence}</Tag>
                  <Tag>{displayedQualification}</Tag>
                  {displayedLevel && <Tag>当前定位：{displayedLevel}</Tag>}
                  {rootCauseClusters.length > 1 && <Tag>{rootCauseClusters.length} 个相关故障方向</Tag>}
                  <Tag color={aiReviewStatus === "succeeded" ? "green" : aiReviewStatus === "failed" ? "red" : "orange"}>
                    {aiReviewStatus === "succeeded" ? "AI 会话裁决已通过" : aiReviewStatus === "failed" ? "AI 会话裁决失败，保留当前结论" : "AI 会话裁决未完成，保留当前结论"}
                  </Tag>
                  {conclusion.ai_review_model && <Tag>{conclusion.ai_review_model}</Tag>}
                </Space>
              </Space>
            )}
            style={{ marginBottom: 12 }}
          />
          {qualificationBoundary.message && (
            <Alert
              type={qualificationBoundary.status === "blocked" ? "warning" : "info"}
              showIcon
              message="证据边界"
              description={(
                <Space direction="vertical" size={4}>
                  <Typography.Text>{qualificationBoundary.message}</Typography.Text>
                  {asArray(qualificationBoundary.missing_evidence).length > 0 && (
                    <Typography.Text type="secondary">
                      尚缺：{asArray(qualificationBoundary.missing_evidence).join("；")}
                    </Typography.Text>
                  )}
                </Space>
              )}
              style={{ marginBottom: 12 }}
            />
          )}
          <RootCauseClusters
            clusters={rootCauseClusters}
            evidenceMap={evidenceMap}
            onInspectTree={inspectClusterInTree}
          />
          {asArray(conclusion.residual_unknowns).length > 0 && (
            <Alert
              type="warning"
              message="仍未确认的边界"
              description={asArray(conclusion.residual_unknowns).join("；")}
              style={{ margin: "12px 0" }}
            />
          )}
          {asArray(conclusion.limitations).length > 0 && (
            <Alert type="warning" message="限制与缺失证据" description={asArray(conclusion.limitations).join("；")} style={{ marginTop: 12 }} />
          )}
        </Card>
      )}

      {sessionMainTree && (
        <Card
          id="controlled-ai-tree"
          title="受控 AI 树（当前状态）"
          extra={(
            <Space wrap>
              <Tag color="red">主因 {treeStats.primary}</Tag>
              <Tag color="default">反证 {treeStats.rejected}</Tag>
              <Tag color="cyan">未决/阻断 {treeStats.unknown}</Tag>
              <Tag color={hasEligiblePrimary ? "green" : "gold"}>
                {hasEligiblePrimary ? "正式结论" : "当前证据边界"}：{sessionMainTree.final_supported_level}
              </Tag>
            </Space>
          )}
        >
          <Alert
            type={asArray(sessionMainTree.probe_edges).length ? "warning" : "success"}
            showIcon
            message={sessionMainTree.stop_reason || "AI 树已按当前证据边界停止。"}
            description={`树中会保留比正式结论更深的已观察节点，并标记为“未入终态”；这些节点不等于正式根因。预算：AI rounds ${sessionMainTree.budget?.used_ai_rounds || 0}/${sessionMainTree.budget?.max_ai_rounds || 0}，探针请求 ${sessionMainTree.budget?.used_probe_requests || 0}/${sessionMainTree.budget?.max_probe_requests_per_round || 0}`}
            style={{ marginBottom: 12 }}
          />
          <ControlledAITreeGraph
            key={`${detail.diagnosis_id}:${sessionMainTree.tree_id || "tree"}`}
            tree={sessionMainTree}
            evidenceMap={evidenceMap}
            highlightedCandidateIds={highlightedTreeCandidates}
          />
        </Card>
      )}

      <Collapse
        defaultActiveKey={[]}
        items={[{
          key: "session-meta",
          label: "诊断元信息",
          children: (
            <Descriptions size="small" column={{ xs: 1, md: 3 }}>
              <Descriptions.Item label="诊断 ID"><Typography.Text copyable>{detail.diagnosis_id}</Typography.Text></Descriptions.Item>
              <Descriptions.Item label="状态"><Status value={detail.status} /></Descriptions.Item>
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
          ),
        }, {
          key: "ai-candidate-audit",
          label: `AI 候选、门禁与审核过程（候选 ${candidateGenerationAttempts.length} 轮，门禁 ${gateFailures.length} 条，已确认 ${candidates.length} 个）`,
          children: (
            <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          {probeConflicts.length > 0 && (
            <Alert
              type="warning"
              showIcon
              message={`探针输入存在 ${probeConflicts.length} 个冲突`}
              description={probeConflicts.map((item) => (
                <Typography.Text key={`${item.evidence_family}-${item.field || item.status}`} style={{ display: "block" }}>
                  {item.evidence_family}：{item.field || item.status}；保留已有完整输入，未静默覆盖。
                </Typography.Text>
              ))}
              style={{ marginBottom: 12 }}
            />
          )}
          {(
            asArray(attributionFacts.cost_centers).length > 0
            || asArray(attributionFacts.trigger_candidates).length > 0
            || asArray(attributionFacts.impact_candidates).length > 0
            || attributionSourceRelations.length > 0
            || attributionMechanismNodes.length > 0
            || asArray(attributionGraph.repair_clusters).length > 0
          ) && (
            <Alert
              type="info"
              showIcon
              message="证据归因分域"
              description={(
                <Descriptions size="small" column={{ xs: 1, md: 2 }}>
                  <Descriptions.Item label="成本中心">
                    {asArray(attributionFacts.cost_centers).map((item) => item.target || item.label || item.candidate_id).join("；") || "未形成"}
                  </Descriptions.Item>
                  <Descriptions.Item label="触发点">
                    {asArray(attributionFacts.trigger_candidates).map((item) => item.statement || item.candidate_id).join("；") || "未观测"}
                  </Descriptions.Item>
                  <Descriptions.Item label="影响">
                    {asArray(attributionFacts.impact_candidates).map((item) => item.statement || item.candidate_id).join("；") || "未观测"}
                  </Descriptions.Item>
                  <Descriptions.Item label="机制点">
                    {attributionMechanismNodes.map((item) => item.label || item.node_id).join("；") || "仅有机制假设，尚无独立机制节点"}
                  </Descriptions.Item>
                  <Descriptions.Item label="源码关系" span={2}>
                    {attributionSourceRelations.map((item) => (
                      `${item.source_ref || "runtime"} ${item.relation || "relates"} ${item.target_ref || "source"}`
                    )).join("；") || "未形成已验证源码关系"}
                  </Descriptions.Item>
                  <Descriptions.Item label="修复簇" span={2}>
                    {asArray(attributionGraph.repair_clusters).map((item) => (
                      `${item.cluster_id}（${item.status || "candidate"}）`
                    )).join("；") || "仅保留候选位置，不声明修复点"}
                  </Descriptions.Item>
                </Descriptions>
              )}
              style={{ marginBottom: 12 }}
            />
          )}
          {(asArray(conclusion.candidate_validation_diagnostics).length > 0
            || asArray(conclusion.candidate_review?.validation_diagnostics).length > 0) && (
            <Alert
              type="warning"
              showIcon
              message="AI 候选未通过结构化门禁"
              description={(
                <Space direction="vertical" size={4}>
                  {(asArray(conclusion.candidate_validation_diagnostics).length > 0
                    ? asArray(conclusion.candidate_validation_diagnostics)
                    : asArray(conclusion.candidate_review?.validation_diagnostics)).map((item, index) => (
                    <Typography.Text key={`${item.attempt || index}-${item.failure_code || "validation"}`}>
                      第 {item.attempt || index + 1} 次：{item.failure_path || "响应结构"}；
                      实际值：{item.actual_value || item.failure_code || "未提供"}；
                      候选 {item.candidate_count ?? 0} 个，合法证据引用 {item.valid_evidence_ref_count ?? 0} 个；
                      初始证据缺失 {asArray(item.missing_initial_evidence_refs).join(", ") || "无"}；
                      缺失父节点 {asArray(item.missing_parent_candidate_ids).join(", ") || "无"}。
                    </Typography.Text>
                  ))}
                </Space>
              )}
              style={{ marginBottom: 12 }}
            />
          )}
          {candidateGenerationAttempts.length > 0 && (
            <Alert
              type={candidateGenerationOutput.status === "succeeded" ? "info" : "warning"}
              showIcon
              message={`AI 首轮候选生成尝试：${candidateGenerationAttempts.length} 次`}
              description={(
                <Space direction="vertical" size={4}>
                  {candidateGenerationAttempts.map((item) => (
                    <Typography.Text key={`candidate-attempt-${item.attempt}`}>
                      第 {item.attempt} 次：{item.status}；
                      实际解析 {item.parsed_candidate_count ?? 0} 个，
                      接受 {item.accepted_candidate_count ?? 0} 个，
                      拒绝 {item.rejected_candidate_count ?? 0} 个；
                      输出摘要：{item.response_excerpt || "无可展示输出"}。
                    </Typography.Text>
                  ))}
                  {asArray(candidateGenerationOutput.validation_diagnostics).map((item, index) => (
                    <Space
                      key={`candidate-diagnostic-${item.attempt || index}-${item.candidate_id || "response"}`}
                      direction="vertical"
                      size={2}
                      style={{ width: "100%" }}
                    >
                      <Typography.Text type="warning">
                        门禁失败：{item.failure_code || "validation_error"}；
                        路径：{item.failure_path || "response"}；
                        候选：{item.candidate_id || "整轮响应"}；
                        原因：{item.reason || item.actual_value || "未提供"}。
                      </Typography.Text>
                      <Typography.Text type="secondary">
                        实际证据引用：{asArray(item.candidate_evidence_refs).join("、") || "无"}；
                        缺失初始证据：{asArray(item.missing_initial_evidence_refs).join("、") || "无"}；
                        父节点：{asArray(item.candidate_parent_candidate_ids).join("、") || "无"}；
                        缺失父节点：{asArray(item.missing_parent_candidate_ids).join("、") || "无"}。
                      </Typography.Text>
                      {item.raw_response_excerpt && (
                        <Typography.Paragraph
                          code
                          ellipsis={{ rows: 4, expandable: true, symbol: "展开实际输出" }}
                          style={{ marginBottom: 0, whiteSpace: "pre-wrap" }}
                        >
                          {item.raw_response_excerpt}
                        </Typography.Paragraph>
                      )}
                    </Space>
                  ))}
                </Space>
              )}
              style={{ marginBottom: 12 }}
            />
          )}
          {(candidateGenerationOutput.status === "failed"
            || candidateGenerationOutput.status === "fallback"
            || candidateGenerationOutput.status === "not_started"
            || candidateGenerationOutput.status === "succeeded") && (
            <Alert
              type={candidateGenerationOutput.status === "succeeded" ? "info" : "warning"}
              showIcon
              message={
                candidateGenerationOutput.status === "succeeded"
                  ? "AI 首轮候选结果"
                  : candidateGenerationOutput.status === "not_started"
                    ? "AI 首轮候选尚未执行"
                    : "AI 首轮候选未形成可用主调查方向，已保留 Analyzer fallback"
              }
              description={(
                <Space direction="vertical" size={4}>
                  <Typography.Text>
                    有效候选：{asArray(candidateGenerationOutput.accepted_candidate_ids).length} 个；
                    active 深探：{asArray(candidateGenerationOutput.active_candidate_ids).length} 个；
                    延后调查：{asArray(candidateGenerationOutput.deferred_candidate_ids).length} 个；
                    校验失败：{asArray(candidateGenerationOutput.rejected_candidate_ids).length} 个。
                  </Typography.Text>
                  {asArray(candidateGenerationOutput.accepted_candidate_ids).length > 0 && (
                    <Typography.Text type="secondary">
                      AI 候选：{asArray(candidateGenerationOutput.accepted_candidate_ids).join("、")}
                    </Typography.Text>
                  )}
                  {asArray(candidateGenerationOutput.accepted_candidates).length > 0 && (
                    <Space direction="vertical" size={2} style={{ width: "100%" }}>
                      {asArray(candidateGenerationOutput.accepted_candidates).map((item) => (
                        <Typography.Text key={`accepted-candidate-${item.candidate_id}`}>
                          {item.candidate_id}：{item.claim || "未提供候选说明"}
                          {"；"}父节点：{item.origin_parent_candidate_id || asArray(item.parent_candidate_ids).join("、") || "无"}
                          {"；"}缺失证据：{asArray(item.missing_evidence).join("、") || "无"}
                        </Typography.Text>
                      ))}
                    </Space>
                  )}
                  {asArray(candidateGenerationOutput.active_candidate_ids).length > 0 && (
                    <Typography.Text type="secondary">
                      本轮进入深探：{asArray(candidateGenerationOutput.active_candidate_ids).join("、")}
                    </Typography.Text>
                  )}
                  {asArray(candidateGenerationOutput.rejected_candidate_ids).length > 0 && (
                    <Typography.Text type="secondary">
                      未通过结构校验：{asArray(candidateGenerationOutput.rejected_candidate_ids).join("、")}
                    </Typography.Text>
                  )}
                  {asArray(candidateGenerationOutput.selection_diagnostics).map((item) => (
                    <Typography.Text key={`selection-${item.candidate_id || item.selection}`}>
                      {item.candidate_id}：{item.selection}；{item.reason}
                    </Typography.Text>
                  ))}
                  {asArray(candidateGenerationOutput.tree_ingestion_diagnostics).map((item, index) => (
                    <Typography.Text type="warning" key={`tree-ingestion-${item.candidate_id || "candidate"}-${index}`}>
                      主树接入：{item.candidate_id || "未命名候选"}；
                      {item.reason || item.failure_code || "未进入 session_main"}；
                      父节点：{asArray(item.parent_candidate_ids).join("、") || "无"}；
                      缺失父节点：{asArray(item.missing_parent_candidate_ids).join("、") || "无"}。
                    </Typography.Text>
                  ))}
                  {asArray(candidateGenerationOutput.gate_failures).map((item, index) => (
                    <Space
                      key={`candidate-gate-${item.candidate_id || "round"}-${item.failure_code || "gate"}-${index}`}
                      direction="vertical"
                      size={2}
                      style={{ width: "100%" }}
                    >
                      <Typography.Text type="warning">
                        正式门禁：{item.candidate_id || "整轮候选"}；
                        失败字段：{asArray(item.failed_gates).join("、") || item.failure_code || "未提供"}；
                        {item.reason || "当前证据不足以升级正式结论"}。
                      </Typography.Text>
                      <Typography.Text type="secondary">
                        初始证据：{asArray(item.initial_evidence_refs).length} 条；
                        候选证据：{asArray(item.evidence_refs).length || asArray(item.candidate_evidence_refs).length} 条；
                        保留父节点：{item.retained_parent_candidate_id || item.origin_parent_candidate_id || "无"}；
                        缺失证据：{asArray(item.required_probe).join("、") || "无"}。
                      </Typography.Text>
                    </Space>
                  ))}
                  {Object.entries(asObject(candidateGenerationOutput.initial_evidence_context?.evidence_snapshots)).map(([ref, snapshot]) => (
                          <Typography.Text type="secondary" key={`initial-snapshot-${ref}`}>
                            初始证据 {ref}：{snapshot?.family || "unknown"} / {snapshot?.status || "unknown"}；
                            {snapshot?.observed_excerpt || "无摘要"}。
                          </Typography.Text>
                        ))}
                  {candidateGenerationOutput.status !== "succeeded"
                    && asArray(candidateGenerationOutput.accepted_candidate_ids).length === 0
                    && (
                      <Typography.Text type="secondary">
                        当前主树中的 Analyzer 方向仅作为 fallback investigation candidate，
                        不代表正式根因。
                      </Typography.Text>
                    )}
                </Space>
              )}
              style={{ marginBottom: 12 }}
            />
          )}
          {candidateGenerationOutput.status !== "not_started" && (
            <Alert
              type="info"
              showIcon
              message="首轮候选使用的初始证据"
              description={(
                <Space direction="vertical" size={4}>
                  <Typography.Text>
                    AI 首轮只读取已有证据目录；合法引用 {asArray(candidateGenerationOutput.initial_evidence_context?.evidence_refs).length} 条，
                    生成候选 {asArray(candidateGenerationOutput.accepted_candidate_ids).length} 个，
                    延后调查 {asArray(candidateGenerationOutput.deferred_candidate_ids).length} 个。
                  </Typography.Text>
                  {asArray(candidateGenerationOutput.initial_evidence_context?.evidence_refs).length > 0 && (
                    <Space wrap>
                    {asArray(candidateGenerationOutput.initial_evidence_context?.evidence_refs).map((ref, index) => (
                      <Tag key={`${ref}-${index}`} color={evidenceMap.has(ref) ? "blue" : "red"}>{ref}</Tag>
                    ))}
                    </Space>
                  )}
                  {asArray(candidateGenerationOutput.initial_evidence_context?.evidence_refs).length === 0 && (
                    <Typography.Text type="warning">
                      本轮没有可供 AI 候选引用的初始 evidence ref，无法通过证据引用门禁。
                    </Typography.Text>
                  )}
                  {candidateGenerationOutput.error && (
                    <Typography.Text type="secondary">{candidateGenerationOutput.error}</Typography.Text>
                  )}
                </Space>
              )}
              style={{ marginBottom: 12 }}
            />
          )}
          {(lineAnchorEligibility.status || heapProbeOutcome.status) && (
            <Space direction="vertical" size={6} style={{ width: "100%", marginBottom: 12 }}>
              {candidateGenerationOutput.line_probe_diagnostic?.status && (
                <Alert
                  type={candidateGenerationOutput.line_probe_diagnostic.status === "requested" ? "info" : candidateGenerationOutput.line_probe_diagnostic.eligibility_status === "verified" ? "success" : "warning"}
                  showIcon
                  message={`Line 探测链路：${candidateGenerationOutput.line_probe_diagnostic.status}`}
                  description={(
                    <Space direction="vertical" size={2}>
                      <Typography.Text>
                        runtime/analyzer anchor → source_snapshot → line eligibility；
                        支持层级 {candidateGenerationOutput.line_probe_diagnostic.supported_level}；
                        运行时行候选 {candidateGenerationOutput.line_probe_diagnostic.runtime_line_candidate_count || 0} 个。
                      </Typography.Text>
                      {candidateGenerationOutput.line_probe_diagnostic.skip_reason && (
                        <Typography.Text type="secondary">
                          跳过/终止原因：{candidateGenerationOutput.line_probe_diagnostic.skip_reason}
                        </Typography.Text>
                      )}
                    </Space>
                  )}
                />
              )}
              {lineAnchorEligibility.status && (
                <Alert
                  type={lineAnchorEligibility.status === "verified" ? "success" : "info"}
                  showIcon
                  message={lineAnchorEligibility.status === "verified" ? "Line 锚点已通过验证" : "Line 层资格边界"}
                  description={(
                    <Space direction="vertical" size={2}>
                      <Typography.Text>{lineAnchorEligibility.reason || "当前证据尚未形成可验证源码行。"}</Typography.Text>
                      {lineAnchorEligibility.file && (
                        <Typography.Text type="secondary">
                          当前候选：{lineAnchorEligibility.file}:{lineAnchorEligibility.line || "?"}
                        </Typography.Text>
                      )}
                    </Space>
                  )}
                />
              )}
              {heapProbeOutcome.status && heapProbeOutcome.status !== "not_started" && (
                <Alert
                  type={heapProbeOutcome.evidence_status === "valid" ? "success" : "warning"}
                  showIcon
                  message={heapProbeOutcome.evidence_status === "valid" ? "Heap 证据已返回" : "Heap 采集结果已降级记录"}
                  description={(
                    <Space direction="vertical" size={2}>
                      <Typography.Text>
                        状态：{heapProbeOutcome.status}
                        {heapProbeOutcome.evidence_status ? ` / ${heapProbeOutcome.evidence_status}` : ""}
                        {heapProbeOutcome.python_heap_status ? `；Python Heap ${heapProbeOutcome.python_heap_status}` : ""}
                        {heapProbeOutcome.fallback_status && heapProbeOutcome.fallback_status !== "none"
                          ? `；降级 ${heapProbeOutcome.fallback_status}`
                          : ""}
                        {heapProbeOutcome.failure_type ? `；失败类型：${heapProbeOutcome.failure_type}` : ""}
                      </Typography.Text>
                      {heapProbeOutcome.formal_heap_retention === false && (
                        <Typography.Text type="secondary">
                          当前结果不能证明 Python 对象 retention、引用链或源码行根因。
                        </Typography.Text>
                      )}
                      {heapProbeOutcome.reason && <Typography.Text type="secondary">{heapProbeOutcome.reason}</Typography.Text>}
                      {heapProbeOutcome.blocked_reason && (
                        <Typography.Text type="secondary">边界原因：{heapProbeOutcome.blocked_reason}</Typography.Text>
                      )}
                      {heapProbeOutcome.retry_skipped_reason && (
                        <Typography.Text type="secondary">重试状态：{heapProbeOutcome.retry_skipped_reason}</Typography.Text>
                      )}
                      {asArray(heapProbeOutcome.helper_trace?.completed_phases).length > 0 && (
                        <Typography.Text type="secondary">
                          现场阶段：{asArray(heapProbeOutcome.helper_trace?.completed_phases).join(" -> ")}
                        </Typography.Text>
                      )}
                      {heapProbeOutcome.attach_preflight?.target_pid && (
                        <Typography.Text type="secondary">
                          Attach 预检：PID {heapProbeOutcome.attach_preflight.target_pid}；
                          PID namespace {String(heapProbeOutcome.attach_preflight.same_pid_namespace ?? "unknown")}；
                          helper {heapProbeOutcome.attach_preflight.helper_available ? "可用" : "不可用"}。
                        </Typography.Text>
                      )}
                    </Space>
                  )}
                />
              )}
            </Space>
          )}
          {gateFailures.length > 0 && (
            <Alert
              type="info"
              showIcon
              message="AI 候选已生成，但未通过正式根因门禁"
              description={(
                <Space direction="vertical" size={4}>
                  {gateFailures.map((item, index) => (
                    <Space
                      direction="vertical"
                      size={2}
                      style={{ width: "100%" }}
                      key={`${item.candidate_id}-${item.failure_code}-${index}`}
                    >
                      <Typography.Text>
                        {item.candidate_id || "未命名候选"}：{item.reason || item.failure_code}；
                        状态 {item.status || "unknown"} / {item.causal_status || "unknown"}；
                        保留来源父节点 {item.retained_parent_candidate_id || item.origin_parent_candidate_id || "无"}。
                      </Typography.Text>
                      <Typography.Text type="secondary">
                        失败门禁：{asArray(item.failed_gates).join("、") || "未提供"}；
                        候选证据 {asArray(item.evidence_refs).length} 条；
                        初始证据 {asArray(item.initial_evidence_refs).length} 条；
                        缺失引用 {asArray(item.missing_initial_evidence_refs).join(", ") || "无"}；
                        缺失父节点 {asArray(item.missing_parent_candidate_ids).join(", ") || "无"}。
                      </Typography.Text>
                      {Object.entries(asObject(item.initial_evidence_context?.evidence_windows)).map(([ref, window]) => (
                          <Typography.Text type="secondary" key={`gate-window-${item.candidate_id}-${ref}`}>
                            {ref} 窗口：{window?.timing_relation || "unknown"}
                            {window?.window_start ? `，${window.window_start}` : ""}
                            {window?.window_end ? ` 至 ${window.window_end}` : ""}。
                          </Typography.Text>
                        ))}
                    </Space>
                  ))}
                </Space>
              )}
              style={{ marginBottom: 12 }}
            />
          )}
          {retainedParentConclusions.length > 0 && (
            <Alert
              type="success"
              showIcon
              message="当前保留的父结论链"
              description={(
                <Space direction="vertical" size={4}>
                  {retainedParentConclusions.map((item) => (
                    <Typography.Text key={item.candidate_id}>
                      {item.candidate_id}：{item.claim}
                      {item.origin_parent_candidate_id ? `；来源父节点：${item.origin_parent_candidate_id}` : ""}
                      {item.retained ? "；当前保留" : "；上层父节点"}
                    </Typography.Text>
                  ))}
                </Space>
              )}
              style={{ marginBottom: 12 }}
            />
          )}
          {conclusionBoundaries.length > 0 && (
            <Alert
              type="warning"
              showIcon
              message="调查边界与降级原因"
              description={(
                <Space direction="vertical" size={4}>
                  {conclusionBoundaries.map((item, index) => (
                    <Typography.Text key={`${item.kind || "boundary"}-${item.candidate_id || index}`}>
                      {item.candidate_id || item.kind}：{item.reason || item.message || "当前分支未继续升级"}
                      {item.origin_parent_candidate_id ? `；回退来源：${item.origin_parent_candidate_id}` : ""}
                    </Typography.Text>
                  ))}
                </Space>
              )}
              style={{ marginBottom: 12 }}
            />
          )}
          {assessment && (
            <Descriptions
              size="small"
              bordered
              column={{ xs: 1, md: 3 }}
              style={{ marginBottom: 12 }}
            >
              <Descriptions.Item label="跨节点判断">{assessment.classification}</Descriptions.Item>
              <Descriptions.Item label="判断置信度">{assessment.confidence}</Descriptions.Item>
              <Descriptions.Item label="对比目标">{asArray(assessment.compared_targets).length}</Descriptions.Item>
              {unifiedQualification.level && (
                <Descriptions.Item label="统一资格层级">
                  <Tag color={unifiedQualification.level === "L3" ? "green" : "gold"}>
                    {unifiedQualification.level}
                  </Tag>
                </Descriptions.Item>
              )}
              {unifiedQualification.decision && (
                <Descriptions.Item label="统一资格决策">
                  {unifiedQualification.decision}
                </Descriptions.Item>
              )}
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
                  {asArray(assessment.evidence_refs).map((ref, index) => (
                    <Tag key={`${ref}-${index}`} color={evidenceMap.has(ref) ? "blue" : "red"}>{ref}</Tag>
                  ))}
                </Space>
              </Descriptions.Item>
              {asArray(unifiedQualification.missing_evidence).length > 0 && (
                <Descriptions.Item label="统一证据缺口" span={3}>
                  {asArray(unifiedQualification.missing_evidence).join("；")}
                </Descriptions.Item>
              )}
            </Descriptions>
          )}
          {candidates.length > 0 && (
            <Table
              rowKey="candidate_id"
              size="small"
              pagination={false}
              dataSource={candidates}
              columns={[
                { title: "排名", dataIndex: "rank", width: 70 },
                { title: "已确认候选", dataIndex: "candidate_id", width: 220 },
                { title: "置信等级", dataIndex: "confidence_level", width: 100, render: (value) => <Tag>{value}</Tag> },
                { title: "说明", dataIndex: "description" },
                {
                  title: "证据",
                  dataIndex: "evidence_refs",
                  render: (refs = []) => <Space wrap>{asArray(refs).map((ref, index) => <Tag key={`${ref}-${index}`} color={evidenceMap.has(ref) ? "blue" : "red"}>{ref}</Tag>)}</Space>,
                },
              ]}
            />
          )}
            </Space>
          ),
        }, {
          key: "diagnostic-details",
          label: `证据、采集与调试明细（证据 ${evidence.length} 条，探针 ${probes.length} 个，子任务 ${asArray(detail.child_task_ids).length} 个）`,
          children: (
            <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {traceProfiles.length > 0 && (
        <Card title="Trace / 栈结构化证据">
          {traceProfiles.map((profile, index) => {
            const stack = profile.stack_source || {};
            const trace = profile.trace_source || {};
            const correlation = profile.correlation_status || {};
            const hotspots = asArray(profile.call_path_hotspots);
            const topFunctions = asArray(profile.top_functions);
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
            const stacks = asArray(profile.top_wait_stacks);
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

      {pythonScenarioGates.length > 0 && (
        <Card title="Python 场景门禁">
          <Table
            rowKey={(item) => item.family || item.scenario_type}
            size="small"
            pagination={false}
            dataSource={pythonScenarioGates}
            columns={[
              { title: "场景", dataIndex: "scenario_type", render: (value, item) => value || item.family },
              {
                title: "证据状态",
                dataIndex: "evidence_status",
                width: 110,
                render: (value) => (
                  <Tag color={value === "valid" ? "green" : value === "partial" ? "blue" : value === "empty_window" ? "orange" : "red"}>
                    {value || "unknown"}
                  </Tag>
                ),
              },
              {
                title: "最高层级",
                dataIndex: "max_supported_claim_type",
                width: 150,
                render: (value) => <Tag>{value || "observation"}</Tag>,
              },
              {
                title: "源码验证",
                dataIndex: "line_verified",
                width: 110,
                render: (value, item) => (
                  <Space>
                    <Tag color={value ? "green" : "gold"}>{value ? "已验证" : "未验证"}</Tag>
                    {item.line_candidate_count > 0 && <Tag>{item.line_candidate_count} 行候选</Tag>}
                  </Space>
                ),
              },
              {
                title: "来源",
                dataIndex: "upstream_sources",
                render: (sources = []) => {
                  const safeSources = asArray(sources);
                  return (
                  <Space wrap>
                    {safeSources.length > 0
                      ? safeSources.map((source, index) => (
                        <Tag key={`${source.source_kind || source.kind}-${index}`} color="blue">
                          {source.source_kind || source.kind || "unknown"} / {source.record_count || 0}
                        </Tag>
                      ))
                      : <Tag color="default">无来源摘要</Tag>}
                  </Space>
                  );
                },
              },
              {
                title: "缺失门禁",
                dataIndex: "missing_evidence",
                render: (items = []) => {
                  const safeItems = asArray(items);
                  return (
                  <Space wrap>
                    {safeItems.length > 0
                      ? safeItems.slice(0, 5).map((item, index) => <Tag key={`${item}-${index}`} color="gold">{item}</Tag>)
                      : <Tag color="green">无</Tag>}
                  </Space>
                  );
                },
              },
              { title: "说明", dataIndex: "eligibility_reason", render: (value) => value || "-" },
            ]}
          />
        </Card>
      )}

      {pythonScenarioProfiles.length > 0 && (
        <Card title="Python 场景证据">
          {pythonScenarioProfiles.map((profile, index) => {
            const validity = profile.evidence_validity || {};
            const adapter = profile.adapter || {};
            const sources = asArray(adapter.sources);
            const lineCandidates = asArray(profile.line_candidates);
            const scenarioRows = [
              ...asArray(profile.wait_sites).map((item) => ({ ...item, kind: "等待点" })),
              ...asArray(profile.exception_clusters).map((item) => ({
                ...item,
                kind: "异常簇",
                function: item.throw_site?.function,
                file: item.throw_site?.file,
                line: item.throw_site?.line,
                samples: item.occurrence_count,
              })),
              ...asArray(profile.slow_task_candidates).map((item) => ({ ...item, kind: "慢任务", function: item.task_name, samples: item.duration_ms })),
              ...asArray(profile.active_tasks).map((item) => ({ ...item, kind: "活跃任务", function: item.task_name, samples: item.duration_ms })),
              ...asArray(profile.acquire_sites).map((item) => ({ ...item, kind: "获取点" })),
              ...asArray(profile.retry_clusters).map((item) => ({ ...item, kind: "重试点" })),
              ...asArray(profile.timeout_sites).map((item) => ({ ...item, kind: "超时点" })),
            ].slice(0, 12);
            return (
              <Card key={`${profile.task_id || profile.scenario_type || "python-scenario"}-${index}`} size="small" type="inner" style={{ marginBottom: 12 }}>
                <Descriptions size="small" bordered column={{ xs: 1, md: 3 }}>
                  <Descriptions.Item label="场景">{profile.scenario_type || profile.collector_family}</Descriptions.Item>
                  <Descriptions.Item label="证据状态">
                    <Tag color={validity.evidence_status === "valid" ? "green" : validity.evidence_status === "partial" ? "blue" : validity.evidence_status === "empty_window" ? "orange" : "red"}>
                      {validity.evidence_status || profile.evidence_status || "unknown"}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="来源策略">{adapter.source_policy || "unknown"}</Descriptions.Item>
                  <Descriptions.Item label="工业来源" span={3}>
                    <Space wrap>
                      {sources.length > 0
                        ? sources.map((source, sourceIndex) => (
                          <Tag key={`${source.source_kind || source.kind}-${sourceIndex}`} color={source.source_status === "loaded" ? "blue" : "orange"}>
                            {source.source_kind || source.kind || "unknown"} / {source.record_count || 0}
                          </Tag>
                        ))
                        : <Tag color="red">无上游来源</Tag>}
                    </Space>
                  </Descriptions.Item>
                  <Descriptions.Item label="源码候选">{lineCandidates.length}</Descriptions.Item>
                  <Descriptions.Item label="结论资格">
                    <Tag color={profile.conclusion_eligible ? "green" : "gold"}>
                      {profile.conclusion_eligible ? "可进入结论" : "仅证据/门禁"}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="缺失门禁" span={3}>
                    <Space wrap>
                      {asArray(profile.missing_evidence).length > 0
                        ? asArray(profile.missing_evidence).map((item, itemIndex) => <Tag key={`${item}-${itemIndex}`} color="gold">{item}</Tag>)
                        : <Tag color="green">无</Tag>}
                    </Space>
                  </Descriptions.Item>
                  {profile.eligibility_reason && (
                    <Descriptions.Item label="门禁说明" span={3}>{profile.eligibility_reason}</Descriptions.Item>
                  )}
                </Descriptions>
                {scenarioRows.length > 0 && (
                  <Table
                    rowKey={(item, rowIndex) => item.evidence_ref || `${profile.scenario_type}-${rowIndex}`}
                    size="small"
                    pagination={false}
                    style={{ marginTop: 12 }}
                    dataSource={scenarioRows}
                    columns={[
                      { title: "类型", dataIndex: "kind", width: 90 },
                      { title: "函数/任务", dataIndex: "function", render: (value) => <Typography.Text code>{value || "unknown"}</Typography.Text> },
                      { title: "文件", dataIndex: "file", render: (value) => value || "-" },
                      { title: "行", dataIndex: "line", width: 80, render: (value) => value || "-" },
                      { title: "样本/次数", dataIndex: "samples", width: 100, render: (value) => value || "-" },
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
          <Card title="自动采集状态">
            {manualActionProbes.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message="以下项目需要人工处理或外部环境调整；普通已注册采集器会自动执行。"
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
                <List.Item>
                  <List.Item.Meta
                    title={<Space><Typography.Text>{item.probe_id}</Typography.Text><Tag color={item.risk_level === "R2" ? "orange" : "green"}>{item.risk_level}</Tag><Status value={item.status} /></Space>}
                    description={(
                      <Space direction="vertical" size={2}>
                        <Typography.Text type="secondary">{`${item.reason} · ${item.parameters?.duration_sec || 0}s`}</Typography.Text>
                        {item.parameters?.collector_fingerprint && (
                          <Typography.Text copyable type="secondary">fingerprint: {item.parameters.collector_fingerprint.slice(0, 24)}...</Typography.Text>
                        )}
                        {item.status === "WAITING_APPROVAL" && (
                          <Typography.Text type="warning">需要人工处理：当前策略或外部边界不允许自动执行。</Typography.Text>
                        )}
                        {item.status === "UNAVAILABLE" && (
                          <Typography.Text type="danger">需要人工处理：目标 Agent 未注册该采集器或当前离线。</Typography.Text>
                        )}
                      </Space>
                    )}
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
          items={asArray(detail.events).map((event, index) => ({
            key: `${event.event_type || "event"}-${event.to_status || "status"}-${index}`,
            color: event.to_status === "FAILED" ? "red" : "blue",
            children: <Space><Typography.Text>{event.event_type}</Typography.Text><Status value={event.to_status} /></Space>,
          }))}
        />
      </Card>

      <ChildTaskList taskIds={asArray(detail.child_task_ids)} />

            </Space>
          ),
        }]}
      />
    </Space>
  );
}

function countControlledTreeBranches(tree) {
  const initial = { primary: 0, secondary: 0, rejected: 0, unknown: 0 };
  const layers = asArray(tree?.layers);
  if (!layers.length) return initial;
  return layers.reduce((acc, layer = {}) => {
    const candidates = [
      ...asArray(layer.primary_causes),
      ...asArray(layer.secondary_causes),
      ...asArray(layer.rejected_causes),
      ...asArray(layer.unknown_causes),
    ];
    for (const candidate of candidates) {
      const rejected = candidate.status !== "forbidden" && (
        candidate.role === "rejected"
        || ["contradicted", "rejected"].includes(candidate.status)
        || candidate.causal_status === "contradicted"
      );
      if (rejected) acc.rejected += 1;
      else if (candidate.conclusion_eligible && candidate.role === "primary") acc.primary += 1;
      else if (candidate.conclusion_eligible && candidate.role === "secondary") acc.secondary += 1;
      else acc.unknown += 1;
    }
    return acc;
  }, initial);
}

function ChildTaskList({ taskIds }) {
  const safeTaskIds = useMemo(() => asArray(taskIds), [taskIds]);
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    if (!safeTaskIds.length) {
      setItems([]);
      return () => { cancelled = true; };
    }
    setLoading(true);
    Promise.allSettled(safeTaskIds.map((taskId) => getTask(taskId)))
      .then((results) => {
        if (cancelled) return;
        setItems(results.map((result, index) => (
          result.status === "fulfilled"
            ? result.value
            : { id: safeTaskIds[index], status: "LOAD_FAILED", load_error: result.reason?.message || "加载失败" }
        )));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [safeTaskIds]);

  return (
    <Card title={<Space>采集子任务 <Tag>{safeTaskIds.length}</Tag></Space>}>
      <Alert
        type="info"
        showIcon
        message="这些旧 task 只作为采集执行与产物明细存在；AI 诊断结论以上方会话和 AI 树为准。"
        style={{ marginBottom: 12 }}
      />
      <Table
        rowKey="id"
        loading={loading}
        size="small"
        pagination={{ pageSize: 6 }}
        scroll={{ x: 900 }}
        dataSource={items}
        locale={{ emptyText: "当前诊断还没有创建采集子任务" }}
        columns={[
          {
            title: "子任务",
            dataIndex: "id",
            width: 220,
            render: (value, record) => (
              <Space direction="vertical" size={0}>
                <Link to={`/task/${value}`}>{record.name || value}</Link>
                <Typography.Text copyable type="secondary" style={{ fontSize: 12 }}>
                  {value}
                </Typography.Text>
              </Space>
            ),
          },
          {
            title: "采集器",
            dataIndex: "collector_type",
            width: 150,
            render: (value) => value ? <Tag color="geekblue">{value}</Tag> : "-",
          },
          { title: "Agent", dataIndex: "agent_id", width: 160, ellipsis: true },
          { title: "PID", dataIndex: "target_pid", width: 90 },
          {
            title: "状态",
            dataIndex: "status",
            width: 130,
            render: (value) => <Tag color={value === "DONE" ? "green" : value === "FAILED" || value === "LOAD_FAILED" ? "red" : "blue"}>{value || "UNKNOWN"}</Tag>,
          },
          {
            title: "采样时长",
            dataIndex: "duration_sec",
            width: 100,
            render: (value) => value ? `${value}s` : "-",
          },
          {
            title: "创建时间",
            dataIndex: "created_at",
            width: 170,
            render: (value) => value ? new Date(value).toLocaleString() : "-",
          },
          {
            title: "说明",
            dataIndex: "load_error",
            ellipsis: true,
            render: (value, record) => value || record.error_message || record.request_params?.options?.probe_id || "-",
          },
        ]}
      />
    </Card>
  );
}
