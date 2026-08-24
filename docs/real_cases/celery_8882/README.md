# L4-CELERY-8882-EXCEPTION-MEMLEAK

The normal run executes the real Celery project at the pre-fix revision for a
400 second vulnerable-only diagnosis window. The fixed revision is an explicit
offline control replay and is not part of routine real-case runs.
The diagnosis input must not contain the issue number, pull request number, or
the hidden oracle. The oracle is only for offline evaluation.

## Runtime

The case has three containers:

- `redis`: the broker used by Celery.
- `worker`: a native `celery worker` process using the stage revision.
- `worker-monitor`: a small monitor sharing the worker PID namespace. It
  records the native Celery PID and RSS without invoking Docker CLI.
- `producer`: a Python process that submits failing tasks with
  `apply_async()` and successful control tasks with `delay()`.

The worker entrypoint `exec`s the native Celery command, so the target PID
reported by Docker is the Celery worker itself. The monitor records its RSS,
cgroup-derived container identity, source root, revision, and container
workdir. Neither process calls the Docker CLI. Container inspection is a host
control-plane operation done by the VM runner.

## Prepare the full project

The Docker build intentionally requires a checkout of the complete Celery
repository. The VM runner performs this step on the worker host for each
stage:

```bash
git clone https://github.com/celery/celery.git celery-src
git -C celery-src checkout a83070e5ec748c32325332db422756cfdd709aae
CELERY_REVISION=a83070e5ec748c32325332db422756cfdd709aae \
  docker compose -f compose.yml build --no-cache

git -C celery-src checkout ca2d22204a47d7c7d553ae1408a099ab10caf055
CELERY_REVISION=ca2d22204a47d7c7d553ae1408a099ab10caf055 \
  docker compose -f compose.yml build --no-cache
```

The checkout is bind-mounted into the worker and producer at `/opt/celery-src`
so the source snapshot path is visible to the Mini-Drop Agent through the
worker host's `/home` mount. The image also installs from that exact checkout.

## Run

For normal repeated vulnerable-only VM runs, use the one-shot entrypoint. It
uses the standard 400 second workload and diagnosis window and creates a
timestamped report directory:

```powershell
.\run_vulnerable_only.ps1
```

The lower-level runner has the same default behavior when `--output-json` is
provided:

```bash
python run_case_vm.py --output-json reports/celery-8882-vulnerable/run.json
```

Run the fixed control replay only when an explicit offline pair comparison is
needed:

```bash
python run_case_vm.py --with-fixed-control \
  --output-json reports/celery-8882-pair/run.json

python evaluate_case.py \
  --run-json reports/celery-8882-pair/run.json \
  --output-json reports/celery-8882-pair/oracle-result.json
```

Keep the worker running while Mini-Drop collects evidence. The VM runner does
this automatically. For a manual run, use a second terminal to inspect
`evidence/worker_observations.ndjson` and the worker container PID, then stop
the stack after the diagnosis is complete.

## Diagnosis contract

The runtime target must contain:

- the live worker PID and container ID;
- the host-visible source root;
- `repo_revision` equal to the vulnerable revision;
- `container_workdir` equal to `/opt/celery-src`.

The source mechanism probe may only upgrade a base node when it has a verified
line anchor in that revision. Mechanism nodes are an explanatory branch under
the line node; they are not additional primary causes.

## Hidden oracle

`oracle.json` is intentionally separate from the diagnosis request. It is
read only by `evaluate_case.py` after both VM stages finish and must never be
copied into the Agent, Control API request, Docker image, or prompt context.
