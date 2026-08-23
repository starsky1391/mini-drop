# Mini-Drop adapter

Pinned source is `651c450867c4d6db26cc78de5928bb14f7b3c3b9`. Use Mini-Drop's
Evidence/Case runtime only through a read-only replay projection. Native
collectors are appendix-only; no network, shell, remediation or raw export is
allowed in the common track.

## Execution status

Executed with DeepSeek `deepseek-v4-flash` through the benchmark-owned unified replay adapter. Upstream runtime not executed in this environment.

## Frozen evidence package input

Mini-Drop may also submit a completed Persistent Watch incident as a
read-only evidence package. Call the Watch incident analysis endpoint and
export the resulting audit bundle with the returned `analysis_<incident_id>`
identifier. The package keeps its original `evidence_cohort_id`,
`rolling_snapshot`, and `same_window` metadata.

The frozen path consumes only saved `structured_evidence`. Package hits are
reported through existing evidence references; package misses remain bounded
with `missing_evidence`, `evidence_package_exhausted`, and `probe_count=0`.
It does not create a live collector, approval item, delayed follow-up, or a
second `hybrid` mode. Ordinary diagnosis requests continue to use
`live_collection`.
