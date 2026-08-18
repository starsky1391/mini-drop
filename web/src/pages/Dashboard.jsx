import { useEffect, useState, useMemo, useCallback } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Input,
  message,
  notification,
  Popconfirm,
  Row,
  Select,
  Skeleton,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";
import {
  ApiOutlined,
  ReloadOutlined,
  DashboardOutlined,
  CloudServerOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  SyncOutlined,
  ClockCircleOutlined,
  DeleteOutlined,
  ExperimentOutlined,
  SearchOutlined,
  SortAscendingOutlined,
  ThunderboltOutlined,
  HddOutlined,
} from "@ant-design/icons";
import { Link, useNavigate } from "react-router-dom";
import { deleteDiagnosisSession, healthz, listAgents, listDiagnosisSessions } from "../api/client";
import ErrorAlert from "../components/ErrorAlert";
import StatusTag from "../components/StatusTag";
import usePolling from "../hooks/usePolling";
import useSSE from "../hooks/useSSE";
import { COLORS, FONT_SIZES, SPACING } from "../theme";

// ── 通知列表（最近 5 条 toast 通知）──────────────────────

const RECENT_KEYS = new Set();
const MAX_NOTIFICATIONS = 5;
const SESSION_ACTIVE_STATUSES = new Set([
  "UNDERSTANDING",
  "PLANNING",
  "COLLECTING",
  "ANALYZING",
  "CONCLUDING",
  "WAITING_APPROVAL",
  "NEEDS_SCOPE_CONFIRMATION",
]);
const SESSION_SUCCESS_STATUSES = new Set(["COMPLETED", "PARTIAL_COMPLETED"]);
const SESSION_FAILED_STATUSES = new Set([
  "FAILED",
  "BUDGET_EXHAUSTED",
  "TOPOLOGY_UNAVAILABLE",
  "USER_CANCELED",
]);
const SESSION_STATUS_COLORS = {
  COMPLETED: "green",
  PARTIAL_COMPLETED: "orange",
  INSUFFICIENT_EVIDENCE: "gold",
  FAILED: "red",
  BUDGET_EXHAUSTED: "red",
  WAITING_APPROVAL: "purple",
  COLLECTING: "blue",
  ANALYZING: "cyan",
  PLANNING: "geekblue",
  UNDERSTANDING: "geekblue",
  CONCLUDING: "cyan",
  NEEDS_SCOPE_CONFIRMATION: "orange",
  TOPOLOGY_UNAVAILABLE: "red",
  USER_CANCELED: "default",
};

function showEventNotification(eventType, data) {
  const key = `${eventType}-${data.task_id || data.agent_id || Date.now()}`;
  if (RECENT_KEYS.has(key)) return;
  RECENT_KEYS.add(key);
  if (RECENT_KEYS.size > MAX_NOTIFICATIONS) {
    const first = RECENT_KEYS.values().next().value;
    RECENT_KEYS.delete(first);
  }

  const messages = {
    task_changed: {
      title: `任务 ${data.task_id?.slice(0, 8)}…`,
      description: `${data.from_status || "?"} → ${data.to_status}`,
      icon: data.to_status === "DONE" ? <CheckCircleOutlined style={{ color: COLORS.success }} />
        : data.to_status === "FAILED" ? <CloseCircleOutlined style={{ color: COLORS.error }} />
        : <SyncOutlined spin style={{ color: COLORS.primary }} />,
    },
    agent_status: {
      title: `Agent ${data.agent_id}`,
      description: data.status === "ONLINE" ? "已上线" : "已离线",
      icon: data.status === "ONLINE"
        ? <CloudServerOutlined style={{ color: COLORS.success }} />
        : <CloudServerOutlined style={{ color: COLORS.error }} />,
    },
    diagnosis_complete: {
      title: `诊断 ${data.diagnosis_id?.slice(0, 8)}…`,
      description: data.status === "DONE" ? "诊断完成" : "诊断失败",
      icon: <ExperimentOutlined style={{ color: data.status === "DONE" ? COLORS.success : COLORS.error }} />,
    },
  };

  const cfg = messages[eventType];
  if (!cfg) return;

  notification.open({
    key,
    message: cfg.title,
    description: cfg.description,
    icon: cfg.icon,
    placement: "bottomRight",
    duration: 4,
    style: { borderRadius: 8 },
  });
}

