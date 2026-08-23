import assert from "node:assert/strict";
import test from "node:test";

import { buildControlledAITreeGraph } from "./aiTreeGraphModel.js";

const candidate = (overrides) => ({
  candidate_id: "candidate",
  role: "unknown",
  claim: "候选机制",
  supported_level: "line",
  confidence: 0.49,
  status: "missing_evidence",
  claim_type: "direct_failure_mechanism",
  causal_status: "inconclusive",
  decision: "backtrack",
  conclusion_eligible: false,
  evidence_refs: ["ev-source"],
  self_challenge: { missing_evidence: ["python_heap_reference"] },
  ...overrides,
});

test("inconclusive candidate remains unresolved and the graph does not invent a global stop", () => {
  const graph = buildControlledAITreeGraph({
    final_supported_level: "line",
    final_primary_causes: [],
    layers: [{
      layer_id: "layer-1",
      depth: 1,
      generated_by: "ai_guarded",
      primary_causes: [candidate({ candidate_id: "possible-retention", role: "primary" })],
      secondary_causes: [],
      rejected_causes: [],
      unknown_causes: [],
    }],
    probe_edges: [],
  });

  const unresolved = graph.nodes.find((node) => node.data?.candidate?.candidate_id === "possible-retention");

  assert.equal(unresolved.data.role, "unknown");
  assert.equal(unresolved.data.candidate.causal_status, "inconclusive");
  assert.equal(graph.nodes.some((node) => node.id === "tree_stop"), false);
});

test("explicit counterevidence remains a rejected grey-node candidate", () => {
  const graph = buildControlledAITreeGraph({
    final_supported_level: "function",
    final_primary_causes: [],
    layers: [{
      layer_id: "layer-1",
      depth: 1,
      generated_by: "ai_guarded",
      primary_causes: [],
      secondary_causes: [],
      unknown_causes: [],
      rejected_causes: [candidate({
        candidate_id: "refuted-retention",
        role: "rejected",
        status: "contradicted",
        causal_status: "contradicted",
        decision: "reject_candidate",
      })],
    }],
    probe_edges: [],
  });

  const rejected = graph.nodes.find((node) => node.data?.candidate?.candidate_id === "refuted-retention");
  assert.equal(rejected.data.role, "rejected");
  assert.equal(rejected.data.candidate.status, "contradicted");
});

test("blocked and partial boundaries stay unresolved instead of becoming grey rejected nodes", () => {
  const graph = buildControlledAITreeGraph({
    final_supported_level: "function",
    layers: [{
      layer_id: "layer-1",
      depth: 1,
      primary_causes: [],
      secondary_causes: [],
      rejected_causes: [],
      unknown_causes: [
        candidate({
          candidate_id: "blocked-child",
          role: "rejected",
          status: "blocked",
          causal_status: "inconclusive",
        }),
        candidate({
          candidate_id: "partial-child",
          role: "rejected",
          status: "partial",
          causal_status: "unproven",
        }),
      ],
    }],
    probe_edges: [],
  });

  const blocked = graph.nodes.find((node) => node.data?.candidate?.candidate_id === "blocked-child");
  const partial = graph.nodes.find((node) => node.data?.candidate?.candidate_id === "partial-child");
  assert.equal(blocked.data.role, "unknown");
  assert.equal(partial.data.role, "unknown");
  assert.ok(blocked.data.badges.includes("证据边界"));
  assert.ok(partial.data.badges.includes("证据边界"));
});

test("legacy forbidden boundary is rendered unresolved even with stale contradicted causal status", () => {
  const graph = buildControlledAITreeGraph({
    final_supported_level: "function",
    final_primary_causes: [],
    layers: [{
      layer_id: "layer-1",
      depth: 1,
      generated_by: "analyzer_fallback",
      primary_causes: [],
      secondary_causes: [],
      rejected_causes: [],
      unknown_causes: [candidate({
        candidate_id: "blocked-line-upgrade",
        status: "forbidden",
        causal_status: "contradicted",
      })],
    }],
    probe_edges: [],
  });

  const boundary = graph.nodes.find((node) => node.data?.candidate?.candidate_id === "blocked-line-upgrade");
  assert.equal(boundary.data.role, "unknown");
  assert.equal(boundary.data.candidate.status, "forbidden");
});

