#!/usr/bin/env python3
"""Run one external-runner PR case on the Docker VM."""

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

import paramiko


ROOT = Path(__file__).resolve().parent
WORKER_IP = "172.18.90.144"
WORKER_USER = "worker1"
CONTROL_IP = "172.18.88.237"
AGENT_ID = "linux-worker-1"
CONTROL_ENV_FILES = (
    "/home/control/mini-drop-active/deploy/env/control-native.env",
    "/home/control/mini-drop-active/deploy/env/control.env",
    "/home/control/mini-drop/deploy/env/control.env",
)
TERMINAL_STATUSES = {
    "COMPLETED", "INSUFFICIENT_EVIDENCE", "PARTIAL_COMPLETED",
    "BUDGET_EXHAUSTED", "TOPOLOGY_UNAVAILABLE", "FAILED",
}
RUNNER_RELEASE_TIMEOUT_REASON = "diagnosis_timeout"
DEFAULT_CASE_DURATION_SEC = 400
DEFAULT_DIAGNOSIS_TIMEOUT_SEC = 1200


def progress(message: str) -> None:
    print(f"[pr-case] {message}", flush=True)


class Remote:
    def __init__(self, password: str):
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
            raise RuntimeError(f"remote command failed ({code}): {error[-1200:] or output[-1200:]}")
        return output

    def put_tree(self, local_root: Path, remote_root: str) -> None:
        sftp = self.client.open_sftp()
        try:
            for path in local_root.iterdir():
                if path.is_file():
                    sftp.put(str(path), posixpath.join(remote_root, path.name))
            shared_lifecycle = local_root.parent / "case_lifecycle.py"
            sftp.put(str(shared_lifecycle), posixpath.join(remote_root, shared_lifecycle.name))
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


class ControlAPI:
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
            headers={"X-API-Key": self.api_key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Control API {method} {path} returned HTTP {exc.code}") from exc
        if payload.get("code") != 0:
            raise RuntimeError(f"Control API {method} {path} failed")
        return payload.get("data") or {}


def read_api_key(password: str) -> str:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(CONTROL_IP, username="control", password=password, timeout=30)
        paths = " ".join(shlex.quote(item) for item in CONTROL_ENV_FILES)
        command = (
            f"for file in {paths}; do test -f \"$file\" || continue; "
            "grep '^MINI_DROP_API_KEY=' \"$file\" | tail -1 | cut -d= -f2-; done | tail -1"
        )
        _, stdout, _ = client.exec_command(command, timeout=30)
        return stdout.read().decode("utf-8", "replace").strip()
    finally:
        client.close()


def diagnosis(api: ControlAPI, manifest: dict, duration_sec: int, timeout_sec: int) -> dict:
    source = manifest["source_context"]
    target = manifest["target"]
    now = datetime.now(timezone.utc)
    started = api.call("/api/v1/diagnoses", "POST", {
        "query": manifest["diagnosis_query"],
        "context": {
            "service_id": target["service_id"],
            "environment": "staging",
            "time_range": {
                "start": (now - timedelta(seconds=duration_sec + 60)).isoformat(),
                "end": (now + timedelta(seconds=duration_sec + 60)).isoformat(),
            },
            "instances": [{**target, "source_context": source}],
            "source_context": source,
        },
        "budget_profile": "development",
        "auto_execute_policy": "all_registered",
    })
    diagnosis_id = started["diagnosis_id"]
    deadline = time.monotonic() + timeout_sec
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
                "approver_id": "pr_case_vm_runner",
            }, timeout=60)
            approved.add(step_id)
        control = latest.get("runner_control") if isinstance(latest.get("runner_control"), dict) else {}
        if control.get("release_requested"):
            return {
                "diagnosis_id": diagnosis_id,
                "detail": latest,
                "terminal": latest.get("status") in TERMINAL_STATUSES,
                "runner_release": control,
                "runner_release_reason": control.get("reason") or "diagnosis_settled",
            }
        time.sleep(3)
    try:
        latest = api.call(f"/api/v1/diagnoses/{diagnosis_id}")
    except (TimeoutError, urllib.error.URLError):
        latest = {}
    control = latest.get("runner_control") if isinstance(latest.get("runner_control"), dict) else {}
    if control.get("release_requested"):
        return {
            "diagnosis_id": diagnosis_id,
            "detail": latest,
            "terminal": latest.get("status") in TERMINAL_STATUSES,
            "runner_release": control,
            "runner_release_reason": control.get("reason") or "diagnosis_settled",
        }
    if not control and latest.get("status") in TERMINAL_STATUSES:
        legacy_control = {
            "release_requested": True,
            "reason": "legacy_terminal_status",
            "released_at": None,
            "outstanding_probes": [],
        }
        return {
            "diagnosis_id": diagnosis_id,
            "detail": latest,
            "terminal": True,
            "runner_release": legacy_control,
            "runner_release_reason": legacy_control["reason"],
        }
    return {
        "diagnosis_id": diagnosis_id,
        "detail": latest,
        "terminal": False,
        "runner_status": RUNNER_RELEASE_TIMEOUT_REASON,
        "runner_release_reason": RUNNER_RELEASE_TIMEOUT_REASON,
        "runner_release": {
            "release_requested": False,
            "reason": RUNNER_RELEASE_TIMEOUT_REASON,
            "released_at": None,
            "outstanding_probes": (
                latest.get("probes", []) if isinstance(latest.get("probes"), list) else []
            ),
        },
        "timeout_sec": timeout_sec,
    }


