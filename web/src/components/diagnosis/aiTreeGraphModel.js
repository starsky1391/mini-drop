const LEVEL_ORDER = [
  "resource",
  "host",
  "process",
  "thread",
  "syscall",
  "dependency",
  "service",
  "endpoint",
  "function",
  "call_path",
  "line",
];

export const ROLE_LABELS = {
  primary: "主因",
  secondary: "次因",
  rejected: "反证",
  unknown: "未决",
};

export function buildControlledAITreeGraph(tree = {}, highlightedCandidateIds = []) {
  const layers = tree.layers || [];
  const highlighted = new Set(highlightedCandidateIds);
  const graphNodes = [];
  const graphEdges = [];
  const layoutEdges = [];
  const candidateIndex = new Map();
  const ambiguousCandidateIds = new Set();
  const layerIndex = new Map();
  const edgeKeys = new Set();
  const finalLevel = tree.final_supported_level || "resource";

  const pushEdge = (edge) => {
    if (!edge.source || !edge.target || edge.source === edge.target) return false;
    const key = `${edge.source}->${edge.target}:${edge.data?.kind || "lineage"}:${edge.data?.edgeId || edge.id || ""}`;
    if (edgeKeys.has(key)) return false;
    edgeKeys.add(key);
    graphEdges.push(edge);
    return true;
  };

  for (const layer of layers) {
    const candidates = flattenLayerCandidates(layer);
    layerIndex.set(layer.layer_id, candidates);
    for (const candidate of candidates) {
      const visualRole = candidate.role === "rejected" || ["contradicted", "rejected"].includes(candidate.status)
        ? "rejected"
        : candidate.node_type === "stop_boundary" || candidate.depth_kind === "boundary"
          ? "unknown"
        : candidate.conclusion_eligible
          ? candidate.role
          : "unknown";
      const outsideFinalBoundary = isDeeperLevel(candidate.supported_level, finalLevel);
      const nodeId = nodeIdFor(layer.layer_id, candidate.candidate_id);
      if (candidateIndex.has(candidate.candidate_id)) {
        ambiguousCandidateIds.add(candidate.candidate_id);
      } else {
        candidateIndex.set(candidate.candidate_id, {
          nodeId,
          layer,
          candidate,
        });
      }
      graphNodes.push({
        id: nodeId,
        type: "aiTreeNode",
        data: {
          nodeKind: "candidate",
          layoutRole: "tree",
          layerId: layer.layer_id,
          layerDepth: layer.depth,
          generatedBy: layer.generated_by,
          layerSummary: layer.summary,
          candidate,
          role: visualRole,
          nodeType: candidate.node_type || nodeTypeForDepth(candidate.depth_kind, candidate.supported_level, visualRole),
          title: candidate.candidate_id,
          claim: candidate.claim,
          level: candidate.supported_level,
          confidence: candidate.confidence || 0,
          status: candidate.status,
          layoutBand: candidate.node_type || candidate.depth_kind || "base",
          outsideFinalBoundary,
          highlighted: highlighted.has(candidate.candidate_id),
          badges: [
            `L${layer.depth}`,
            ROLE_LABELS[visualRole] || visualRole,
            candidate.supported_level,
            candidate.claim_type,
            candidate.conclusion_eligible ? "可进入结论" : "未过门禁",
            candidate.causal_status === "inconclusive" ? "探针未决" : candidate.causal_status,
            candidate.node_type === "stop_boundary" ? "局部STOP" : candidate.node_type === "observation" ? "观察上下文" : candidate.node_type === "mechanism_explanation" || candidate.depth_kind === "mechanism" ? "机制链" : candidate.depth_kind === "boundary" ? "边界" : "基础定位",
            outsideFinalBoundary ? "已观察/未入终态" : "终态边界内",
            layer.generated_by === "ai_guarded" ? "AI" : "fallback",
          ],
        },
      });
    }
  }

  for (const layer of layers) {
    for (const candidate of layerIndex.get(layer.layer_id) || []) {
      const parentIds = Array.isArray(candidate.parent_candidate_ids)
        ? candidate.parent_candidate_ids
        : [];
      for (const parentId of parentIds) {
        if (ambiguousCandidateIds.has(parentId)) continue;
        const parent = candidateIndex.get(parentId);
        if (!parent) {
          graphNodes.push(orphanNodeFor(layer, candidate, parentId));
          continue;
        }
        const depthKind = candidate.depth_kind || "base";
        if (
          depthKind === "mechanism"
          && (parent.candidate.depth_kind !== "base" || parent.candidate.supported_level !== "line")
        ) continue;
        if (pushEdge({
          id: `lineage-${parentId}-${candidate.candidate_id}`,
          source: parent.nodeId,
          target: nodeIdFor(layer.layer_id, candidate.candidate_id),
          type: "smoothstep",
          label: depthKind === "mechanism" ? "机制展开" : depthKind === "boundary" ? "证据边界" : "定位细化",
          data: {
            kind: depthKind === "mechanism" ? "mechanism" : depthKind === "boundary" ? "boundary" : "lineage",
            layoutRole: "tree",
            effect: "refine",
            parentCandidateId: parentId,
            candidateId: candidate.candidate_id,
          },
        })) {
          layoutEdges.push(graphEdges[graphEdges.length - 1]);
        }
      }
    }
  }

  const lineagePairs = new Set(
    graphEdges
      .filter((edge) => edge.data?.kind === "lineage" || edge.data?.kind === "mechanism" || edge.data?.kind === "boundary")
      .map((edge) => `${edge.source}->${edge.target}`),
  );

  for (const probeEdge of tree.probe_edges || []) {
    const fromNodes = resolveEdgeCandidates(probeEdge.from_candidate_ids, probeEdge.from_layer_id, layerIndex, candidateIndex);
    const toNodes = resolveEdgeCandidates(probeEdge.to_candidate_ids, probeEdge.to_layer_id, layerIndex, candidateIndex);
    if (!shouldRenderProbeEdge(probeEdge, fromNodes, toNodes, lineagePairs)) {
      continue;
    }
    for (const source of fromNodes) {
      for (const target of toNodes) {
        pushEdge({
          id: `probe-${probeEdge.edge_id}-${source.nodeId}-${target.nodeId}`,
          source: source.nodeId,
          target: target.nodeId,
          type: "smoothstep",
          label: probeEdge.transition_type === "backtrack" || probeEdge.effect === "rollback"
            ? "回溯转查"
            : probeEdge.transition_type === "boundary"
              ? "证据边界"
            : probeEdge.probe_requests?.join(" + ") || "探针补证",
          data: {
            kind: probeEdge.transition_type === "backtrack" || probeEdge.effect === "rollback" ? "backtrack" : probeEdge.transition_type === "boundary" ? "boundary" : "probe",
            layoutRole: "annotation",
            edgeId: probeEdge.edge_id,
            status: probeEdge.status,
            effect: probeEdge.effect,
            reuseStatus: probeEdge.reuse_status,
            reason: probeEdge.reason,
            probeRequests: probeEdge.probe_requests || [],
            evidenceRefs: [
              ...(probeEdge.evidence_refs || []),
              ...((probeEdge.probe_results || []).flatMap((item) => item.evidence_refs || [])),
            ],
            probeResults: probeEdge.probe_results || [],
          },
          animated: probeEdge.transition_type !== "backtrack"
            && ["pending", "not_started", "unknown"].includes(probeEdge.status || "unknown"),
        });
      }
    }
  }

  const hasEligiblePrimary = (tree.final_primary_causes || [])
    .map((candidateId) => candidateIndex.get(candidateId))
    .some((item) => item?.candidate?.depth_kind === "base" && item.candidate.conclusion_eligible);

  return { nodes: graphNodes, edges: graphEdges, layoutEdges, hasEligiblePrimary };
}