test("mechanism and stop nodes keep their explicit local parents", () => {
  const graph = buildControlledAITreeGraph({
    final_supported_level: "line",
    final_primary_causes: [],
    final_unknown_causes: ["base-retention", "mechanism-child", "gap-python_heap_reference"],
    layers: [
      {
        layer_id: "layer-0",
        depth: 0,
        generated_by: "analyzer_fallback",
        primary_causes: [candidate({
          candidate_id: "coarse-python-memory",
          role: "primary",
          supported_level: "resource",
          depth_kind: "base",
        })],
      },
      {
        layer_id: "layer-1",
        depth: 1,
        generated_by: "ai_guarded",
        unknown_causes: [
          candidate({
            candidate_id: "base-retention",
            parent_candidate_ids: ["coarse-python-memory"],
            depth_kind: "base",
          }),
          candidate({
            candidate_id: "mechanism-child",
            parent_candidate_ids: ["base-retention"],
            depth_kind: "mechanism",
          }),
        ],
      },
      {
        layer_id: "layer-2",
        depth: 2,
        generated_by: "ai_guarded",
        unknown_causes: [
          candidate({
            candidate_id: "gap-python_heap_reference",
            parent_candidate_ids: ["base-retention"],
            node_type: "stop_boundary",
            depth_kind: "boundary",
          }),
          candidate({
            candidate_id: "stop-base-retention",
            parent_candidate_ids: ["base-retention"],
            node_type: "stop_boundary",
            depth_kind: "boundary",
            claim: "当前分支停在 base-retention。",
          }),
        ],
      },
    ],
    probe_edges: [],
  });

  const mechanismNode = graph.nodes.find((node) => node.data?.candidate?.candidate_id === "mechanism-child");
  const baseToMechanism = graph.edges.find((edge) => edge.source.endsWith("__base-retention") && edge.target.endsWith("__mechanism-child"));
  const coarseToMechanism = graph.edges.find((edge) => edge.source.endsWith("__coarse-python-memory") && edge.target.endsWith("__mechanism-child"));
  const localStopEdge = graph.edges.find((edge) => edge.source.endsWith("__base-retention") && edge.target.endsWith("__stop-base-retention"));

  assert.ok(mechanismNode.data.badges.includes("机制链"));
  assert.ok(graph.nodes.some((node) => node.id.endsWith("__coarse-python-memory")));
  assert.ok(baseToMechanism);
  assert.equal(coarseToMechanism, undefined);
  assert.ok(localStopEdge);
  assert.equal(graph.layoutEdges.some((edge) => edge.target.endsWith("__stop-base-retention")), false);
  assert.equal(graph.nodes.some((node) => node.id === "tree_stop"), false);
});

test("stop boundary does not infer a source when the backend omits one", () => {
  const graph = buildControlledAITreeGraph({
    final_supported_level: "line",
    final_primary_causes: [],
    layers: [{
      layer_id: "layer-0",
      depth: 0,
      unknown_causes: [candidate({ candidate_id: "base-only" })],
    }],
    probe_edges: [],
  });

  assert.equal(graph.nodes.some((node) => node.id === "tree_stop"), false);
  assert.equal(graph.edges.length, 0);
});

test("missing parent renders an orphan marker instead of attaching to index zero", () => {
  const graph = buildControlledAITreeGraph({
    final_supported_level: "process",
    final_primary_causes: [],
    layers: [{
      layer_id: "layer-0",
      depth: 0,
      unknown_causes: [
        candidate({ candidate_id: "first-node", supported_level: "resource" }),
        candidate({
          candidate_id: "child-with-missing-parent",
          parent_candidate_ids: ["missing-parent"],
          supported_level: "process",
        }),
      ],
    }],
    probe_edges: [],
  });

  const orphan = graph.orphanNodes.find((node) => node.data?.nodeKind === "orphan");
  const guessedEdge = graph.edges.find((edge) => edge.source.endsWith("__first-node") && edge.target.endsWith("__child-with-missing-parent"));

  assert.ok(orphan);
  assert.equal(orphan.data.role, "orphan");
  assert.equal(graph.dataQuality.length, 1);
  assert.equal(graph.historyEdges.length, 0);
  assert.equal(graph.nodes.some((node) => node.data?.candidate?.candidate_id === "child-with-missing-parent"), false);
  assert.equal(guessedEdge, undefined);
});

