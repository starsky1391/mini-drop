import { Alert, Button, Collapse, Descriptions, Progress, Space, Tag, Timeline, Typography } from "antd";
import { BranchesOutlined, BulbOutlined, SafetyCertificateOutlined, ToolOutlined } from "@ant-design/icons";
import "./RootCauseClusters.css";

const ROLE_META = {
  primary: { label: "主因", color: "red" },
  contributing: { label: "贡献因", color: "orange" },
  independent: { label: "独立原因", color: "blue" },
};

const QUALIFICATION_META = {
  confirmed_root_cause: { label: "已确认根因", color: "green" },
  possible_root_cause: { label: "可能根因", color: "gold" },
  partial_localization: { label: "局部定位", color: "cyan" },
  observation: { label: "观察事实", color: "default" },
};

const LEVEL_LABELS = {
  observation: "观察事实",
  direct_failure_mechanism: "直接故障机制",
  direct_root_cause: "直接根因",
  complete_source_root_cause: "完整来源根因",
};

const SUPPORTED_LEVEL_LABELS = {
  resource: "资源",
  host: "主机",
  process: "进程",
  thread: "线程",
  syscall: "系统调用",
  dependency: "依赖",
  service: "服务",
  endpoint: "端点",
  function: "函数",
  call_path: "调用路径",
  line: "源码行",
};

const RECOMMENDATION_LABELS = {
  investigation: "继续查证",
  temporary_mitigation: "临时缓解",
  permanent_fix: "长期修复",
};

export default function RootCauseClusters({ clusters = [], evidenceMap, onInspectTree }) {
  if (!clusters.length) return null;
  const confirmedCount = clusters.filter((item) => item.qualification === "confirmed_root_cause").length;
  const pendingCount = clusters.length - confirmedCount;

  return (
    <section className="root-cause-clusters" aria-label="根因簇">
      <div className="root-cause-clusters-heading">
        <div>
          <Typography.Title level={5}>根因与影响范围</Typography.Title>
          <Typography.Text type="secondary">展开可核查每个相关方向的定位、证据和处理建议。</Typography.Text>
        </div>
        <Space wrap>
          <Tag icon={<SafetyCertificateOutlined />} color="green">{confirmedCount} 个已确认根因</Tag>
          {pendingCount > 0 && <Tag color="gold">{pendingCount} 个待验证结论</Tag>}
        </Space>
      </div>
      <Collapse
        className="root-cause-cluster-list"
        defaultActiveKey={clusters[0]?.cluster_id ? [clusters[0].cluster_id] : []}
        items={clusters.map((cluster) => ({
          key: cluster.cluster_id,
          label: <ClusterLabel cluster={cluster} />,
          children: (
            <ClusterDetail
              cluster={cluster}
              evidenceMap={evidenceMap}
              onInspectTree={onInspectTree}
            />
          ),
        }))}
      />
    </section>
  );
}

function ClusterLabel({ cluster }) {
  const role = ROLE_META[cluster.role] || ROLE_META.independent;
  const qualification = QUALIFICATION_META[cluster.qualification] || QUALIFICATION_META.observation;
  return (
    <div className="root-cause-cluster-label">
      <Space wrap size={6}>
        <Tag color={role.color}>{role.label}</Tag>
        <Tag color={qualification.color}>{qualification.label}</Tag>
        <Tag>{LEVEL_LABELS[cluster.cause_level] || cluster.cause_level}</Tag>
        <Tag color="geekblue">定位到 {SUPPORTED_LEVEL_LABELS[cluster.supported_level] || cluster.supported_level || "资源"}</Tag>
        {!cluster.conclusion_eligible && <Tag>未过结论门禁</Tag>}
      </Space>
      <Typography.Text strong>{cluster.claim}</Typography.Text>
      <Progress
        percent={Math.round((cluster.confidence || 0) * 100)}
        size="small"
        showInfo={false}
        strokeColor={cluster.conclusion_eligible ? "#389e0d" : cluster.qualification === "possible_root_cause" ? "#d48806" : "#08979c"}
      />
    </div>
  );
}

function ClusterDetail({ cluster, evidenceMap, onInspectTree }) {
  const sourceIds = cluster.source_tree_candidate_ids || [];
  const recommendations = cluster.recommendations || [];
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Descriptions size="small" column={{ xs: 1, md: 2 }}>
        <Descriptions.Item label="作用目标"><Typography.Text code>{cluster.target}</Typography.Text></Descriptions.Item>
        <Descriptions.Item label="故障机制"><Typography.Text code>{cluster.mechanism}</Typography.Text></Descriptions.Item>
        <Descriptions.Item label="为什么会这样" span={2}>{cluster.why_it_happened}</Descriptions.Item>
        <Descriptions.Item label="解释的症状" span={2}>{(cluster.explained_symptoms || []).join("；") || "未明确"}</Descriptions.Item>
        {cluster.relation_to_primary && <Descriptions.Item label="与主因关系" span={2}>{cluster.relation_to_primary}</Descriptions.Item>}
      </Descriptions>

      {(cluster.causal_chain || []).length > 0 && (
        <div>
          <Typography.Text strong><BranchesOutlined /> 因果链</Typography.Text>
          <Timeline
            className="root-cause-chain"
            items={cluster.causal_chain.map((step) => ({
              color: "blue",
              children: (
                <div>
                  <Typography.Paragraph>{step.statement}</Typography.Paragraph>
                  <EvidenceTags refs={step.evidence_refs} evidenceMap={evidenceMap} />
                </div>
              ),
            }))}
          />
        </div>
      )}

      {(cluster.residual_unknowns || []).length > 0 && (
        <Alert
          type="warning"
          showIcon
          message="仍未确认"
          description={cluster.residual_unknowns.join("；")}
        />
      )}

      {recommendations.length > 0 && (
        <div>
          <Typography.Text strong><BulbOutlined /> 建议处理</Typography.Text>
          <div className="root-cause-recommendations">
            {recommendations.map((item, index) => (
              <div className="root-cause-recommendation" key={`${item.recommendation_type}-${index}`}>
                <Tag icon={<ToolOutlined />}>{RECOMMENDATION_LABELS[item.recommendation_type] || item.recommendation_type}</Tag>
                <Typography.Text>{item.action}</Typography.Text>
                {item.rationale && <Typography.Text type="secondary">{item.rationale}</Typography.Text>}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="root-cause-cluster-footer">
        <EvidenceTags refs={cluster.evidence_refs} evidenceMap={evidenceMap} />
        {sourceIds.length > 0 && (
          <Button size="small" icon={<BranchesOutlined />} onClick={() => onInspectTree?.(sourceIds)}>
            在 AI 树中查看
          </Button>
        )}
      </div>
    </Space>
  );
}

function EvidenceTags({ refs = [], evidenceMap }) {
  if (!refs.length) return <Typography.Text type="secondary">无证据引用</Typography.Text>;
  return (
    <Space wrap size={4}>
      {refs.map((ref) => (
        <Tag key={ref} color={evidenceMap?.has(ref) ? "blue" : "red"}>{ref}</Tag>
      ))}
    </Space>
  );
}
