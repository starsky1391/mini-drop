#!/usr/bin/env python3
"""Copy scheme B files to the three VM lab and rebuild services.

This intentionally uses SFTP file replacement instead of `git pull` so the
real-case validation runs exactly the current worktree changes.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import uuid
from dataclasses import dataclass
from posixpath import dirname as posix_dirname
from pathlib import Path

import paramiko


ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Node:
    name: str
    ip: str
    user: str
    role: str


NODES = (
    Node("control", "172.18.88.237", "control", "control"),
    Node("worker1", "172.18.90.144", "worker1", "worker"),
    Node("worker2", "172.18.87.120", "worker2", "worker"),
)


SCHEME_B_FILES = (
    "agent/mini_drop_agent/collector_profile.py",
    "agent/mini_drop_agent/runtime_control.py",
    "agent/mini_drop_agent/collectors/continuous.py",
    "agent/mini_drop_agent/collectors/evidence_validity.py",
    "agent/mini_drop_agent/collectors/log_scan.py",
    "agent/mini_drop_agent/collectors/trace.py",
    "agent/mini_drop_agent/collectors/off_cpu.py",
    "agent/mini_drop_agent/collectors/perf.py",
    "agent/mini_drop_agent/collectors/python_heap.py",
    "agent/mini_drop_agent/collectors/python_heap_reference.py",
    "agent/mini_drop_agent/collectors/pyspy.py",
    "agent/mini_drop_agent/collectors/runtime_control.py",
    "agent/mini_drop_agent/collectors/source_mechanism.py",
    "agent/mini_drop_agent/collectors/source_snapshot.py",
    "agent/mini_drop_agent/collectors/sys_metrics.py",
    "agent/mini_drop_agent/config.py",
    "agent/mini_drop_agent/main.py",
    "agent/mini_drop_agent/watch_observer.py",
    "deploy/collectors/otel-collector/config.yaml",
    "deploy/collectors/fluent-bit/fluent-bit.conf",
    "deploy/collectors/pyheap/pyheap_dump",
    "deploy/dockerfiles/agent.Dockerfile",
    "deploy/dockerfiles/agent.cached.Dockerfile",
    "deploy/dockerfiles/server.cached.Dockerfile",
    "deploy/dockerfiles/web.control.cached.Dockerfile",
    "deploy/env/control.env.example",
    "deploy/env/worker.env.example",
    "docker-compose.control.yml",
    "docker-compose.worker.yml",
    "docs/ai_ops_v2_test/benchmarks/online_boutique_vm/stack.yml",
    "docs/ai_ops_v2_test/scripts/run_ai_ops_v2_vm.py",
    "docs/ai_ops_v2_test/scripts/test_persistent_watch_vm.py",
    "docs/evidence_to_attribution_analyzer_rewrite.md",
    "web/src/components/AppLayout.jsx",
    "web/src/components/AppLayout.module.css",
    "web/src/components/diagnosis/ControlledAITreeGraph.css",
    "web/src/components/diagnosis/ControlledAITreeGraph.jsx",
    "web/src/components/diagnosis/aiTreeGraphModel.js",
    "web/src/components/diagnosis/RootCauseClusters.css",
    "web/src/components/diagnosis/RootCauseClusters.jsx",
    "web/src/pages/AIDiagnosis.css",
    "web/src/pages/AIDiagnosis.jsx",
    "web/src/pages/PersistentWatch.jsx",
    "proto/compile.sh",
    "proto/watch.proto",
    "server/app/diagnosis/evidence_structurer.py",
    "server/app/diagnosis/intent.py",
    "server/app/diagnosis/schemas.py",
    "server/app/diagnosis/audit_bundle.py",
    "server/app/diagnosis/benchmark_score.py",
    "server/app/diagnosis/codeql_query_guard.py",
    "server/app/diagnosis/collector_invocation.py",
    "server/app/diagnosis/orchestrator.py",
    "server/app/diagnosis/probe_registry.py",
    "server/app/diagnosis/session_conclusion.py",
    "server/app/diagnosis/store.py",
    "server/app/diagnosis/persistent_trigger.py",
    "server/app/diagnosis/rolling_buffer.py",
    "server/app/diagnosis/watch_runtime.py",
    "server/app/grpc_services/hotmethod_service.py",
    "server/app/grpc_services/healthcheck_service.py",
    "server/app/grpc_services/watch_runtime_service.py",
    "server/app/database.py",
    "server/app/main.py",
    "server/app/models.py",
    "server/app/sql_repository.py",
    "server/app/storage.py",
    "server/app/schemas.py",
    "server/app/rca/controlled_tree.py",
    "server/app/rca/llm_client.py",
    "server/app/rca/models.py",
    "specs/001-evidence-attribution-analyzer/tasks.md",
)

SCHEME_B_TREES = ("web/dist",)


class SSH:
    def __init__(self, node: Node, password: str) -> None:
        self.node = node
        self.password = password
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(node.ip, username=node.user, password=password, timeout=20)

    def close(self) -> None:
        self.client.close()

    def run(self, command: str, *, timeout: int = 600) -> str:
        _, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        if code:
            raise RuntimeError(f"{self.node.name} command failed ({code}): {err[-1000:] or out[-1000:]}")
        return out

    def sudo(self, command: str, *, timeout: int = 600) -> str:
        password = shlex.quote(self.password)
        return self.run(f"printf '%s\\n' {password} | sudo -S /bin/bash -c {shlex.quote(command)}", timeout=timeout)

    def put(self, local: Path, remote: str) -> None:
        sftp = self.client.open_sftp()
        temporary = f"/tmp/mini-drop-upload-{uuid.uuid4().hex}"
        try:
            sftp.put(str(local), temporary)
            self.sudo(
                f"install -D -m 0644 {shlex.quote(temporary)} {shlex.quote(remote)}",
                timeout=30,
            )
        finally:
            try:
                self.run(f"rm -f {shlex.quote(temporary)}", timeout=30)
            except RuntimeError:
                pass
            sftp.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument(
        "--rca-llm-timeout-sec",
        type=int,
        default=180,
        help="Control 会话级 RCA LLM 读取超时，默认 180 秒",
    )
    parser.add_argument(
        "--verify-ai-only",
        action="store_true",
        help="只验证 Control 容器当前 AI Provider，不同步或重建服务",
    )
    parser.add_argument(
        "--configure-perf-sysctl",
        action="store_true",
        help="在 Worker 宿主机持久化 kernel.perf_event_paranoid=1；默认只读检查",
    )
    parser.add_argument("--target-root", default="", help="override remote repo root")
    args = parser.parse_args()

    password = os.getenv("MINI_DROP_VM_PASSWORD", "")
    if not password:
        parser.error("MINI_DROP_VM_PASSWORD is required")
    if not 30 <= args.rca_llm_timeout_sec <= 600:
        parser.error("--rca-llm-timeout-sec must be between 30 and 600")

    if args.verify_ai_only:
        result = _verify_control_ai(password, args.target_root)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0

    deployment_files = _deployment_files()
    missing = [item for item in SCHEME_B_FILES if not (ROOT / item).is_file()]
    missing.extend(item for item in SCHEME_B_TREES if not (ROOT / item).is_dir())
    if missing:
        raise RuntimeError(f"local files missing: {missing}")

    for node in NODES:
        if args.dry_run:
            remote_root = args.target_root or f"/home/{node.user}/mini-drop-active"
            print(f"[{node.name}] would sync {len(deployment_files)} files -> {remote_root}", flush=True)
            continue
        ssh = SSH(node, password)
        try:
            remote_root = args.target_root or _resolve_remote_root(ssh, node)
            print(f"[{node.name}] sync {len(deployment_files)} files -> {remote_root}", flush=True)
            for rel in deployment_files:
                ssh.put(ROOT / rel, f"{remote_root}/{rel}")
            ssh.sudo(_normalize_uploaded_scripts(remote_root), timeout=30)
            if node.role == "control":
                ssh.sudo(
                    _configure_control_llm_timeout(remote_root, args.rca_llm_timeout_sec),
                    timeout=30,
                )
            if not args.skip_build:
                if node.role == "control":
                    ssh.sudo(_control_rebuild(remote_root), timeout=1200)
                else:
                    if args.configure_perf_sysctl:
                        ssh.sudo(_configure_worker_perf_sysctl(), timeout=30)
                    ssh.sudo(_worker_rebuild(remote_root), timeout=1200)
        finally:
            ssh.close()
    return 0


def _deployment_files() -> tuple[str, ...]:
    files = set(SCHEME_B_FILES)
    for tree in SCHEME_B_TREES:
        files.update(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / tree).rglob("*")
            if path.is_file()
        )
    return tuple(sorted(files))


def _verify_control_ai(password: str, target_root: str = "") -> dict:
    node = next(item for item in NODES if item.role == "control")
    ssh = SSH(node, password)
    try:
        remote_root = target_root or _resolve_remote_root(ssh, node)
        command = (
            f"cd {shlex.quote(remote_root)}; "
            "server_container=$(docker compose --env-file deploy/env/control.env "
            "-f docker-compose.control.yml ps -q server); "
            "test -n \"$server_container\"; "
            "docker exec \"$server_container\" sh -lc "
            "'curl -fsS -X POST -H \"X-API-Key: ${MINI_DROP_API_KEY}\" "
            "http://localhost:8191/api/ai-config/test'"
        )
        payload = json.loads(ssh.run(command, timeout=120))
    finally:
        ssh.close()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data.get("passed"):
        message = str((data or {}).get("message") or "AI Provider test did not pass")
        raise RuntimeError(f"control AI connectivity check failed: {message[:300]}")
    return {
        "passed": True,
        "provider": data.get("provider"),
        "model": data.get("model"),
        "http_status": data.get("http_status"),
        "duration_ms": data.get("duration_ms"),
        "content_valid": data.get("content_valid"),
    }


def _resolve_remote_root(ssh: SSH, node: Node) -> str:
    candidates = (
        f"/home/{node.user}/mini-drop-active",
        f"/home/{node.user}/mini-drop",
    )
    required = (
        ("docker-compose.control.yml", "deploy/env/control.env")
        if node.role == "control"
        else ("docker-compose.worker.yml", "deploy/env/worker.env")
    )
    for candidate in candidates:
        try:
            checks = " && ".join(
                f"test -f {shlex.quote(f'{candidate}/{relative}')}"
                for relative in required
            )
            ssh.run(checks, timeout=30)
            return candidate
        except RuntimeError:
            continue
    raise RuntimeError(
        f"{node.name}: no complete {node.role} checkout found in {candidates}; "
        f"required={required}"
    )


def _control_rebuild(remote_root: str) -> str:
    return (
        "set -e; "
        f"cd {shlex.quote(remote_root)}; "
        "bash -n proto/compile.sh; "
        "if docker compose --env-file deploy/env/control.env "
        "-f docker-compose.control.yml build server web; then "
        "docker compose --env-file deploy/env/control.env "
        "-f docker-compose.control.yml up -d --no-build server web; "
        "else "
        "echo 'registry build unavailable; rebuilding from cached Mini-Drop images'; "
        "docker build -f deploy/dockerfiles/server.cached.Dockerfile "
        "-t mini-drop-control-server .; "
        "docker build -f deploy/dockerfiles/web.control.cached.Dockerfile "
        "-t mini-drop-control-web web; "
        "docker compose --env-file deploy/env/control.env "
        "-f docker-compose.control.yml up -d --no-build --force-recreate server web; "
        "fi; "
        "docker compose --env-file deploy/env/control.env -f docker-compose.control.yml ps"
    )


def _worker_rebuild(remote_root: str) -> str:
    return (
        "set -e; "
        f"cd {shlex.quote(remote_root)}; "
        "bash -n proto/compile.sh; "
        "if docker compose --profile redis --env-file deploy/env/worker.env "
        "-f docker-compose.worker.yml build agent; then "
        "docker compose --profile redis --env-file deploy/env/worker.env "
        "-f docker-compose.worker.yml up -d --no-build agent fluent-bit blackbox-exporter otel-collector redis-exporter; "
        "else "
        "echo 'registry build unavailable; rebuilding from cached Mini-Drop image'; "
        "docker build -f deploy/dockerfiles/agent.cached.Dockerfile "
        "-t mini-drop-worker-agent .; "
        "docker compose --profile redis --env-file deploy/env/worker.env "
        "-f docker-compose.worker.yml up -d --no-build --force-recreate agent fluent-bit blackbox-exporter otel-collector redis-exporter; "
        "fi; "
        "agent_container=$(docker compose --env-file deploy/env/worker.env "
        "-f docker-compose.worker.yml ps -q agent); "
        "test -n \"$agent_container\"; "
        "docker exec \"$agent_container\" sh -lc "
        "'cat /proc/sys/kernel/perf_event_paranoid; "
        "grep CapEff /proc/1/status; "
        "grep CapBnd /proc/1/status; "
        "grep NoNewPrivs /proc/1/status; "
        "command -v perf || true; "
        "command -v bpftrace || true'; "
        "printf 'host_perf_event_paranoid='; cat /proc/sys/kernel/perf_event_paranoid; "
        "docker compose --profile redis --env-file deploy/env/worker.env -f docker-compose.worker.yml ps"
    )


def _configure_worker_perf_sysctl() -> str:
    return (
        "printf '%s\\n' 'kernel.perf_event_paranoid=1' | "
        "sudo tee /etc/sysctl.d/99-mini-drop-perf.conf >/dev/null; "
        "sudo sysctl -w kernel.perf_event_paranoid=1 >/dev/null; "
        "test \"$(cat /proc/sys/kernel/perf_event_paranoid)\" = \"1\""
    )


def _normalize_uploaded_scripts(remote_root: str) -> str:
    return (
        f"cd {shlex.quote(remote_root)}; "
        "sed -i 's/\\r$//' proto/compile.sh; "
        "chmod 755 proto/compile.sh"
    )


def _configure_control_llm_timeout(remote_root: str, timeout_sec: int) -> str:
    env_file = f"{remote_root}/deploy/env/control.env"
    setting = f"MINI_DROP_RCA_LLM_TIMEOUT_SEC={int(timeout_sec)}"
    return (
        "set -e; "
        f"if grep -q '^MINI_DROP_RCA_LLM_TIMEOUT_SEC=' {shlex.quote(env_file)}; then "
        f"sed -i -E 's/^MINI_DROP_RCA_LLM_TIMEOUT_SEC=.*/{setting}/' {shlex.quote(env_file)}; "
        "else "
        f"printf '%s\\n' {shlex.quote(setting)} >> {shlex.quote(env_file)}; "
        "fi"
    )


if __name__ == "__main__":
    raise SystemExit(main())