test("coarse refine probe edge is hidden when parent-child edges already show the tree", () => {
  const graph = buildControlledAITreeGraph({
    final_supported_level: "call_path",
    layers: [
      {
        layer_id: "layer-0",
        depth: 0,
        unknown_causes: [candidate({ candidate_id: "coarse-root", supported_level: "resource" })],
      },
      {
        layer_id: "layer-1",
        depth: 1,
        unknown_causes: [
          candidate({
            candidate_id: "branch-a",
            parent_candidate_ids: ["coarse-root"],
            supported_level: "call_path",
          }),
          candidate({
            candidate_id: "branch-b",
            parent_candidate_ids: ["coarse-root"],
            supported_level: "service",
          }),
        ],
      },
    ],
    probe_edges: [{
      edge_id: "session_edge_coarse_to_supported_causes",
      from_layer_id: "layer-0",
      to_layer_id: "layer-1",
      from_candidate_ids: ["coarse-root"],
      to_candidate_ids: ["branch-a", "branch-b"],
      probe_requests: ["memory_map", "log_scan", "python_runtime_profile"],
      status: "completed",
      effect: "refined",
      transition_type: "refine",
    }],
  });

  assert.ok(graph.edges.some((edge) => edge.source.endsWith("__coarse-root") && edge.target.endsWith("__branch-a")));
  assert.ok(graph.edges.some((edge) => edge.source.endsWith("__coarse-root") && edge.target.endsWith("__branch-b")));
  assert.equal(graph.edges.some((edge) => edge.data?.edgeId === "session_edge_coarse_to_supported_causes"), false);
});

test("ordinary probe edges are hidden from the main graph while rollback and boundary remain", () => {
  const graph = buildControlledAITreeGraph({
    final_supported_level: "call_path",
    layers: [
      {
        layer_id: "layer-0",
        depth: 0,
        unknown_causes: [candidate({ candidate_id: "base-root", supported_level: "resource" })],
      },
      {
        layer_id: "layer-1",
        depth: 1,
        unknown_causes: [candidate({
          candidate_id: "base-child",
          parent_candidate_ids: ["base-root"],
          supported_level: "call_path",
        })],
      },
    ],
    probe_edges: [
      {
        edge_id: "probe-pending",
        from_candidate_ids: ["base-root"],
        to_candidate_ids: ["base-child"],
        transition_type: "probe",
        effect: "pending",
        status: "not_started",
      },
      {
        edge_id: "boundary-edge",
        from_candidate_ids: ["base-child"],
        to_candidate_ids: ["base-root"],
        transition_type: "boundary",
        effect: "no_change",
        status: "completed",
      },
      {
        edge_id: "rollback-edge",
        from_candidate_ids: ["base-child"],
        to_candidate_ids: ["base-root"],
        transition_type: "backtrack",
        effect: "rollback",
        status: "blocked",
      },
    ],
  });

  assert.equal(graph.edges.some((edge) => edge.data?.edgeId === "probe-pending"), false);
  assert.equal(graph.historyEdges.some((edge) => edge.data?.edgeId === "probe-pending"), true);
  assert.equal(graph.edges.some((edge) => edge.data?.edgeId === "boundary-edge" && edge.data.kind === "boundary"), true);
  assert.equal(graph.edges.some((edge) => edge.data?.edgeId === "rollback-edge"), true);
});

test("annotation placement never falls back to the first declared parent", () => {
  const graph = buildControlledAITreeGraph({
    layers: [
      {
        layer_id: "layer-0",
        depth: 0,
        unknown_causes: [
          candidate({ candidate_id: "first-parent", supported_level: "resource" }),
          candidate({ candidate_id: "actual-parent", supported_level: "resource" }),
        ],
      },
      {
        layer_id: "layer-1",
        depth: 1,
        unknown_causes: [candidate({
          candidate_id: "boundary-without-origin",
          parent_candidate_ids: ["actual-parent", "first-parent"],
          origin_parent_candidate_id: "",
          depth_kind: "boundary",
          node_type: "stop_boundary",
          supported_level: "line",
        })],
      },
    ],
  });

  assert.equal(graph.orphanNodes.length, 1);
  assert.equal(graph.edges.length, 0);
  assert.equal(graph.historyEdges.length, 0);
});

