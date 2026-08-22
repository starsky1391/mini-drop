import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
} from "@xyflow/react";
import { Alert, Collapse, Drawer, Empty, Popover, Progress, Space, Tag, Typography } from "antd";
import "@xyflow/react/dist/style.css";
import "./ControlledAITreeGraph.css";
import { buildControlledAITreeGraph, ROLE_LABELS } from "./aiTreeGraphModel";

let elkInstance = null;

const NODE_SIZE = {
  width: 280,
  height: 158,
};

const EDGE_COLORS = {
  probe: "#1677ff",
  mechanism: "#389e0d",
  backtrack: "#8c8c8c",
  boundary: "#d48806",
  lineage: "#748094",
};

function ControlledAITreeGraphInner({ tree, evidenceMap, highlightedCandidateIds = [] }) {
  const sourceGraph = useMemo(
    () => buildControlledAITreeGraph(tree, highlightedCandidateIds),
    [tree, highlightedCandidateIds],
  );
  const [nodes, setNodes] = useState([]);
  const [edges, setEdges] = useState([]);
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    let cancelled = false;
    layoutGraph(sourceGraph).then(({ nodes: nextNodes, edges: nextEdges }) => {
      if (cancelled) return;
      setNodes(nextNodes);
      setEdges(nextEdges);
    });
    return () => { cancelled = true; };
  }, [sourceGraph]);

  const nodeTypes = useMemo(() => ({ aiTreeNode: AITreeNode }), []);
  const decoratedEdges = useMemo(
    () => edges.map((edge) => decorateEdge(edge)),
    [edges],
  );

  const onNodeClick = useCallback((_, node) => {
    setSelected({ type: "node", value: node.data });
  }, []);

  const onEdgeClick = useCallback((event, edge) => {
    event.stopPropagation();
    setSelected({ type: "edge", value: edge.data, label: edge.label });
  }, []);

  if (!tree?.layers?.length) {
    return <Empty description="当前诊断没有 AI 树数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  }

  return (
    <div className="ai-tree-shell">
      <div className="ai-tree-toolbar">
        <Space wrap>
          <Tag color="red">主因</Tag>
          <Tag color="orange">次因</Tag>
          <Tag color="default">反证灰节点</Tag>
          <Tag color="cyan">未决/阻断节点</Tag>
          <Tag color="blue">探针边</Tag>
          <Tag color="green">机制分支</Tag>
          <Tag color="default">回溯边</Tag>
          <Tag color="gold">停止边界</Tag>
        </Space>
        <Typography.Text type="secondary">
          Hover 看节点反问，点击节点或探针边查看完整证据。
        </Typography.Text>
      </div>
      {sourceGraph.orphanNodes?.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`数据质量：${sourceGraph.orphanNodes.length} 个节点缺少可用父节点（未进入主树）`}
          description={(
            <Space direction="vertical" size={2}>
              {sourceGraph.orphanNodes.map((node) => (
                <Typography.Text key={node.id}>
                  {node.data?.candidate?.candidate_id || node.data?.title}：
                  {node.data?.claim || "缺少显式来源父节点"}
                </Typography.Text>
              ))}
            </Space>
          )}
          style={{ marginBottom: 12 }}
        />
      )}
      <div className="ai-tree-canvas">
        <ReactFlow
          nodes={nodes}
          edges={decoratedEdges}
          nodeTypes={nodeTypes}
          onNodeClick={onNodeClick}
          onEdgeClick={onEdgeClick}
          fitView
          fitViewOptions={{ padding: 0.2 }}
          minZoom={0.35}
          maxZoom={1.45}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable
        >
          <Background color="#d8dee9" gap={18} />
          <Controls showInteractive={false} />
          <MiniMap pannable zoomable nodeColor={(node) => miniMapColor(node.data?.role)} />
        </ReactFlow>
      </div>
      {(sourceGraph.historyEdges?.length > 0 || sourceGraph.dataQuality?.length > 0) && (
        <Collapse
          ghost
          items={[{
            key: "tree-audit",
            label: `调查历史与数据质量（${sourceGraph.historyEdges?.length || 0} 条历史边，${sourceGraph.dataQuality?.length || 0} 条记录）`,
            children: (
              <Space direction="vertical" size={6} style={{ width: "100%" }}>
                {sourceGraph.historyEdges?.map((edge) => (
                  <Typography.Text key={`history-${edge.id}`}>
                    {edge.label || edge.data?.kind || "历史边"}：
                    {edge.data?.reason || edge.data?.status || "未说明"}
                  </Typography.Text>
                ))}
                {sourceGraph.dataQuality?.map((item, index) => (
                  <Typography.Text type="warning" key={`quality-${item.candidate_id || index}`}>
                    数据质量：{item.candidate_id || item.title || "未命名节点"}；
                    {item.claim || item.status || "父节点或节点关系无效"}。
                  </Typography.Text>
                ))}
              </Space>
            ),
          }]}
        />
      )}
      <TreeDetailDrawer
        selected={selected}
        evidenceMap={evidenceMap}
        onClose={() => setSelected(null)}
      />
    </div>
  );
}

