import { useEffect, useState, useMemo, useCallback } from "react";
import {
  Button,
  Card,
  Col,
  Input,
  message,
  Popconfirm,
  Row,
  Select,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import {
  DeleteOutlined,
  ExperimentOutlined,
  FilterOutlined,
  ReloadOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import { Link } from "react-router-dom";
import { deleteDiagnosisSession, listDiagnosisSessions } from "../api/client";
import ErrorAlert from "../components/ErrorAlert";
import { COLORS, FONT_SIZES, SPACING } from "../theme";

const STATUS_COLORS = {
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

const TERMINAL = new Set([
  "COMPLETED",
  "PARTIAL_COMPLETED",
  "INSUFFICIENT_EVIDENCE",
  "FAILED",
  "BUDGET_EXHAUSTED",
  "TOPOLOGY_UNAVAILABLE",
  "USER_CANCELED",
]);

function latestConclusion(item) {
  const versions = item.conclusion_versions || [];
  return item.latest_conclusion || versions[versions.length - 1] || {};
}

function conclusionSummary(item) {
  const conclusion = latestConclusion(item);
  return conclusion.human_summary || conclusion.summary || item.raw_query || "-";
}

function primaryCause(item) {
  const conclusion = latestConclusion(item);
  const cause = conclusion.primary_cause || conclusion.ranked_causes?.[0] || {};
  return cause.title || cause.description || cause.candidate_id || "-";
}

export default function DiagnosisHistory() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [diagnoses, setDiagnoses] = useState([]);
  const [search, setSearch] = useState("");
  const [filterStatus, setFilterStatus] = useState("all");
  const [filterService, setFilterService] = useState("");

  const load = useCallback(async () => {
    setError("");
    setLoading(true);
    try {
      setDiagnoses(await listDiagnosisSessions({ limit: 300, summary: true }));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleDelete = useCallback(async (diagnosisId) => {
    try {
      await deleteDiagnosisSession(diagnosisId);
      message.success("诊断记录已删除");
      load();
    } catch (err) {
      message.error(err.message || "删除失败");
    }
  }, [load]);

  const filtered = useMemo(() => {
    let items = diagnoses;
    if (search.trim()) {
      const q = search.toLowerCase();
      items = items.filter((d) => [
        d.diagnosis_id,
        d.raw_query,
        d.target_scope?.target_service,
        d.normalized_intent?.symptom,
        conclusionSummary(d),
        primaryCause(d),
      ].filter(Boolean).join(" ").toLowerCase().includes(q));
    }
    if (filterStatus !== "all") {
      items = items.filter((d) => d.status === filterStatus);
    }
    if (filterService.trim()) {
      const q = filterService.trim().toLowerCase();
      items = items.filter((d) => (d.target_scope?.target_service || "").toLowerCase().includes(q));
    }
    return items;
  }, [diagnoses, search, filterStatus, filterService]);

  const statusOptions = useMemo(() => {
    const values = Array.from(new Set(diagnoses.map((item) => item.status).filter(Boolean))).sort();
    return [
      { value: "all", label: "全部状态" },
      ...values.map((value) => ({ value, label: value })),
    ];
  }, [diagnoses]);

  const columns = useMemo(
    () => [
      {
        title: "诊断 ID",
        dataIndex: "diagnosis_id",
        width: 180,
        ellipsis: true,
        render: (value) => (
          <Typography.Text copyable={{ text: value }} style={{ fontSize: FONT_SIZES.sm }}>
            {value?.slice(0, 28)}…
          </Typography.Text>
        ),
      },
      {
        title: "目标服务",
        width: 160,
        ellipsis: true,
        render: (_, record) => (
          <Link to={`/ai-diagnosis/${record.diagnosis_id}`}>
            {record.target_scope?.target_service || "未绑定服务"}
          </Link>
        ),
      },
      {
        title: "主因/停留层",
        width: 260,
        ellipsis: true,
        render: (_, record) => {
          const conclusion = latestConclusion(record);
          return (
            <Space direction="vertical" size={2}>
              <Typography.Text ellipsis style={{ maxWidth: 240 }}>
                {primaryCause(record)}
              </Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: FONT_SIZES.sm }}>
                {conclusion.supported_level || conclusion.max_supported_level || "unknown"}
              </Typography.Text>
            </Space>
          );
        },
      },
      {
        title: "摘要",
        key: "summary",
        ellipsis: true,
        render: (_, record) => conclusionSummary(record),
      },
      {
        title: "状态",
        dataIndex: "status",
        width: 150,
        render: (value) => <Tag color={STATUS_COLORS[value] || "default"}>{value || "UNKNOWN"}</Tag>,
      },
      {
        title: "采集子任务",
        width: 110,
        render: (_, record) => <Tag color="geekblue">{record.child_task_count ?? (record.child_task_ids?.length || 0)}</Tag>,
      },
      {
        title: "更新时间",
        dataIndex: "updated_at",
        width: 180,
        render: (value) => (value ? new Date(value).toLocaleString() : "-"),
      },
      {
        title: "操作",
        width: 150,
        fixed: "right",
        render: (_, record) => (
          <Space size={4}>
            <Button size="small" type="link">
              <Link to={`/ai-diagnosis/${record.diagnosis_id}`}>查看</Link>
            </Button>
            <Popconfirm
              title="删除这个诊断记录？"
              description="只删除 AI 树会话，不删除采集子任务。"
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              disabled={!TERMINAL.has(record.status)}
              onConfirm={() => handleDelete(record.diagnosis_id)}
            >
              <Button
                size="small"
                danger
                type="text"
                icon={<DeleteOutlined />}
                disabled={!TERMINAL.has(record.status)}
              />
            </Popconfirm>
          </Space>
        ),
      },
    ],
    [handleDelete]
  );

  const stats = useMemo(() => {
    const total = diagnoses.length;
    const done = diagnoses.filter((d) => d.status === "COMPLETED" || d.status === "PARTIAL_COMPLETED").length;
    const active = diagnoses.filter((d) => !TERMINAL.has(d.status)).length;
    const evidenceLimited = diagnoses.filter((d) => d.status === "INSUFFICIENT_EVIDENCE" || d.status === "BUDGET_EXHAUSTED").length;
    return { total, done, active, evidenceLimited };
  }, [diagnoses]);

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
          <Skeleton active paragraph={{ rows: 8 }} />
        </Card>
      </Space>
    );
  }

  return (
    <Space direction="vertical" size={SPACING.lg} style={{ width: "100%" }}>
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
          <ExperimentOutlined style={{ fontSize: 20, color: COLORS.primary }} />
          <Typography.Title level={4} style={{ margin: 0 }}>
            AI 集群诊断历史
          </Typography.Title>
        </Space>
        <Button icon={<ReloadOutlined />} onClick={load}>
          刷新
        </Button>
      </div>

      <ErrorAlert error={error} onClose={() => setError("")} />

      <Row gutter={SPACING.lg}>
        {[
          { label: "总会话", value: stats.total, color: COLORS.primary },
          { label: "已完成", value: stats.done, color: COLORS.success },
          { label: "诊断中", value: stats.active, color: COLORS.warning },
          { label: "证据受限", value: stats.evidenceLimited, color: COLORS.error },
        ].map((s) => (
          <Col xs={12} md={6} key={s.label}>
            <Card
              size="small"
              style={{ textAlign: "center" }}
              bodyStyle={{ padding: "12px 16px" }}
            >
              <Typography.Text type="secondary" style={{ fontSize: FONT_SIZES.sm }}>
                {s.label}
              </Typography.Text>
              <Typography.Title
                level={3}
                style={{ margin: "4px 0 0", fontSize: 28, color: s.color }}
              >
                {s.value}
              </Typography.Title>
            </Card>
          </Col>
        ))}
      </Row>

      <Card size="small" bodyStyle={{ padding: "12px 16px" }}>
        <Space wrap size="middle">
          <Input
            placeholder="搜索服务 / 诊断 ID / 主因 / 摘要…"
            prefix={<SearchOutlined style={{ color: COLORS.textSecondary }} />}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            allowClear
            style={{ width: 300 }}
          />
          <Select
            value={filterStatus}
            onChange={setFilterStatus}
            style={{ width: 170 }}
            options={statusOptions}
          />
          <Input
            placeholder="按服务筛选…"
            prefix={<FilterOutlined style={{ color: COLORS.textSecondary }} />}
            value={filterService}
            onChange={(e) => setFilterService(e.target.value)}
            allowClear
            style={{ width: 220 }}
          />
          <Tag>{filtered.length} / {diagnoses.length} 条</Tag>
        </Space>
      </Card>

      <Card size="small">
        <Table
          rowKey="diagnosis_id"
          columns={columns}
          dataSource={filtered}
          pagination={{ pageSize: 15, showSizeChanger: true, showTotal: (t) => `共 ${t} 条诊断` }}
          size="middle"
          scroll={{ x: 1250 }}
          locale={{ emptyText: "暂无 AI 集群诊断记录，请先创建一个 AI 诊断会话" }}
        />
      </Card>
    </Space>
  );
}