test("layer zero is the rendered root and the graph does not invent a start node", () => {
  const graph = buildControlledAITreeGraph({
    layers: [{
      layer_id: "layer-0",
      depth: 0,
      unknown_causes: [candidate({ candidate_id: "coarse-root" })],
    }],
    final_supported_level: "resource",
    stop_source_candidate_ids: [],
  });

  assert.equal(graph.nodes.some((node) => node.id === "tree_start"), false);
  assert.ok(graph.nodes.some((node) => node.id === "layer-0__coarse-root"));
});

test("child snapshot is not rendered as the session main tree", () => {
  const graph = buildControlledAITreeGraph({
    tree_kind: "child_snapshot",
    renderable: false,
    layers: [{
      layer_id: "child-layer",
      depth: 0,
      unknown_causes: [candidate({ candidate_id: "historical-hotspot" })],
    }],
  });

  assert.deepEqual(graph.nodes, []);
  assert.deepEqual(graph.edges, []);
  assert.deepEqual(graph.historyEdges, []);
  assert.deepEqual(graph.dataQuality, []);
  assert.deepEqual(graph.dataQualityErrors, ["child_snapshot_not_renderable"]);
});

test("duplicate candidate ids become a visible data-quality annotation", () => {
  const graph = buildControlledAITreeGraph({
    layers: [
      {
        layer_id: "layer-0",
        depth: 0,
        unknown_causes: [candidate({ candidate_id: "duplicate" })],
      },
      {
        layer_id: "layer-1",
        depth: 1,
        unknown_causes: [candidate({ candidate_id: "duplicate" })],
      },
    ],
  });

  assert.ok(graph.nodes.some((node) => node.data?.status === "duplicate_candidate_id"));
  assert.equal(graph.edges.length, 0);
});

test("parent candidate id 0 is a real parent and rollback uses the explicit origin", () => {
  const graph = buildControlledAITreeGraph({
    final_primary_causes: [],
    layers: [
      {
        layer_id: "layer-0",
        depth: 0,
        unknown_causes: [candidate({ candidate_id: "0", supported_level: "resource" })],
      },
      {
        layer_id: "layer-1",
        depth: 1,
        unknown_causes: [candidate({
          candidate_id: "child",
          parent_candidate_ids: ["0", "other"],
          origin_parent_candidate_id: "0",
        })],
      },
    ],
    probe_edges: [{
      edge_id: "rollback-child",
      from_layer_id: "layer-1",
      to_layer_id: "layer-0",
      from_candidate_ids: ["child"],
      to_candidate_ids: ["0"],
      status: "blocked",
      effect: "rollback",
      transition_type: "backtrack",
    }],
  });

  assert.ok(graph.edges.some((edge) => edge.source.endsWith("__0") && edge.target.endsWith("__child")));
  const rollback = graph.edges.find((edge) => edge.data?.kind === "backtrack");
  assert.ok(rollback);
  assert.equal(rollback.source.endsWith("__child"), true);
  assert.equal(rollback.target.endsWith("__0"), true);
  assert.equal(graph.layoutEdges.length, 1);
  assert.ok(graph.layoutEdges.every((edge) => edge.data?.layoutRole === "tree"));
  assert.equal(graph.nodes.some((node) => node.id === "tree_stop"), false);
  assert.ok(graph.edges.filter((edge) => edge.data?.layoutRole === "annotation").length >= 1);
});

