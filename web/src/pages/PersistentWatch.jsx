import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  DeleteOutlined,
  EyeOutlined,
  FieldTimeOutlined,
  PlayCircleOutlined,
  RadarChartOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import {
  analyzeWatchIncident,
  createWatchSubscription,
  disableWatchSubscription,
  evaluateWatchSubscription,
  listAgentWatchLeases,
  listAgents,
  listWatchIncidents,
  listWatchSubscriptions,
} from "../api/client";
import ErrorAlert from "../components/ErrorAlert";
import usePolling from "../hooks/usePolling";
import { COLORS, FONT_SIZES, SPACING } from "../theme";

const DEFAULT_FORM = {
  watch_profile: "low_cost_default",
  trigger_action: "freeze_and_safe_probe",
  retention_seconds: 120,
  enabled_collectors: ["sys_metrics", "light_stack", "trace_window"],
};

function buildCpuShiftPayload() {
  const baselineStart = "2026-08-11T10:00:00Z";
  const triggerStart = "2026-08-11T10:05:00Z";
  return {
    baseline_window: {
      start: baselineStart,
      end: "2026-08-11T10:00:30Z",
      samples: [{ cpu_percent: 20 }, { cpu_percent: 21 }, { cpu_percent: 20 }],
    },
    trigger_window: {
      start: triggerStart,
      end: "2026-08-11T10:05:30Z",
      samples: [{ cpu_percent: 55 }, { cpu_percent: 56 }, { cpu_percent: 55 }],
    },
  };
}