def request_case_release(
    remote: Remote,
    evidence_root: str,
    reason: str,
    diagnosis_id: str | None = None,
) -> dict:
    payload = {
        "reason": reason,
        "diagnosis_id": diagnosis_id,
        "released_at": datetime.now(timezone.utc).isoformat(),
    }
    script = (
        "import json, pathlib; "
        f"path = pathlib.Path({evidence_root!r}) / 'runner-release.json'; "
        f"path.write_text(json.dumps({payload!r}, sort_keys=True), encoding='utf-8')"
    )
    remote.run(f"python3 -c {shlex.quote(script)}", timeout=60)
    return payload


def wait_for_initial_window_complete(
    remote: Remote,
    evidence_root: str,
    duration_sec: int,
) -> None:
    timeout_sec = max(60, duration_sec + 60)
    remote.run(
        f"for i in $(seq 1 {timeout_sec}); do "
        f"test -f {shlex.quote(evidence_root)}/initial-window-complete && exit 0; "
        "sleep 1; "
        f"done; echo 'initial window did not complete' >&2; exit 1",
        timeout=timeout_sec + 30,
    )


def wait_for_workload_complete(
    remote: Remote,
    evidence_root: str,
    timeout_sec: int = 120,
) -> bool:
    try:
        remote.run(
            f"for i in $(seq 1 {timeout_sec}); do "
            f"test -f {shlex.quote(evidence_root)}/complete && exit 0; "
            "sleep 1; "
            "done; exit 1",
            timeout=timeout_sec + 30,
        )
    except RuntimeError:
        return False
    return True


def inspect_target(remote: Remote, remote_root: str, project_name: str, service: str, target_pattern: str) -> dict:
    project = shlex.quote(project_name)
    service_arg = shlex.quote(service)
    container_name = shlex.quote(f"{project_name}-{service}-1")
    pattern = shlex.quote(target_pattern)
    command = (
        f"cd {shlex.quote(remote_root)}; "
        f"cid=$(docker inspect -f '{{{{.Id}}}}' {container_name} 2>/dev/null || true); "
        f"if [ -z \"$cid\" ]; then cid=$(docker compose -p {project} ps -q {service_arg} 2>/dev/null | head -n 1); fi; "
        "if [ -n \"$cid\" ]; then "
        "  main_pid=$(docker inspect -f '{{.State.Pid}}' \"$cid\" 2>/dev/null || true); "
        "  container_id=$(docker inspect -f '{{.Id}}' \"$cid\" 2>/dev/null || true); "
        "fi; "
        "if [ -n \"$main_pid\" ] && [ \"$main_pid\" != \"0\" ]; then "
        f"  target_pid=$(nsenter -t \"$main_pid\" -p ps -eo pid=,args= 2>/dev/null | awk -v p={pattern} '$0 ~ p {{print $1; exit}}'); "
        "fi; "
        "printf '%s|%s|%s|cid=%s name=%s\\n' \"${target_pid:-$main_pid}\" \"$main_pid\" \"$container_id\" \"$cid\" "
        f"{container_name}"
    )
    last_raw = ""
    for _ in range(60):
        raw = remote.run(command).strip()
        last_raw = raw
        parts = raw.split("|", 3)
        if len(parts) < 3:
            time.sleep(2)
            continue
        pid, main_pid, container_id = (part.strip() for part in parts[:3])
        if not main_pid or not container_id:
            time.sleep(2)
            continue
        if not pid:
            pid = main_pid
        return {"pid": int(pid), "main_pid": int(main_pid), "container_id": container_id}
    raise RuntimeError(f"failed to inspect target process: {last_raw!r}")