export default function ControlledAITreeGraph(props) {
  return (
    <ReactFlowProvider>
      <ControlledAITreeGraphInner {...props} />
    </ReactFlowProvider>
  );
}

function AITreeNode({ data }) {
  const candidate = data.candidate || {};
  const challenge = candidate.self_challenge || {};
  const isBoundaryStatus = ["blocked", "partial", "inconclusive"].includes(candidate.status)
    || ["blocked", "partial", "inconclusive"].includes(candidate.causal_status);
  const isRejected = !isBoundaryStatus && candidate.status !== "forbidden" && (
    ["contradicted", "rejected"].includes(candidate.status)
    || candidate.causal_status === "contradicted"
  );
  const isUnresolved = candidate.causal_status === "inconclusive"
    || (data.role === "unknown" && candidate.status === "missing_evidence");
  const isForbidden = candidate.status === "forbidden" || data.status === "forbidden";
  const depthKind = candidate.node_type === "observation" ? "observation" : candidate.depth_kind || data.layoutBand || "base";
  const roleLabel = depthKind === "observation"
    ? "观察上下文"
    : depthKind === "mechanism"
    ? "机制分支"
    : depthKind === "boundary"
      ? "证据边界"
      : ROLE_LABELS[data.role] || data.role;
  const content = (
    <Space direction="vertical" size={6} className="ai-tree-popover">
      <Typography.Text strong>{data.claim}</Typography.Text>
      <Typography.Text type="secondary">为什么是它：{challenge.why_this_claim || "未说明"}</Typography.Text>
      <Typography.Text type="secondary">为什么不是其他：{challenge.why_not_other_claims || "未说明"}</Typography.Text>
      {(challenge.missing_evidence || []).length > 0 && (
        <Typography.Text type="warning">缺失：{challenge.missing_evidence.join("；")}</Typography.Text>
      )}
      <Typography.Text type="secondary">改变结论条件：{challenge.what_would_change_my_mind || "未说明"}</Typography.Text>
    </Space>
  );

  return (
    <Popover trigger="hover" placement="right" content={content}>
      <div className={`ai-tree-node ai-tree-node-${data.role} ai-tree-node-depth-${depthKind} ${isRejected ? "ai-tree-node-muted" : ""} ${isUnresolved ? "ai-tree-node-unresolved" : ""} ${isForbidden ? "ai-tree-node-forbidden" : ""} ${data.outsideFinalBoundary ? "ai-tree-node-observed-only" : ""} ${data.highlighted ? "ai-tree-node-highlighted" : ""}`}>
        <Handle type="target" position={Position.Top} />
        <div className="ai-tree-node-topline">
          <span className="ai-tree-node-role">{roleLabel}</span>
          <span className="ai-tree-node-level">{data.level}</span>
        </div>
        <Typography.Text className="ai-tree-node-title" ellipsis>
          {data.title}
        </Typography.Text>
        <Typography.Paragraph className="ai-tree-node-claim" ellipsis={{ rows: 2 }}>
          {data.claim}
        </Typography.Paragraph>
        <div className="ai-tree-node-footer">
          <Progress
            percent={Math.round((data.confidence || 0) * 100)}
            size="small"
            showInfo={false}
            strokeColor={progressColor(data.role)}
            trailColor="#edf0f5"
          />
          <Space size={4} wrap>
            {(data.badges || []).slice(0, 4).map((badge) => (
              <Tag key={badge} className="ai-tree-node-badge">{badge}</Tag>
            ))}
          </Space>
          {data.outsideFinalBoundary && <span className="ai-tree-node-boundary-note">未入终态</span>}
        </div>
        <Handle type="source" position={Position.Bottom} />
      </div>
    </Popover>
  );
}

