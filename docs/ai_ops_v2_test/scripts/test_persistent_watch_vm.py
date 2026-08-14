#!/usr/bin/env python3
"""Smoke-test Persistent Watch against the real three-node VM lab."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from run_ai_ops_v2_vm import SSH, WORKERS, load_control_api_key


CONTROL_IP = "172.18.88.237"
DEFAULT_AGENT_ID = "linux-worker-2"


class APIError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class API:
    def __init__(self, api_key: str, base_url: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.context = ssl.create_default_context()
        self.context.check_hostname = False
        self.context.verify_mode = ssl.CERT_NONE

    def call(
        self,
        path: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        *,
        timeout: int = 45,
    ) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("X-API-Key", self.api_key)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise APIError(exc.code, detail) from exc
        return payload.get("data", payload)


def utc_iso(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def wait_task(api: API, task_id: str, timeout_sec: int = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = api.call(f"/api/tasks/{task_id}")
        status = str(last.get("status", "")).upper()
        if status in {"DONE", "COMPLETED", "FAILED", "CANCELLED", "CANCELED"}:
            return last
        time.sleep(2)
    raise TimeoutError(f"task {task_id} did not finish within {timeout_sec}s; last={last}")


def wait_incident_analysis(
    api: API,
    watch_id: str,
    incident_id: str,
    timeout_sec: int = 180,
) -> dict[str, Any]:
    """Wait for triggered collector tasks and automatic incident re-analysis."""
    deadline = time.monotonic() + timeout_sec
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = api.call(f"/api/v1/watches/{watch_id}/incidents")
        items = payload.get("items") or []
        last = next(
            (item for item in items if item.get("incident_id") == incident_id),
            {},
        )
        tasks = last.get("collector_tasks") or []
        statuses = {str(item.get("status", "")).upper() for item in tasks}
        analysis_status = str(last.get("analysis_status", "")).lower()
        if tasks and statuses <= {"DONE", "FAILED"} and analysis_status in {
            "analyzed",
            "needs_evidence",
            "analysis_failed",
        }:
            return last
        time.sleep(3)
    raise TimeoutError(
        f"incident {incident_id} did not reach automatic analysis within "
        f"{timeout_sec}s; last={last}"
    )


def wait_agent_incident(api: API, watch_id: str, timeout_sec: int = 240) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last_items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        payload = api.call(f"/api/v1/watches/{watch_id}/incidents")
        last_items = payload.get("items") or []
        for item in last_items:
            if item.get("trigger_event_id") and item.get("structured_evidence"):
                return item
        time.sleep(5)
    raise TimeoutError(
        f"watch {watch_id} did not produce an agent-observed incident "
        f"within {timeout_sec}s; last={last_items}"
    )


def choose_pid(api: API, agent_id: str, query: str) -> dict[str, Any]:
    refresh = api.call(f"/api/v1/agents/{agent_id}/process-inventory/refresh", "POST")
    wait_task(api, refresh["task_id"], timeout_sec=90)
    processes = api.call(f"/api/v1/agents/{agent_id}/processes?query={quote(query)}&limit=20")
    items = processes.get("items") or []
    if not items:
        raise RuntimeError(f"no process matched query={query!r} on agent={agent_id}")
    return items[0]


def make_window(start_offset: int, end_offset: int, cpu_values: list[float]) -> dict[str, Any]:
    end = utc_iso(end_offset)
    samples = [
        {
            "observed_at": utc_iso(start_offset + index),
            "cpu_percent": value,
            "rss_mb": 100.0,
            "thread_count": 8,
        }
        for index, value in enumerate(cpu_values)
    ]
    return {"start": utc_iso(start_offset), "end": end, "samples": samples}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("manual_evaluate", "agent_observe"),
        default="manual_evaluate",
        help="manual_evaluate 调 API 注入窗口；agent_observe 等 Agent 真实采样后自动触发",
    )
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID)
    parser.add_argument("--process-query", default="cartservice")
    parser.add_argument("--service-id", default="cartservice")
    parser.add_argument("--base-url", default=f"https://{CONTROL_IP}")
    parser.add_argument("--output", default="")
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument(
        "--trigger-action",
        choices=("freeze_only", "freeze_and_safe_probe", "auto_all_registered", "manual_approval"),
        default="freeze_and_safe_probe",
    )
    parser.add_argument("--fixture-node", choices=tuple(WORKERS), default="worker1")
    parser.add_argument("--fixture", default="productcatalog_cpu_hotspot_v1")
    parser.add_argument(
        "--prestart-fixture",
        default="",
        help="先启动一个可监视目标，再刷新进程清单；适合 watch_cpu_shift_v1 这类延迟异常 fixture",
    )
    parser.add_argument("--baseline-wait-sec", type=int, default=75)
    parser.add_argument("--incident-timeout-sec", type=int, default=260)
    parser.add_argument("--analysis-timeout-sec", type=int, default=300)
    parser.add_argument("--cleanup-fixture", action="store_true")
    parser.add_argument("--cleanup-watch", action="store_true")
    args = parser.parse_args()

    api_key = os.getenv("MINI_DROP_API_KEY", "").strip()
    password = os.getenv("MINI_DROP_VM_PASSWORD", "")
    if not api_key:
        if password:
            api_key = load_control_api_key(SSH(password))
    if not api_key:
        parser.error(
            "MINI_DROP_API_KEY is required, or set MINI_DROP_VM_PASSWORD so "
            "the script can read the Control env without printing the key"
        )
    api = API(api_key, args.base_url)
    if args.mode == "agent_observe" and not password:
        parser.error("agent_observe mode requires MINI_DROP_VM_PASSWORD for VM fault injection")
    remote_scripts: dict[str, str] = {}
    vm_ssh = SSH(password) if args.mode == "agent_observe" else None
    if args.mode == "agent_observe" and vm_ssh:
        node = WORKERS[args.fixture_node]
        remote_scripts[node.name] = vm_ssh.deploy_faultctl(node)
        if args.cleanup_fixture:
            vm_ssh.sudo(node, f"bash {remote_scripts[node.name]} clean", timeout=180)
        if args.prestart_fixture:
            vm_ssh.sudo(
                node,
                f"bash {remote_scripts[node.name]} inject {shlex.quote(args.prestart_fixture)}",
                timeout=180,
            )

    try:
        process = choose_pid(api, args.agent_id, args.process_query)
    except Exception:
        if args.mode == "agent_observe" and args.cleanup_fixture and vm_ssh:
            for node_name, script in remote_scripts.items():
                vm_ssh.sudo(WORKERS[node_name], f"bash {script} clean", timeout=180)
        raise
    pid = int(process["pid"])
    watch = api.call("/api/v1/watches", "POST", {
        "name": f"vm-smoke-{args.service_id}-{pid}",
        "target": {
            "agent_id": args.agent_id,
            "target_pid": pid,
            "service_id": args.service_id,
            "instance_id": process.get("name") or args.service_id,
        },
        "target_config": {
            "service_id": args.service_id,
            "process_query": args.process_query,
            "container_id": process.get("container_id"),
        },
        "trigger_action": args.trigger_action,
    })
    leases = api.call(f"/api/v1/agents/{args.agent_id}/watch-leases")
    lease_items = leases.get("items") or []
    if not any(item.get("watch_id") == watch["watch_id"] for item in lease_items):
        raise RuntimeError(f"watch lease not visible for agent {args.agent_id}: {lease_items}")

    result: dict[str, Any]
    try:
        if args.mode == "agent_observe":
            node = WORKERS[args.fixture_node]
            time.sleep(max(0, args.baseline_wait_sec))
            if vm_ssh and args.fixture and args.fixture.lower() not in {"none", "null", "-"}:
                vm_ssh.sudo(
                    node,
                    f"bash {remote_scripts[node.name]} inject {shlex.quote(args.fixture)}",
                    timeout=180,
                )
            incident = wait_agent_incident(api, watch["watch_id"], timeout_sec=args.incident_timeout_sec)
            result = {
                "mode": args.mode,
                "fixture_node": args.fixture_node,
                "fixture": args.fixture,
                "prestart_fixture": args.prestart_fixture,
                "baseline_wait_sec": args.baseline_wait_sec,
                "incident_timeout_sec": args.incident_timeout_sec,
                "analysis_timeout_sec": args.analysis_timeout_sec,
                "incident": incident,
            }
        else:
            result = api.call(f"/api/v1/watches/{watch['watch_id']}/evaluate", "POST", {
                "baseline_window": make_window(-70, -40, [1.0, 1.2, 1.1, 1.0, 1.3]),
                "trigger_window": make_window(-10, 0, [70.0, 76.0, 82.0, 79.0, 85.0]),
            })
            incident = result.get("incident")
    except Exception:
        if args.mode == "agent_observe" and args.cleanup_fixture and vm_ssh:
            for node_name, script in remote_scripts.items():
                vm_ssh.sudo(WORKERS[node_name], f"bash {script} clean", timeout=180)
        raise

    if not incident:
        raise RuntimeError(f"watch test did not create incident: {result}")
    required = ("trigger_event_id", "evidence_cohort_id", "snapshot_refs", "structured_evidence")
    missing = [key for key in required if not incident.get(key)]
    if missing:
        raise RuntimeError(f"incident missing required fields {missing}: {incident}")

    analysis = None
    auto_analysis_verified = False
    try:
        if args.analyze:
            analysis = wait_incident_analysis(
                api,
                watch["watch_id"],
                incident["incident_id"],
                timeout_sec=args.analysis_timeout_sec,
            )
            auto_analysis_verified = bool(
                (analysis.get("analysis_result") or {}).get("analysis_session_id")
            )
            collector_tasks = (
                ((result.get("trigger") or {}).get("collector_tasks") or [])
                + (incident.get("collector_tasks") or [])
            )
            if collector_tasks and not auto_analysis_verified:
                raise RuntimeError(
                    "triggered collector tasks reached terminal state, but automatic "
                    "Watch incident analysis was not persisted"
                )
            if not auto_analysis_verified and not collector_tasks:
                analysis = api.call(
                    f"/api/v1/watch-incidents/{incident['incident_id']}/analyze",
                    "POST",
                    {
                        "query": (
                            "请基于冻结现场和结构化证据进行 AI 树分析，"
                            "明确定位边界和下一步证据。"
                        )
                    },
                )
    except Exception:
        if args.mode == "agent_observe" and args.cleanup_fixture and vm_ssh:
            for node_name, script in remote_scripts.items():
                vm_ssh.sudo(WORKERS[node_name], f"bash {script} clean", timeout=180)
        raise

    if args.mode == "agent_observe" and args.cleanup_fixture and vm_ssh:
        for node_name, script in remote_scripts.items():
            vm_ssh.sudo(WORKERS[node_name], f"bash {script} clean", timeout=180)
    cleanup_watch_result = None
    if args.cleanup_watch:
        cleanup_watch_result = api.call(f"/api/v1/watches/{watch['watch_id']}", "DELETE")

    report = {
        "agent_id": args.agent_id,
        "process": process,
        "watch": watch,
        "leases": lease_items,
        "result": result,
        "analysis": analysis,
        "auto_analysis_verified": auto_analysis_verified,
        "cleanup_watch": cleanup_watch_result,
        "checked_at": utc_iso(),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
