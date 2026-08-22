#!/usr/bin/env python3
"""Run the Celery L4 case on the Docker VM and submit one real diagnosis."""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import shlex
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import paramiko


ROOT = Path(__file__).resolve().parent
VULNERABLE_REVISION = "a83070e5ec748c32325332db422756cfdd709aae"
FIXED_REVISION = "ca2d22204a47d7c7d553ae1408a099ab10caf055"
WORKER_IP = "172.18.90.144"
WORKER_USER = "worker1"
CONTROL_IP = "172.18.88.237"
CONTROL_USER = "control"
AGENT_ID = "linux-worker-1"
CONTROL_ENV_FILES = (
    "/home/control/mini-drop-active/deploy/env/control-native.env",
    "/home/control/mini-drop-active/deploy/env/control.env",
    "/home/control/mini-drop/deploy/env/control.env",
)
RUNTIME_FILES = {
    "Dockerfile",
    "compose.yml",
    "celery_case_tasks.py",
    "producer.py",
    "worker_entrypoint.py",
    "worker_monitor.py",
}
LOCAL_SOURCE_SEED = "/home/worker1/mini-drop-cases/celery_8882/celery-src"


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


WARMUP_COUNT = env_int("CELERY_L4_WARMUP_COUNT", 1000)
FAILURE_COUNT = env_int("CELERY_L4_FAILURE_COUNT", 1000)
FAILURE_BATCHES = env_int("CELERY_L4_FAILURE_BATCHES", 2)
WARMUP_SETTLE_SEC = env_float("CELERY_L4_WARMUP_SETTLE_SEC", 10.0)
FAILURE_TASK_SECONDS = env_float("CELERY_L4_FAILURE_TASK_SECONDS", 0.25)
FAILURE_PAYLOAD_BYTES = env_int("CELERY_L4_FAILURE_PAYLOAD_BYTES", 16384)
PRODUCER_INTERVAL_SEC = env_float("CELERY_L4_PRODUCER_INTERVAL_SEC", 0.0)
WORKER_POOL = os.environ.get("CELERY_L4_WORKER_POOL", "prefork")
WORKER_CONCURRENCY = env_int("CELERY_L4_WORKER_CONCURRENCY", 1)
BARRIER_GC_COLLECT = env_int("CELERY_L4_BARRIER_GC_COLLECT", 0)


def progress(message: str) -> None:
    print(f"[celery-l4] {message}", flush=True)


class Remote:
    def __init__(self, password: str):
        self.password = password
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(WORKER_IP, username=WORKER_USER, password=password, timeout=30)

    def close(self) -> None:
        self.client.close()

    def run(self, command: str, *, timeout: int = 600) -> str:
        _, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        output = stdout.read().decode("utf-8", "replace")
        error = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        if code:
            raise RuntimeError(f"worker command failed ({code}): {error[-1200:] or output[-1200:]}")
        return output

    def put_tree(self, local_root: Path, remote_root: str) -> None:
        sftp = self.client.open_sftp()
        try:
            for local in local_root.iterdir():
                if local.name not in RUNTIME_FILES:
                    continue
                if not local.is_file():
                    continue
                remote = posixpath.join(remote_root, local.name)
                sftp.put(str(local), remote)
        finally:
            sftp.close()

    def get_tree(self, remote_root: str, local_root: Path) -> None:
        local_root.mkdir(parents=True, exist_ok=True)
        sftp = self.client.open_sftp()
        try:
            for item in sftp.listdir_attr(remote_root):
                remote_path = posixpath.join(remote_root, item.filename)
                local_path = local_root / item.filename
                if item.st_mode & 0o40000:
                    self.get_tree(remote_path, local_path)
                else:
                    sftp.get(remote_path, str(local_path))
        finally:
            sftp.close()


def read_control_api_key(password: str) -> str:
    """Read the API key from the VM deployment without printing it."""

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(CONTROL_IP, username=CONTROL_USER, password=password, timeout=30)
        files = " ".join(shlex.quote(path) for path in CONTROL_ENV_FILES)
        command = (
            "for file in " + files + "; do "
            "[ -f \"$file\" ] || continue; "
            "grep '^MINI_DROP_API_KEY=' \"$file\" | tail -1 | cut -d= -f2-; "
            "done | tail -1"
        )
        _, stdout, _ = client.exec_command(command, timeout=30)
        value = stdout.read().decode("utf-8", "replace").strip()
        if not value or value.startswith("CHANGE_ME"):
            return ""
        return value
    finally:
        client.close()


