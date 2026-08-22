# Tasks: Four External-Runner PR Cases

## Shared runner contract

- [x] T001 Define `vulnerable` and `pair` modes.
- [x] T002 Keep container lifecycle, revision checkout, workload launch, timeout, cleanup, and raw evidence collection in the external runner.
- [x] T003 Keep Agent inputs generic and exclude issue, PR, answer, phase, batch, and fixed/vulnerable labels.
- [x] T004 Add VM capability preflight and explicit blocked results for unsupported hardware.
- [x] T005 Add per-case vulnerable-only one-shot entrypoints so repeated VM case runs do not require re-entering standard runner settings.
- [x] T006 Make vulnerable-only the default acceptance path with a 600 second workload and diagnosis window.
- [x] T007 Keep pair control replay opt-in only and out of normal acceptance.

## Case adapters

- [x] T010 Add Playwright Python #3004 adapter and concurrent shutdown workload.
- [x] T011 Add aiohttp #10570 large-stream workload.
- [x] T012 Add kafka_2286 broker and AdminClient workload.
- [x] T013 Add PyAV #751 threaded logging/media workload.
- [x] T014 Exclude `vllm_38602` from the accepted case set and keep it out of normal validation.

## Validation

- [x] T020 Run Playwright vulnerable case on the VM.
- [x] T021 Run aiohttp vulnerable case on the VM.
- [x] T022 Run kafka-python vulnerable case on the VM.
- [x] T023 Run PyAV vulnerable case on the VM.
- [x] T024 Run an aiohttp pair control replay on the VM after vulnerable-only runs were stable.
- [x] T025 Record the `vllm_38602` GPU prerequisite block without treating it as an accepted case.