function TreeDetailDrawer({ selected, evidenceMap, onClose }) {
  const open = Boolean(selected);
  const value = selected?.value || {};
  const candidate = value.candidate || {};
  const challenge = candidate.self_challenge || {};
  const depthKind = candidate.node_type === "observation" ? "observation" : candidate.depth_kind || value.layoutBand || "base";
  const roleLabel = depthKind === "observation"
    ? "观察上下文"
    : depthKind === "mechanism"
    ? "机制分支"
    : depthKind === "boundary"
      ? "证据边界"
      : ROLE_LABELS[value.role] || value.role;
  const evidenceRefs = selected?.type === "edge"
    ? value.evidenceRefs || []
    : [
      ...(candidate.evidence_refs || []),
      ...(challenge.supporting_evidence_refs || []),
      ...(challenge.opposing_evidence_refs || []),
    ];

  return (
    <Drawer
      title={selected?.type === "edge" ? (value.kind === "backtrack" ? "回溯边详情" : "探针边详情") : "AI 树节点详情"}
      open={open}
      width={520}
      onClose={onClose}
    >
      {selected?.type === "edge" ? (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Typography.Title level={5}>{selected.label || "探针补证"}</Typography.Title>
          <Typography.Paragraph>{value.reason || "未说明请求原因。"}</Typography.Paragraph>
          <Space wrap>
            <Tag color="blue">{value.status || "unknown"}</Tag>
            <Tag color={value.reuseStatus === "reuse_hit" ? "green" : "gold"}>{value.reuseStatus || "not_checked"}</Tag>
            <Tag>{value.effect || "pending"}</Tag>
          </Space>
          <TagList title="请求探针" values={value.probeRequests || []} color="blue" />
          <TagList title="证据引用" values={evidenceRefs} evidenceMap={evidenceMap} color="geekblue" />
          <TagList
            title="探针结果"
            values={(value.probeResults || []).map((item) => item.blocked_reason || item.status)}
            color="orange"
          />
        </Space>
      ) : (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Space wrap>
            <Tag color={roleColor(value.role)}>{roleLabel}</Tag>
            <Tag>{value.level}</Tag>
            <Tag>{value.generatedBy}</Tag>
            <Tag>{candidate.status || value.status || "unknown"}</Tag>
            <Tag>{candidate.claim_type || "partial_localization"}</Tag>
            <Tag color={candidate.conclusion_eligible ? "green" : "default"}>
              {candidate.conclusion_eligible ? "可进入结论" : "未过结论门禁"}
            </Tag>
          </Space>
          <Typography.Title level={5}>{candidate.candidate_id || value.title}</Typography.Title>
          <Typography.Paragraph>{candidate.claim || value.claim}</Typography.Paragraph>
          <Typography.Paragraph type="secondary">
            机制：{candidate.mechanism || "未形成"}；目标：{candidate.target || "未定位"}
          </Typography.Paragraph>
          {!candidate.conclusion_eligible && candidate.eligibility_reason && (
            <Typography.Paragraph type="warning">门禁原因：{candidate.eligibility_reason}</Typography.Paragraph>
          )}
          <Typography.Text strong>节点反问</Typography.Text>
          <Typography.Paragraph>为什么是它：{challenge.why_this_claim || "未说明"}</Typography.Paragraph>
          <Typography.Paragraph>为什么不是其他：{challenge.why_not_other_claims || "未说明"}</Typography.Paragraph>
          <Typography.Paragraph>改变结论条件：{challenge.what_would_change_my_mind || "未说明"}</Typography.Paragraph>
          <TagList title="支持证据" values={challenge.supporting_evidence_refs || []} evidenceMap={evidenceMap} color="blue" />
          <TagList title="反驳证据" values={challenge.opposing_evidence_refs || []} evidenceMap={evidenceMap} color="red" />
          <TagList title="缺失证据" values={challenge.missing_evidence || []} color="gold" />
          <TagList title="候选证据" values={candidate.evidence_refs || []} evidenceMap={evidenceMap} color="geekblue" />
        </Space>
      )}
    </Drawer>
  );
}

function TagList({ title, values = [], evidenceMap, color }) {
  if (!values.length) return null;
  return (
    <Space direction="vertical" size={4} style={{ width: "100%" }}>
      <Typography.Text strong>{title}</Typography.Text>
      <Space wrap>
        {values.map((value) => (
          <Tag key={value} color={evidenceMap ? (evidenceMap.has(value) ? color : "red") : color}>
            {value}
          </Tag>
        ))}
      </Space>
    </Space>
  );
}

