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
  unknown: "未知",
};

export function buildControlledAITreeGraph(tree = {}, highlightedCandidateIds = []) {
  const layers = tree.layers || [];
  const highlighted = new Set(highlightedCandidateIds);
  const graphNodes = [];
  const graphEdges = [];
  const candidateIndex = new Map();
  const layerIndex = new Map();
  const edgeKeys = new Set();
  const finalLevel = tree.final_supported_level || "resource";
  const observedLevels = [];

  const pushEdge = (edge) => {
    if (!edge.source || !edge.target || edge.source === edge.target) return;
    const key = `${edge.source}->${edge.target}:${edge.data?.kind || "lineage"}:${edge.data?.edgeId || edge.id || ""}`;
    if (edgeKeys.has(key)) return;
    edgeKeys.add(key);
    graphEdges.push(edge);
  };

  graphNodes.push({
    id: "tree_start",
    type: "aiTreeNode",
    data: {
      nodeKind: "start",
      role: "start",
      title: "诊断起点",
      claim: "AI 树从粗粒度候选开始，再通过探针补证逐层收敛。",
      level: "resource",
      confidence: 1,
      badges: ["start"],
    },
  });

  for (const layer of layers) {
    const candidates = flattenLayerCandidates(layer);
    layerIndex.set(layer.layer_id, candidates);
    for (const candidate of candidates) {
      observedLevels.push(candidate.supported_level);
      const outsideFinalBoundary = isDeeperLevel(candidate.supported_level, finalLevel);
      const nodeId = nodeIdFor(layer.layer_id, candidate.candidate_id);
      candidateIndex.set(candidate.candidate_id, {
        nodeId,
        layer,
        candidate,
      });
      graphNodes.push({
        id: nodeId,
        type: "aiTreeNode",
        data: {
          nodeKind: "candidate",
          layerId: layer.layer_id,
          layerDepth: layer.depth,
          generatedBy: layer.generated_by,
          layerSummary: layer.summary,
          candidate,
          role: candidate.role,
          title: candidate.candidate_id,
          claim: candidate.claim,
          level: candidate.supported_level,
          confidence: candidate.confidence || 0,
          status: candidate.status,
          outsideFinalBoundary,
          highlighted: highlighted.has(candidate.candidate_id),
          badges: [
            `L${layer.depth}`,
            ROLE_LABELS[candidate.role] || candidate.role,
            candidate.supported_level,
            candidate.claim_type,
            candidate.conclusion_eligible ? "可进入结论" : "未过门禁",
            outsideFinalBoundary ? "已观察/未入终态" : "终态边界内",
            layer.generated_by === "ai_guarded" ? "AI" : "fallback",
          ],
        },
      });
    }
  }

  const firstLayer = layers[0] ? layerIndex.get(layers[0].layer_id) || [] : [];
  for (const candidate of firstLayer) {
    pushEdge({
      id: `start-${candidate.candidate_id}`,
      source: "tree_start",
      target: nodeIdFor(layers[0].layer_id, candidate.candidate_id),
      type: "smoothstep",
      label: "粗候选",
      data: { kind: "lineage", effect: "start" },
      animated: false,
    });
  }

  for (const layer of layers) {
    for (const candidate of layerIndex.get(layer.layer_id) || []) {
      for (const parentId of candidate.parent_candidate_ids || []) {
        const parent = candidateIndex.get(parentId);
        if (!parent) continue;
        pushEdge({
          id: `lineage-${parentId}-${candidate.candidate_id}`,
          source: parent.nodeId,
          target: nodeIdFor(layer.layer_id, candidate.candidate_id),
          type: "smoothstep",
          label: "细化",
          data: {
            kind: "lineage",
            effect: "refine",
            parentCandidateId: parentId,
            candidateId: candidate.candidate_id,
          },
        });
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

  const finalCandidateIds = [
    ...(tree.final_primary_causes || []),
    ...(tree.final_secondary_causes || []),
    ...(tree.final_unknown_causes || []),
  ];
  const finalSources = finalCandidateIds
    .map((candidateId) => candidateIndex.get(candidateId))
    .filter(Boolean);
  const fallbackFinalSources = finalSources.length
    ? finalSources
    : (layers.length ? layerIndex.get(layers[layers.length - 1].layer_id) || [] : [])
      .map((candidate) => candidateIndex.get(candidate.candidate_id))
      .filter(Boolean);

  graphNodes.push({
    id: "tree_stop",
    type: "aiTreeNode",
    data: {
      nodeKind: "stop",
      role: "stop",
      title: `正式结论停在 ${finalLevel}`,
      claim: boundaryClaim(tree.stop_reason, deepestLevel(observedLevels), finalLevel),
      level: finalLevel,
      confidence: levelProgress(finalLevel),
      badges: ["stop", finalLevel],
    },
  });
  for (const source of fallbackFinalSources) {
    pushEdge({
      id: `stop-${source.nodeId}`,
      source: source.nodeId,
      target: "tree_stop",
      type: "smoothstep",
      label: "停止边界",
      data: {
        kind: "boundary",
        effect: "stop",
        reason: tree.stop_reason,
      },
    });
  }

  return { nodes: graphNodes, edges: graphEdges };
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