export default function PersistentWatch() {
  const [form] = Form.useForm();
  const [agents, setAgents] = useState([]);
  const [watches, setWatches] = useState([]);
  const [incidentsByWatch, setIncidentsByWatch] = useState({});
  const [leases, setLeases] = useState([]);
  const [selectedAgentId, setSelectedAgentId] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [testingWatchId, setTestingWatchId] = useState("");
  const [analyzingIncidentId, setAnalyzingIncidentId] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setError("");
    try {
      const [agentItems, watchItems] = await Promise.all([
        listAgents(),
        listWatchSubscriptions(),
      ]);
      const nextWatches = watchItems || [];
      setAgents(agentItems || []);
      setWatches(nextWatches);
      const incidentPairs = await Promise.all(
        nextWatches.map(async (watch) => [watch.watch_id, await listWatchIncidents(watch.watch_id)])
      );
      setIncidentsByWatch(Object.fromEntries(incidentPairs));
      const nextAgentId = selectedAgentId || agentItems?.[0]?.id || "";
      if (!selectedAgentId && nextAgentId) setSelectedAgentId(nextAgentId);
      if (nextAgentId) {
        setLeases(await listAgentWatchLeases(nextAgentId));
      } else {
        setLeases([]);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [selectedAgentId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  usePolling(refresh, { interval: 10000, enabled: !loading });

  const stats = useMemo(() => ({
    active: watches.filter((watch) => watch.status === "active").length,
    triggered: Object.values(incidentsByWatch).reduce((sum, items) => sum + items.length, 0),
    leased: leases.length,
  }), [watches, leases, incidentsByWatch]);

  async function handleCreate(values) {
    setSubmitting(true);
    try {
      await createWatchSubscription({
        name: values.name,
        target: {
          agent_id: values.agent_id,
          target_pid: values.target_pid,
          service_id: values.service_id || null,
          instance_id: values.instance_id || null,
          endpoint: values.endpoint || null,
        },
        watch_profile: values.watch_profile,
        enabled_collectors: values.enabled_collectors,
        retention_seconds: values.retention_seconds,
        trigger_action: values.trigger_action,
        trigger_policy: "relative_shift_only",
      });
      message.success("监视订阅已创建");
      form.resetFields();
      refresh();
    } catch (err) {
      message.error(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDisable(watch) {
    Modal.confirm({
      title: "停用这个监视订阅？",
      content: "停用后 agent 不会再领取这个 watch lease，但历史触发信息仍保留在页面中。",
      okText: "停用",
      okType: "danger",
      cancelText: "取消",
      onOk: async () => {
        await disableWatchSubscription(watch.watch_id);
        message.success("监视订阅已停用");
        refresh();
      },
    });
  }

  async function handleTestTrigger(watch) {
    setTestingWatchId(watch.watch_id);
    try {
      await evaluateWatchSubscription(watch.watch_id, buildCpuShiftPayload());
      message.success("已提交测试窗口，若 Agent 支持对应能力会生成同窗采集任务");
      refresh();
    } catch (err) {
      message.error(err.message);
    } finally {
      setTestingWatchId("");
    }
  }

  async function handleAnalyzeIncident(incident) {
    setAnalyzingIncidentId(incident.incident_id);
    try {
      await analyzeWatchIncident(incident.incident_id);
      message.success("AI 树分析已完成");
      refresh();
    } catch (err) {
      message.error(err.message);
    } finally {
      setAnalyzingIncidentId("");
    }
  }

  const watchColumns = [
    {
      title: "监视对象",
      dataIndex: "name",
      render: (value, record) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{value}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: FONT_SIZES.sm }}>
            {record.target.service_id || "未绑定服务"} · PID {record.target.target_pid}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "Agent",
      dataIndex: ["target", "agent_id"],
      width: 160,
      render: (value) => <Tag color="blue">{value}</Tag>,
    },
    {
      title: "Profile",
      dataIndex: "watch_profile",
      width: 150,
      render: (value) => <Tag color={value === "resource_guarded" ? "gold" : "cyan"}>{value}</Tag>,
    },
    {
      title: "最近触发",
      width: 260,
      render: (_, record) => record.last_trigger_event_id ? (
        <Space direction="vertical" size={0}>
          <Typography.Text code>{record.last_trigger_event_id}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: FONT_SIZES.sm }}>
            {record.last_trigger_type} · {record.last_evidence_cohort_id || "无 cohort"}
          </Typography.Text>
        </Space>
      ) : (
        <Typography.Text type="secondary">尚未触发</Typography.Text>
      ),
    },
    {
      title: "异常窗口",
      width: 110,
      render: (_, record) => (
        <Tag color={(incidentsByWatch[record.watch_id] || []).length ? "orange" : "default"}>
          {(incidentsByWatch[record.watch_id] || []).length} 个
        </Tag>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 100,
      render: (value) => <Tag color={value === "active" ? "green" : "default"}>{value}</Tag>,
    },
    {
      title: "操作",
      width: 190,
      render: (_, record) => (
        <Space size={4}>
          <Button
            size="small"
            icon={<PlayCircleOutlined />}
            loading={testingWatchId === record.watch_id}
            disabled={record.status !== "active"}
            onClick={() => handleTestTrigger(record)}
          >
            测试触发
          </Button>
          <Button
            danger
            size="small"
            icon={<DeleteOutlined />}
            disabled={record.status !== "active"}
            onClick={() => handleDisable(record)}
          />
        </Space>
      ),
    },
  ];

  const incidentColumns = [
    {
      title: "异常窗口",
      dataIndex: "incident_id",
      render: (value, record) => (
        <Space direction="vertical" size={0}>
          <Typography.Text code>{value}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: FONT_SIZES.sm }}>
            {record.trigger_type} · {new Date(record.trigger_observed_at).toLocaleString()}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "证据 Cohort",
      width: 230,
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <Typography.Text code>{record.evidence_cohort_id}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: FONT_SIZES.sm }}>
            snapshot: {record.snapshot_id || "未冻结"}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "冻结证据",
      width: 180,
      render: (_, record) => record.snapshot_refs?.length ? (
        <Space size={[4, 4]} wrap>
          {record.snapshot_refs.map((ref) => (
            <Tag key={ref.evidence_ref || ref.artifact_type}>{ref.artifact_type}</Tag>
          ))}
        </Space>
      ) : (
        <Typography.Text type="secondary">暂无 refs</Typography.Text>
      ),
    },
    {
      title: "采集任务",
      width: 160,
      render: (_, record) => record.collector_tasks?.length ? (
        <Space size={[4, 4]} wrap>
          {record.collector_tasks.map((task) => (
            <Tag color="blue" key={task.task_id}>{task.probe_id}</Tag>
          ))}
        </Space>
      ) : (
        <Tag>仅冻结</Tag>
      ),
    },
    {
      title: "状态",
      width: 240,
      render: (_, record) => (
        <Space direction="vertical" size={2}>
          <Space size={4}>
            <Tag color={record.status === "collecting" ? "processing" : "gold"}>{record.status}</Tag>
            <Tag color={record.analysis_status === "analyzed" ? "green" : record.analysis_status === "needs_evidence" ? "orange" : "default"}>
              {record.analysis_status}
            </Tag>
          </Space>
          {record.analysis_result?.summary && (
            <Typography.Text type="secondary" style={{ fontSize: FONT_SIZES.sm }}>
              {record.analysis_result.summary}
            </Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: "操作",
      width: 140,
      render: (_, record) => (
        <Tooltip title="使用这个 incident 的 same-window frozen snapshot 进入 AI 树分析">
          <Button
            size="small"
            loading={analyzingIncidentId === record.incident_id}
            disabled={record.analysis_status === "analyzing"}
            onClick={() => handleAnalyzeIncident(record)}
          >
            AI 树分析
          </Button>
        </Tooltip>
      ),
    },
  ];

  const leaseColumns = [
    {
      title: "Watch",
      dataIndex: "watch_id",
      render: (value) => <Typography.Text code>{value}</Typography.Text>,
    },
    {
      title: "Target",
      render: (_, record) => (
        <Typography.Text>
          PID {record.target.target_pid}
          {record.target.service_id ? ` · ${record.target.service_id}` : ""}
        </Typography.Text>
      ),
    },
    {
      title: "采样节奏",
      width: 120,
      render: (_, record) => `${record.poll_interval_seconds}s`,
    },
    {
      title: "保留窗口",
      width: 120,
      render: (_, record) => `${record.retention_seconds}s`,
    },
  ];

  return (
    <Space direction="vertical" size={SPACING.lg} style={{ width: "100%" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <Space align="center">
          <RadarChartOutlined style={{ fontSize: 22, color: COLORS.primary }} />
          <div>
            <Typography.Title level={4} style={{ margin: 0 }}>
              持续监视
            </Typography.Title>
            <Typography.Text type="secondary">
              创建 watch subscription，让 agent 明确知道自己该观察哪个目标。
            </Typography.Text>
          </div>
        </Space>
        <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
      </div>

      <Alert
        type="info"
        showIcon
        message="Watch 只负责证据窗口，不发表根因结论"
        description="这里的触发表示某个时间窗口值得采证。真正归因仍然交给结构化证据层和 AI 树处理。"
      />

      <ErrorAlert error={error} onClose={() => setError("")} />

      <Row gutter={[SPACING.lg, SPACING.lg]}>
        <Col xs={24} md={8}>
          <Card size="small" style={{ borderLeft: `3px solid ${COLORS.primary}` }}>
            <Statistic title="Active watches" value={stats.active} prefix={<EyeOutlined />} />
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card size="small" style={{ borderLeft: `3px solid ${COLORS.success}` }}>
            <Statistic title="Agent leases" value={stats.leased} prefix={<FieldTimeOutlined />} />
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card size="small" style={{ borderLeft: `3px solid ${COLORS.warning}` }}>
            <Statistic title="Triggered windows" value={stats.triggered} prefix={<RadarChartOutlined />} />
          </Card>
        </Col>
      </Row>

      <Card title="创建监视订阅" size="small">
        <Form
          form={form}
          layout="vertical"
          initialValues={DEFAULT_FORM}
          onFinish={handleCreate}
        >
          <Row gutter={SPACING.lg}>
            <Col xs={24} md={8}>
              <Form.Item name="name" label="订阅名称" rules={[{ required: true, message: "请输入订阅名称" }]}>
                <Input placeholder="order service watch" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="agent_id" label="Agent" rules={[{ required: true, message: "请选择 Agent" }]}>
                <Select placeholder="选择 agent">
                  {agents.map((agent) => (
                    <Select.Option key={agent.id} value={agent.id}>
                      {agent.id} · {agent.status}
                    </Select.Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="target_pid" label="目标 PID" rules={[{ required: true, message: "请输入 PID" }]}>
                <InputNumber min={1} max={4194304} style={{ width: "100%" }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="service_id" label="Service">
                <Input placeholder="order-service" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="instance_id" label="Instance">
                <Input placeholder="order-1" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="endpoint" label="Endpoint">
                <Input placeholder="/orders" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="watch_profile" label="Watch profile">
                <Select>
                  <Select.Option value="low_cost_default">low_cost_default</Select.Option>
                  <Select.Option value="latency_sensitive">latency_sensitive</Select.Option>
                  <Select.Option value="resource_guarded">resource_guarded</Select.Option>
                </Select>
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="trigger_action" label="触发动作">
                <Select>
                  <Select.Option value="freeze_and_safe_probe">先冻结 + 安全短采</Select.Option>
                  <Select.Option value="freeze_only">只冻结现场</Select.Option>
                  <Select.Option value="auto_all_registered">自动执行全部已注册采集</Select.Option>
                  <Select.Option value="manual_approval">冻结后人工确认</Select.Option>
                </Select>
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="retention_seconds" label="保留窗口">
                <InputNumber min={30} max={1800} addonAfter="秒" style={{ width: "100%" }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="enabled_collectors" label="低成本观察族">
                <Select mode="tags" tokenSeparators={[","]} />
              </Form.Item>
            </Col>
          </Row>
          <Button type="primary" htmlType="submit" loading={submitting}>
            创建 watch
          </Button>
        </Form>
      </Card>

      <Card title={<Space>Watch 列表<Tag>{watches.length}</Tag></Space>} size="small">
        <Table
          rowKey="watch_id"
          columns={watchColumns}
          dataSource={watches}
          loading={loading}
          pagination={{ pageSize: 6 }}
          scroll={{ x: 980 }}
          expandable={{
            expandedRowRender: (record) => (
              <Table
                rowKey="incident_id"
                columns={incidentColumns}
                dataSource={incidentsByWatch[record.watch_id] || []}
                pagination={false}
                size="small"
                scroll={{ x: 980 }}
                locale={{ emptyText: <Empty description="这个监视对象还没有冻结异常窗口" /> }}
              />
            ),
            rowExpandable: () => true,
          }}
          locale={{ emptyText: <Empty description="暂无 watch subscription" /> }}
        />
      </Card>

      <Card
        title="Agent 当前领取的 WatchLease"
        size="small"
        extra={
          <Select
            size="small"
            style={{ width: 220 }}
            value={selectedAgentId || undefined}
            placeholder="选择 agent"
            onChange={setSelectedAgentId}
          >
            {agents.map((agent) => (
              <Select.Option key={agent.id} value={agent.id}>
                {agent.id}
              </Select.Option>
            ))}
          </Select>
        }
      >
        <Table
          rowKey="watch_id"
          columns={leaseColumns}
          dataSource={leases}
          pagination={false}
          size="middle"
          locale={{ emptyText: <Empty description="该 agent 当前没有 active watch lease" /> }}
        />
      </Card>
    </Space>
  );
}