async function layoutGraph(graph) {
  const layoutNodes = graph.nodes.filter((node) => node.data?.layoutRole !== "annotation");
  const annotationNodes = graph.nodes.filter((node) => node.data?.layoutRole === "annotation");
  const layoutEdges = graph.layoutEdges || graph.edges.filter((edge) => edge.data?.layoutRole === "tree");
  const elkGraph = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "DOWN",
      "elk.layered.spacing.nodeNodeBetweenLayers": "86",
      "elk.spacing.nodeNode": "42",
      "elk.edgeRouting": "ORTHOGONAL",
    },
    children: layoutNodes.map((node) => ({
      id: node.id,
      width: NODE_SIZE.width,
      height: NODE_SIZE.height,
    })),
    edges: layoutEdges.map((edge) => ({
      id: edge.id,
      sources: [edge.source],
      targets: [edge.target],
    })),
  };

  try {
    const elk = await getElk();
    const layouted = await elk.layout(elkGraph);
    const positions = new Map((layouted.children || []).map((node) => [node.id, node]));
    const positionedLayoutNodes = layoutNodes.map((node) => {
      const position = positions.get(node.id);
      return {
        ...node,
        position: { x: position?.x || 0, y: position?.y || 0 },
      };
    });
    return {
      nodes: placeAnnotationNodes(positionedLayoutNodes, annotationNodes),
      edges: graph.edges.filter((edge) => edge.data?.layoutRole === "tree"),
    };
  } catch {
    const fallbackLayoutNodes = layoutNodes.map((node, index) => ({
      ...node,
      position: {
        x: (index % 4) * (NODE_SIZE.width + 60),
        y: Math.floor(index / 4) * (NODE_SIZE.height + 90),
      },
    }));
    return {
      nodes: placeAnnotationNodes(fallbackLayoutNodes, annotationNodes),
      edges: graph.edges.filter((edge) => edge.data?.layoutRole === "tree"),
    };
  }
}

async function getElk() {
  if (elkInstance) return elkInstance;
  const module = await import("elkjs/lib/elk.bundled.js");
  const ELK = module.default;
  elkInstance = new ELK();
  return elkInstance;
}

function placeAnnotationNodes(layoutedNodes, annotationNodes) {
  if (!annotationNodes.length) return layoutedNodes;
  const bounds = layoutedNodes.reduce((acc, node) => {
    const x = node.position?.x || 0;
    const y = node.position?.y || 0;
    return {
      minX: Math.min(acc.minX, x),
      maxX: Math.max(acc.maxX, x),
      maxBottom: Math.max(acc.maxBottom, y + NODE_SIZE.height),
    };
  }, {
    minX: Number.POSITIVE_INFINITY,
    maxX: Number.NEGATIVE_INFINITY,
    maxBottom: Number.NEGATIVE_INFINITY,
  });
  const hasLayoutBounds = Number.isFinite(bounds.minX) && Number.isFinite(bounds.maxX) && Number.isFinite(bounds.maxBottom);
  const anchorX = hasLayoutBounds ? (bounds.minX + bounds.maxX) / 2 : 0;
  let nextY = hasLayoutBounds ? bounds.maxBottom + 86 : 0;
  return [
    ...layoutedNodes,
    ...annotationNodes.map((node) => {
      const positioned = {
        ...node,
        position: { x: anchorX, y: nextY },
      };
      nextY += NODE_SIZE.height + 24;
      return positioned;
    }),
  ];
}

function decorateEdge(edge) {
  const kind = edge.data?.kind || "lineage";
  const failed = edge.data?.status === "failed";
  const unresolved = ["blocked", "inconclusive"].includes(edge.data?.status);
  const edgeColor = failed ? "#cf1322" : unresolved ? "#d48806" : EDGE_COLORS[kind] || EDGE_COLORS.lineage;
  return {
    ...edge,
    markerEnd: { type: MarkerType.ArrowClosed, color: edgeColor },
    style: {
      stroke: edgeColor,
      strokeWidth: kind === "probe" ? 2.4 : 1.6,
      strokeDasharray: failed || unresolved || kind === "boundary" || kind === "backtrack" ? "6 5" : undefined,
    },
    labelStyle: {
      fill: edgeColor,
      fontWeight: 700,
      fontSize: 11,
    },
    labelBgStyle: {
      fill: "#ffffff",
      fillOpacity: 0.88,
    },
  };
}

function roleColor(role) {
  return {
    primary: "red",
    secondary: "orange",
    rejected: "default",
    unknown: "blue",
    start: "purple",
    stop: "gold",
  }[role] || "default";
}

function progressColor(role) {
  return {
    primary: "#cf1322",
    secondary: "#d46b08",
    rejected: "#8c8c8c",
    unknown: "#1677ff",
    start: "#722ed1",
    stop: "#d48806",
  }[role] || "#1677ff";
}

function miniMapColor(role) {
  return {
    primary: "#ff7875",
    secondary: "#ffc069",
    rejected: "#d9d9d9",
    unknown: "#91caff",
    start: "#b37feb",
    stop: "#ffd666",
  }[role] || "#cbd5e1";
}
