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
import { Drawer, Empty, Popover, Progress, Space, Tag, Typography } from "antd";
import "@xyflow/react/dist/style.css";
import "./ControlledAITreeGraph.css";
import { buildControlledAITreeGraph, ROLE_LABELS } from "./aiTreeGraphModel";

let elkInstance = null;

const NODE_SIZE = {
  width: 280,
  height: 142,
};

const EDGE_COLORS = {
  probe: "#1677ff",
  backtrack: "#8c8c8c",
  boundary: "#d48806",
  lineage: "#748094",
};

function ControlledAITreeGraphInner({ tree, evidenceMap }) {
  const sourceGraph = useMemo(() => buildControlledAITreeGraph(tree), [tree]);
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
          <Tag color="blue">探针边</Tag>
          <Tag color="default">回溯边</Tag>
          <Tag color="gold">停止边界</Tag>
        </Space>
        <Typography.Text type="secondary">
          Hover 看节点反问，点击节点或探针边查看完整证据。
        </Typography.Text>
      </div>
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
  const isRejected = data.role === "rejected" || ["contradicted", "rejected"].includes(candidate.status);
  const isForbidden = candidate.status === "forbidden" || data.status === "forbidden";
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
      <div className={`ai-tree-node ai-tree-node-${data.role} ${isRejected ? "ai-tree-node-muted" : ""} ${isForbidden ? "ai-tree-node-forbidden" : ""}`}>
        <Handle type="target" position={Position.Top} />
        <div className="ai-tree-node-topline">
          <span className="ai-tree-node-role">{ROLE_LABELS[data.role] || data.role}</span>
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
            <Tag color={roleColor(value.role)}>{ROLE_LABELS[value.role] || value.role}</Tag>
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
  const elkGraph = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "DOWN",
      "elk.layered.spacing.nodeNodeBetweenLayers": "86",
      "elk.spacing.nodeNode": "42",
      "elk.edgeRouting": "ORTHOGONAL",
    },
    children: graph.nodes.map((node) => ({
      id: node.id,
      width: NODE_SIZE.width,
      height: NODE_SIZE.height,
    })),
    edges: graph.edges.map((edge) => ({
      id: edge.id,
      sources: [edge.source],
      targets: [edge.target],
    })),
  };

  try {
    const elk = await getElk();
    const layouted = await elk.layout(elkGraph);
    const positions = new Map((layouted.children || []).map((node) => [node.id, node]));
    return {
      nodes: graph.nodes.map((node) => {
        const position = positions.get(node.id);
        return {
          ...node,
          position: { x: position?.x || 0, y: position?.y || 0 },
        };
      }),
      edges: graph.edges,
    };
  } catch {
    return {
      nodes: graph.nodes.map((node, index) => ({
        ...node,
        position: {
          x: (index % 4) * (NODE_SIZE.width + 60),
          y: Math.floor(index / 4) * (NODE_SIZE.height + 90),
        },
      })),
      edges: graph.edges,
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

function decorateEdge(edge) {
  const kind = edge.data?.kind || "lineage";
  const failed = ["failed", "blocked"].includes(edge.data?.status);
  return {
    ...edge,
    markerEnd: { type: MarkerType.ArrowClosed, color: EDGE_COLORS[kind] || EDGE_COLORS.lineage },
    style: {
      stroke: failed ? "#cf1322" : EDGE_COLORS[kind] || EDGE_COLORS.lineage,
      strokeWidth: kind === "probe" ? 2.4 : 1.6,
      strokeDasharray: failed || kind === "boundary" || kind === "backtrack" ? "6 5" : undefined,
    },
    labelStyle: {
      fill: EDGE_COLORS[kind] || EDGE_COLORS.lineage,
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
