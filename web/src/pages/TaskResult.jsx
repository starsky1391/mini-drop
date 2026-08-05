import { useEffect, useState, useRef, useCallback } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Empty,
  message,
  Progress,
  Row,
  Select,
  Skeleton,
  Space,
  Spin,
  Table,
  Tag,
  Timeline,
  Tooltip,
  Typography,
} from "antd";
import {
  ArrowLeftOutlined,
  BarChartOutlined,
  DownloadOutlined,
  ExperimentOutlined,
  FileTextOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { useParams, useNavigate } from "react-router-dom";
import {
  downloadTaskArtifact,
  getDiagnosis,
  getTask,
  getTaskArtifactContent,
  getTaskArtifacts,
  getTaskEvents,
  listTaskDiagnoses,
  submitDiagnosisFeedback,
  triggerDiagnose,
} from "../api/client";
import FlamegraphViewer from "../components/FlamegraphViewer";
import TopNChart from "../components/TopNChart";
import EBPFHistogram from "../components/EBPFHistogram";
import StatusTag from "../components/StatusTag";
import ErrorAlert from "../components/ErrorAlert";
import usePolling from "../hooks/usePolling";
import { isTaskActive } from "../utils/status";
import { COLORS, SPACING } from "../theme";
import styles from "./TaskResult.module.css";

export default function TaskResult() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const flameRef = useRef(null);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [task, setTask] = useState(null);
  const [events, setEvents] = useState([]);
  const [artifacts, setArtifacts] = useState([]);
  const [diagnoses, setDiagnoses] = useState([]);
  const [diagnosis, setDiagnosis] = useState(null);
  const [diagnosing, setDiagnosing] = useState(false);
  const [analysisStrategy, setAnalysisStrategy] = useState("linear");
  const [analysisPipeline, setAnalysisPipeline] = useState("evidence_to_attribution");
  const [analysis, setAnalysis] = useState({ top: [], svg: "", hasFlameJson: false });
  const [analysisLoading, setAnalysisLoading] = useState(true);
  const [selectedContinuousIndex, setSelectedContinuousIndex] = useState(null);
  const [downloadingArtifact, setDownloadingArtifact] = useState("");

  // ── 数据加载 ──────────────────────────────────────────

  const loadAll = useCallback(async () => {
    setError("");
    try {
      const results = await Promise.allSettled([
        getTask(taskId),
        getTaskEvents(taskId),
        getTaskArtifacts(taskId),
        listTaskDiagnoses(taskId),
      ]);
      const [taskResp, eventResp, artifactResp, diagnosisList] = results.map(
        (r) => (r.status === "fulfilled" ? r.value : null)
      );
      const failedNames = ["task", "events", "artifacts", "diagnoses"].filter((_, i) => results[i].status === "rejected");
      if (failedNames.length > 0 && failedNames.length < 4) {
        console.warn("部分数据加载失败:", failedNames.join(", "));
      }
      if (!taskResp) {
        setError("无法加载任务数据");
        setLoading(false);
        return;
      }
      setTask(taskResp);
      setEvents(eventResp || []);
      setArtifacts(artifactResp || []);

      // 内联加载分析产物
      const resp = artifactResp || [];
      setAnalysisLoading(true);
      const hasTop = resp.some((item) => item.artifact_type === "top_json");
      const hasFlameJson = resp.some((item) => item.artifact_type === "flamegraph_json");
      const hasSvg = resp.some((item) => item.artifact_type === "flamegraph_svg");
      const hasJavaHtml = resp.some((item) => item.artifact_type === "java_flamegraph_html");
      const next = { top: [], svg: "", hasFlameJson: false, hasJavaHtml: false };
      if (hasTop) {
        try { next.top = await getTaskArtifactContent(taskId, "top_json"); } catch { next.top = []; }
      }
      if (hasFlameJson) { next.hasFlameJson = true; }
      if (hasSvg && !hasFlameJson) {
        try { const c = await getTaskArtifactContent(taskId, "flamegraph_svg"); next.svg = c.text || ""; } catch { next.svg = ""; }
      }
      if (hasJavaHtml) { next.hasJavaHtml = true; }
      setAnalysis(next);
      setAnalysisLoading(false);

      setDiagnoses(diagnosisList || []);
      if (diagnosisList?.[0]?.id) {
        try {
          setDiagnosis(await getDiagnosis(diagnosisList[0].id));
        } catch {
          setDiagnosis(null);
        }
      } else {
        setDiagnosis(null);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
      setAnalysisLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // 任务活跃时每 5 秒自动刷新
  const isActive = isTaskActive(task?.status);
  usePolling(loadAll, { interval: 5000, enabled: isActive });

  // ── 诊断操作 ──────────────────────────────────────────

  async function runDiagnosis() {
    setDiagnosing(true);
    setError("");
    try {
      const result = await triggerDiagnose(taskId, {
        analysis_strategy: analysisStrategy,
        analysis_pipeline: analysisPipeline,
      });
      const detail = await getDiagnosis(result.diagnosis_id);
      const list = await listTaskDiagnoses(taskId);
      setDiagnosis(detail);
      setDiagnoses(list || []);
      message.success(
        analysisPipeline === "legacy"
          ? "旧版假设验证诊断完成"
          : "Evidence-to-Attribution 诊断完成"
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setDiagnosing(false);
    }
  }

  async function sendFeedback(label, causeId) {
    if (!diagnosis?.run?.id) return;
    try {
      await submitDiagnosisFeedback(diagnosis.run.id, {
        predicted_cause_id: causeId || "insufficient_data",
        feedback_label: label,
      });
      message.success("反馈已记录");
    } catch (err) {
      setError(err.message);
    }
  }

  async function refreshDiagnosis() {
    if (!diagnosis?.run?.id) return;
    try {
      setDiagnosis(await getDiagnosis(diagnosis.run.id));
    } catch (err) {
      setError(err.message);
    }
  }

  // ── 产物提取 ──────────────────────────────────────────

  const report = diagnosis?.report?.report || {};
  const rankedCauses = diagnosis?.report?.ranked_causes || [];
  const repairPlan = diagnosis?.repair_plan;
  const toolResults = diagnosis?.tool_results || [];
  const structuredResult = report.analysis_result || {};
  const structuredBoundary = report.conclusion_boundary || structuredResult.conclusion_boundary || null;
  const structuredFacts = structuredResult.facts || [];
  const structuredSymptoms = structuredResult.symptoms || [];
  const structuredLocalizations = structuredResult.localizations || [];
  const structuredChallenges = structuredResult.evidence_challenges || [];
  const structuredPrimaryCauseId = report.primary_cause_id || structuredResult.primary_cause_id || "";
  const structuredStability = report.stability_score ?? structuredResult.stability_score ?? 0;
  const structuredPrimaryCauseReason = report.primary_cause_reason || structuredResult.primary_cause_reason || "";
  const structuredAllowedCauseIds = structuredResult.allowed_cause_ids || [];
  const topCause = rankedCauses[0];
  const topArtifact = artifacts.find((item) => item.artifact_type === "top_json");
  const flameArtifact = artifacts.find(
    (item) =>
      item.artifact_type === "flamegraph_svg" ||
      item.artifact_type === "flamegraph_json" ||
      item.artifact_type === "java_flamegraph_html"
  );
  const javaHtmlArtifact = artifacts.find((item) => item.artifact_type === "java_flamegraph_html");
  const memoryArtifact = artifacts.find((item) => item.artifact_type === "memory_json");
  const pprofArtifact = artifacts.find((item) => item.artifact_type === "pprof_raw");
  const sysMetricsArtifact = artifacts.find((item) => item.artifact_type === "sys_metrics");
  const ebpfArtifact = artifacts.find((item) => item.artifact_type === "ebpf_metrics");
  const suggestionArtifact = artifacts.find(
    (item) => item.artifact_type === "suggestions_md"
  );
  const continuousSummary = artifacts.find(
    (item) => item.artifact_type === "continuous_summary"
  );
  const continuousWindowArtifacts = artifacts.filter(
    (item) => item.artifact_type === "continuous_window"
  );
  const continuousWindows =
    continuousSummary?.metadata?.windows?.length > 0
      ? continuousSummary.metadata.windows
      : continuousWindowArtifacts
          .map((item, index) => ({
            window_index: item.metadata?.window_index ?? index,
            start_ts: item.metadata?.start_ts,
            end_ts: item.metadata?.end_ts,
            ok: item.metadata?.ok ?? true,
            reason: item.metadata?.reason || item.filename || item.object_key || "",
          }))
          .sort((a, b) => a.window_index - b.window_index);
  const continuousFlameArtifacts = artifacts.filter(
    (item) => item.artifact_type === "continuous_flamegraph_json"
  );
  const hasFlameOrTop = Boolean(flameArtifact || topArtifact);
  const hasContinuousAnalysis = continuousFlameArtifacts.length > 0;
  const hasPrimaryAnalysis = Boolean(hasFlameOrTop || ebpfArtifact || hasContinuousAnalysis);

  useEffect(() => {
    if (selectedContinuousIndex === null && continuousWindows.length > 0) {
      setSelectedContinuousIndex(continuousWindows[0].window_index);
    }
  }, [continuousWindows, selectedContinuousIndex]);

  async function downloadArtifact(record) {
    const key = `${record.artifact_type}:${record.metadata?.window_index ?? ""}`;
    setDownloadingArtifact(key);
    try {
      const params = record.metadata?.window_index === undefined
        ? {}
        : { index: record.metadata.window_index };
      const { blob, filename } = await downloadTaskArtifact(taskId, record.artifact_type, params);
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      message.error(`下载失败：${err.message}`);
    } finally {
      setDownloadingArtifact("");
    }
  }

  const artifactColumns = [
    {
      title: "类型",
      dataIndex: "artifact_type",
      width: 140,
      render: (value) => {
        const colors = {
          flamegraph_json: "blue",
          flamegraph_svg: "blue",
          java_flamegraph_html: "magenta",
          top_json: "green",
          suggestions_md: "orange",
          memory_json: "volcano",
          pprof_raw: "geekblue",
          ebpf_metrics: "green",
          ebpf_raw: "lime",
          raw: "default",
          continuous_window: "cyan",
          continuous_summary: "cyan",
        };
        return <Tag color={colors[value] || "default"}>{value}</Tag>;
      },
    },
    {
      title: "文件",
      dataIndex: "filename",
      ellipsis: true,
      render: (value, record) =>
        value || record.object_key || record.local_path || "-",
    },
    {
      title: "大小",
      dataIndex: "size_bytes",
      width: 100,
      render: (v) => (v ? `${(v / 1024).toFixed(1)} KB` : "-"),
    },
    {
      title: "操作",
      width: 100,
      render: (_, record) => {
        const key = `${record.artifact_type}:${record.metadata?.window_index ?? ""}`;
        return (
          <Button
            type="link"
            size="small"
            icon={<DownloadOutlined />}
            loading={downloadingArtifact === key}
            onClick={() => downloadArtifact(record)}
          >
            下载
          </Button>
        );
      },
    },
  ];

  // ── 加载骨架屏 ────────────────────────────────────────

  const FLAMEGRAPH_HEIGHT = 360;

  if (loading) {
    return (
      <Space direction="vertical" size={SPACING.lg} style={{ width: "100%" }}>
        <Skeleton.Input active size="small" style={{ width: 200 }} />
        <div className={styles.skeletonCard}>
          <Skeleton active paragraph={{ rows: 4 }} />
        </div>
        <div className={styles.skeletonCard}>
          <Skeleton active paragraph={{ rows: 3 }} />
        </div>
        <div className={styles.skeletonCard}>
          <Skeleton.Input active block style={{ height: FLAMEGRAPH_HEIGHT, borderRadius: 8 }} />
        </div>
      </Space>
    );
  }

  // ── 主渲染 ────────────────────────────────────────────

  return (
    <Space direction="vertical" size={SPACING.lg} style={{ width: "100%" }}>
      {/* 页面标题 + 返回 + 自动刷新指示 */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        <Space align="center">
          <Button
            icon={<ArrowLeftOutlined />}
            type="text"
            onClick={() => navigate("/")}
          >
            返回任务面板
          </Button>
          <Typography.Title level={4} style={{ margin: 0 }}>
            任务详情
          </Typography.Title>
        </Space>
        {isActive && (
          <Tag color="blue">自动刷新中（任务运行中）</Tag>
        )}
      </div>

      <ErrorAlert error={error} style={{ marginBottom: 0 }} onClose={() => setError("")} />

      {/* 任务基本信息 */}
      {task && (
        <Card size="small">
          <Descriptions column={{ xs: 1, sm: 2, md: 2, lg: 4 }} size="small">
            <Descriptions.Item label="任务 ID">
              <Typography.Text copyable={{ text: task.id }} style={{ fontSize: 12 }}>
                {task.id}
              </Typography.Text>
            </Descriptions.Item>
            <Descriptions.Item label="状态">
              <StatusTag status={task.status} />
            </Descriptions.Item>
            <Descriptions.Item label="名称">{task.name}</Descriptions.Item>
            <Descriptions.Item label="Agent">{task.agent_id}</Descriptions.Item>
            <Descriptions.Item label="PID">{task.target_pid}</Descriptions.Item>
            <Descriptions.Item label="采集器">
              <Tag>{task.collector_type}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="采样率">{task.sample_rate} Hz</Descriptions.Item>
            <Descriptions.Item label="采样时长">{task.duration_sec}s</Descriptions.Item>
          </Descriptions>
        </Card>
      )}

      {/* 状态时间线 + 产物 并排 */}
      <Row gutter={SPACING.lg}>
        <Col xs={24} lg={12}>
          <Card title="状态时间线" size="small">
            {events.length > 0 ? (
              <Timeline
                items={events.map((event) => ({
                  color: event.to_status === "DONE"
                    ? "green"
                    : event.to_status === "FAILED"
                    ? "red"
                    : "blue",
                  children: (
                    <Space direction="vertical" size={0}>
                      <Typography.Text strong>
                        <StatusTag status={event.to_status} />
                      </Typography.Text>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {event.reason}
                      </Typography.Text>
                    </Space>
                  ),
                }))}
              />
            ) : (
              <Empty description="暂无状态事件" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card title="产物" size="small">
            {artifacts.length > 0 ? (
              <Table
                rowKey={(record, index) => `${record.artifact_type || "artifact"}-${index}`}
                columns={artifactColumns}
                dataSource={artifacts}
                pagination={false}
                size="small"
                scroll={{ x: 400 }}
              />
            ) : (
              <Empty description="暂无分析产物" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            )}
          </Card>
        </Col>
      </Row>

      {/* 火焰图 + TopN 并排 ⭐ 核心区域 */}
      <Card
        title={
          <Space>
            <FileTextOutlined style={{ color: COLORS.primary }} />
            分析结果
            {isActive && <Spin size="small" />}
          </Space>
        }
        size="small"
      >
        {hasPrimaryAnalysis ? (
          hasFlameOrTop ? (
            <Row gutter={SPACING.lg}>
              {/* 火焰图 */}
              <Col xs={24} lg={topArtifact ? 16 : 24}>
                <Typography.Text strong style={{ display: "block", marginBottom: 8 }}>
                  🔥 火焰图
                </Typography.Text>
                {analysis.hasFlameJson ? (
                  <FlamegraphViewer ref={flameRef} taskId={taskId} />
                ) : analysis.hasJavaHtml ? (
                  <JavaFlameViewer taskId={taskId} artifact={javaHtmlArtifact} />
                ) : analysis.svg ? (
                  <iframe
                    srcDoc={analysis.svg}
                    sandbox=""
                    title="火焰图"
                    style={{
                      width: "100%",
                      height: FLAMEGRAPH_HEIGHT,
                      border: `1px solid ${COLORS.border}`,
                      borderRadius: 6,
                      background: "#fff",
                    }}
                  />
                ) : analysisLoading ? (
                  <Skeleton.Input active block style={{ height: FLAMEGRAPH_HEIGHT, borderRadius: 8 }} />
                ) : (
                  <Empty description="暂无火焰图，请等待分析完成" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                )}
              </Col>

              {/* TopN 柱状图 */}
              {topArtifact && (
                <Col xs={24} lg={8}>
                  <Typography.Text strong style={{ display: "block", marginBottom: 8 }}>
                    📊 热点 Top {Math.min(analysis.top.length, 10)}
                  </Typography.Text>
                  {analysisLoading || analysis.top.length > 0 ? (
                    <>
                      <TopNChart
                        data={analysis.top.slice(0, 10)}
                        loading={analysisLoading}
                        height={FLAMEGRAPH_HEIGHT}
                        onBarClick={(funcName) => {
                          if (flameRef.current) {
                            flameRef.current.search(funcName);
                          }
                        }}
                      />
                      <Typography.Text
                        type="secondary"
                        style={{ fontSize: 11, display: "block", marginTop: 4, textAlign: "center" }}
                      >
                        点击柱状图 → 火焰图中高亮对应函数
                      </Typography.Text>
                    </>
                  ) : (
                    <Empty description="暂无热点函数数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                  )}
                </Col>
              )}
            </Row>
          ) : ebpfArtifact ? (
            <div>
              <Typography.Text strong style={{ display: "block", marginBottom: 8 }}>
                eBPF IO 延迟分布
              </Typography.Text>
              <EBPFHistogramChart taskId={taskId} artifact={ebpfArtifact} />
              <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
                当前任务类型为 eBPF IO 采集，结果以延迟直方图和原始 bpftrace 输出呈现，不会生成 CPU 火焰图。
              </Typography.Text>
            </div>
          ) : (
            <Empty
              description="连续采样任务已生成窗口化火焰图，请在下方“连续采样窗口”选择窗口查看"
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            />
          )
        ) : (
          <Empty
            description={
              isActive
                ? "任务运行中，分析产物将在完成后生成…"
                : "暂无火焰图或 TopN 分析结果"
            }
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          >
            {isActive && <Spin />}
          </Empty>
        )}

        {/* 建议 */}
        {suggestionArtifact && (
          <div style={{ marginTop: 12 }}>
            <Tag color="orange" icon={<BarChartOutlined />}>
              建议已生成: {suggestionArtifact.filename || suggestionArtifact.local_path || suggestionArtifact.object_key}
            </Tag>
          </div>
        )}

        {/* 持续采样窗口 */}
        {continuousWindows.length > 0 && (
          <div style={{ marginTop: 12 }}>
            <Space style={{ width: "100%", justifyContent: "space-between", marginBottom: 8 }}>
              <Typography.Text strong>连续采样窗口</Typography.Text>
              {continuousFlameArtifacts.length > 0 && (
                <Select
                  size="small"
                  style={{ width: 180 }}
                  value={selectedContinuousIndex}
                  onChange={setSelectedContinuousIndex}
                  options={continuousWindows.map((item) => ({
                    value: item.window_index,
                    label: `窗口 ${item.window_index}`,
                  }))}
                />
              )}
            </Space>
            <Table
              rowKey={(record) => record.window_index}
              dataSource={continuousWindows}
              pagination={false}
              size="small"
              style={{ marginTop: 8 }}
              scroll={{ x: 600 }}
              columns={[
                { title: "窗口", dataIndex: "window_index", width: 70 },
                {
                  title: "开始",
                  dataIndex: "start_ts",
                  width: 170,
                  render: (value) =>
                    value
                      ? new Date(value * 1000).toLocaleString()
                      : "-",
                },
                {
                  title: "结束",
                  dataIndex: "end_ts",
                  width: 170,
                  render: (value) =>
                    value
                      ? new Date(value * 1000).toLocaleString()
                      : "-",
                },
                {
                  title: "状态",
                  dataIndex: "ok",
                  width: 100,
                  render: (value) => (
                    <Tag color={value ? "green" : "red"}>
                      {value ? "OK" : "FAILED"}
                    </Tag>
                  ),
                },
                { title: "说明", dataIndex: "reason", ellipsis: true },
              ]}
            />
            {continuousFlameArtifacts.length > 0 && selectedContinuousIndex !== null && (
              <div style={{ marginTop: 12 }}>
                <Typography.Text type="secondary" style={{ display: "block", marginBottom: 8 }}>
                  当前窗口火焰图
                </Typography.Text>
                <FlamegraphViewer
                  taskId={taskId}
                  artifactType="continuous_flamegraph_json"
                  artifactIndex={selectedContinuousIndex}
                />
              </div>
            )}
          </div>
        )}
      </Card>

      {/* Java 火焰图 HTML */}
      {javaHtmlArtifact && (
        <Card title="Java 火焰图" size="small">
          <JavaFlameViewer taskId={taskId} artifact={javaHtmlArtifact} />
        </Card>
      )}

      {/* eBPF IO 延迟分布 */}
      {ebpfArtifact && hasFlameOrTop && (
        <Card title="eBPF IO 延迟分布" size="small">
          <EBPFHistogramChart taskId={taskId} artifact={ebpfArtifact} />
        </Card>
      )}

      {/* 内存时间序列 */}
      {memoryArtifact && (
        <Card title="内存分析" size="small">
          <MemoryChart taskId={taskId} artifact={memoryArtifact} />
        </Card>
      )}

      {/* 系统多维指标 */}
      {sysMetricsArtifact && (
        <Card title="系统多维指标" size="small">
          <SysMetricsView taskId={taskId} artifact={sysMetricsArtifact} />
        </Card>
      )}

      {/* Go pprof 状态 */}
      {pprofArtifact && !flameArtifact && (
        <Card title="Go pprof 采集" size="small">
          <Alert
            type="info"
            message="pprof 数据已采集"
            description={`原始 pprof 数据 (${(pprofArtifact.size_bytes / 1024).toFixed(1)} KB) 已保存。使用 go tool pprof 查看或安装 go 后自动生成火焰图。`}
            showIcon
          />
        </Card>
      )}

      {/* 智能归因 */}
      <Card
        title={
          <Space>
            <ExperimentOutlined style={{ color: COLORS.primary }} />
            智能归因
          </Space>
        }
        size="small"
        extra={
          <Space>
            {diagnoses.length > 0 && <Tag>{diagnoses.length} 次诊断</Tag>}
            <Tooltip title="候选原因的组织方式">
              <Select
                aria-label="归因策略"
                size="small"
                value={analysisStrategy}
                onChange={setAnalysisStrategy}
                disabled={diagnosing}
                style={{ width: 132 }}
                options={[
                  { value: "linear", label: "线性候选" },
                  { value: "graph", label: "因果图（方案 B）" },
                ]}
              />
            </Tooltip>
            <Tooltip title="旧版直接验证候选；新版先处理事实、现象、定位和证据反问">
              <Select
                aria-label="分析链路"
                size="small"
                value={analysisPipeline}
                onChange={setAnalysisPipeline}
                disabled={diagnosing}
                style={{ width: 178 }}
                options={[
                  { value: "evidence_to_attribution", label: "新版：证据归因" },
                  { value: "legacy", label: "旧版：假设验证" },
                ]}
              />
            </Tooltip>
            <Button
              icon={<ExperimentOutlined />}
              loading={diagnosing}
              onClick={runDiagnosis}
              type="primary"
              size="small"
            >
              运行诊断
            </Button>
            <Tooltip title="刷新诊断报告">
              <Button
                icon={<ReloadOutlined />}
                size="small"
                onClick={refreshDiagnosis}
                disabled={!diagnosis?.run?.id}
              />
            </Tooltip>
          </Space>
        }
      >
        {!diagnosis ? (
          <Empty
            description={
              diagnosing
                ? "诊断进行中…"
                : "暂无诊断报告，点击「运行诊断」基于当前证据进行 AI 归因分析"
            }
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          >
            {diagnosing && <Spin />}
          </Empty>
        ) : (
          <Space direction="vertical" size={SPACING.lg} style={{ width: "100%" }}>
            {/* 诊断元数据 */}
            <Descriptions column={{ xs: 1, sm: 2, md: 4 }} size="small">
              <Descriptions.Item label="诊断 ID">
                <Typography.Text copyable={{ text: diagnosis.run?.id }} style={{ fontSize: 12 }}>
                  {diagnosis.run?.id}
                </Typography.Text>
              </Descriptions.Item>
              <Descriptions.Item label="状态">
                <StatusTag
                  status={diagnosis.run?.status === "DONE" ? "DONE" : "FAILED"}
                />
              </Descriptions.Item>
              <Descriptions.Item label="模型">
                <Tag>{diagnosis.run?.model_name}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="校验">
                <Tag color={diagnosis.run?.validated ? "green" : "orange"}>
                  {diagnosis.run?.validated ? "通过" : "未通过"}
                </Tag>
              </Descriptions.Item>
            </Descriptions>

            {/* 摘要 */}
            <Alert
              type={report.not_enough_evidence ? "warning" : "info"}
              message={report.summary || diagnosis.run?.summary || "诊断完成"}
              showIcon
            />

            {/* 归因列表 */}
            {rankedCauses.length > 0 && (
              <Table
                rowKey={(record) => record.cause_id}
                dataSource={rankedCauses}
                pagination={false}
                size="small"
                scroll={{ x: 600 }}
                columns={[
                  { title: "根因", dataIndex: "cause_id", width: 200 },
                  {
                    title: "置信度",
                    dataIndex: "confidence",
                    width: 140,
                    render: (value) => (
                      <Progress
                        percent={Math.round((value || 0) * 100)}
                        size="small"
                        strokeColor={
                          (value || 0) > 0.7
                            ? COLORS.success
                            : (value || 0) > 0.4
                            ? COLORS.warning
                            : COLORS.error
                        }
                      />
                    ),
                  },
                  { title: "结论", dataIndex: "claim", ellipsis: true },
                  {
                    title: "证据引用",
                    dataIndex: "evidence_refs",
                    width: 200,
                    render: (refs = []) => (
                      <Space size={[2, 2]} wrap>
                        {refs.map((ref) => (
                          <Tag key={ref} style={{ fontSize: 10, margin: 0 }}>
                            {ref}
                          </Tag>
                        ))}
                      </Space>
                    ),
                  },
                ]}
              />
            )}

            {/* 结构化分析 */}
            <Card size="small" title="结构化分析" bordered={false} style={{ background: "#fafafa" }}>
              <Space direction="vertical" size={SPACING.md} style={{ width: "100%" }}>
                <Descriptions column={{ xs: 1, sm: 2, md: 4 }} size="small">
                  <Descriptions.Item label="主因">
                    {structuredPrimaryCauseId || "-"}
                  </Descriptions.Item>
                  <Descriptions.Item label="稳定性">
                    <Progress
                      percent={Math.round((structuredStability || 0) * 100)}
                      size="small"
                      strokeColor={
                        (structuredStability || 0) > 0.7
                          ? COLORS.success
                          : (structuredStability || 0) > 0.4
                          ? COLORS.warning
                          : COLORS.error
                      }
                    />
                  </Descriptions.Item>
                  <Descriptions.Item label="最大定位层级">
                    {structuredBoundary?.max_supported_level || "-"}
                  </Descriptions.Item>
                  <Descriptions.Item label="可用原因数">
                    {structuredAllowedCauseIds.length}
                  </Descriptions.Item>
                </Descriptions>

                <Alert
                  type={structuredBoundary?.can_claim_root_cause ? "success" : "warning"}
                  message={structuredBoundary?.reason || "当前结构化分析暂无边界说明"}
                  showIcon
                />

                {structuredPrimaryCauseReason && (
                  <Typography.Text type="secondary" style={{ display: "block" }}>
                    主因判定：{structuredPrimaryCauseReason}
                  </Typography.Text>
                )}

                {structuredFacts.length > 0 && (
                  <Table
                    rowKey="fact_id"
                    dataSource={structuredFacts}
                    pagination={false}
                    size="small"
                    scroll={{ x: 760 }}
                    columns={[
                      { title: "事实", dataIndex: "fact_id", width: 180 },
                      { title: "来源", dataIndex: "source", width: 120 },
                      { title: "证据引用", dataIndex: "evidence_ref", width: 200 },
                      { title: "值", dataIndex: "value", ellipsis: true },
                      { title: "状态", dataIndex: "status", width: 100 },
                      {
                        title: "阈值带",
                        dataIndex: "threshold_band",
                        width: 100,
                        render: (value) => <Tag>{value}</Tag>,
                      },
                    ]}
                  />
                )}

                <Collapse
                  ghost
                  items={[
                    {
                      key: "symptoms",
                      label: `现象 (${structuredSymptoms.length})`,
                      children: structuredSymptoms.length > 0 ? (
                        <Table
                          rowKey="symptom_id"
                          dataSource={structuredSymptoms}
                          pagination={false}
                          size="small"
                          columns={[
                            { title: "现象", dataIndex: "symptom_id", width: 200 },
                            { title: "类型", dataIndex: "symptom_type", width: 180 },
                            { title: "严重度", dataIndex: "severity", width: 100 },
                            {
                              title: "支持事实",
                              dataIndex: "fact_ids",
                              render: (ids = []) => (
                                <Space size={[4, 4]} wrap>
                                  {ids.map((item) => <Tag key={item}>{item}</Tag>)}
                                </Space>
                              ),
                            },
                          ]}
                        />
                      ) : (
                        <Empty description="暂无现象" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                      ),
                    },
                    {
                      key: "localizations",
                      label: `定位层级 (${structuredLocalizations.length})`,
                      children: structuredLocalizations.length > 0 ? (
                        <Table
                          rowKey={(record, index) => `${record.level}-${record.target || index}`}
                          dataSource={structuredLocalizations}
                          pagination={false}
                          size="small"
                          columns={[
                            { title: "层级", dataIndex: "level", width: 140 },
                            { title: "目标", dataIndex: "target", width: 180 },
                            {
                              title: "支持事实",
                              dataIndex: "fact_ids",
                              render: (ids = []) => (
                                <Space size={[4, 4]} wrap>
                                  {ids.map((item) => <Tag key={item}>{item}</Tag>)}
                                </Space>
                              ),
                            },
                          ]}
                        />
                      ) : (
                        <Empty description="暂无定位层级" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                      ),
                    },
                    {
                      key: "challenges",
                      label: `证据反问 (${structuredChallenges.length})`,
                      children: structuredChallenges.length > 0 ? (
                        <Table
                          rowKey="candidate_id"
                          dataSource={structuredChallenges}
                          pagination={false}
                          size="small"
                          columns={[
                            { title: "候选", dataIndex: "candidate_id", width: 220 },
                            { title: "稳定性", dataIndex: "conclusion_stability", width: 120 },
                            {
                              title: "关键事实",
                              dataIndex: "critical_fact_ids",
                              render: (ids = []) => (
                                <Space size={[4, 4]} wrap>
                                  {ids.map((item) => <Tag key={item}>{item}</Tag>)}
                                </Space>
                              ),
                            },
                            {
                              title: "反问测试",
                              dataIndex: "tests",
                              render: (tests = []) => (
                                <Space size={[4, 4]} wrap>
                                  {tests.map((item) => (
                                    <Tag key={`${item.removed_fact_id}-${item.result}`}>{item.result}</Tag>
                                  ))}
                                </Space>
                              ),
                            },
                          ]}
                        />
                      ) : (
                        <Empty description="暂无证据反问结果" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                      ),
                    },
                  ]}
                />
              </Space>
            </Card>

            {/* 反馈 */}
            <Space>
              <Button
                size="small"
                onClick={() => sendFeedback("correct", topCause?.cause_id)}
                disabled={!topCause}
              >
                👍 正确
              </Button>
              <Button
                size="small"
                onClick={() => sendFeedback("partial", topCause?.cause_id)}
                disabled={!topCause}
              >
                🔶 部分正确
              </Button>
              <Button
                size="small"
                danger
                onClick={() => sendFeedback("wrong", topCause?.cause_id)}
                disabled={!topCause}
              >
                👎 错误
              </Button>
            </Space>

            {/* 可折叠详情 */}
            <Collapse
              ghost
              items={[
                {
                  key: "tools",
                  label: `Tool-Use 证据链 (${toolResults.length})`,
                  children: toolResults.length > 0 ? (
                    <Table
                      rowKey={(record, index) => `${record.tool_name}-${index}`}
                      dataSource={toolResults}
                      pagination={false}
                      size="small"
                      scroll={{ x: 700 }}
                      columns={[
                        { title: "工具", dataIndex: "tool_name", width: 200 },
                        {
                          title: "状态",
                          dataIndex: "status",
                          width: 100,
                          render: (value) => <Tag>{value}</Tag>,
                        },
                        {
                          title: "证据引用",
                          dataIndex: "evidence_ref",
                          width: 240,
                          ellipsis: true,
                        },
                        {
                          title: "结果",
                          dataIndex: "output",
                          render: (value) => (
                            <Typography.Text
                              code
                              ellipsis
                              style={{ maxWidth: 200, display: "inline-block" }}
                            >
                              {JSON.stringify(value).slice(0, 160)}
                            </Typography.Text>
                          ),
                        },
                      ]}
                    />
                  ) : (
                    <Empty description="无工具调用记录" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                  ),
                },
                {
                  key: "repair",
                  label: "修复计划",
                  children: repairPlan ? (
                    <Space direction="vertical" style={{ width: "100%" }}>
                      <Space wrap>
                        <Tag
                          color={
                            repairPlan.risk_level === "safe_auto" ? "green" : "orange"
                          }
                        >
                          {repairPlan.risk_level}
                        </Tag>
                        <Tag>{repairPlan.status}</Tag>
                        {repairPlan.requires_user_confirm && (
                          <Tag color="orange">需人工确认风险动作</Tag>
                        )}
                      </Space>
                      <Table
                        rowKey="action_id"
                        dataSource={repairPlan.actions || []}
                        pagination={false}
                        size="small"
                        scroll={{ x: 600 }}
                        columns={[
                          { title: "动作", dataIndex: "action_type", width: 180 },
                          {
                            title: "风险",
                            dataIndex: "risk_level",
                            width: 120,
                            render: (value) => <Tag>{value}</Tag>,
                          },
                          { title: "状态", dataIndex: "status", width: 100 },
                          { title: "说明", dataIndex: "description", ellipsis: true },
                          { title: "结果", dataIndex: "result", ellipsis: true },
                        ]}
                      />
                    </Space>
                  ) : (
                    <Empty description="暂无修复计划" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                  ),
                },
              ]}
            />
          </Space>
        )}
      </Card>
    </Space>
  );
}

// ── 辅助组件：Java 火焰图 HTML Viewer ────────────────────────

function JavaFlameViewer({ taskId, artifact }) {
  const [html, setHtml] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await getTaskArtifactContent(taskId, "java_flamegraph_html");
        if (!cancelled) setHtml(data?.text || "");
      } catch {
        if (!cancelled) setHtml("");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [taskId]);

  if (loading) return <Skeleton.Input active block style={{ height: 400, borderRadius: 8 }} />;
  if (!html) return <Empty description="无法加载 Java 火焰图" image={Empty.PRESENTED_IMAGE_SIMPLE} />;

  return (
    <iframe
      srcDoc={html}
      sandbox=""
      title="Java 火焰图"
      style={{ width: "100%", height: 420, border: "none", borderRadius: 6 }}
    />
  );
}

// ── 辅助组件：内存时间序列图表 ──────────────────────────────

function SysMetricsView({ taskId, artifact }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const chartRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const content = await getTaskArtifactContent(taskId, "sys_metrics");
        if (!cancelled) setData(content || artifact.metadata);
      } catch {
        if (!cancelled) setData(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [taskId]);

  useEffect(() => {
    if (!data?.summary || !chartRef.current) return;

    import("echarts").then((echarts) => {
      const inst = echarts.init(chartRef.current);
      const s = data.summary;

      inst.setOption({
        title: { text: "System Metrics Dashboard", left: "center", textStyle: { fontSize: 13 } },
        tooltip: {},
        grid: [
          { left: "8%", top: "8%", width: "20%", height: "38%" },
          { left: "36%", top: "8%", width: "20%", height: "38%" },
          { left: "64%", top: "8%", width: "20%", height: "38%" },
          { left: "8%", top: "54%", width: "38%", height: "38%" },
          { left: "54%", top: "54%", width: "38%", height: "38%" },
        ],
        xAxis: [
          { gridIndex: 0, data: ["User", "Sys", "Iowait"], axisLabel: { fontSize: 10 } },
          { gridIndex: 1, data: ["1m", "5m", "15m"], axisLabel: { fontSize: 10 } },
          { gridIndex: 2, data: ["Threads", "FD"], axisLabel: { fontSize: 10 } },
          { gridIndex: 3, data: ["Rx KB/s", "Tx KB/s"], axisLabel: { fontSize: 10 } },
          { gridIndex: 4, data: ["RSS MB", "RSS Peak"], axisLabel: { fontSize: 10 } },
        ],
        yAxis: [
          { gridIndex: 0, name: "%", axisLabel: { fontSize: 9 } },
          { gridIndex: 1, axisLabel: { fontSize: 9 } },
          { gridIndex: 2, axisLabel: { fontSize: 9 } },
          { gridIndex: 3, axisLabel: { fontSize: 9 } },
          { gridIndex: 4, axisLabel: { fontSize: 9 } },
        ],
        series: [
          { type: "bar", xAxisIndex: 0, yAxisIndex: 0, data: [
            { value: s.avg_cpu_user_pct, itemStyle: { color: "#5470c6" } },
            { value: s.avg_cpu_sys_pct, itemStyle: { color: "#fac858" } },
            { value: s.avg_cpu_iowait_pct, itemStyle: { color: "#ee6666" } },
          ]},
          { type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: [
            { value: s.load1m || 0, itemStyle: { color: "#91cc75" } },
            { value: s.load5m || 0, itemStyle: { color: "#73c0de" } },
            { value: s.load15m || 0, itemStyle: { color: "#a0a7e6" } },
          ]},
          { type: "bar", xAxisIndex: 2, yAxisIndex: 2, data: [
            { value: s.thread_count, itemStyle: { color: s.thread_trend === "increasing" ? "#ee6666" : "#73c0de" } },
            { value: s.fd_count, itemStyle: { color: s.fd_trend === "increasing" ? "#ee6666" : "#fac858" } },
          ]},
          { type: "bar", xAxisIndex: 3, yAxisIndex: 3, data: [
            { value: s.net_rx_kbps, itemStyle: { color: "#5470c6" } },
            { value: s.net_tx_kbps, itemStyle: { color: "#91cc75" } },
          ]},
          { type: "bar", xAxisIndex: 4, yAxisIndex: 4, data: [
            { value: s.vmrss_mb, itemStyle: { color: "#fc8452" } },
            { value: s.vmrss_mb_max, itemStyle: { color: "#9a60b4" } },
          ]},
        ],
      });

      const handleResize = () => inst.resize();
      window.addEventListener("resize", handleResize);
      return () => window.removeEventListener("resize", handleResize);
    });
  }, [data]);

  if (loading) return <Skeleton.Input active block style={{ height: 400, borderRadius: 8 }} />;
  if (!data?.summary) return <Empty description="无系统指标数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />;

  const s = data.summary;
  const trends = {
    fd: { increasing: ["red", "FD ↑"], decreasing: ["green", "FD ↓"], stable: ["blue", "FD →"] },
    thread: { increasing: ["red", "线程 ↑"], decreasing: ["green", "线程 ↓"], stable: ["blue", "线程 →"] },
  };

  return (
    <div>
      <Space style={{ marginBottom: 8 }} wrap>
        <Tag>样本: {data.sample_count}</Tag>
        <Tag>CPU sys: {s.avg_cpu_sys_pct}%</Tag>
        <Tag>iowait: {s.avg_cpu_iowait_pct}%</Tag>
        <Tag color={trends.thread[s.thread_trend]?.[0] || "default"}>
          {trends.thread[s.thread_trend]?.[1] || s.thread_trend}: {s.thread_count}
        </Tag>
        <Tag color={trends.fd[s.fd_trend]?.[0] || "default"}>
          {trends.fd[s.fd_trend]?.[1] || s.fd_trend}: {s.fd_count}
        </Tag>
        <Tag>ctx/s: {s.ctx_nonvoluntary_rate}/s</Tag>
      </Space>
      <div ref={chartRef} style={{ width: "100%", height: 420 }} />
    </div>
  );
}

function MemoryChart({ taskId, artifact }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const chartRef = useRef(null);
  const chartInstance = useRef(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const content = await getTaskArtifactContent(taskId, "memory_json");
        if (!cancelled) setData(content || artifact.metadata);
      } catch {
        if (!cancelled) setData(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [taskId]);

  useEffect(() => {
    if (!data?.samples?.length || !chartRef.current) return;

    import("echarts").then((echarts) => {
      if (chartInstance.current) chartInstance.current.dispose();
      const inst = echarts.init(chartRef.current);
      chartInstance.current = inst;

      const times = data.samples.map((s) => new Date(s.ts * 1000).toLocaleTimeString());
      const rss = data.samples.map((s) => s.rss_mb ?? 0);
      const pss = data.samples.some((s) => s.pss_mb != null) ? data.samples.map((s) => s.pss_mb ?? 0) : null;
      const swap = data.samples.some((s) => s.swap_mb > 0) ? data.samples.map((s) => s.swap_mb ?? 0) : null;

      const series = [{ name: "RSS", type: "line", data: rss, smooth: true, areaStyle: { opacity: 0.15 } }];
      if (pss) series.push({ name: "PSS", type: "line", data: pss, smooth: true });
      if (swap) series.push({ name: "Swap", type: "line", data: swap, smooth: true, lineStyle: { type: "dashed" } });

      inst.setOption({
        tooltip: { trigger: "axis" },
        legend: { data: series.map((s) => s.name), bottom: 0 },
        grid: { left: 50, right: 20, top: 20, bottom: 30 },
        xAxis: { type: "category", data: times, boundaryGap: false },
        yAxis: { type: "value", name: "MB" },
        series,
      });

      const handleResize = () => inst.resize();
      window.addEventListener("resize", handleResize);
      return () => window.removeEventListener("resize", handleResize);
    });
  }, [data]);

  if (loading) return <Skeleton.Input active block style={{ height: 300, borderRadius: 8 }} />;
  if (!data?.samples?.length) return <Empty description="无内存数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />;

  const trendTag = { increasing: ["red", "增长 ↑"], decreasing: ["green", "下降 ↓"], stable: ["blue", "稳定 →"] }[data.trend] || ["default", data.trend];

  return (
    <div>
      <Space style={{ marginBottom: 8 }}>
        <Tag>样本数: {data.sample_count}</Tag>
        <Tag>RSS: {data.first_rss_mb} → {data.last_rss_mb} MB</Tag>
        <Tag>峰值: {data.peak_rss_mb} MB</Tag>
        <Tag color={trendTag[0]}>趋势: {trendTag[1]}</Tag>
      </Space>
      <div ref={chartRef} style={{ width: "100%", height: 300 }} />
    </div>
  );
}

// ── 辅助组件：eBPF IO 延迟分布 Histogram ─────────────────────

function EBPFHistogramChart({ taskId, artifact }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const content = await getTaskArtifactContent(taskId, "ebpf_metrics");
        if (!cancelled) setData(content || null);
      } catch {
        if (!cancelled) setData(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [taskId]);

  return <EBPFHistogram data={data} loading={loading} />;
}