class API:
    def __init__(self, api_key: str):
        self.base = f"https://{CONTROL_IP}"
        self.api_key = api_key
        self.context = ssl.create_default_context()
        self.context.check_hostname = False
        self.context.verify_mode = ssl.CERT_NONE

    def call(self, path: str, method: str = "GET", body: dict | None = None, timeout: int = 90) -> dict:
        request = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={
                "X-API-Key": self.api_key,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"API {method} {path} returned HTTP {exc.code}: {exc.read().decode()[:500]}") from exc
        if payload.get("code") != 0:
            raise RuntimeError(f"API {method} {path} failed: {payload.get('message')}")
        return payload.get("data") or {}


def diagnosis(
    api: API,
    target: dict,
    source_context: dict,
    timeout: int,
    time_range: dict,
) -> dict:
    started = api.call("/api/v1/diagnoses", "POST", {
        "query": "Celery worker 在持续提交失败任务期间 RSS 持续增长，请定位 worker 内部内存异常并提供可验证证据。",
        "context": {
            "service_id": "celery-worker",
            "environment": "staging",
            "time_range": time_range,
            "instances": [{**target, "source_context": source_context}],
            "source_context": source_context,
        },
        "budget_profile": "development",
        "auto_execute_policy": "all_registered",
    })
    diagnosis_id = started["diagnosis_id"]
    deadline = time.monotonic() + timeout
    approved: set[str] = set()
    latest: dict = {}
    while time.monotonic() < deadline:
        try:
            latest = api.call(f"/api/v1/diagnoses/{diagnosis_id}")
        except (TimeoutError, urllib.error.URLError):
            time.sleep(3)
            continue
        for probe in latest.get("probes") or []:
            step_id = probe.get("step_id")
            if probe.get("status") != "WAITING_APPROVAL" or not step_id or step_id in approved:
                continue
            api.call(f"/api/v1/diagnoses/{diagnosis_id}/approvals", "POST", {
                "step_id": step_id,
                "decision": "approve",
                "scope": "single_execution",
                "approver_id": "celery_l4_vm_runner",
            }, timeout=60)
            approved.add(step_id)
        if latest.get("status") in {
            "COMPLETED", "INSUFFICIENT_EVIDENCE", "PARTIAL_COMPLETED",
            "BUDGET_EXHAUSTED", "TOPOLOGY_UNAVAILABLE", "FAILED",
        }:
            return {"diagnosis_id": diagnosis_id, "detail": latest}
        time.sleep(3)
    if not latest:
        try:
            latest = api.call(f"/api/v1/diagnoses/{diagnosis_id}", timeout=60)
        except (TimeoutError, urllib.error.URLError):
            latest = {}
    return {
        "diagnosis_id": diagnosis_id,
        "detail": latest,
        "runner_status": "diagnosis_timeout",
        "terminal": False,
        "timeout_sec": timeout,
    }


