# Tasks: External-Runner PR Cases

## Shared runner contract

- [x] T001 Define `vulnerable` and `pair` modes.
- [x] T002 Keep container lifecycle, revision checkout, workload launch, timeout, cleanup, and raw evidence collection in the external runner.
- [x] T003 Keep Agent inputs generic and exclude issue, PR, answer, phase, batch, and fixed/vulnerable labels.
- [x] T004 Add VM capability preflight and explicit blocked results for unsupported hardware.
- [x] T005 Add per-case vulnerable-only one-shot entrypoints so repeated VM case runs do not require re-entering standard runner settings.
- [x] T006 Make vulnerable-only the default acceptance path with a 400 second workload and a 1200 second diagnosis wait window.
- [x] T007 Keep pair control replay opt-in only and out of normal acceptance.

## Case adapters

- [x] T010 Add Playwright Python #3004 adapter and concurrent shutdown workload.
- [x] T011 Add aiohttp #10570 large-stream workload.
- [x] T012 Add kafka_2286 broker and AdminClient workload.
- [x] T013 Add PyAV #751 threaded logging/media workload.
- [x] T014 Exclude `vllm_38602` from the accepted case set and keep it out of normal validation.
- [x] T015 Add requests #5891/#5924 CPU hotspot adapter and local HTTP workload.
- [x] T016 Add Starlette #1868/#1940 endpoint latency adapter and streaming middleware workload.
- [x] T017 Add Celery #9849/#9853 ETA queue backlog adapter with real Redis, worker, and native producer.
- [x] T018 Add urllib3 #2197 pool exhaustion adapter and concurrent blocking-pool workload.
- [x] T019 Add urllib3 #2494 retry/backoff adapter and local 503 workload.
- [x] T020 Add requests-cache #1050/#1052 cache growth adapter and filesystem cache workload.
- [x] T021 Add pandas #58084 input-triggered slow-path adapter and categorical groupby workload.

## Validation

- [x] T030 Run Playwright vulnerable case on the VM.
- [x] T031 Run aiohttp vulnerable case on the VM.
- [x] T032 Run kafka-python vulnerable case on the VM.
- [x] T033 Run PyAV vulnerable case on the VM.
- [x] T034 Run an aiohttp pair control replay on the VM after vulnerable-only runs were stable.
- [x] T035 Record the `vllm_38602` GPU prerequisite block without treating it as an accepted case.
- [x] T036 Run `requests_5891` vulnerable case on the VM.
- [x] T037 Run `starlette_1868` vulnerable case on the VM.
- [x] T038 Run `celery_9849` vulnerable case on the VM.
- [x] T039 Run `urllib3_2197` vulnerable case on the VM.
- [x] T040 Run `urllib3_2494` vulnerable case on the VM.
- [x] T041 Run `requests_cache_1050` vulnerable case on the VM.
- [x] T042 Run `pandas_58084` vulnerable case on the VM.