def run_stage(remote: Remote, case: dict, *, revision: str, stage: str, mode: str, duration_sec: int,
              diagnosis_timeout_sec: int, output_root: Path, api_key: str | None) -> dict:
    case_id = case["case_id"]
    remote_root = f"/home/worker1/mini-drop-cases/pr-cases/{case_id}"
    evidence_root = f"{remote_root}/evidence/{stage}"
    project = case["compose_project"]
    source_root = f"{remote_root}/source"
    fetch_ref = case.get(stage + "_fetch_ref") or case.get("revision_fetch_ref")
    fetch_command = (
        f"  git -C {shlex.quote(source_root)} fetch --depth=50 origin {shlex.quote(str(fetch_ref))} && "
        if fetch_ref
        else ""
    )
    remote.run(f"rm -rf {shlex.quote(evidence_root)}; mkdir -p {shlex.quote(remote_root)} {shlex.quote(evidence_root)}")
    remote.put_tree(ROOT / case_id, remote_root)
    remote.run(
        "set -e; "
        f"rm -rf {shlex.quote(source_root)}; "
        f"for attempt in 1 2 3; do "
        f"  git clone --filter=blob:none {shlex.quote(case['repo'])} {shlex.quote(source_root)} && "
        + fetch_command +
        f"  git -C {shlex.quote(source_root)} checkout --detach {shlex.quote(revision)} && "
        f"  git -C {shlex.quote(source_root)} submodule update --init --recursive && break; "
        f"  rm -rf {shlex.quote(source_root)}; sleep 3; "
        f"done; "
        f"test -d {shlex.quote(source_root)}/.git; "
        f"cd {shlex.quote(remote_root)}; "
        f"PROJECT_SOURCE_ROOT={shlex.quote(source_root)} CASE_EVIDENCE_ROOT={shlex.quote(evidence_root)} "
        f"CASE_REVISION={shlex.quote(revision)} CASE_DURATION_SEC={duration_sec} "
        f"docker compose -p {shlex.quote(project)} build",
        timeout=3600,
    )
    remote.run(
        f"cd {shlex.quote(remote_root)}; PROJECT_SOURCE_ROOT={shlex.quote(source_root)} "
        f"CASE_EVIDENCE_ROOT={shlex.quote(evidence_root)} CASE_REVISION={shlex.quote(revision)} "
        f"CASE_DURATION_SEC={duration_sec} docker compose -p {shlex.quote(project)} up -d target monitor dependency",
        timeout=300,
    )
    try:
        remote.run(
            f"for i in $(seq 1 90); do test -f {shlex.quote(evidence_root)}/ready; exit_code=$?; "
            "if [ $exit_code -eq 0 ]; then exit 0; fi; sleep 1; done; exit 1",
            timeout=120,
        )
        inspected = inspect_target(remote, remote_root, project, "target", case["target_pattern"])
        source_context = {
            "source_paths": [f"/host{source_root}"],
            "repo_revision": revision,
            "language": case["language"],
            "container_workdir": case["container_workdir"],
            "application_runtime_log_paths": [
                f"/host{evidence_root}/workload.ndjson",
                f"/host{evidence_root}/worker_observations.ndjson",
            ],
        }
        target = {
            "service_id": case["service_id"],
            "instance_id": f"{case_id}-{inspected['container_id'][:12]}",
            "host_id": "worker1",
            "agent_id": AGENT_ID,
            "pid": inspected["pid"],
            "container_id": inspected["container_id"],
            "environment": "staging",
            "application_runtime_log_paths": [
                f"/host{evidence_root}/workload.ndjson",
                f"/host{evidence_root}/worker_observations.ndjson",
            ],
        }
        manifest = {
            "case_id": case_id,
            "mode": mode,
            "stage": stage,
            "stage_role": "diagnosis_target" if mode == "vulnerable" else "regression_control",
            "diagnosis_mode": "full" if mode == "vulnerable" else "none",
            "worker_pid": inspected["pid"],
            "container_id": inspected["container_id"],
            "source_context": source_context,
            "target": target,
            "diagnosis_query": case["diagnosis_query"],
            "workload": {
                **case["workload"],
                "duration_sec": duration_sec,
            },
        }
        remote.run(
            f"python3 -c {shlex.quote('import json; print(json.dumps(' + repr(manifest) + '))')} > {shlex.quote(evidence_root)}/runtime-manifest.json"
        )
        result = {"runtime_manifest": manifest}
        if mode == "vulnerable":
            progress(f"{case_id}: starting Analyzer diagnosis")
            result["diagnosis"] = diagnosis(ControlAPI(api_key or ""), manifest, duration_sec, diagnosis_timeout_sec)
        diagnosis_result = result.get("diagnosis") if isinstance(result.get("diagnosis"), dict) else {}
        release = (
            diagnosis_result.get("runner_release")
            if isinstance(diagnosis_result.get("runner_release"), dict)
            else {}
        )
        release_requested = bool(release.get("release_requested"))
        result["runner_control"] = {
            "release_requested": release_requested,
            "reason": (
                release.get("reason")
                or diagnosis_result.get("runner_release_reason")
                or ("workload_complete" if mode != "vulnerable" else "diagnosis_unavailable")
            ),
            "released_at": release.get("released_at"),
            "outstanding_probes": release.get("outstanding_probes") or [],
            "diagnosis_terminal_status": (
                (diagnosis_result.get("detail") or {}).get("status")
                if isinstance(diagnosis_result.get("detail"), dict)
                else None
            ),
        }
        if mode == "vulnerable":
            wait_for_initial_window_complete(remote, evidence_root, duration_sec)
            release_reason = (
                diagnosis_result.get("runner_release_reason")
                or RUNNER_RELEASE_TIMEOUT_REASON
            )
            release = request_case_release(
                remote,
                evidence_root,
                release_reason,
                diagnosis_result.get("diagnosis_id"),
            )
            result["runner_control"]["release_file"] = release
            result["workload_stopped_by_runner_release"] = True
        else:
            wait_for_initial_window_complete(remote, evidence_root, duration_sec)
            release = request_case_release(remote, evidence_root, "control_window_complete")
            result["runner_control"]["release_file"] = release
        result["workload_completed"] = wait_for_workload_complete(
            remote,
            evidence_root,
        )
        return result
    finally:
        progress(f"{case_id}: collecting evidence and cleaning containers")
        try:
            remote.run(
                f"cd {shlex.quote(remote_root)}; docker compose -p {shlex.quote(project)} logs --no-color > {shlex.quote(evidence_root)}/compose.log 2>&1 || true; "
                f"docker compose -p {shlex.quote(project)} down -v --remove-orphans || true",
                timeout=240,
            )
        finally:
            local = output_root / "evidence" / stage
            remote.get_tree(evidence_root, local)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--mode", choices=("vulnerable", "pair"), default="vulnerable")
    parser.add_argument("--duration-sec", type=int, default=DEFAULT_CASE_DURATION_SEC)
    parser.add_argument("--diagnosis-timeout-sec", type=int, default=DEFAULT_DIAGNOSIS_TIMEOUT_SEC)
    parser.add_argument("--output-root", default="reports/eval/real-open-source/pr-cases")
    parser.add_argument("--password", default="")
    args = parser.parse_args()
    case_path = ROOT / args.case / "case.json"
    case = json.loads(case_path.read_text(encoding="utf-8"))
    if case.get("requires_gpu") and not os.environ.get("MINI_DROP_PR_CASE_ALLOW_GPU", ""):
        password = args.password or os.environ.get("MINI_DROP_VM_PASSWORD", "admin")
        remote = Remote(password)
        try:
            gpu = remote.run("command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L || true").strip()
        finally:
            remote.close()
        if not gpu:
            output = Path(args.output_root) / f"{case['case_id']}-blocked-{time.strftime('%Y%m%d-%H%M%S')}"
            output.mkdir(parents=True, exist_ok=True)
            (output / "preflight.json").write_text(json.dumps({"case_id": case["case_id"], "blocked": True, "reason": "nvidia_gpu_unavailable", "mode": args.mode}, indent=2), encoding="utf-8")
            print(json.dumps({"case_id": case["case_id"], "blocked": True, "reason": "nvidia_gpu_unavailable"}, indent=2))
            return 3
    password = args.password or os.environ.get("MINI_DROP_VM_PASSWORD", "admin")
    api_key = os.environ.get("MINI_DROP_API_KEY", "")
    if not api_key:
        api_key = read_api_key(password)
    output = Path(args.output_root) / f"{case['case_id']}-{args.mode}-{time.strftime('%Y%m%d-%H%M%S')}"
    output.mkdir(parents=True, exist_ok=True)
    remote = Remote(password)
    try:
        result = {"case_id": case["case_id"], "mode": args.mode, "case": case}
        result["vulnerable"] = run_stage(remote, case, revision=case["vulnerable_revision"], stage="vulnerable", mode="vulnerable", duration_sec=args.duration_sec, diagnosis_timeout_sec=args.diagnosis_timeout_sec, output_root=output, api_key=api_key)
        if args.mode == "pair":
            result["fixed"] = run_stage(remote, case, revision=case["fixed_revision"], stage="fixed", mode="pair", duration_sec=args.duration_sec, diagnosis_timeout_sec=args.diagnosis_timeout_sec, output_root=output, api_key=None)
        (output / "run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output_root": str(output), "case_id": case["case_id"], "mode": args.mode}, ensure_ascii=False, indent=2))
        return 0
    finally:
        remote.close()


if __name__ == "__main__":
    raise SystemExit(main())