def run_stage(
    remote: Remote,
    api_key: str | None,
    *,
    stage: str,
    revision: str,
    remote_root_value: str,
    duration_sec: int,
    diagnosis_timeout_sec: int,
    output_root: Path,
    stage_role: Literal["diagnosis_target", "regression_control"],
    diagnosis_mode: Literal["full", "none"],
    keep_running: bool,
) -> dict:
    if stage_role == "diagnosis_target" and diagnosis_mode == "full" and not api_key:
        raise ValueError("full diagnosis requires an API key")
    if stage_role == "regression_control" and diagnosis_mode != "none":
        raise ValueError("regression control must use diagnosis_mode=none")
    remote_root = shlex.quote(remote_root_value)
    source_root_value = f"{remote_root_value}/celery-src"
    evidence_root_value = f"{remote_root_value}/evidence/{stage_role}"
    source_root = shlex.quote(source_root_value)
    evidence_root = shlex.quote(evidence_root_value)
    project_name = "python_worker_failure_case"
    workload_timeout = duration_sec + 300
    worker_duration = max(
        workload_timeout,
        diagnosis_timeout_sec + duration_sec + 60
        if diagnosis_mode == "full"
        else duration_sec + 60,
    )
    progress(f"{stage_role}: preparing revision and evidence directory")
    remote.run(
        f"mkdir -p {remote_root} {evidence_root}; "
        f"rm -f {evidence_root}/producer_observations.ndjson "
        f"{evidence_root}/worker_observations.ndjson "
        f"{evidence_root}/task_observations.ndjson "
        f"{evidence_root}/runtime-manifest.json "
        f"{evidence_root}/worker.log"
    )
    progress(f"{stage_role}: building complete Celery checkout")
    remote.run(
        "set -e; "
        f"git -C {source_root} cat-file -e {shlex.quote(revision + '^{commit}')}; "
        f"git -C {source_root} checkout --detach {revision}; "
        f"cd {remote_root}; "
        f"CELERY_REVISION={revision} CELERY_SOURCE_ROOT={source_root} CELERY_EVIDENCE_ROOT={evidence_root} "
        f"CELERY_PRODUCER_DURATION_SEC={duration_sec} "
        f"CELERY_FAILURE_PAYLOAD_BYTES={FAILURE_PAYLOAD_BYTES} "
        f"CELERY_FAILURE_TASK_SECONDS={FAILURE_TASK_SECONDS} "
        f"CELERY_WORKER_POOL={WORKER_POOL} CELERY_WORKER_CONCURRENCY={WORKER_CONCURRENCY} "
        f"CELERY_BARRIER_GC_COLLECT={BARRIER_GC_COLLECT} "
        f"docker compose -p {project_name} -f compose.yml build",
        timeout=2400,
    )
    progress(f"{stage_role}: starting Redis, worker, monitor, and producer")
    remote.run(
        f"cd {remote_root}; CELERY_REVISION={revision} "
        f"CELERY_SOURCE_ROOT={source_root} CELERY_EVIDENCE_ROOT={evidence_root} "
        f"CELERY_PRODUCER_DURATION_SEC={duration_sec} CELERY_CASE_DURATION_SEC={worker_duration} "
        f"CELERY_FAILURE_PAYLOAD_BYTES={FAILURE_PAYLOAD_BYTES} "
        f"CELERY_FAILURE_TASK_SECONDS={FAILURE_TASK_SECONDS} "
        f"CELERY_WORKER_POOL={WORKER_POOL} CELERY_WORKER_CONCURRENCY={WORKER_CONCURRENCY} "
        f"CELERY_BARRIER_GC_COLLECT={BARRIER_GC_COLLECT} "
        f"docker compose -p {project_name} -f compose.yml up -d worker worker-monitor producer",
        timeout=180,
    )
    case_started = True
    try:
        remote.run(
            f"for attempt in $(seq 1 60); do "
            f"grep -q 'submission_sample' {evidence_root}/producer_observations.ndjson 2>/dev/null && exit 0; "
            "sleep 1; done; "
            "echo 'producer did not emit a submission sample' >&2; exit 1",
            timeout=90,
        )
        progress(f"{stage_role}: native apply_async workload started")
        time.sleep(5)
        vm_now = datetime.fromisoformat(remote.run("date -u +%Y-%m-%dT%H:%M:%S%z").strip())
        time_range = {
            "start": (vm_now - timedelta(seconds=duration_sec + 45)).astimezone(timezone.utc).isoformat(),
            "end": (
                vm_now + timedelta(seconds=max(90, duration_sec + 60))
            ).astimezone(timezone.utc).isoformat(),
        }
        inspected = remote.run(
            f"cd {remote_root}; export CELERY_SOURCE_ROOT={source_root} CELERY_EVIDENCE_ROOT={evidence_root}; "
            f"cid=$(docker compose -p {project_name} -f compose.yml ps -q worker); "
            "test -n \"$cid\"; "
            "main_pid=$(docker inspect -f '{{{{.State.Pid}}}}' \"$cid\"); "
            "container_id=$(docker inspect -f '{{{{.Id}}}}' \"$cid\"); "
            "target_pid=$(ps --no-headers -o pid=,ppid=,rss=,args= --ppid \"$main_pid\" "
            "| sort -k3 -nr | awk 'NR==1 {print $1}'); "
            "printf '%s|%s|%s\\n' \"${target_pid:-$main_pid}\" \"$main_pid\" \"$container_id\""
        ).strip().split("|", 1)
        pid = int(inspected[0])
        worker_main_pid, container_id = inspected[1].split("|", 1)
        source_context = {
            "source_paths": [f"/host{source_root_value}"],
            "repo_revision": revision,
            "language": "python",
            "container_workdir": "/opt/celery-src",
        }
        target = {
            "service_id": "celery-worker",
            "instance_id": f"celery-worker-{container_id[:12]}",
            "host_id": "worker1",
            "agent_id": AGENT_ID,
            "pid": pid,
            "container_id": container_id,
            "environment": "staging",
        }
        runtime_manifest = {
            "stage_role": stage_role,
            "diagnosis_mode": diagnosis_mode,
            "worker_pid": pid,
            "worker_main_pid": int(worker_main_pid),
            "container_id": container_id,
            "source_context": source_context,
            "target": target,
            "workload": {
                "duration_sec": duration_sec,
                "warmup_count": WARMUP_COUNT,
                "failure_count": FAILURE_COUNT,
                "failure_batches": FAILURE_BATCHES,
                "producer_interval_sec": PRODUCER_INTERVAL_SEC,
                "warmup_settle_sec": WARMUP_SETTLE_SEC,
                "failure_task_seconds": FAILURE_TASK_SECONDS,
                "failure_payload_bytes": FAILURE_PAYLOAD_BYTES,
                "worker_pool": WORKER_POOL,
                "worker_concurrency": WORKER_CONCURRENCY,
                "barrier_gc_collect": bool(BARRIER_GC_COLLECT),
                "worker_barriers": True,
                "tracemalloc_enabled": True,
                "queue": "failure-workload",
            },
            "collected_at": time.time(),
        }
        remote.run(
            f"python3 -c {shlex.quote('import json; print(json.dumps(' + repr(runtime_manifest) + '))')} "
            f"> {evidence_root}/runtime-manifest.json"
        )
        result = {"runtime_manifest": runtime_manifest}
        if diagnosis_mode == "full":
            progress(f"{stage_role}: starting the only Analyzer diagnosis")
            result["diagnosis"] = diagnosis(
                API(api_key or ""), target, source_context, diagnosis_timeout_sec, time_range
            )
            progress(f"{stage_role}: Analyzer diagnosis reached a terminal state")
        completion_timeout = max(180, duration_sec + 300)
        producer_completed = True
        try:
            remote.run(
                f"for attempt in $(seq 1 {completion_timeout}); do "
                f"grep -q 'producer_complete' {evidence_root}/producer_observations.ndjson 2>/dev/null && exit 0; "
                "sleep 1; done; "
                "echo 'producer did not complete the workload' >&2; exit 1",
                timeout=completion_timeout + 30,
            )
        except RuntimeError:
            producer_completed = False
            progress(f"{stage_role}: producer did not complete within the wait window; saving partial evidence")
        else:
            progress(f"{stage_role}: producer completed both worker-side barriers")
        return {
            "stage_role": stage_role,
            "diagnosis_mode": diagnosis_mode,
            "producer_completed": producer_completed,
            **result,
        }
    finally:
        if not keep_running:
            progress(f"{stage_role}: collecting logs and stopping runtime")
            try:
                remote.run(
                    f"cd {remote_root}; export CELERY_SOURCE_ROOT={source_root} CELERY_EVIDENCE_ROOT={evidence_root}; "
                    f"docker compose -p {project_name} -f compose.yml logs --no-color worker producer > {evidence_root}/worker.log 2>&1 || true; "
                    f"docker compose -p {project_name} -f compose.yml down",
                    timeout=180,
                )
            except Exception:
                pass
        if not keep_running:
            local_evidence = output_root / "evidence" / stage
            try:
                remote.get_tree(evidence_root_value, local_evidence)
            except Exception as exc:
                (local_evidence / "evidence-fetch-error.txt").parent.mkdir(parents=True, exist_ok=True)
                (local_evidence / "evidence-fetch-error.txt").write_text(str(exc), encoding="utf-8")
            else:
                progress(f"{stage_role}: evidence copied to local report")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote-root", default="/home/worker1/mini-drop-cases/python_worker_failure_case")
    parser.add_argument("--duration-sec", type=int, default=600)
    parser.add_argument("--diagnosis-timeout-sec", type=int, default=600)
    parser.add_argument(
        "--skip-diagnosis",
        action="store_true",
        help="仅跳过 vulnerable 阶段的诊断；fixed 始终只做无诊断回放",
    )
    parser.add_argument(
        "--vulnerable-only",
        action="store_true",
        help="兼容旧入口；vulnerable-only 现在是默认行为",
    )
    parser.add_argument(
        "--with-fixed-control",
        action="store_true",
        help="显式追加 fixed control replay；fixed 不创建 Analyzer 诊断",
    )
    parser.add_argument("--keep-running", action="store_true", help="保留最后一个 stage 的 VM 服务供人工检查")
    parser.add_argument("--output-json", default="")
    args = parser.parse_args()

    password = os.environ.get("MINI_DROP_VM_PASSWORD", "")
    if not password:
        parser.error("MINI_DROP_VM_PASSWORD is required")
    api_key = os.environ.get("MINI_DROP_API_KEY", "").strip()
    if not api_key and not args.skip_diagnosis:
        api_key = read_control_api_key(password)
    if not api_key and not args.skip_diagnosis:
        parser.error("MINI_DROP_API_KEY is unavailable from the local environment or Control deployment")

    if not args.output_json:
        parser.error("--output-json is required so the timestamped evidence path is explicit")
    output_path = Path(args.output_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    remote = Remote(password)
    try:
        remote.run(f"mkdir -p {shlex.quote(args.remote_root)}")
        remote.put_tree(ROOT, args.remote_root)
        remote.run(
            "set -e; "
            f"source_root={shlex.quote(args.remote_root + '/celery-src')}; "
            f"seed_root={shlex.quote(LOCAL_SOURCE_SEED)}; "
            "if [ ! -d \"$source_root/.git\" ]; then "
            "test -d \"$seed_root/.git\"; "
            "git clone --no-hardlinks \"$seed_root\" \"$source_root\"; "
            "fi",
            timeout=900,
        )
        result = {
            "case_id": "L4-CELERY-8882-EXCEPTION-MEMLEAK",
            "level": "L4_FULL_PROJECT_PR_CASE",
            "workload": {
                "duration_sec": args.duration_sec,
                "warmup_count": WARMUP_COUNT,
                "failure_count": FAILURE_COUNT,
                "failure_batches": FAILURE_BATCHES,
                "producer_interval_sec": PRODUCER_INTERVAL_SEC,
                "warmup_settle_sec": WARMUP_SETTLE_SEC,
                "failure_task_seconds": FAILURE_TASK_SECONDS,
                "failure_payload_bytes": FAILURE_PAYLOAD_BYTES,
                "worker_pool": WORKER_POOL,
                "worker_concurrency": WORKER_CONCURRENCY,
                "barrier_gc_collect": bool(BARRIER_GC_COLLECT),
                "queue": "failure-workload",
                "submission": "celery_case_tasks.unhandled_failure.apply_async",
                "control_submission": "celery_case_tasks.control_success.apply_async",
            },
        }
        progress("running vulnerable diagnosis target")
        result["vulnerable"] = run_stage(
            remote, api_key, stage="vulnerable", revision=VULNERABLE_REVISION,
            remote_root_value=args.remote_root, duration_sec=args.duration_sec,
            diagnosis_timeout_sec=args.diagnosis_timeout_sec, output_root=output_path.parent,
            stage_role="diagnosis_target",
            diagnosis_mode="none" if args.skip_diagnosis else "full",
            keep_running=False,
        )
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.with_fixed_control:
            progress("vulnerable-only run completed and saved; fixed control replay skipped by request")
            print(json.dumps({
                "output_json": str(output_path),
                "vulnerable": {
                    "stage_role": result["vulnerable"].get("stage_role"),
                    "diagnosis_mode": result["vulnerable"].get("diagnosis_mode"),
                    "diagnosis_id": ((result["vulnerable"].get("diagnosis") or {}).get("diagnosis_id")),
                },
                "fixed": None,
            }, ensure_ascii=False, indent=2))
            return 0
        progress("vulnerable stage saved; running fixed control replay without Analyzer")
        result["fixed"] = run_stage(
            remote, None, stage="fixed", revision=FIXED_REVISION,
            remote_root_value=args.remote_root, duration_sec=args.duration_sec,
            diagnosis_timeout_sec=args.diagnosis_timeout_sec, output_root=output_path.parent,
            stage_role="regression_control",
            diagnosis_mode="none",
            keep_running=args.keep_running,
        )
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        progress("pair run completed and saved")
        print(json.dumps({
            "output_json": str(output_path),
            "vulnerable": {
                "stage_role": result["vulnerable"].get("stage_role"),
                "diagnosis_mode": result["vulnerable"].get("diagnosis_mode"),
                "diagnosis_id": ((result["vulnerable"].get("diagnosis") or {}).get("diagnosis_id")),
            },
            "fixed": {
                "stage_role": result["fixed"].get("stage_role"),
                "diagnosis_mode": result["fixed"].get("diagnosis_mode"),
                "diagnosis_present": bool(result["fixed"].get("diagnosis")),
            },
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        remote.close()


if __name__ == "__main__":
    raise SystemExit(main())
