# Four Real PR Cases

These cases use the same external-runner contract as the Celery case.

## Modes

- `vulnerable`: checkout the pre-fix revision, start the real workload, create one Analyzer diagnosis, and save raw runtime evidence.
- `pair`: run the vulnerable diagnosis target, then replay the identical workload on the fixed revision without creating an Analyzer diagnosis. Use this only when you explicitly want an offline control replay.

The Agent receives only generic target information: PID, container ID, source root, revision, time window, and service identity. It does not receive workload phases, task counts, issue numbers, PR numbers, or fixed/vulnerable labels.

## Cases

| Case | Runtime signal | Primary workload | VM status |
|---|---|---|---|
| `playwright_3004` | async shutdown / event-loop stall | concurrent browser operations followed by `playwright.stop()` | CPU-only runnable |
| `aiohttp_10570` | large-stream read stall / excessive buffering | local aiohttp server returns a response larger than the stream limit | CPU-only runnable |
| `kafka_2286` | broker wakeup / request timeout | real Kafka broker plus repeated AdminClient requests | CPU-only runnable |
| `pyav_751` | FFmpeg logging contention during threaded media work | threaded PyAV logging and decode/encode workload | CPU-only runnable |

`vllm_38602` remains a preflight-only blocked scenario on this VM and is not part of the accepted case set.

Each directory contains an adapter manifest, Dockerfile, Compose file, workload, monitor, README, and tasks checklist. The shared VM runner is `run_pr_case_vm.py`.

## Runner

```powershell
python run_pr_case_vm.py --case playwright_3004
python run_pr_case_vm.py --case aiohttp_10570 --mode pair
```

The runner creates timestamped output under `reports/eval/real-open-source/pr-cases/`. It builds and controls the containers from the VM host, waits for diagnosis or workload timeout, collects evidence, and always performs Compose cleanup. The default run is vulnerable-only with a 600 second workload and diagnosis window.

For normal repeated vulnerable-only runs, use the case-local one-shot entrypoint. It pins the case and standard defaults so future real-case runs do not require re-entering duration, diagnosis timeout, output settings, or even the case name:

```powershell
.\playwright_3004\run_vulnerable_only.ps1
.\kafka_2286\run_vulnerable_only.ps1
.\pyav_751\run_vulnerable_only.ps1
```

The shared wrapper remains available when automation needs to choose a case dynamically:

```powershell
.\run_vulnerable_only.ps1 -Case playwright_3004
```

## Current VM Validation

Vulnerable-only 600 second VM runs have completed for `playwright_3004`, `aiohttp_10570`, `kafka_2286`, and `pyav_751`. The `vllm_38602` preflight records `nvidia_gpu_unavailable` on the current VM.
