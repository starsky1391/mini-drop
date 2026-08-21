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
  const observedLevels = [];

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
        : candidate.conclusion_eligible
          ? candidate.role
          : "unknown";
      observedLevels.push(candidate.supported_level);
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
          title: candidate.candidate_id,
          claim: candidate.claim,
          level: candidate.supported_level,
          confidence: candidate.confidence || 0,
          status: candidate.status,
          layoutBand: candidate.depth_kind || "base",
          outsideFinalBoundary,
          highlighted: highlighted.has(candidate.candidate_id),
          badges: [
            `L${layer.depth}`,
            ROLE_LABELS[visualRole] || visualRole,
            candidate.supported_level,
            candidate.claim_type,
            candidate.conclusion_eligible ? "可进入结论" : "未过门禁",
            candidate.causal_status === "inconclusive" ? "探针未决" : candidate.causal_status,
            candidate.depth_kind === "mechanism" ? "机制链" : candidate.depth_kind === "boundary" ? "边界" : "基础定位",
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
        if (!parent) continue;
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

  for (const probeEdge of tree.probe_edges || []) {
    const fromNodes = resolveEdgeCandidates(probeEdge.from_candidate_ids, probeEdge.from_layer_id, layerIndex, candidateIndex);
    const toNodes = resolveEdgeCandidates(probeEdge.to_candidate_ids, probeEdge.to_layer_id, layerIndex, candidateIndex);
    for (const source of fromNodes) {
      for (const target of toNodes) {
        pushEdge({
          id: `probe-${probeEdge.edge_id}-${source.nodeId}-${target.nodeId}`,
          source: source.nodeId,
          target: target.nodeId,
          type: "smoothstep",
          label: probeEdge.transition_type === "backtrack" || probeEdge.effect === "rollback"
            ? "回溯转查"
            : probeEdge.probe_requests?.join(" + ") || "探针补证",
          data: {
            kind: probeEdge.transition_type === "backtrack" || probeEdge.effect === "rollback" ? "backtrack" : "probe",
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
  const stopSources = (tree.stop_source_candidate_ids || [])
    .filter((candidateId) => !ambiguousCandidateIds.has(candidateId))
    .map((candidateId) => candidateIndex.get(candidateId))
    .filter(Boolean);

  graphNodes.push({
    id: "tree_stop",
    type: "aiTreeNode",
    data: {
      nodeKind: "stop",
      layoutRole: "annotation",
      role: "stop",
      title: hasEligiblePrimary ? `正式结论停在 ${finalLevel}` : `当前证据边界停在 ${finalLevel}`,
      claim: boundaryClaim(tree.stop_reason, deepestLevel(observedLevels), finalLevel),
      level: finalLevel,
      confidence: levelProgress(finalLevel),
      layoutBand: "stop",
      badges: [hasEligiblePrimary ? "正式结论" : "未形成最终根因", finalLevel],
    },
  });
  for (const source of stopSources) {
    pushEdge({
      id: `stop-${source.nodeId}`,
      source: source.nodeId,
      target: "tree_stop",
      type: "smoothstep",
      label: "停止边界",
      data: {
        kind: "boundary",
        layoutRole: "annotation",
        effect: "stop",
        reason: tree.stop_reason,
      },
    });
  }

  return { nodes: graphNodes, edges: graphEdges, layoutEdges };
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

function boundaryClaim(reason, observedLevel, finalLevel) {
  const detail = reason || "当前证据边界不支持继续下钻。";
  if (!isDeeperLevel(observedLevel, finalLevel)) return detail;
  return `${detail} 已观察到的 ${observedLevel} 节点仍保留在树中，但未进入正式结论。`;
}

function nodeIdFor(layerId, candidateId) {
  return `${layerId}__${candidateId}`;
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