// ── 组件 ──────────────────────────────────────────────────

export default function Dashboard() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [service, setService] = useState(null);
  const [sessions, setSessions] = useState([]);
  const [agents, setAgents] = useState([]);
  const [agentsLoaded, setAgentsLoaded] = useState(false);

  // ── 搜索 / 排序 ──────────────────────────────────────

  const [searchText, setSearchText] = useState("");
  const [sortBy, setSortBy] = useState("created_at");
  const [sortOrder, setSortOrder] = useState("desc");

  // ── 数据加载 ──────────────────────────────────────────

  const refresh = useCallback(async () => {
    setError("");
    try {
      const [healthRes, diagnosisRes, agentRes] = await Promise.allSettled([
        healthz(),
        listDiagnosisSessions({ limit: 100 }),
        listAgents(),
      ]);
      if (healthRes.status === "fulfilled") setService(healthRes.value);
      if (diagnosisRes.status === "fulfilled") setSessions(diagnosisRes.value || []);
      if (agentRes.status === "fulfilled") {
        setAgents(agentRes.value || []);
        setAgentsLoaded(true);
      } else {
        setAgentsLoaded(false);
      }
      const failures = [diagnosisRes, agentRes]
        .filter((item) => item.status === "rejected")
        .map((item) => item.reason?.message || "数据加载失败");
      if (failures.length) setError([...new Set(failures)].join("；"));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleDeleteSession = useCallback(async (diagnosisId) => {
    try {
      await deleteDiagnosisSession(diagnosisId);
      message.success("集合诊断记录已删除");
      refresh();
    } catch (err) {
      message.error(err.message || "删除失败");
    }
  }, [refresh]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // 每 10 秒自动轮询（SSE 断线时兜底）
  const { isPolling } = usePolling(refresh, { interval: 10000, enabled: !loading });

  // ── SSE 实时事件 ──────────────────────────────────────

  useSSE({
    onTaskChanged(data) {
      showEventNotification("task_changed", data);
      refresh(); // 事件到达后刷新数据
    },
    onAgentStatus(data) {
      showEventNotification("agent_status", data);
      refresh();
    },
    onDiagnosisComplete(data) {
      showEventNotification("diagnosis_complete", data);
      refresh();
    },
  });

  // ── 统计 ──────────────────────────────────────────────

  const stats = useMemo(() => {
    const doneCount = sessions.filter((item) => SESSION_SUCCESS_STATUSES.has(item.status)).length;
    const failedCount = sessions.filter((item) => SESSION_FAILED_STATUSES.has(item.status)).length;
    const activeCount = sessions.filter((item) => SESSION_ACTIVE_STATUSES.has(item.status)).length;
    const onlineCount = agents.filter((a) => a.status === "ONLINE").length;
    const offlineCount = agents.filter((a) => a.status === "OFFLINE").length;
    const successRate = sessions.length > 0
      ? Math.round((doneCount / (doneCount + failedCount || 1)) * 100)
      : 100;

    return {
      total: sessions.length,
      doneCount,
      failedCount,
      activeCount,
      onlineCount,
      offlineCount,
      successRate,
    };
  }, [sessions, agents]);

  // ── 最新集合任务 ──────────────────────────────────────

  const recentDone = useMemo(
    () => sessions.filter((item) => SESSION_SUCCESS_STATUSES.has(item.status)).slice(0, 3),
    [sessions]
  );

  const visibleSessions = useMemo(() => {
    const keyword = searchText.trim().toLowerCase();
    const filtered = sessions.filter((item) => {
      if (!keyword) return true;
      const haystack = [
        item.diagnosis_id,
        item.raw_query,
        item.target_scope?.target_service,
        item.normalized_intent?.symptom,
        item.status,
      ].filter(Boolean).join(" ").toLowerCase();
      return haystack.includes(keyword);
    });
    const sorted = [...filtered].sort((a, b) => {
      const valueOf = (item) => {
        if (sortBy === "target_service") return item.target_scope?.target_service || "";
        if (sortBy === "status") return item.status || "";
        if (sortBy === "updated_at") return item.updated_at || "";
        return item.created_at || "";
      };
      const av = valueOf(a);
      const bv = valueOf(b);
      if (av === bv) return 0;
      return sortOrder === "asc" ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
    });
    return sorted;
  }, [sessions, searchText, sortBy, sortOrder]);

  const totalChildTasks = useMemo(
    () => sessions.reduce((sum, item) => sum + (item.child_task_ids?.length || 0), 0),
    [sessions],
  );

  // ── 表格列 ────────────────────────────────────────────

  const sessionColumns = useMemo(
    () => [
      {
        title: "集合任务",
        dataIndex: "raw_query",
        ellipsis: true,
        render: (value, record) => (
          <Space direction="vertical" size={2}>
            <Space size={6} wrap>
              <Link to={`/ai-diagnosis/${record.diagnosis_id}`}>
                {record.target_scope?.target_service || "未绑定服务"}
              </Link>
              <Tag color="purple">AI树</Tag>
            </Space>
            <Typography.Text type="secondary" ellipsis style={{ maxWidth: 420 }}>
              {value || record.diagnosis_id}
            </Typography.Text>
          </Space>
        ),
      },
      {
        title: "状态",
        dataIndex: "status",
        width: 140,
        render: (value) => <Tag color={SESSION_STATUS_COLORS[value] || "default"}>{value || "UNKNOWN"}</Tag>,
      },
      {
        title: "症状",
        width: 150,
        render: (_, record) => record.normalized_intent?.symptom || "-",
      },
      {
        title: "采集子任务",
        width: 110,
        render: (_, record) => <Tag color="geekblue">{record.child_task_ids?.length || 0}</Tag>,
      },
      {
        title: "创建时间",
        dataIndex: "created_at",
        width: 170,
        render: (v) => (v ? new Date(v).toLocaleString() : "-"),
      },
      {
        title: "更新时间",
        dataIndex: "updated_at",
        width: 170,
        render: (v) => (v ? new Date(v).toLocaleString() : "-"),
      },
      {
        title: "操作",
        width: 130,
        fixed: "right",
        render: (_, record) => (
          <Space size={4}>
            <Button size="small" type="link" onClick={() => navigate(`/ai-diagnosis/${record.diagnosis_id}`)}>
              查看
            </Button>
            <Popconfirm
              title="删除这个集合诊断记录？"
              description="只删除 AI 树会话记录，不删除采集子任务。"
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => handleDeleteSession(record.diagnosis_id)}
            >
              <Button size="small" danger type="text" icon={<DeleteOutlined />} />
            </Popconfirm>
          </Space>
        ),
      },
    ],
    [handleDeleteSession, navigate]
  );

  const agentColumns = useMemo(
    () => [
      {
        title: "Agent",
        dataIndex: "id",
        width: 180,
        ellipsis: true,
        render: (value) => (
          <Typography.Link
            onClick={() => navigate(`/agent/${value}`)}
            style={{ cursor: "pointer", fontSize: FONT_SIZES.sm }}
          >
            {value}
          </Typography.Link>
        ),
      },
      { title: "Host", dataIndex: "hostname", width: 140, ellipsis: true },
      { title: "IP", dataIndex: "ip_addr", width: 140 },
      {
        title: "CPU",
        width: 70,
        render: (_, record) =>
          `${record.latest_metrics?.self?.cpu_percent ?? 0}%`,
      },
      {
        title: "RSS",
        width: 90,
        render: (_, record) =>
          `${(record.latest_metrics?.self?.rss_mb ?? 0).toFixed(1)} MB`,
      },
      {
        title: "IO R/W",
        width: 100,
        render: (_, record) => {
          const s = record.latest_metrics?.self || {};
          return `${s.read_kb_s ?? 0}/${s.write_kb_s ?? 0}`;
        },
      },
      {
        title: "采集能力",
        width: 120,
        render: (_, record) => {
          const summary = record.collector_profile?.summary || record.latest_metrics?.collector_profile?.summary;
          if (!summary) return <Tag>未知</Tag>;
          const degraded = summary.degraded || 0;
          const unavailable = summary.unavailable || 0;
          if (unavailable > 0) return <Tag color="red">缺失 {unavailable}</Tag>;
          if (degraded > 0) return <Tag color="orange">降级 {degraded}</Tag>;
          return <Tag color="green">可用</Tag>;
        },
      },
      {
        title: "状态",
        dataIndex: "status",
        width: 100,
        render: (value) => <StatusTag status={value} />,
      },
    ],
    [navigate]
  );

  // ── 加载骨架屏 ────────────────────────────────────────

  if (loading) {
    return (
      <Space direction="vertical" size={SPACING.lg} style={{ width: "100%" }}>
        <Skeleton.Input active size="small" style={{ width: 160 }} />
        <Row gutter={SPACING.lg}>
          {[1, 2, 3, 4].map((i) => (
            <Col xs={12} md={6} key={i}>
              <Card size="small">
                <Skeleton active paragraph={{ rows: 1 }} />
              </Card>
            </Col>
          ))}
        </Row>
        <Card size="small">
          <Skeleton active paragraph={{ rows: 5 }} />
        </Card>
        <Card size="small">
          <Skeleton active paragraph={{ rows: 3 }} />
        </Card>
      </Space>
    );
  }

  return (
    <Space direction="vertical" size={SPACING.lg} style={{ width: "100%" }}>
      {/* ── 页头 ──────────────────────────────────────────── */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          flexWrap: "wrap",
          gap: 8,
        }}
      >
        <Space align="center">
          <DashboardOutlined style={{ fontSize: 20, color: COLORS.primary }} />
          <Typography.Title level={4} style={{ margin: 0 }}>
            诊断面板
          </Typography.Title>
        </Space>
        <Space size="small">
          {isPolling && (
            <Tag icon={<SyncOutlined spin />} color="processing">
              10s 自动刷新
            </Tag>
          )}
          <Button icon={<ReloadOutlined />} onClick={refresh}>
            刷新
          </Button>
        </Space>
      </div>

      <Alert
        type="info"
        showIcon
        message="首页现在展示最新集合诊断任务；采集子任务仍在后台执行，只在诊断详情底部作为明细查看。"
        action={
          <Button size="small" type="primary" onClick={() => navigate("/ai-diagnosis")}>
            创建 AI 诊断
          </Button>
        }
      />

      <ErrorAlert error={error} onClose={() => setError("")} />

      {/* ── 统计卡片组 ────────────────────────────────────── */}
      <Row gutter={[SPACING.lg, SPACING.lg]}>
        {/* 服务 */}
        <Col xs={12} md={6}>
          <Card
            size="small"
            bodyStyle={{ padding: "14px 18px" }}
            style={{ borderLeft: `3px solid ${COLORS.primary}` }}
          >
            <Statistic
              title={
                <Space size={4}>
                  <ApiOutlined style={{ color: COLORS.primary, fontSize: 14 }} />
                  <Typography.Text style={{ fontSize: FONT_SIZES.sm, color: COLORS.textSecondary }}>
                    服务版本
                  </Typography.Text>
                </Space>
              }
              value={service?.version || "0.1.0"}
              valueStyle={{ fontSize: 22, color: COLORS.primary }}
            />
          </Card>
        </Col>

        {/* Agent 在线 */}
        <Col xs={12} md={6}>
          <Card
            size="small"
            bodyStyle={{ padding: "14px 18px" }}
            style={{ borderLeft: `3px solid ${COLORS.success}` }}
          >
            <Statistic
              title={
                <Space size={4}>
                  <CloudServerOutlined style={{ color: COLORS.success, fontSize: 14 }} />
                  <Typography.Text style={{ fontSize: FONT_SIZES.sm, color: COLORS.textSecondary }}>
                    Agent 在线
                  </Typography.Text>
                </Space>
              }
              value={agentsLoaded ? stats.onlineCount : "—"}
              suffix={
                <Typography.Text style={{ fontSize: FONT_SIZES.sm, color: COLORS.textSecondary }}>
                  {agentsLoaded ? `/ ${stats.onlineCount + stats.offlineCount}` : "认证后显示"}
                </Typography.Text>
              }
              valueStyle={{ fontSize: 28, color: agentsLoaded && stats.onlineCount > 0 ? COLORS.success : COLORS.offline }}
            />
          </Card>
        </Col>

        {/* 任务统计 */}
        <Col xs={12} md={6}>
          <Card
            size="small"
            bodyStyle={{ padding: "14px 18px" }}
            style={{ borderLeft: `3px solid ${COLORS.warning}` }}
          >
            <Statistic
              title={
                <Space size={4}>
                    <ThunderboltOutlined style={{ color: COLORS.warning, fontSize: 14 }} />
                  <Typography.Text style={{ fontSize: FONT_SIZES.sm, color: COLORS.textSecondary }}>
                    诊断中
                  </Typography.Text>
                </Space>
              }
              value={stats.activeCount}
              suffix={
                <Typography.Text style={{ fontSize: FONT_SIZES.sm, color: COLORS.textSecondary }}>
                  / {stats.total}
                </Typography.Text>
              }
              valueStyle={{ fontSize: 28, color: stats.activeCount > 0 ? COLORS.warning : COLORS.textSecondary }}
            />
          </Card>
        </Col>

        {/* 成功率 */}
        <Col xs={12} md={6}>
          <Card
            size="small"
            bodyStyle={{ padding: "14px 18px" }}
            style={{ borderLeft: `3px solid ${stats.successRate >= 80 ? COLORS.success : stats.successRate >= 50 ? COLORS.warning : COLORS.error}` }}
          >
            <Statistic
              title={
                <Space size={4}>
                    <CheckCircleOutlined style={{ color: COLORS.success, fontSize: 14 }} />
                  <Typography.Text style={{ fontSize: FONT_SIZES.sm, color: COLORS.textSecondary }}>
                    完成率
                  </Typography.Text>
                </Space>
              }
              value={stats.successRate}
              suffix="%"
              valueStyle={{
                fontSize: 28,
                color: stats.successRate >= 80 ? COLORS.success : stats.successRate >= 50 ? COLORS.warning : COLORS.error,
              }}
            />
          </Card>
        </Col>
      </Row>

      {/* ── 子统计 ────────────────────────────────────────── */}
      <Row gutter={SPACING.lg}>
        <Col xs={24} md={12}>
          <Card size="small" bodyStyle={{ padding: "12px 16px" }}>
            <Space size={[8, 4]} wrap>
              <Typography.Text style={{ fontSize: FONT_SIZES.sm, color: COLORS.textSecondary }}>
                集合任务：
              </Typography.Text>
              <Tag icon={<CheckCircleOutlined />} color="green">
                {stats.doneCount} 完成
              </Tag>
              <Tag icon={<CloseCircleOutlined />} color="red">
                {stats.failedCount} 失败
              </Tag>
              <Tag icon={<SyncOutlined spin={stats.activeCount > 0} />} color="blue">
                {stats.activeCount} 进行中
              </Tag>
              <Tag icon={<ClockCircleOutlined />} color="default">
                {stats.total - stats.doneCount - stats.failedCount - stats.activeCount} 其他
              </Tag>
              <Tag color="geekblue">
                子任务 {totalChildTasks}
              </Tag>
            </Space>
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card size="small" bodyStyle={{ padding: "12px 16px" }}>
            <Space size={[8, 4]} wrap>
              <Typography.Text style={{ fontSize: FONT_SIZES.sm, color: COLORS.textSecondary }}>
                Agent 状态：
              </Typography.Text>
              {agentsLoaded ? (
                <>
                  <Badge status="success" text={`${stats.onlineCount} 在线`} />
                  <Badge status="default" text={`${stats.offlineCount} 离线`} />
                </>
              ) : (
                <Tag color="red">认证失败，状态未知</Tag>
              )}
              {recentDone.length > 0 && (
                <Tag icon={<ExperimentOutlined />} color="purple">
                  最近完成: {recentDone.map((item) => item.target_scope?.target_service || item.diagnosis_id?.slice(0, 8)).join(", ")}
                </Tag>
              )}
            </Space>
          </Card>
        </Col>
      </Row>

      {/* ── 集合任务列表 ──────────────────────────────────────── */}
      <Card
        title={
          <Space>
            <HddOutlined style={{ color: COLORS.primary }} />
            最新集合任务
            <Tag>{visibleSessions.length}</Tag>
          </Space>
        }
        size="small"
        extra={
          <Space size={8} wrap>
            <Input
              style={{ width: 180 }}
              size="small"
              placeholder="搜索服务 / 诊断描述…"
              prefix={<SearchOutlined />}
              allowClear
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
              onPressEnter={refresh}
            />
            <Select
              size="small"
              style={{ width: 110 }}
              value={sortBy}
              onChange={(v) => setSortBy(v)}
              suffixIcon={<SortAscendingOutlined />}
            >
              <Select.Option value="created_at">创建时间</Select.Option>
              <Select.Option value="updated_at">更新时间</Select.Option>
              <Select.Option value="target_service">目标服务</Select.Option>
              <Select.Option value="status">状态</Select.Option>
            </Select>
            <Select
              size="small"
              style={{ width: 80 }}
              value={sortOrder}
              onChange={(v) => setSortOrder(v)}
            >
              <Select.Option value="desc">↓ 降序</Select.Option>
              <Select.Option value="asc">↑ 升序</Select.Option>
            </Select>
            <Button size="small" type="link" onClick={() => navigate("/ai-diagnosis")}>
              <ExperimentOutlined /> AI 诊断
            </Button>
          </Space>
        }
      >
        <Table
          rowKey="diagnosis_id"
          columns={sessionColumns}
          dataSource={visibleSessions}
          pagination={{ pageSize: 8, showSizeChanger: true, showTotal: (t) => `共 ${t} 条` }}
          size="middle"
          scroll={{ x: 880 }}
          locale={{ emptyText: "暂无集合诊断任务，请创建一个 AI 诊断会话" }}
        />
      </Card>

      {/* ── Agent 列表 ─────────────────────────────────────── */}
      <Card
        title={
          <Space>
            <CloudServerOutlined style={{ color: COLORS.success }} />
            Agent 列表
            <Tag>{agentsLoaded ? agents.length : "未知"}</Tag>
          </Space>
        }
        size="small"
      >
        <Table
          rowKey="id"
          columns={agentColumns}
          dataSource={agents}
          pagination={false}
          size="middle"
          scroll={{ x: 800 }}
          locale={{ emptyText: agentsLoaded ? "暂无 Agent 注册，请在目标主机上启动 Agent" : "认证后加载 Agent 数据" }}
        />
      </Card>
    </Space>
  );
}
