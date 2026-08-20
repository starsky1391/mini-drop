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
