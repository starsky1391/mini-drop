#!/usr/bin/env python3
"""Copy scheme B files to the three VM lab and rebuild services.

This intentionally uses SFTP file replacement instead of `git pull` so the
real-case validation runs exactly the current worktree changes.
"""

from __future__ import annotations

import argparse
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
    "agent/mini_drop_agent/collectors/trace.py",
    "agent/mini_drop_agent/collectors/off_cpu.py",
    "agent/mini_drop_agent/config.py",
    "agent/mini_drop_agent/main.py",
    "agent/mini_drop_agent/watch_observer.py",
    "deploy/collectors/otel-collector/config.yaml",
    "deploy/env/control.env.example",
    "deploy/env/worker.env.example",
    "docker-compose.control.yml",
    "docker-compose.worker.yml",
    "docs/ai_ops_v2_test/benchmarks/online_boutique_vm/stack.yml",
    "docs/ai_ops_v2_test/scripts/run_ai_ops_v2_vm.py",
    "docs/ai_ops_v2_test/scripts/test_persistent_watch_vm.py",
    "docs/evidence_to_attribution_analyzer_rewrite.md",
    "docs/superpowers/specs/2026-08-13-industrial-collector-hardening-design.md",
    "web/src/pages/PersistentWatch.jsx",
    "proto/compile.sh",
    "proto/watch.proto",
    "server/app/diagnosis/evidence_structurer.py",
    "server/app/diagnosis/audit_bundle.py",
    "server/app/diagnosis/orchestrator.py",
    "server/app/diagnosis/persistent_trigger.py",
    "server/app/diagnosis/rolling_buffer.py",
    "server/app/diagnosis/watch_runtime.py",
    "server/app/grpc_services/hotmethod_service.py",
    "server/app/grpc_services/watch_runtime_service.py",
    "server/app/grpc_server.py",
    "server/app/main.py",
    "server/app/models.py",
    "server/app/sql_repository.py",
    "server/app/storage.py",
    "specs/001-evidence-attribution-analyzer/tasks.md",
)


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
        "--configure-perf-sysctl",
        action="store_true",
        help="在 Worker 宿主机持久化 kernel.perf_event_paranoid=1；默认只读检查",
    )
    parser.add_argument("--target-root", default="", help="override remote repo root")
    args = parser.parse_args()

    password = os.getenv("MINI_DROP_VM_PASSWORD", "")
    if not password:
        parser.error("MINI_DROP_VM_PASSWORD is required")

    missing = [item for item in SCHEME_B_FILES if not (ROOT / item).is_file()]
    if missing:
        raise RuntimeError(f"local files missing: {missing}")

    for node in NODES:
        if args.dry_run:
            remote_root = args.target_root or f"/home/{node.user}/mini-drop-active"
            print(f"[{node.name}] would sync {len(SCHEME_B_FILES)} files -> {remote_root}", flush=True)
            continue
        ssh = SSH(node, password)
        try:
            remote_root = args.target_root or _resolve_remote_root(ssh, node)
            print(f"[{node.name}] sync {len(SCHEME_B_FILES)} files -> {remote_root}", flush=True)
            for rel in SCHEME_B_FILES:
                ssh.put(ROOT / rel, f"{remote_root}/{rel}")
            ssh.sudo(_normalize_uploaded_scripts(remote_root), timeout=30)
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
        f"cd {shlex.quote(remote_root)}; "
        "bash -n proto/compile.sh; "
        "docker compose --env-file deploy/env/control.env "
        "-f docker-compose.control.yml up -d --build server web; "
        "docker compose --env-file deploy/env/control.env -f docker-compose.control.yml ps"
    )


def _worker_rebuild(remote_root: str) -> str:
    return (
        f"cd {shlex.quote(remote_root)}; "
        "bash -n proto/compile.sh; "
        "docker compose --profile redis --env-file deploy/env/worker.env "
        "-f docker-compose.worker.yml up -d --build agent fluent-bit blackbox-exporter otel-collector redis-exporter; "
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


if __name__ == "__main__":
    raise SystemExit(main())
