# Runner Release Handshake

## Goal

Keep the real-case target process and workload alive until the diagnosis
session has settled, including all AI follow-up probes needed for source-line
localization. The runner must not clean up merely because the diagnosis has
entered a terminal status.

## Scope

This change covers:

- Server-side release eligibility and persisted runner control state.
- Diagnosis detail API exposure of the release signal.
- PR case VM runner polling and cleanup ordering.
- Tests for settled, unsettled, and timeout paths.

This change does not add a server-to-runner inbound callback. The runner keeps
using the existing control-plane polling path.

## Contract

The diagnosis detail response exposes:

```json
{
  "runner_control": {
    "release_requested": true,
    "reason": "diagnosis_settled",
    "released_at": "2026-08-24T00:00:00+00:00",
    "outstanding_probes": []
  }
}
```

`release_requested` is true only when:

- The diagnosis status is terminal.
- No diagnosis probe is `PLANNED`, `WAITING_APPROVAL`, `SCHEDULED`, or
  `RUNNING`.
- No follow-up source snapshot or other probe task is still outstanding.

Terminal diagnosis status and runner release are intentionally separate
signals. A terminal status with outstanding work keeps the runner alive.

The runner has a bounded timeout fallback. If the release signal is not
received before the configured diagnosis timeout, it records
`runner_release_reason=diagnosis_timeout`, preserves the latest diagnosis
detail, and then performs cleanup. The fallback is an operational safety
limit, not a successful diagnosis settlement.

## Data Flow

```text
runner starts target and workload
  -> runner creates diagnosis
  -> server schedules and analyzes probes
  -> server settles diagnosis and all probes
  -> server persists runner_control.release_requested
  -> runner observes release signal
  -> runner saves manifest/report
  -> runner stops workload and containers
```

The report records:

- `runner_release_reason`
- `runner_release_at`
- `diagnosis_terminal_status`
- `outstanding_probes_at_release`
- `runner_release_timeout` when the fallback path is used

## Server Behavior

The orchestrator computes release eligibility after each analysis/update.
Eligibility is derived from the current session and probe records rather than
from a single task status. The server must not release the runner while a
follow-up can still produce runtime or source-line evidence.

The control state is persisted with the diagnosis session so that repeated API
reads return the same signal and timestamp.

## Runner Behavior

The PR case runner continues polling the diagnosis detail API. It:

1. Approves any explicitly waiting registered probes according to the current
   runner policy.
2. Waits for `runner_control.release_requested=true`.
3. Returns the latest diagnosis detail and release metadata.
4. Only then waits for final evidence files and executes container cleanup.

The runner must not infer release from `status` alone.

## Failure Handling

- Control-plane transient read failures do not immediately stop the workload.
- A diagnosis timeout triggers bounded cleanup and is reported distinctly.
- Missing `runner_control` in an older server response falls back to the
  existing terminal-status behavior only for compatibility; current server
  responses must always provide the field.
- Cleanup remains in the runner `finally` block.

## Validation

Focused tests must cover:

1. Terminal status with outstanding probes does not request release.
2. Terminal status with no outstanding probes requests release.
3. Runner waits for release instead of stopping on terminal status alone.
4. Diagnosis timeout records the timeout release reason.
5. Release metadata is preserved in the runtime report.

The real VM case should verify that the target PID remains present until the
source snapshot and runtime profiler follow-ups finish, or until the explicit
diagnosis timeout fallback is recorded.
