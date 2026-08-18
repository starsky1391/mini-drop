#!/usr/bin/env python3
"""Run the ai_ops_v2 dataset against the real three-node Hyper-V lab.

The public query and fixture id are sent through the normal Case API. Private
oracles are deliberately not loaded by this runner. Scoring is a separate
post-run step so expected answers cannot leak into diagnosis context.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import ssl
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paramiko


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_CASES = ROOT / "benchmarks" / "ai_ops_v2" / "public" / "cases.json"
FAULTCTL = ROOT / "benchmarks" / "ai_ops_v2" / "vm_faultctl.sh"
FAULT_HELPERS = ROOT / "benchmarks" / "online_boutique_vm" / "fault_helpers"
TERMINAL = {
    "COMPLETED", "INSUFFICIENT_EVIDENCE", "PARTIAL_COMPLETED",
    "BUDGET_EXHAUSTED", "TOPOLOGY_UNAVAILABLE", "USER_CANCELED", "FAILED",
    # This is the correct safe outcome for the missing-scope robustness case.
    # The interactive product remains resumable; the benchmark records the
    # refusal to guess as its terminal observation.
    "NEEDS_SCOPE_CONFIRMATION",
}


@dataclass(frozen=True)
class Node:
    name: str
    ip: str
    user: str
    agent_id: str


CONTROL = Node("control", "172.18.88.237", "control", "")
WORKER1 = Node("worker1", "172.18.90.144", "worker1", "linux-worker-1")
WORKER2 = Node("worker2", "172.18.87.120", "worker2", "linux-worker-2")
WORKERS = {node.name: node for node in (WORKER1, WORKER2)}
FRONTEND_URL = f"http://{WORKER1.ip}:8080/"
CONTROL_ENV_FILES = (
    "/home/control/mini-drop-active/deploy/env/control-native.env",
    "/home/control/mini-drop-active/deploy/env/control.env",
    "/home/control/mini-drop/deploy/env/control.env",
)
CONTROL_COMPOSE_DIRS = (
    "/home/control/mini-drop-active",
    "/home/control/mini-drop",
)


@dataclass(frozen=True)
class Target:
    service: str
    node: str
    source: str
    value: str
    instance_id: str | None = None


@dataclass(frozen=True)
class Spec:
    fixture: str
    service: str
    inject: tuple[tuple[str, str], ...] = ()
    targets: tuple[Target, ...] = ()
    dependencies: tuple[tuple[str, str, str], ...] = ()
    no_scope: bool = False
    settle_sec: int = 3


def docker(service: str, node: str, *, instance_id: str | None = None) -> Target:
    return Target(service, node, "docker", service, instance_id)


def unit(service: str, node: str, name: str, *, instance_id: str | None = None) -> Target:
    return Target(service, node, "unit", name, instance_id)


SPECS: dict[str, Spec] = {
    "OB-SINGLE-CPU-001": Spec("productcatalog_cpu_hotspot_v1", "productcatalogservice",
        (("worker1", "productcatalog_cpu_hotspot_v1"),), (docker("productcatalogservice", "worker1"),)),
    "OB-SINGLE-LATENCY-001": Spec("productcatalog_latency_v1", "productcatalogservice",
        (("worker1", "productcatalog_latency_v1"),), (docker("productcatalogservice", "worker1"),)),
    "OB-SINGLE-REDIS-001": Spec("redis_pause_v1", "cartservice",
        (("worker2", "redis_pause_v1"),),
        (docker("cartservice", "worker2"), docker("redis-cart", "worker2")),
        (("cartservice", "redis-cart", "READS_FROM"),)),
    "OB-SINGLE-PAYMENT-001": Spec("payment_pause_v1", "checkoutservice",
        (("worker2", "payment_pause_v1"),),
        (docker("checkoutservice", "worker2"), docker("paymentservice", "worker2")),
        (("checkoutservice", "paymentservice", "CALLS"),)),
    "OB-SINGLE-NOISY-CPU-001": Spec("worker_cpu_noise_v1", "checkoutservice",
        (("worker2", "worker_cpu_noise_v1"),),
        (docker("checkoutservice", "worker2"), unit("noise-generator", "worker2", "md-aiopsv2-cpu"))),
    "OB-SINGLE-HOST-IO-001": Spec("worker_io_contention_v1", "frontend",
        (("worker1", "worker_io_contention_v1"),),
        (docker("frontend", "worker1"), unit("io-generator", "worker1", "md-aiopsv2-io"))),
    "OB-SINGLE-HOST-MEM-001": Spec("worker_memory_pressure_v1", "checkoutservice",
        (("worker2", "worker_memory_pressure_v1"),),
        (docker("checkoutservice", "worker2"), unit("memory-generator", "worker2", "md-aiopsv2-memory"))),
    "OB-SINGLE-PARTITION-001": Spec("overlay_partition_v1", "frontend",
        (("worker1", "overlay_partition_v1"),),
        (docker("frontend", "worker1"), docker("cartservice", "worker2")),
        (("frontend", "cartservice", "CALLS"),), settle_sec=8),
    "OB-SINGLE-OOM-001": Spec("process_oom_v1", "service-x",
        (("worker1", "process_oom_v1"),), (unit("service-x", "worker1", "md-aiopsv2-oom"),)),
    "OB-SINGLE-DISK-001": Spec("loopback_enospc_v1", "service-x",
        (("worker1", "loopback_enospc_v1"),), (unit("service-x", "worker1", "md-aiopsv2-disk"),)),
    "OB-SINGLE-JAVA-LOCK-001": Spec("java_lock_v1", "java-service",
        (("worker1", "java_lock_v1"),), (unit("java-service", "worker1", "md-aiopsv2-java"),)),
    "OB-SINGLE-GO-LOCK-001": Spec("go_lock_v1", "go-service",
        (("worker1", "go_lock_v1"),), (unit("go-service", "worker1", "md-aiopsv2-go"),)),
    "OB-SINGLE-PYTHON-LOCK-001": Spec("python_lock_v1", "python-service",
        (("worker1", "python_lock_v1"),), (unit("python-service", "worker1", "md-aiopsv2-python"),)),
    "OB-SINGLE-MEMLEAK-001": Spec("python_memory_growth_v1", "python-service",
        (("worker1", "python_memory_growth_v1"),), (unit("python-service", "worker1", "md-aiopsv2-python"),)),
    "OB-SINGLE-RUNTIME-STALL-001": Spec("runtime_stall_v1", "paymentservice",
        (("worker1", "runtime_stall_v1"),), (unit("paymentservice", "worker1", "md-aiopsv2-stall"),)),
    "OB-SINGLE-NETLOSS-001": Spec("network_loss_v1", "checkoutservice",
        (("worker1", "network_loss_v1"),), (unit("checkoutservice", "worker1", "md-aiopsv2-net"),)),
    "OB-COMPOUND-MEM-LOCK-001": Spec("python_memory_lock_v1", "python-service",
        (("worker1", "python_memory_lock_v1"),), (unit("python-service", "worker1", "md-aiopsv2-python"),)),
    "OB-COMPOUND-DISK-NET-001": Spec("disk_network_v1", "service-x",
        (("worker1", "disk_network_v1"),),
        (unit("service-x", "worker1", "md-aiopsv2-disk"), unit("network-client", "worker1", "md-aiopsv2-net"))),
    "OB-COMPOUND-NOISY-DOWNSTREAM-001": Spec("noisy_downstream_v1", "checkoutservice",
        (("worker2", "noisy_downstream_v1"),),
        (docker("checkoutservice", "worker2"), docker("paymentservice", "worker2"),
         unit("noise-generator", "worker2", "md-aiopsv2-cpu")),
        (("checkoutservice", "paymentservice", "CALLS"),)),
    "OB-COMPOUND-OOM-RECOVERY-001": Spec("oom_after_restart_v1", "service-x",
        (("worker1", "oom_after_restart_v1"),), (unit("service-x", "worker1", "md-aiopsv2-oom"),)),
    "OB-COMPOUND-CROSS-WORKER-001": Spec("cross_worker_two_roots_v1", "frontend",
        (("worker1", "cross_worker_two_roots_v1"), ("worker2", "cross_worker_two_roots_v1")),
        (docker("frontend", "worker1"), unit("network-client", "worker1", "md-aiopsv2-net"),
         docker("checkoutservice", "worker2"), unit("memory-generator", "worker2", "md-aiopsv2-memory")),
        (("frontend", "checkoutservice", "CALLS"),)),
    "OB-COMPOUND-PAYMENT-REDIS-001": Spec("payment_redis_pause_v1", "frontend",
        (("worker2", "payment_redis_pause_v1"),),
        (docker("frontend", "worker1"), docker("cartservice", "worker2"),
         docker("checkoutservice", "worker2"), docker("paymentservice", "worker2"),
         docker("redis-cart", "worker2")),
        (("frontend", "cartservice", "CALLS"), ("frontend", "checkoutservice", "CALLS"),
         ("cartservice", "redis-cart", "READS_FROM"), ("checkoutservice", "paymentservice", "CALLS"))),
    "OB-COMPOUND-STALE-REAL-001": Spec("stale_plus_network_v1", "service-x",
        (("worker1", "stale_plus_network_v1"),), (unit("service-x", "worker1", "md-aiopsv2-net"),)),
    "OB-NEG-HEALTHY-001": Spec("healthy_baseline_v1", "frontend", targets=(docker("frontend", "worker1"),)),
    "OB-NEG-TRANSIENT-001": Spec("transient_spike_v1", "frontend",
        (("worker1", "transient_spike_v1"),), (docker("frontend", "worker1"),)),
    "OB-ROBUST-STALE-001": Spec("stale_evidence_replay_v1", "frontend", targets=(docker("frontend", "worker1"),)),
    "OB-ROBUST-DUPLICATE-001": Spec("duplicate_evidence_replay_v1", "productcatalogservice",
        (("worker1", "productcatalog_cpu_hotspot_v1"),),
        (docker("productcatalogservice", "worker1", instance_id="duplicate-target"),
         docker("productcatalogservice", "worker1", instance_id="duplicate-target"))),
    "OB-ROBUST-COLLECTOR-FAIL-001": Spec("collector_failure_replay_v1", "service-x",
        targets=(Target("service-x", "worker1", "pid", "4194303"),)),
    "OB-ROBUST-CONFLICT-001": Spec("conflicting_sources_replay_v1", "service-x",
        (("worker1", "productcatalog_cpu_hotspot_v1"),),
        (docker("service-x", "worker1", instance_id="conflict-target"),
         docker("frontend", "worker1", instance_id="conflict-target"))),
    "OB-ROBUST-SCOPE-001": Spec("missing_scope_v1", "service-x", no_scope=True),
}


class SSH:
    def __init__(self, password: str):
        self.password = password

    def run(self, node: Node, command: str, *, timeout: int = 180) -> str:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(node.ip, username=node.user, password=self.password, timeout=15)
        try:
            _, stdout, stderr = client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            code = stdout.channel.recv_exit_status()
            if code:
                raise RuntimeError(f"{node.name} command failed ({code}): {err[-800:] or out[-800:]}")
            return out
        finally:
            client.close()

    def sudo(self, node: Node, command: str, *, timeout: int = 240) -> str:
        password = shlex.quote(self.password)
        return self.run(node, f"printf '%s\\n' {password} | sudo -S {command}", timeout=timeout)

    def repo_root(self, node: Node) -> str:
        candidates = (
            f"/home/{node.user}/mini-drop-active",
            f"/home/{node.user}/mini-drop",
        )
        required = (
            ("docker-compose.control.yml", "deploy/env/control.env")
            if node.name == "control"
            else ("docker-compose.worker.yml", "deploy/env/worker.env")
        )
        for candidate in candidates:
            try:
                checks = " && ".join(
                    f"test -f {shlex.quote(f'{candidate}/{relative}')}"
                    for relative in required
                )
                self.run(node, checks, timeout=15)
                return candidate
            except RuntimeError:
                continue
        raise RuntimeError(
            f"{node.name}: no complete checkout found in {candidates}; required={required}"
        )

    def deploy_faultctl(self, node: Node) -> str:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(node.ip, username=node.user, password=self.password, timeout=15)
        repo_root = self.repo_root(node)
        remote_dir = f"{repo_root}/benchmarks/ai_ops_v2"
        remote = f"{remote_dir}/vm_faultctl.sh"
        remote_helpers = f"{repo_root}/benchmarks/online_boutique_vm/fault_helpers"
        try:
            _, stdout, _ = client.exec_command(f"mkdir -p {remote_dir} {remote_helpers}")
            if stdout.channel.recv_exit_status():
                raise RuntimeError(f"cannot create {remote_dir} and {remote_helpers}")
            sftp = client.open_sftp()
            sftp.put(str(FAULTCTL), remote)
            sftp.chmod(remote, 0o755)
            for helper in sorted(FAULT_HELPERS.iterdir()):
                if helper.is_file():
                    helper_remote = f"{remote_helpers}/{helper.name}"
                    sftp.put(str(helper), helper_remote)
                    sftp.chmod(helper_remote, 0o644)
            sftp.close()
            _, stdout, stderr = client.exec_command(f"bash -n {remote}")
            if stdout.channel.recv_exit_status():
                raise RuntimeError(stderr.read().decode("utf-8", "replace"))
            return remote
        finally:
            client.close()


class API:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base = f"https://{CONTROL.ip}"
        self.context = ssl.create_default_context()
        self.context.check_hostname = False
        self.context.verify_mode = ssl.CERT_NONE

    def call(self, path: str, method: str = "GET", body: dict[str, Any] | None = None,
             *, timeout: int = 45) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("X-API-Key", self.api_key)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise APIError(method, path, exc.code, detail) from exc
        if payload.get("code") != 0:
            raise RuntimeError(f"API {method} {path}: {payload.get('message')}")
        return payload.get("data")


class APIError(RuntimeError):
    def __init__(self, method: str, path: str, status_code: int, detail: str):
        super().__init__(f"API {method} {path}: HTTP {status_code}: {detail}")
        self.method = method
        self.path = path
        self.status_code = status_code
        self.detail = detail


class DiagnosisRunError(RuntimeError):
    def __init__(self, message: str, *, diagnosis_id: str | None = None, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnosis_id = diagnosis_id
        self.detail = detail or {}


def is_transient_api_error(exc: Exception) -> bool:
    if isinstance(exc, APIError):
        return exc.status_code in {502, 503, 504}
    return isinstance(exc, (TimeoutError, urllib.error.URLError))


def call_with_retries(
    api: API,
    path: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    *,
    timeout: int = 120,
    attempts: int = 5,
    sleep_sec: float = 3.0,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return api.call(path, method, body, timeout=timeout)
        except Exception as exc:
            if not is_transient_api_error(exc) or attempt == attempts:
                raise
            last_error = exc
            time.sleep(sleep_sec)
    if last_error:
        raise last_error
    raise RuntimeError(f"API {method} {path}: retry attempts exhausted")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def service_pid(ssh: SSH, target: Target) -> tuple[int, str | None]:
    node = WORKERS[target.node]
    if target.source == "pid":
        return int(target.value), None
    if target.source == "unit":
        raw = ssh.sudo(node, f"systemctl show {shlex.quote(target.value)}.service -p MainPID --value").strip()
        pid = int(raw or 0)
        if pid <= 0:
            raise RuntimeError(f"{node.name}: no live PID for {target.value}")
        return pid, None
    service = target.value if target.value != "service-x" else "productcatalogservice"
    command = (
        "cid=$(docker ps --filter 'name=boutique_" + service + ".1' --format '{{.ID}}' | head -1); "
        "test -n \"$cid\"; docker inspect -f '{{.State.Pid}}|{{.Id}}' \"$cid\""
    )
    raw = ssh.sudo(node, f"/bin/bash -c {shlex.quote(command)}").strip().splitlines()[-1]
    pid_text, container_id = raw.split("|", 1)
    return int(pid_text), container_id


def resolve_scope(ssh: SSH, spec: Spec, run_key: str) -> dict[str, Any]:
    if spec.no_scope:
        return {"service_id": spec.service, "instances": [], "dependencies": []}
    instances: list[dict[str, Any]] = []
    for index, target in enumerate(spec.targets):
        pid, container_id = service_pid(ssh, target)
        node = WORKERS[target.node]
        instance_id = target.instance_id or f"{target.service}-{node.name}-{pid}-{run_key}-{index}"
        item = {
            "service_id": target.service,
            "instance_id": instance_id,
            "host_id": node.name,
            "agent_id": node.agent_id,
            "pid": pid,
            "environment": "production",
        }
        if container_id:
            item["container_id"] = container_id
        instances.append(item)
    dependencies = [
        _dependency_edge(source, dest, relation)
        for source, dest, relation in spec.dependencies
    ]
    return {"service_id": spec.service, "instances": instances, "dependencies": dependencies}


def _dependency_edge(source: str, dest: str, relation: str) -> dict[str, Any]:
    edge: dict[str, Any] = {
        "source_service": source,
        "target_service": dest,
        "relation": relation,
        "confidence": "high",
        "source": "ai_ops_v2_fixture",
    }
    if dest == "redis-cart":
        edge.update({
            "protocol": "redis",
            "host": "redis-cart",
            "port": 6379,
            "url": "redis://redis-cart:6379",
        })
    elif dest in {"paymentservice", "shippingservice"}:
        edge.update({"protocol": "grpc", "host": dest, "port": 50051})
    elif dest == "cartservice":
        edge.update({"protocol": "grpc", "host": dest, "port": 7070})
    elif dest == "checkoutservice":
        edge.update({"protocol": "grpc", "host": dest, "port": 5050})
    else:
        edge.update({"protocol": "tcp", "host": dest})
    return edge


def frontend_probe(timeout: float = 5.0) -> dict[str, Any]:
    started = time.monotonic()
    request = urllib.request.Request(FRONTEND_URL, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read(1024)
            return {"ok": response.status == 200, "status": response.status,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__,
                "latency_ms": round((time.monotonic() - started) * 1000, 1)}


def inspect_ai_ops_v2_environment(ssh: SSH) -> dict[str, Any]:
    swarm = {}
    for node in (CONTROL, WORKER1, WORKER2):
        try:
            raw = ssh.sudo(
                node,
                "docker info --format '{{.Swarm.LocalNodeState}}|{{.Swarm.ControlAvailable}}|{{.Name}}'",
                timeout=30,
            ).strip()
            parts = raw.split("|")
            swarm[node.name] = {
                "state": parts[0] if len(parts) > 0 else raw,
                "control_available": parts[1] if len(parts) > 1 else "",
                "docker_name": parts[2] if len(parts) > 2 else "",
            }
        except Exception as exc:
            swarm[node.name] = {"error": f"{type(exc).__name__}: {exc}"}
    manager = next(
        (
            node
            for node in (CONTROL, WORKER1, WORKER2)
            if swarm.get(node.name, {}).get("control_available") == "true"
        ),
        None,
    )
    services_raw = ""
    if manager is not None:
        services_raw = ssh.sudo(manager, "docker service ls --format '{{.Name}}|{{.Replicas}}'", timeout=30)
    return {
        "swarm": swarm,
        "manager": manager.name if manager else "",
        "services": services_raw.splitlines() if services_raw else [],
    }


def validate_ai_ops_v2_environment(ssh: SSH) -> None:
    env = inspect_ai_ops_v2_environment(ssh)
    services = env["services"]
    required = {
        "boutique_frontend",
        "boutique_productcatalogservice",
        "boutique_cartservice",
        "boutique_redis-cart",
        "boutique_checkoutservice",
        "boutique_paymentservice",
    }
    service_names = {line.split("|", 1)[0] for line in services}
    missing = sorted(required - service_names)
    if not env["manager"] or missing:
        raise RuntimeError(
            "AI Ops v2 runner requires the Online Boutique Docker Swarm environment. "
            f"swarm={env['swarm']}; manager={env['manager'] or 'none'}; missing_services={missing}. "
            "Current VM appears to be a different compose-based Mini-Drop microservices environment, "
            "so the official ai_ops_v2 fixture/oracle runner cannot execute safely."
        )


def prepare_worker_collectors(ssh: SSH) -> None:
    password = shlex.quote(ssh.password)
    for node in (WORKER1, WORKER2):
        command = _worker_collector_override_command(ssh.repo_root(node))
        ssh.run(node, f"printf '%s\\n' {password} | sudo -S /bin/bash -c {shlex.quote(command)}", timeout=240)


def _worker_collector_override_command(repo_root: str) -> str:
    return (
        "set -e; "
        f"cd {shlex.quote(repo_root)}; "
        "cat > docker-compose.ai-ops-v2-collectors.yml <<'YAML'\n"
        "services:\n"
        "  agent:\n"
        "    networks:\n"
        "      - default\n"
        "      - boutique\n"
        "    environment:\n"
        "      MINI_DROP_BLACKBOX_URL: http://blackbox-exporter:9115\n"
        "      MINI_DROP_REDIS_EXPORTER_URL: http://redis-exporter:9121\n"
        "  blackbox-exporter:\n"
        "    networks:\n"
        "      - default\n"
        "      - boutique\n"
        "  otel-collector:\n"
        "    networks:\n"
        "      - default\n"
        "      - boutique\n"
        "  redis-exporter:\n"
        "    networks:\n"
        "      - default\n"
        "      - boutique\n"
        "    environment:\n"
        "      REDIS_ADDR: redis://redis-cart:6379\n"
        "networks:\n"
        "  boutique:\n"
        "    external: true\n"
        "    name: boutique_boutique\n"
        "YAML\n"
        "docker compose --profile redis --env-file deploy/env/worker.env "
        "-f docker-compose.worker.yml -f docker-compose.ai-ops-v2-collectors.yml up -d agent blackbox-exporter otel-collector redis-exporter"
    )


def clean_environment(ssh: SSH, remote_scripts: dict[str, str]) -> dict[str, Any]:
    errors: list[str] = []
    for node in (WORKER1, WORKER2):
        try:
            ssh.sudo(node, f"bash {remote_scripts[node.name]} clean", timeout=180)
        except Exception as exc:
            errors.append(f"{node.name}: {exc}")
    env = inspect_ai_ops_v2_environment(ssh)
    if not env["manager"]:
        raise RuntimeError(f"AI Ops v2 requires a Docker Swarm manager; swarm={env['swarm']}")
    services = "\n".join(env["services"])
    unhealthy = [line for line in services.splitlines() if not line.endswith("|1/1")]
    return {"errors": errors, "unhealthy_services": unhealthy, "frontend": frontend_probe()}


def create_and_run(api: API, case_id: str, query: str, scope: dict[str, Any],
                   repetition: int, *, budget_profile: str = "production_safe",
                   auto_execute_policy: str = "safe_only",
                   timeout_sec: int = 420) -> tuple[str, dict[str, Any]]:
    started = api.call("/api/v1/diagnoses", "POST", {
        "query": (
            f"[AI Ops v2 {case_id} / repetition {repetition}] {query}\n"
            "请定位根因、说明证据限制，并给出可验证且可回滚的处理建议。"
        ),
        "context": {
            "service_id": scope.get("service_id"),
            "environment": "production",
            "instances": scope.get("instances") or [],
            "dependencies": scope.get("dependencies") or [],
        },
        "budget_profile": budget_profile,
        "auto_execute_policy": auto_execute_policy,
    }, timeout=90)
    diagnosis_id = started["diagnosis_id"]
    deadline = time.monotonic() + timeout_sec
    approved: set[str] = set()
    started_at = time.monotonic()
    last: dict[str, Any] = {}
    transient_errors: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        try:
            last = api.call(f"/api/v1/diagnoses/{diagnosis_id}", timeout=120)
        except Exception as exc:
            if not is_transient_api_error(exc):
                raise
            transient_errors.append({
                "at_elapsed_sec": round(time.monotonic() - started_at, 2),
                "error": f"{type(exc).__name__}: {exc}",
            })
            time.sleep(3)
            continue
        for probe in last.get("probes") or []:
            step_id = probe.get("step_id")
            if probe.get("status") == "WAITING_APPROVAL" and step_id not in approved:
                try:
                    api.call(f"/api/v1/diagnoses/{diagnosis_id}/approvals", "POST", {
                        "step_id": step_id,
                        "decision": "approve",
                        "scope": "single_execution",
                        "approver_id": "ai_ops_v2_eval_runner",
                    }, timeout=60)
                    approved.add(step_id)
                except APIError as exc:
                    if exc.status_code != 409 or "并发探针预算已用尽" not in exc.detail:
                        raise
        if last.get("status") in TERMINAL:
            break
        time.sleep(2)
    else:
        raise DiagnosisRunError(
            f"diagnosis {diagnosis_id} exceeded {timeout_sec}s",
            diagnosis_id=diagnosis_id,
            detail=last,
        )
    last["evaluation_runtime"] = {
        "elapsed_sec": round(time.monotonic() - started_at, 2),
        "approved_probe_count": len(approved),
        "transient_api_errors": transient_errors,
    }
    return diagnosis_id, last


def assert_controlled_ai_tree_participated(diagnosis_id: str, detail: dict[str, Any]) -> None:
    conclusion = detail.get("latest_conclusion") or {}
    tree = conclusion.get("controlled_ai_tree")
    if not isinstance(tree, dict):
        raise DiagnosisRunError(
            f"diagnosis {diagnosis_id} completed without controlled_ai_tree",
            diagnosis_id=diagnosis_id,
            detail=detail,
        )
    layers = tree.get("layers") if isinstance(tree.get("layers"), list) else []
    probe_edges = tree.get("probe_edges") if isinstance(tree.get("probe_edges"), list) else []
    if not layers:
        raise DiagnosisRunError(
            f"diagnosis {diagnosis_id} controlled_ai_tree has no layers",
            diagnosis_id=diagnosis_id,
            detail=detail,
        )
    if not any(isinstance(layer, dict) and layer.get("generated_by") == "ai_guarded" for layer in layers):
        raise DiagnosisRunError(
            f"diagnosis {diagnosis_id} controlled_ai_tree did not include an ai_guarded layer",
            diagnosis_id=diagnosis_id,
            detail=detail,
        )
    if not probe_edges:
        raise DiagnosisRunError(
            f"diagnosis {diagnosis_id} controlled_ai_tree has no probe_edges",
            diagnosis_id=diagnosis_id,
            detail=detail,
        )


def install_no_reuse_override(ssh: SSH) -> None:
    password = shlex.quote(ssh.password)
    command = (
        "set -e; "
        "if systemctl list-unit-files mini-drop-server.service >/dev/null 2>&1; then "
        "install -d /run/systemd/system/mini-drop-server.service.d; "
        "printf '[Service]\\nEnvironment=MINI_DROP_DIAGNOSIS_REUSE_MAX_AGE_SECONDS=0\\n' "
        "> /run/systemd/system/mini-drop-server.service.d/ai-ops-v2-eval.conf; "
        "systemctl daemon-reload; systemctl restart mini-drop-server; "
        "elif docker ps --format '{{.Names}}' | grep -qx 'mini-drop-control-server-1'; then "
        + _compose_no_reuse_install_command() + " "
        "else true; fi; "
        "for i in $(seq 1 30); do curl -kfsS https://127.0.0.1/api/healthz >/dev/null && exit 0; sleep 1; done; exit 1"
    )
    ssh.run(CONTROL, f"printf '%s\\n' {password} | sudo -S /bin/bash -c {shlex.quote(command)}", timeout=90)


def remove_no_reuse_override(ssh: SSH) -> None:
    password = shlex.quote(ssh.password)
    command = (
        "if systemctl list-unit-files mini-drop-server.service >/dev/null 2>&1; then "
        "rm -f /run/systemd/system/mini-drop-server.service.d/ai-ops-v2-eval.conf; "
        "systemctl daemon-reload; systemctl restart mini-drop-server; "
        "elif docker ps --format '{{.Names}}' | grep -qx 'mini-drop-control-server-1'; then "
        + _compose_no_reuse_remove_command() + " "
        "fi"
    )
    ssh.run(CONTROL, f"printf '%s\\n' {password} | sudo -S /bin/bash -c {shlex.quote(command)}", timeout=90)


def _compose_no_reuse_install_command() -> str:
    dirs = " ".join(shlex.quote(path) for path in CONTROL_COMPOSE_DIRS)
    return (
        "for dir in " + dirs + "; do "
        "[ -f \"$dir/docker-compose.control.yml\" ] || continue; "
        "cat > \"$dir/docker-compose.ai-ops-v2-no-reuse.yml\" <<'YAML'\n"
        "services:\n"
        "  server:\n"
        "    environment:\n"
        "      MINI_DROP_DIAGNOSIS_REUSE_MAX_AGE_SECONDS: \"0\"\n"
        "YAML\n"
        "cd \"$dir\"; "
        "docker compose --env-file deploy/env/control.env -f docker-compose.control.yml "
        "-f docker-compose.ai-ops-v2-no-reuse.yml up -d server; "
        "exit 0; "
        "done; true;"
    )


def _compose_no_reuse_remove_command() -> str:
    dirs = " ".join(shlex.quote(path) for path in CONTROL_COMPOSE_DIRS)
    return (
        "for dir in " + dirs + "; do "
        "[ -f \"$dir/docker-compose.control.yml\" ] || continue; "
        "rm -f \"$dir/docker-compose.ai-ops-v2-no-reuse.yml\"; "
        "cd \"$dir\"; "
        "docker compose --env-file deploy/env/control.env -f docker-compose.control.yml up -d server; "
        "exit 0; "
        "done; true;"
    )


def load_queries() -> dict[str, str]:
    payload = json.loads(PUBLIC_CASES.read_text(encoding="utf-8"))
    return {item["case_id"]: item["query"] for item in payload["cases"]}


def load_control_api_key(ssh: SSH) -> str:
    local_key = os.getenv("MINI_DROP_API_KEY", "").strip()
    if local_key:
        return local_key
    candidates = " ".join(shlex.quote(path) for path in CONTROL_ENV_FILES)
    command = (
        "for file in " + candidates + "; do "
        "[ -f \"$file\" ] || continue; "
        "key=$(grep '^MINI_DROP_API_KEY=' \"$file\" | tail -1 | cut -d= -f2-); "
        "[ -n \"$key\" ] && printf '%s' \"$key\" && exit 0; "
        "done"
    )
    return ssh.run(CONTROL, f"/bin/bash -c {shlex.quote(command)}").strip()


def append_jsonl(path: Path, item: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        handle.flush()


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [item for item in records if item.get("phase") == "completed"]
    elapsed = [float(item.get("elapsed_sec", 0)) for item in completed]
    return {
        "planned_runs": len(records),
        "completed_runs": len(completed),
        "failed_runs": sum(1 for item in records if item.get("phase") == "failed"),
        "terminal_statuses": {
            status: sum(1 for item in completed if item.get("diagnosis_status") == status)
            for status in sorted({str(item.get("diagnosis_status")) for item in completed})
        },
        "mean_elapsed_sec": round(statistics.mean(elapsed), 2) if elapsed else None,
        "p95_elapsed_sec": round(sorted(elapsed)[max(0, int(len(elapsed) * .95) - 1)], 2) if elapsed else None,
        "rollback_failures": sum(1 for item in completed if not item.get("rollback", {}).get("frontend", {}).get("ok")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="", help="comma-separated case ids; default all")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260811)
    parser.add_argument("--output-dir", type=Path,
                        default=ROOT / "reports" / "eval" / "ai-ops-v2" / "live-vm-20260811")
    parser.add_argument("--keep-reuse-policy", action="store_true")
    parser.add_argument("--resume", action="store_true", help="skip completed case/repetition pairs in output")
    parser.add_argument("--cleanup-only", action="store_true", help="remove benchmark faults and restore control policy")
    parser.add_argument(
        "--budget-profile",
        choices=("production_safe", "staging", "development"),
        default="production_safe",
    )
    parser.add_argument(
        "--auto-execute-policy",
        choices=("safe_only", "all_registered", "manual"),
        default="safe_only",
        help="all_registered 会自动执行已注册深度探针，但仍受预算和 capability 门禁约束",
    )
    args = parser.parse_args()
    password = os.getenv("MINI_DROP_VM_PASSWORD", "")
    if not password:
        parser.error("MINI_DROP_VM_PASSWORD is required")
    if not 1 <= args.repetitions <= 10:
        parser.error("--repetitions must be between 1 and 10")

    queries = load_queries()
    wanted = [item.strip() for item in args.cases.split(",") if item.strip()] or list(SPECS)
    unknown = sorted(set(wanted) - set(SPECS))
    if unknown:
        parser.error(f"unknown cases: {', '.join(unknown)}")
    jobs = [(case_id, repetition) for case_id in wanted for repetition in range(1, args.repetitions + 1)]
    random.Random(args.seed).shuffle(jobs)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    bundle_dir = args.output_dir / "bundles"
    bundle_dir.mkdir(exist_ok=True)
    records_path = args.output_dir / "run-records.jsonl"
    plan_path = args.output_dir / "run-plan.json"
    existing_records: list[dict[str, Any]] = []
    if args.resume and records_path.is_file():
        existing_records = [
            json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        completed_keys = {
            (item.get("case_id"), int(item.get("repetition", 0)))
            for item in existing_records if item.get("phase") == "completed"
        }
        jobs = [job for job in jobs if job not in completed_keys]
    if not (args.resume and plan_path.is_file()):
        plan_path.write_text(json.dumps({
            "schema_version": "1.0", "created_at": utcnow(), "seed": args.seed,
            "repetitions": args.repetitions, "jobs": [
                {"case_id": case_id, "repetition": repetition} for case_id, repetition in jobs
            ],
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    ssh = SSH(password)
    remote_scripts = {node.name: ssh.deploy_faultctl(node) for node in (WORKER1, WORKER2)}
    if args.cleanup_only:
        health = clean_environment(ssh, remote_scripts)
        remove_no_reuse_override(ssh)
        print(json.dumps({"cleanup": "completed", "health": health}, ensure_ascii=False, indent=2))
        return 0 if not health["errors"] and not health["unhealthy_services"] and health["frontend"]["ok"] else 1
    validate_ai_ops_v2_environment(ssh)
    prepare_worker_collectors(ssh)
    for node in (WORKER1, WORKER2):
        ssh.sudo(node, f"bash {remote_scripts[node.name]} prepare", timeout=180)
    key = load_control_api_key(ssh)
    if not key:
        raise RuntimeError(
            "control API key is empty; set local MINI_DROP_API_KEY or add MINI_DROP_API_KEY "
            f"to one of: {', '.join(CONTROL_ENV_FILES)}"
        )
    api = API(key)
    if not args.keep_reuse_policy:
        install_no_reuse_override(ssh)

    records: list[dict[str, Any]] = list(existing_records)
    previous_rollback = clean_environment(ssh, remote_scripts)
    try:
        for ordinal, (case_id, repetition) in enumerate(jobs, 1):
            spec = SPECS[case_id]
            run_key = f"r{repetition}-{int(time.time())}"
            record: dict[str, Any] = {
                "case_id": case_id, "fixture": spec.fixture, "repetition": repetition,
                "ordinal": ordinal, "started_at": utcnow(), "phase": "started",
            }
            print(f"[{ordinal}/{len(jobs)}] {case_id} repetition={repetition}", flush=True)
            baseline = previous_rollback
            record["baseline"] = baseline
            if baseline["errors"] or baseline["unhealthy_services"] or not baseline["frontend"]["ok"]:
                raise RuntimeError(f"unclean baseline for {case_id}: {baseline}")
            started_at = time.monotonic()
            diagnosis_id: str | None = None
            detail: dict[str, Any] = {}
            try:
                for node_name, fixture in spec.inject:
                    node = WORKERS[node_name]
                    ssh.sudo(node, f"bash {remote_scripts[node_name]} inject {shlex.quote(fixture)}", timeout=180)
                time.sleep(spec.settle_sec)
                scope = resolve_scope(ssh, spec, run_key)
                record["target_count"] = len(scope.get("instances") or [])
                record["fault_probe"] = frontend_probe()
                diagnosis_id, detail = create_and_run(
                    api,
                    case_id,
                    queries[case_id],
                    scope,
                    repetition,
                    budget_profile=args.budget_profile,
                    auto_execute_policy=args.auto_execute_policy,
                )
                record["diagnosis_id"] = diagnosis_id
                assert_controlled_ai_tree_participated(diagnosis_id, detail)
                bundle = call_with_retries(
                    api,
                    f"/api/v1/diagnoses/{diagnosis_id}/audit-bundle",
                    timeout=180,
                    attempts=4,
                    sleep_sec=5,
                )
                bundle_path = bundle_dir / f"{case_id}__r{repetition:02d}.json"
                bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
                record.update({
                    "phase": "completed",
                    "diagnosis_id": diagnosis_id,
                    "diagnosis_status": detail.get("status"),
                    "evidence_count": len(detail.get("evidence") or []),
                    "probe_count": len(detail.get("probes") or []),
                    "approved_probe_count": (detail.get("evaluation_runtime") or {}).get("approved_probe_count", 0),
                    "budget_profile": args.budget_profile,
                    "auto_execute_policy": args.auto_execute_policy,
                    "diagnosis_elapsed_sec": (detail.get("evaluation_runtime") or {}).get("elapsed_sec"),
                    "bundle": str(bundle_path.relative_to(args.output_dir)),
                })
            except DiagnosisRunError as exc:
                if exc.diagnosis_id:
                    record["diagnosis_id"] = exc.diagnosis_id
                detail = exc.detail
                record.update({
                    "phase": "failed",
                    "diagnosis_status": detail.get("status"),
                    "error": f"{type(exc).__name__}: {exc}",
                })
                print(f"  failed: {record['error']}", flush=True)
            except Exception as exc:
                record.update({"phase": "failed", "error": f"{type(exc).__name__}: {exc}"})
                print(f"  failed: {record['error']}", flush=True)
            finally:
                record["rollback"] = clean_environment(ssh, remote_scripts)
                previous_rollback = record["rollback"]
                record["elapsed_sec"] = round(time.monotonic() - started_at, 2)
                record["finished_at"] = utcnow()
                records.append(record)
                append_jsonl(records_path, record)
                print(f"  {record['phase']} status={record.get('diagnosis_status')} elapsed={record['elapsed_sec']}s", flush=True)
    finally:
        final_health = clean_environment(ssh, remote_scripts)
        if not args.keep_reuse_policy:
            remove_no_reuse_override(ssh)
        summary = summarize(records)
        summary["final_health"] = final_health
        summary["finished_at"] = utcnow()
        (args.output_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        mapped_runs: dict[str, list[tuple[int, str]]] = {}
        for record in records:
            if record.get("diagnosis_id"):
                mapped_runs.setdefault(record["case_id"], []).append((
                    int(record.get("repetition") or 0), record["diagnosis_id"],
                ))
        diagnosis_map = {
            case_id: [diagnosis_id for _, diagnosis_id in sorted(items)]
            for case_id, items in mapped_runs.items()
        }
        (args.output_dir / "diagnosis-map.json").write_text(
            json.dumps(diagnosis_map, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if records and all(item.get("phase") == "completed" for item in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
