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

test("inconclusive candidate remains unresolved and stop node is not a formal conclusion", () => {
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
  const stop = graph.nodes.find((node) => node.id === "tree_stop");

  assert.equal(unresolved.data.role, "unknown");
  assert.equal(unresolved.data.candidate.causal_status, "inconclusive");
  assert.match(stop.data.title, /^当前证据边界/);
  assert.ok(stop.data.badges.includes("未形成最终根因"));
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

test("mechanism nodes keep their parent and stop edge avoids boundary nodes", () => {
  const graph = buildControlledAITreeGraph({
    final_supported_level: "line",
    final_primary_causes: [],
    final_unknown_causes: ["base-retention", "mechanism-child", "gap-python_heap_reference"],
    stop_source_candidate_ids: ["base-retention"],
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
        unknown_causes: [candidate({
          candidate_id: "gap-python_heap_reference",
          parent_candidate_ids: ["base-retention"],
          depth_kind: "boundary",
        })],
      },
    ],
    probe_edges: [],
  });

  const mechanismNode = graph.nodes.find((node) => node.data?.candidate?.candidate_id === "mechanism-child");
  const baseToMechanism = graph.edges.find((edge) => edge.source.endsWith("__base-retention") && edge.target.endsWith("__mechanism-child"));
  const coarseToMechanism = graph.edges.find((edge) => edge.source.endsWith("__coarse-python-memory") && edge.target.endsWith("__mechanism-child"));
  const stopEdges = graph.edges.filter((edge) => edge.target === "tree_stop");

  assert.ok(mechanismNode.data.badges.includes("机制链"));
  assert.ok(graph.nodes.some((node) => node.id.endsWith("__coarse-python-memory")));
  assert.ok(baseToMechanism);
  assert.equal(coarseToMechanism, undefined);
  assert.equal(stopEdges.length, 1);
  assert.ok(stopEdges[0].source.endsWith("__base-retention"));
});

test("stop node does not infer a source when the backend omits one", () => {
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

  assert.equal(graph.edges.filter((edge) => edge.target === "tree_stop").length, 0);
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

test("parent candidate id 0 is a real parent and rollback uses the explicit origin", () => {
  const graph = buildControlledAITreeGraph({
    final_primary_causes: [],
    stop_source_candidate_ids: ["0"],
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
  assert.equal(graph.nodes.find((node) => node.id === "tree_stop").data.layoutRole, "annotation");
  assert.ok(graph.edges.filter((edge) => edge.data?.layoutRole === "annotation").length >= 2);
});