test("source labels and observation lineage remain visible in the canonical tree", () => {
  const graph = buildControlledAITreeGraph({
    final_supported_level: "line",
    layers: [
      {
        layer_id: "layer-0",
        depth: 0,
        generated_by: "fallback_observation",
        unknown_causes: [candidate({
          candidate_id: "coarse-root",
          supported_level: "resource",
          node_type: "cluster_root",
          relation: "root",
        })],
      },
      {
        layer_id: "layer-1",
        depth: 1,
        generated_by: "analyzer_observation",
        unknown_causes: [candidate({
          candidate_id: "verified-line",
          parent_candidate_ids: ["coarse-root"],
          origin_parent_candidate_id: "coarse-root",
          supported_level: "line",
          node_type: "line_anchor",
        })],
      },
      {
        layer_id: "layer-2",
        depth: 2,
        generated_by: "ai_guarded",
        unknown_causes: [candidate({
          candidate_id: "runtime-observation",
          parent_candidate_ids: ["verified-line"],
          origin_parent_candidate_id: "verified-line",
          supported_level: "line",
          node_type: "observation",
          claim_type: "observation_only",
        })],
      },
    ],
    probe_edges: [{
      edge_id: "coarse-overview",
      from_candidate_ids: ["coarse-root"],
      to_candidate_ids: ["verified-line"],
      transition_type: "refine",
      effect: "refined",
      status: "completed",
    }],
  });

  const root = graph.nodes.find((node) => node.data?.candidate?.candidate_id === "coarse-root");
  const line = graph.nodes.find((node) => node.data?.candidate?.candidate_id === "verified-line");
  const observation = graph.nodes.find((node) => node.data?.candidate?.candidate_id === "runtime-observation");
  assert.ok(root.data.badges.includes("fallback"));
  assert.ok(line.data.badges.includes("fallback"));
  assert.ok(observation.data.badges.includes("AI"));
  assert.ok(graph.edges.some((edge) => edge.source.endsWith("__coarse-root") && edge.target.endsWith("__verified-line")));
  assert.ok(graph.edges.some((edge) => edge.source.endsWith("__verified-line") && edge.target.endsWith("__runtime-observation")));
  assert.equal(graph.edges.some((edge) => edge.data?.edgeId === "coarse-overview"), false);
});

test("unparented runtime observations are data-quality orphans, never guessed into the main tree", () => {
  const graph = buildControlledAITreeGraph({
    final_supported_level: "process",
    layers: [{
      layer_id: "layer-0",
      depth: 0,
      generated_by: "analyzer_observation",
      unknown_causes: [
        candidate({
          candidate_id: "python_runtime_stack_hotspot",
          supported_level: "process",
          node_type: "observation",
          relation: "evidence_context",
          claim_type: "observation_only",
        }),
        candidate({
          candidate_id: "python_userland_hotspot",
          supported_level: "process",
          node_type: "observation",
          relation: "evidence_context",
          claim_type: "observation_only",
        }),
      ],
    }],
    probe_edges: [{
      edge_id: "coarse-probe",
      from_candidate_ids: ["python_runtime_stack_hotspot"],
      to_candidate_ids: ["python_userland_hotspot"],
      transition_type: "refine",
      effect: "refined",
      status: "completed",
    }],
  });

  assert.equal(graph.nodes.some((node) => (
    node.data?.candidate?.candidate_id === "python_runtime_stack_hotspot"
  )), false);
  assert.equal(graph.nodes.some((node) => (
    node.data?.candidate?.candidate_id === "python_userland_hotspot"
  )), false);
  assert.equal(graph.orphanNodes.length, 2);
  assert.equal(graph.layoutEdges.length, 0);
  assert.equal(graph.edges.length, 0);
});

test("missing origin keeps a refinement out of the main tree even when a declared parent exists", () => {
  const graph = buildControlledAITreeGraph({
    final_supported_level: "process",
    layers: [{
      layer_id: "layer-0",
      depth: 0,
      unknown_causes: [candidate({
        candidate_id: "coarse-root",
        relation: "root",
      })],
    }, {
      layer_id: "layer-1",
      depth: 1,
      unknown_causes: [candidate({
        candidate_id: "refinement-without-origin",
        relation: "refinement",
        parent_candidate_ids: ["coarse-root", "other-root"],
        origin_parent_candidate_id: "",
        supported_level: "function",
      })],
    }],
  });

  assert.equal(graph.nodes.some((node) => (
    node.data?.candidate?.candidate_id === "refinement-without-origin"
  )), false);
  assert.equal(graph.orphanNodes.length, 1);
  assert.equal(graph.layoutEdges.length, 0);
});