function isRedundantRefineProbeEdge(probeEdge, fromNodes, toNodes, lineagePairs) {
  if (probeEdge.transition_type !== "refine" && probeEdge.effect !== "refined") return false;
  if (!fromNodes.length || !toNodes.length) return false;
  return toNodes.every((target) => (
    fromNodes.some((source) => lineagePairs.has(`${source.nodeId}->${target.nodeId}`))
  ));
}

function shouldRenderProbeEdge(probeEdge, fromNodes, toNodes, lineagePairs) {
  if (isRedundantRefineProbeEdge(probeEdge, fromNodes, toNodes, lineagePairs)) return false;
  return probeEdge.transition_type === "backtrack"
    || probeEdge.effect === "rollback"
    || probeEdge.transition_type === "boundary";
}

export function flattenLayerCandidates(layer = {}) {
  return [
    ...(layer.primary_causes || []),
    ...(layer.secondary_causes || []),
    ...(layer.rejected_causes || []),
    ...(layer.unknown_causes || []),
  ];
}

export function levelProgress(level) {
  const index = LEVEL_ORDER.indexOf(level);
  return index < 0 ? 0 : index / (LEVEL_ORDER.length - 1);
}

export function isDeeperLevel(level, boundary) {
  const levelIndex = LEVEL_ORDER.indexOf(level);
  const boundaryIndex = LEVEL_ORDER.indexOf(boundary);
  return levelIndex >= 0 && boundaryIndex >= 0 && levelIndex > boundaryIndex;
}

export function deepestLevel(levels = []) {
  return levels.reduce(
    (deepest, level) => (isDeeperLevel(level, deepest) ? level : deepest),
    "resource",
  );
}

function nodeIdFor(layerId, candidateId) {
  return `${layerId}__${candidateId}`;
}

function nodeTypeForDepth(depthKind, level, role) {
  if (depthKind === "mechanism") return "mechanism_explanation";
  if (depthKind === "boundary") return "stop_boundary";
  if (role === "rejected") return "rejected_candidate";
  if (level === "line") return "line_anchor";
  if (level === "call_path") return "call_path_context";
  return "base_cause";
}

function orphanNodeFor(layer, candidate, missingParentId) {
  const id = `${nodeIdFor(layer.layer_id, candidate.candidate_id)}__missing_parent_${missingParentId}`;
  return {
    id,
    type: "aiTreeNode",
    data: {
      nodeKind: "orphan",
      layoutRole: "tree",
      layerId: layer.layer_id,
      layerDepth: layer.depth,
      generatedBy: layer.generated_by,
      layerSummary: layer.summary,
      role: "orphan",
      nodeType: "orphan",
      title: `missing parent: ${missingParentId}`,
      claim: `后端声明的父节点 ${missingParentId} 不存在，未把该节点挂到 index 0。`,
      level: candidate.supported_level,
      confidence: 0,
      status: "missing_parent",
      layoutBand: "orphan",
      badges: ["orphan", `child=${candidate.candidate_id}`],
    },
  };
}

function resolveEdgeCandidates(candidateIds, layerId, layerIndex, candidateIndex) {
  const explicit = (candidateIds || [])
    .map((candidateId) => candidateIndex.get(candidateId))
    .filter(Boolean);
  if (explicit.length) return explicit;
  if (!layerId) return [];
  return (layerIndex.get(layerId) || [])
    .map((candidate) => candidateIndex.get(candidate.candidate_id))
    .filter(Boolean);
}
