"""micro-drop command line interface.

The CLI is intentionally script-friendly: commands print JSON by default and
avoid depending on a running browser. It complements the Web UI for SSH,
automation, CI checks, batch jobs and diff analysis.
"""

from __future__ import annotations

import server.app._env  # noqa: F401 — 自动加载 .env

import argparse
import json
import time
from pathlib import Path

from server.app.ai_provider import get_ai_settings
from server.app.nlp.intent_parser import parse_intent
from server.app.nlp.summarizer import summarize, suggest_followup
from server.app.rca.models import EvidenceInput
from server.app.rca.report import run_diagnosis_context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="micro-drop", description="Micro-Drop profiling CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="start Server + Web API")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8191)

    sub.add_parser("agent", help="start profiling Agent")
    sub.add_parser("ai-config", help="print resolved AI provider and feature flags")

    p_parse = sub.add_parser("parse", help="parse natural language into profiling intent")
    p_parse.add_argument("query", nargs="+")

    p_sum = sub.add_parser("summarize", help="summarize a TopN JSON file")
    p_sum.add_argument("--top-json", required=True)
    p_sum.add_argument("--collector", default="perf_cpu")

    p_diag = sub.add_parser("diagnose-local", help="run local RCA from structured evidence JSON")
    p_diag.add_argument("--evidence", required=True)

    p_diff = sub.add_parser("diff-top", help="compare two TopN JSON files")
    p_diff.add_argument("--base", required=True)
    p_diff.add_argument("--head", required=True)
    p_diff.add_argument("--threshold", type=float, default=5.0)

    p_ci = sub.add_parser("ci-check", help="fail CI when TopN diff exceeds threshold")
    p_ci.add_argument("--base", required=True)
    p_ci.add_argument("--head", required=True)
    p_ci.add_argument("--threshold", type=float, default=5.0)

    p_alert = sub.add_parser("alert", help="evaluate hotspot thresholds from TopN JSON")
    p_alert.add_argument("--top-json", required=True)
    p_alert.add_argument("--hotspot-threshold", type=float, default=70.0)
    p_alert.add_argument("--sample-threshold", type=int, default=0)

    p_batch = sub.add_parser("batch-diagnose", help="run local RCA for a directory of evidence JSON files")
    p_batch.add_argument("--dir", required=True)
    p_batch.add_argument("--pattern", default="*.json")

    p_export = sub.add_parser("export-summary", help="export TopN summary as markdown or JSON")
    p_export.add_argument("--top-json", required=True)
    p_export.add_argument("--format", choices=["json", "markdown"], default="json")
    p_export.add_argument("--limit", type=int, default=10)

    p_watch = sub.add_parser("watch-task", help="poll a Server task until terminal status")
    p_watch.add_argument("--url", default="http://localhost:8191")
    p_watch.add_argument("--task-id", required=True)
    p_watch.add_argument("--interval", type=float, default=2.0)
    p_watch.add_argument("--timeout", type=float, default=120.0)

    p_keywords = sub.add_parser("keywords", help="print CLI keyword dictionaries")
    p_keywords.add_argument("--kind", choices=["all", "commands", "collectors", "causes", "fields"], default="all")

    p_suggest = sub.add_parser("suggest", help="suggest commands/keywords for a prefix")
    p_suggest.add_argument("prefix", nargs="?", default="")
    p_suggest.add_argument("--kind", choices=["all", "commands", "collectors", "causes", "fields"], default="all")

    p_completion = sub.add_parser("completion", help="print shell completion script")
    p_completion.add_argument("--shell", choices=["bash", "zsh", "powershell"], required=True)

    # ── 新增命令 ──────────────────────────────────────────────

    p_collect = sub.add_parser("collect", help="one-shot remote collection via Server API")
    p_collect.add_argument("--url", default="http://localhost:8191")
    p_collect.add_argument("--agent", required=True, help="target agent ID")
    p_collect.add_argument("--pid", type=int, required=True, help="target process PID")
    p_collect.add_argument("--collector", default="perf_cpu",
                           choices=["perf_cpu", "ebpf_io", "pyspy", "continuous_perf", "java_async", "go_pprof", "memory_smaps", "off_cpu_wait_profile", "trace_endpoint_profile", "baseline_window_profile"])
    p_collect.add_argument("--duration", type=int, default=15)
    p_collect.add_argument("--sample-rate", type=int, default=99)
    p_collect.add_argument("--name", default="", help="task display name")
    p_collect.add_argument("--watch", action="store_true", default=True, help="poll until terminal (default)")
    p_collect.add_argument("--no-watch", action="store_false", dest="watch")

    p_status = sub.add_parser("status", help="query Server/Agent status")
    p_status.add_argument("--url", default="http://localhost:8191")
    p_status.add_argument("--agents", action="store_true", help="show agent list with resource metrics")
    p_status.add_argument("--tasks", action="store_true", help="show active tasks only")

    p_perf_top = sub.add_parser("perf-top", help="local perf top-N without Agent/Server")
    p_perf_top.add_argument("--pid", type=int, required=True)
    p_perf_top.add_argument("--duration", type=int, default=10)
    p_perf_top.add_argument("--limit", type=int, default=10)

    sub.add_parser("version", help="print Mini-Drop version")

    # ── 新增命令 (v0.2) ────────────────────────────────────────

    p_cancel = sub.add_parser("task-cancel", help="cancel a running task via Server API")
    p_cancel.add_argument("--url", default="http://localhost:8191")
    p_cancel.add_argument("--task-id", required=True)

    p_remote_diag = sub.add_parser("diagnose-remote", help="trigger remote diagnosis via Server API")
    p_remote_diag.add_argument("--url", default="http://localhost:8191")
    p_remote_diag.add_argument("--task-id", required=True)
    p_remote_diag.add_argument("--wait", action="store_true", help="wait for diagnosis to complete")

    p_report = sub.add_parser("report", help="generate a comprehensive profiling report")
    p_report.add_argument("--top-json", required=True)
    p_report.add_argument("--evidence", default="")
    p_report.add_argument("--format", choices=["json", "markdown"], default="markdown")
    p_report.add_argument("--output", default="-", help="output file path, - for stdout")

    p_feedback = sub.add_parser("feedback-stats", help="show RCA feedback statistics")
    p_feedback.add_argument("--url", default="http://localhost:8191")

    p_storage_ls = sub.add_parser("storage-ls", help="list tasks in object storage")
    p_storage_ls.add_argument("--url", default="http://localhost:8191")
    p_storage_ls.add_argument("--task-id", default="", help="filter by task ID prefix")

    p_storage_prune = sub.add_parser("storage-prune", help="delete old task artifacts (dry-run by default)")
    p_storage_prune.add_argument("--url", default="http://localhost:8191")
    p_storage_prune.add_argument("--older-than-days", type=int, default=30)
    p_storage_prune.add_argument("--execute", action="store_true", help="actually delete (default: dry-run)")
    p_storage_prune.add_argument("--task-id", default="", help="only prune specified task ID prefix")

    p_agent_exec = sub.add_parser("agent-exec", help="execute a diagnostic/repair command on agent host (via task)")
    p_agent_exec.add_argument("--url", default="http://localhost:8191")
    p_agent_exec.add_argument("--diagnosis-id", required=True)
    p_agent_exec.add_argument("--action-index", type=int, default=0, help="which repair action to execute")

    p_install_check = sub.add_parser("install-check", help="check system dependencies and permissions")
    p_install_check.add_argument("--full", action="store_true", help="run full check including optional tools")

    args = parser.parse_args(argv)
    return _COMMANDS[args.command](args)


def _cmd_serve(args) -> int:
    import uvicorn
    uvicorn.run("server.app.main:app", host=args.host, port=args.port)
    return 0


def _cmd_agent(_args) -> int:
    from agent.mini_drop_agent.main import main as agent_main
    agent_main()
    return 0


def _cmd_ai_config(_args) -> int:
    settings = get_ai_settings()
    _print_json({
        "enabled": settings.enabled,
        "provider": settings.provider,
        "base_url": settings.base_url,
        "model": settings.model,
        "has_api_key": bool(settings.api_key),
        "features": {
            "nlp": settings.nlp_enabled,
            "rca": settings.rca_enabled,
            "summarize": settings.summarize_enabled,
        },
    })
    return 0


def _cmd_parse(args) -> int:
    intent = parse_intent(" ".join(args.query))
    _print_json(intent.to_dict())
    return 0


def _cmd_summarize(args) -> int:
    top = _read_json(args.top_json)
    summary = summarize(top)
    _print_json({
        "summary": summary,
        "followup_questions": suggest_followup(top, args.collector),
    })
    return 0


def _cmd_diagnose_local(args) -> int:
    _print_json(_diagnose_local_payload(args.evidence))
    return 0


def _cmd_diff_top(args) -> int:
    result = _diff_top(args.base, args.head, args.threshold)
    _print_json(result)
    return 2 if result["failed"] else 0


def _cmd_ci_check(args) -> int:
    result = _diff_top(args.base, args.head, args.threshold)
    result["ci_status"] = "failed" if result["failed"] else "passed"
    _print_json(result)
    return 2 if result["failed"] else 0


def _cmd_alert(args) -> int:
    top = _read_json(args.top_json)
    top1 = top[0] if top else {}
    percent = float(top1.get("percent", 0.0))
    samples = int(top1.get("samples", 0))
    triggered = percent >= args.hotspot_threshold and samples >= args.sample_threshold
    _print_json({
        "triggered": triggered,
        "reason": "hotspot_threshold_exceeded" if triggered else "within_threshold",
        "top_function": top1.get("name", ""),
        "percent": percent,
        "samples": samples,
        "thresholds": {
            "hotspot_percent": args.hotspot_threshold,
            "samples": args.sample_threshold,
        },
    })
    return 2 if triggered else 0


def _cmd_batch_diagnose(args) -> int:
    rows = []
    for path in sorted(Path(args.dir).glob(args.pattern)):
        try:
            report = _diagnose_local_payload(str(path))
            rows.append({
                "file": str(path),
                "ok": True,
                "summary": report["report"].get("summary", ""),
                "top_cause": (report["report"].get("ranked_causes") or [{}])[0].get("cause_id"),
            })
        except Exception as exc:
            rows.append({"file": str(path), "ok": False, "error": str(exc)})
    _print_json({"total": len(rows), "items": rows})
    return 1 if any(not item["ok"] for item in rows) else 0


def _cmd_export_summary(args) -> int:
    top = _read_json(args.top_json)[:args.limit]
    if args.format == "markdown":
        print("| Rank | Function | Samples | Percent |")
        print("|---:|---|---:|---:|")
        for idx, item in enumerate(top, start=1):
            print(f"| {idx} | `{item.get('name', '')}` | {item.get('samples', 0)} | {item.get('percent', 0)}% |")
    else:
        _print_json({"top_functions": top})
    return 0


def _cmd_watch_task(args) -> int:
    import requests
    deadline = time.time() + args.timeout
    last = None
    while time.time() <= deadline:
        resp = requests.get(f"{args.url.rstrip('/')}/api/tasks/{args.task_id}", timeout=5)
        resp.raise_for_status()
        data = resp.json()["data"]
        last = data
        status = data.get("status")
        print(json.dumps(data, ensure_ascii=False, default=str))
        if status in {"DONE", "FAILED"}:
            return 0 if status == "DONE" else 2
        time.sleep(args.interval)
    _print_json({"status": "TIMEOUT", "last": last})
    return 124


def _cmd_keywords(args) -> int:
    _print_json(_filter_keywords(args.kind))
    return 0


def _cmd_suggest(args) -> int:
    prefix = args.prefix.lower()
    result = {}
    for key, values in _filter_keywords(args.kind).items():
        result[key] = [item for item in values if item.lower().startswith(prefix)]
    _print_json(result)
    return 0


def _cmd_completion(args) -> int:
    print(_completion_script(args.shell))
    return 0


def _read_json(path: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"error: JSON 解析失败: {path} ({exc})", file=__import__("sys").stderr)
        raise SystemExit(1)
    except FileNotFoundError:
        print(f"error: 文件不存在: {path}", file=__import__("sys").stderr)
        raise SystemExit(1)
    except PermissionError:
        print(f"error: 无权限读取文件: {path}", file=__import__("sys").stderr)
        raise SystemExit(1)


def _top_map(rows: list[dict]) -> dict[str, float]:
    return {str(item.get("name")): float(item.get("percent", 0.0)) for item in rows}


def _diff_top(base_path: str, head_path: str, threshold: float) -> dict:
    base = _top_map(_read_json(base_path))
    head = _top_map(_read_json(head_path))
    names = sorted(set(base) | set(head))
    changes = []
    failed = False
    for name in names:
        delta = round(head.get(name, 0.0) - base.get(name, 0.0), 2)
        if abs(delta) >= threshold:
            failed = True
        if delta:
            changes.append({
                "name": name,
                "base_percent": base.get(name, 0.0),
                "head_percent": head.get(name, 0.0),
                "delta_percent": delta,
            })
    return {
        "threshold": threshold,
        "failed": failed,
        "changes": sorted(changes, key=lambda item: abs(item["delta_percent"]), reverse=True),
    }


def _diagnose_local_payload(evidence_path: str) -> dict:
    evidence_data = _read_json(evidence_path)
    evidence = EvidenceInput(**evidence_data)
    task_record = _TaskRecord(evidence.task_metadata)
    outcome = run_diagnosis_context(
        task_id=task_record.id,
        task_record=task_record,
        top_functions=evidence.top_functions,
        ebpf_metrics=evidence.ebpf_metrics,
        evidence_index=evidence.evidence_index,
        suggestions=evidence.suggestions,
        failure_events=evidence.failure_events,
        baseline_diff=evidence.baseline_diff,
        agent_stats=evidence.agent_stats,
        auto_execute_safe=False,
    )
    return {
        "report": outcome.report.report.model_dump(),
        "validated": outcome.report.validated,
        "tool_results": [item.model_dump() for item in outcome.tool_results],
        "repair_plan": outcome.repair_plan.model_dump() if outcome.repair_plan else None,
    }


def _print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


class _TaskRecord:
    def __init__(self, metadata: dict):
        self.id = metadata.get("task_id", "cli_task")
        self.agent_id = metadata.get("agent_id", "cli_agent")
        self.collector_type = metadata.get("collector_type", "perf_cpu")
        self.target_pid = int(metadata.get("target_pid", 0))
        self.sample_rate = int(metadata.get("sample_rate", 99))
        self.duration_sec = max(1, int(metadata.get("duration_sec", 15)))
        self.status = metadata.get("status", "DONE")
        self.status_reason = metadata.get("status_reason", "CLI local diagnosis")
        self.request_params = metadata.get("request_params", {})


def _filter_keywords(kind: str) -> dict[str, list[str]]:
    if kind == "all":
        return _KEYWORDS
    return {kind: _KEYWORDS[kind]}


def _completion_script(shell: str) -> str:
    words = _completion_words()
    if shell == "bash":
        return (
            "_micro_drop_complete() {\n"
            "  local cur=\"${COMP_WORDS[COMP_CWORD]}\"\n"
            f"  COMPREPLY=( $(compgen -W \"{' '.join(words)}\" -- \"$cur\") )\n"
            "}\n"
            "complete -F _micro_drop_complete micro-drop\n"
        )
    if shell == "zsh":
        zsh_words = " ".join(_shell_single_quote(word) for word in words)
        return (
            "#compdef micro-drop\n"
            "_micro_drop() {\n"
            f"  compadd -- {zsh_words}\n"
            "}\n"
            "compdef _micro_drop micro-drop\n"
        )
    ps_words = ", ".join(_powershell_single_quote(word) for word in words)
    return (
        "Register-ArgumentCompleter -Native -CommandName micro-drop -ScriptBlock {\n"
        "  param($wordToComplete)\n"
        f"  @({ps_words}) | Where-Object {{ $_ -like \"$wordToComplete*\" }}\n"
        "}\n"
    )


def _completion_words() -> list[str]:
    words = []
    for values in _KEYWORDS.values():
        words.extend(values)
    return sorted(set(words))


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _powershell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# ── 新增 CLI 命令处理 ───────────────────────────────────────────


def _cmd_collect(args) -> int:
    """一键远程采集：创建任务 + 轮询直到完成。"""
    import urllib.request
    import urllib.error

    url = args.url.rstrip("/")
    name = args.name or f"cli-collect-{args.collector}-{args.pid}"

    payload = json.dumps({
        "name": name,
        "agent_id": args.agent,
        "target_pid": args.pid,
        "collector_type": args.collector,
        "sample_rate": args.sample_rate,
        "duration_sec": args.duration,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{url}/api/tasks",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        print(f"error: HTTP {exc.code}: {err}")
        return 1
    except urllib.error.URLError as exc:
        print(f"error: cannot reach {url}: {exc.reason}")
        return 1

    task_id = result["data"]["task_id"]
    status = result["data"]["status"]
    print(json.dumps({"task_id": task_id, "status": status}, ensure_ascii=False))

    if not args.watch:
        return 0

    # 复用 watch 逻辑
    return _watch_until_terminal(url, task_id)


def _cmd_status(args) -> int:
    """查询 Server/Agent 状态。"""
    import urllib.request
    import urllib.error

    url = args.url.rstrip("/")

    try:
        with urllib.request.urlopen(f"{url}/api/healthz", timeout=5) as resp:
            hz = json.loads(resp.read())
        if hz.get("code") != 0:
            print(json.dumps({"error": "server unhealthy", "detail": hz}))
            return 1
    except urllib.error.URLError as exc:
        print(json.dumps({"error": f"cannot reach {url}: {exc.reason}"}))
        return 1

    # 获取 Agent 列表
    if args.agents:
        try:
            with urllib.request.urlopen(f"{url}/api/agents", timeout=5) as resp:
                agents_data = json.loads(resp.read())
            agents = agents_data.get("data", {}).get("items", [])
            print(json.dumps({
                "agents": [{
                    "id": a["id"],
                    "hostname": a["hostname"],
                    "ip_addr": a["ip_addr"],
                    "status": a["status"],
                    "capabilities": a["capabilities"],
                    "metrics": a.get("latest_metrics", {}),
                } for a in agents],
            }, ensure_ascii=False, indent=2))
        except Exception as exc:
            print(json.dumps({"error": f"agents query failed: {exc}"}))
            return 0

    # 获取任务列表
    if args.tasks:
        try:
            with urllib.request.urlopen(f"{url}/api/tasks?limit=50", timeout=5) as resp:
                tasks_data = json.loads(resp.read())
            tasks = tasks_data.get("data", {}).get("items", [])
            active = [t for t in tasks if t["status"] in ("PENDING", "RUNNING", "UPLOADING", "ANALYZING")]
            print(json.dumps({
                "tasks_total": len(tasks),
                "tasks_active": len(active),
                "active": [{"id": t["id"], "name": t["name"], "status": t["status"],
                            "collector": t["collector_type"], "agent": t["agent_id"]} for t in active],
            }, ensure_ascii=False, indent=2))
        except Exception as exc:
            print(json.dumps({"error": f"tasks query failed: {exc}"}))
            return 0

    # 缺省：总体概览
    if not args.agents and not args.tasks:
        try:
            with urllib.request.urlopen(f"{url}/api/agents", timeout=5) as resp:
                agents_data = json.loads(resp.read())
            agents = agents_data.get("data", {}).get("items", [])
            online = sum(1 for a in agents if a["status"] == "ONLINE")

            with urllib.request.urlopen(f"{url}/api/tasks?limit=100", timeout=5) as resp:
                tasks_data = json.loads(resp.read())
            tasks = tasks_data.get("data", {}).get("items", [])
            active = sum(1 for t in tasks if t["status"] in ("PENDING", "RUNNING", "UPLOADING", "ANALYZING"))
            done = sum(1 for t in tasks if t["status"] == "DONE")
            failed = sum(1 for t in tasks if t["status"] == "FAILED")

            print(json.dumps({
                "server": "healthy",
                "agents_online": online,
                "agents_total": len(agents),
                "tasks_total": len(tasks),
                "tasks_active": active,
                "tasks_done": done,
                "tasks_failed": failed,
            }, ensure_ascii=False, indent=2))
        except Exception as exc:
            print(json.dumps({"server": "healthy", "error": str(exc)}))
    return 0


def _cmd_perf_top(args) -> int:
    """本地 perf TopN：直接在本地执行 perf record + analyzer。"""
    import subprocess
    import sys
    import shutil

    perf = shutil.which("perf")
    if not perf:
        print("error: perf not installed")
        return 1

    # 采集
    perf_data = f"/tmp/mini-drop-cli-perf-{args.pid}.data"
    try:
        proc = subprocess.run(
            [perf, "record", "-F", "99", "-g", "-p", str(args.pid),
             "-o", perf_data, "--", "sleep", str(args.duration)],
            capture_output=True, timeout=args.duration + 30,
        )
    except subprocess.TimeoutExpired:
        print("error: perf record timed out")
        return 1

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")[:200]
        print(f"error: perf record failed: {err}")
        return 1

    # 分析
    collapsed = perf_data + ".collapsed"
    try:
        # 折叠栈
        with open(collapsed, "w") as fh:
            result = subprocess.run(
                [perf, "script", "-i", perf_data],
                stdout=fh, stderr=subprocess.PIPE, timeout=60,
            )
        if result.returncode != 0:
            print("error: perf script failed")
            return 1

        # 调用 analyzer 的 TopN 解析
        from analyzer.mini_drop_analyzer.hotmethod_analyzer import _parse_top
        top = _parse_top(Path(collapsed), limit=args.limit)

        print(f"Top {len(top)} 热点函数 (PID={args.pid}, duration={args.duration}s):\n")
        print(f"{'#':>3}  {'%':>7}  {'Count':>8}  Function")
        print("-" * 60)
        for i, f in enumerate(top, 1):
            print(f"{i:>3}  {f['percent']:>6.1f}%  {f['count']:>8}  {f['name']}")
        print("-" * 60)
        print(f"Total samples: {sum(f['count'] for f in top)}")

    finally:
        # 清理临时文件
        for f in [perf_data, collapsed]:
            try:
                Path(f).unlink(missing_ok=True)
            except Exception:
                pass

    return 0


def _cmd_version(args) -> int:
    """打印 Mini-Drop 版本信息。"""
    import sys as _sys
    try:
        import grpc as _grpc
        grpc_ver = _grpc.__version__
    except ImportError:
        grpc_ver = "N/A"
    print(json.dumps({
        "version": "0.2.0",
        "python": _sys.version.split()[0],
        "collectors": _KEYWORDS["collectors"],
        "grpc": grpc_ver,
    }, ensure_ascii=False, indent=2))
    return 0


# ── 新增命令处理函数 ────────────────────────────────────────────────


def _cmd_task_cancel(args) -> int:
    """通过 Server API 取消一个运行中的任务。"""
    import urllib.request
    import urllib.error

    url = args.url.rstrip("/")
    try:
        # 通过 control service 或直接设置状态为 FAILED
        payload = json.dumps({"status": "CANCELLED", "reason": "CLI task-cancel"}).encode("utf-8")
        req = urllib.request.Request(
            f"{url}/api/tasks/{args.task_id}/cancel",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        _print_json({"task_id": args.task_id, "cancelled": True, "response": result})
        return 0
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        _print_json({"task_id": args.task_id, "cancelled": False, "error": f"HTTP {exc.code}: {err_body}"})
        return 1
    except Exception as exc:
        _print_json({"task_id": args.task_id, "cancelled": False, "error": str(exc)})
        return 1


def _cmd_diagnose_remote(args) -> int:
    """通过 Server API 触发远程诊断。"""
    import urllib.request
    import urllib.error

    url = args.url.rstrip("/")
    try:
        req = urllib.request.Request(
            f"{url}/api/tasks/{args.task_id}/diagnose",
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())

        diag_data = result.get("data", result)
        diag_id = diag_data.get("diagnosis_id", "")
        summary = diag_data.get("summary", "")
        ranked = diag_data.get("ranked_causes", [])

        _print_json({
            "task_id": args.task_id,
            "diagnosis_id": diag_id,
            "summary": summary,
            "top_cause": ranked[0] if ranked else None,
            "all_causes": ranked,
        })
        return 0 if diag_data.get("validated", True) else 1
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        _print_json({"error": f"HTTP {exc.code}", "detail": err_body})
        return 1
    except Exception as exc:
        _print_json({"error": str(exc)})
        return 1


def _cmd_report(args) -> int:
    """生成综合性能分析报告（JSON 或 Markdown）。"""
    top = _read_json(args.top_json)

    # 收集证据（可选）
    evidence = None
    if args.evidence:
        try:
            evidence = _read_json(args.evidence)
        except SystemExit:
            pass

    lines = []
    if args.format == "markdown":
        lines.append("# Mini-Drop 性能分析报告")
        lines.append("")
        lines.append(f"**生成时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**热点函数数**: {len(top)}")
        lines.append("")
        lines.append("## 热点 TopN")
        lines.append("")
        lines.append("| Rank | Function | Samples | Percent |")
        lines.append("|-----:|----------|--------:|--------:|")
        for idx, item in enumerate(top[:20], start=1):
            lines.append(
                f"| {idx} | `{item.get('name', '')}` "
                f"| {item.get('samples', 0)} "
                f"| {item.get('percent', 0)}% |"
            )
        lines.append("")

        if evidence and evidence.get("diagnosis"):
            diag = evidence["diagnosis"]
            lines.append("## 归因分析")
            lines.append("")
            lines.append(f"**摘要**: {diag.get('summary', 'N/A')}")
            lines.append("")
            for cause in diag.get("ranked_causes", [])[:5]:
                lines.append(f"- **{cause.get('cause_id')}** (置信度: {cause.get('confidence', 0):.0%})")
                lines.append(f"  {cause.get('claim', '')}")
                lines.append("")

        if evidence and evidence.get("suggestions"):
            lines.append("## 建议")
            lines.append("")
            for s in evidence["suggestions"]:
                lines.append(f"- {s}")
            lines.append("")

        lines.append("---")
        lines.append(f"*报告由 Mini-Drop {args.format} 生成*")

        output = "\n".join(lines)
    else:
        report = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "top_functions": top[:20],
            "evidence": evidence,
        }
        output = json.dumps(report, indent=2, ensure_ascii=False, default=str)

    if args.output == "-":
        print(output)
    else:
        Path(args.output).write_text(output, encoding="utf-8")
        _print_json({"output": args.output, "format": args.format, "functions": len(top)})
    return 0


def _cmd_feedback_stats(args) -> int:
    """查询 RCA 反馈统计。"""
    import urllib.request
    import urllib.error

    url = args.url.rstrip("/")
    try:
        # 获取最近诊断列表并提取反馈信息
        with urllib.request.urlopen(f"{url}/api/tasks?limit=100", timeout=10) as resp:
            tasks_data = json.loads(resp.read())

        tasks = tasks_data.get("data", {}).get("items", [])
        stats = {"correct": 0, "partial": 0, "wrong": 0, "total_checked": 0}

        for task in tasks:
            tid = task["id"]
            try:
                with urllib.request.urlopen(f"{url}/api/tasks/{tid}/diagnoses", timeout=5) as resp:
                    diag_list = json.loads(resp.read())
                for d in (diag_list.get("data", diag_list) or []):
                    fb = d.get("feedback")
                    if fb and fb.get("feedback_label"):
                        label = fb["feedback_label"]
                        stats[label] = stats.get(label, 0) + 1
                        stats["total_checked"] += 1
            except Exception:
                continue

        total = stats["total_checked"]
        accuracy = round(stats["correct"] / total * 100, 1) if total > 0 else 0

        _print_json({
            "total_diagnoses_with_feedback": total,
            "correct": stats["correct"],
            "partial": stats["partial"],
            "wrong": stats["wrong"],
            "accuracy_pct": accuracy,
            "message": f"AI 归因准确率: {accuracy}% ({stats['correct']}/{total})"
            if total > 0 else "暂无反馈数据",
        })
        return 0
    except Exception as exc:
        _print_json({"error": str(exc)})
        return 1


def _cmd_storage_ls(args) -> int:
    """列出对象存储中的任务产物。"""
    import urllib.request
    import urllib.error

    url = args.url.rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/api/tasks?limit=500", timeout=10) as resp:
            tasks_data = json.loads(resp.read())

        tasks = tasks_data.get("data", {}).get("items", [])
        result = []
        for task in tasks:
            tid = task["id"]
            if args.task_id and args.task_id not in tid:
                continue
            try:
                with urllib.request.urlopen(f"{url}/api/tasks/{tid}/artifacts", timeout=5) as resp:
                    artifacts = json.loads(resp.read())
                artifact_list = artifacts.get("data", artifacts) or []
                for a in artifact_list:
                    result.append({
                        "task_id": tid,
                        "artifact_type": a.get("artifact_type", "unknown"),
                        "filename": a.get("filename", ""),
                        "size_bytes": a.get("size_bytes", 0),
                        "object_key": a.get("object_key", ""),
                    })
            except Exception:
                continue

        total_size = sum(r["size_bytes"] for r in result)
        _print_json({
            "total_artifacts": len(result),
            "total_size_mb": round(total_size / 1024 / 1024, 2),
            "items": result,
        })
        return 0
    except Exception as exc:
        _print_json({"error": str(exc)})
        return 1


def _cmd_storage_prune(args) -> int:
    """清理过期任务产物（干运行默认）。"""
    import urllib.request
    import urllib.error
    from datetime import datetime, timedelta, timezone as _tz

    url = args.url.rstrip("/")
    cutoff = datetime.now(_tz.utc) - timedelta(days=args.older_than_days)

    try:
        with urllib.request.urlopen(f"{url}/api/tasks?limit=500", timeout=10) as resp:
            tasks_data = json.loads(resp.read())

        tasks = tasks_data.get("data", {}).get("items", [])
        to_prune = []

        for task in tasks:
            tid = task["id"]
            if args.task_id and args.task_id not in tid:
                continue

            # 检查任务是否已终止且过期
            status = task.get("status", "")
            if status not in ("DONE", "FAILED", "CANCELLED"):
                continue

            created = task.get("created_at", "")
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue

            if created_dt < cutoff:
                try:
                    with urllib.request.urlopen(f"{url}/api/tasks/{tid}/artifacts", timeout=5) as resp:
                        artifacts = json.loads(resp.read())
                    artifact_list = artifacts.get("data", artifacts) or []
                    size = sum(a.get("size_bytes", 0) for a in artifact_list)
                    to_prune.append({"task_id": tid, "status": status, "created_at": created, "artifacts": len(artifact_list), "size_bytes": size})
                except Exception:
                    continue

        total_size = sum(r["size_bytes"] for r in to_prune)

        if args.execute:
            # 实际删除
            deleted = 0
            for item in to_prune:
                try:
                    req = urllib.request.Request(
                        f"{url}/api/tasks/{item['task_id']}",
                        method="DELETE",
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        pass
                    deleted += 1
                except Exception as exc:
                    _print_json({"warning": f"Failed to delete {item['task_id']}: {exc}"})

            _print_json({
                "mode": "execute",
                "older_than_days": args.older_than_days,
                "deleted": deleted,
                "total_artifacts": sum(r["artifacts"] for r in to_prune),
                "freed_mb": round(total_size / 1024 / 1024, 2),
                "items": [{"task_id": r["task_id"], "artifacts": r["artifacts"]} for r in to_prune],
            })
        else:
            _print_json({
                "mode": "dry-run",
                "older_than_days": args.older_than_days,
                "would_delete": len(to_prune),
                "total_artifacts": sum(r["artifacts"] for r in to_prune),
                "would_free_mb": round(total_size / 1024 / 1024, 2),
                "items": [{"task_id": r["task_id"], "created_at": r["created_at"], "artifacts": r["artifacts"]} for r in to_prune],
                "hint": "添加 --execute 执行实际删除",
            })
        return 0
    except Exception as exc:
        _print_json({"error": str(exc)})
        return 1


def _cmd_agent_exec(args) -> int:
    """通过诊断 ID 执行修复计划中的某个操作。"""
    import urllib.request
    import urllib.error

    url = args.url.rstrip("/")
    try:
        # 获取诊断详情
        with urllib.request.urlopen(f"{url}/api/diagnoses/{args.diagnosis_id}", timeout=10) as resp:
            diag = json.loads(resp.read())

        diag_data = diag.get("data", diag)
        repair_plan = diag_data.get("repair_plan")

        if not repair_plan:
            _print_json({"error": "该诊断没有修复计划"})
            return 1

        actions = repair_plan.get("actions", [])
        if args.action_index >= len(actions):
            _print_json({"error": f"action-index {args.action_index} 超出范围 (共 {len(actions)} 个 action)"})
            return 1

        action = actions[args.action_index]
        _print_json({
            "diagnosis_id": args.diagnosis_id,
            "action_index": args.action_index,
            "action_type": action.get("action_type"),
            "risk_level": action.get("risk_level"),
            "description": action.get("description"),
            "status": action.get("status", "unknown"),
            "result": action.get("result"),
            "payload": action.get("payload"),
            "command": action.get("command"),
            "hint": (
                "safe_auto actions are auto-executed by the Server. "
                "confirm_required / manual_only actions require manual review."
            ),
        })
        return 0
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        _print_json({"error": f"HTTP {exc.code}: {err_body}"})
        return 1
    except Exception as exc:
        _print_json({"error": str(exc)})
        return 1


def _cmd_install_check(args) -> int:
    """检查系统依赖和权限。"""
    import shutil
    import sys as _sys
    import os as _os

    checks = {}
    all_ok = True

    # Python 版本
    py_ver = _sys.version_info
    checks["python"] = {
        "ok": py_ver >= (3, 9),
        "version": f"{py_ver.major}.{py_ver.minor}.{py_ver.micro}",
        "required": ">= 3.9",
    }
    if not checks["python"]["ok"]:
        all_ok = False

    # perf
    perf = shutil.which("perf")
    checks["perf"] = {"ok": perf is not None, "path": perf or "not found"}
    if not checks["perf"]["ok"]:
        all_ok = False

    # bpftrace (optional)
    bpftrace = shutil.which("bpftrace")
    checks["bpftrace"] = {"ok": bpftrace is not None, "path": bpftrace or "not found (optional)"}

    # /proc filesystem
    proc_ok = _os.path.isdir("/proc/1")
    checks["procfs"] = {"ok": proc_ok, "path": "/proc"}
    if not proc_ok:
        all_ok = False

    # perf_event_paranoid
    try:
        with open("/proc/sys/kernel/perf_event_paranoid", "r") as fh:
            paranoid = int(fh.read().strip())
        checks["perf_event_paranoid"] = {
            "ok": paranoid <= 2,
            "value": paranoid,
            "hint": "<= 1 建议 (当前值阻止了部分符号采集)" if paranoid > 1 else "ok",
        }
        if paranoid > 2:
            all_ok = False
    except (FileNotFoundError, PermissionError, ValueError):
        checks["perf_event_paranoid"] = {"ok": False, "value": "unknown", "hint": "无法读取 /proc/sys/kernel/perf_event_paranoid"}

    # Analyzer 依赖
    if args.full:
        perl = shutil.which("perl")
        checks["perl"] = {"ok": perl is not None, "path": perl or "not found (needed for flamegraph.pl)"}

        py_spy = shutil.which("py-spy")
        checks["py-spy"] = {"ok": py_spy is not None, "path": py_spy or "not found (optional)"}

        # Python 包
        for pkg in ("fastapi", "grpc", "sqlalchemy", "minio", "requests"):
            try:
                __import__(pkg)
                checks[f"pkg:{pkg}"] = {"ok": True}
            except ImportError:
                checks[f"pkg:{pkg}"] = {"ok": False, "hint": f"pip install {pkg}"}
                all_ok = False

    _print_json({
        "all_ok": all_ok,
        "platform": _sys.platform,
        "checks": checks,
        "recommendation": "所有核心依赖满足" if all_ok else "请根据上述检查结果安装缺失依赖或调整权限",
    })
    return 0 if all_ok else 1


def _watch_until_terminal(url: str, task_id: str, interval: float = 2.0, timeout: float = 120.0) -> int:
    """轮询任务状态直到终止。"""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(interval)
        try:
            with urllib.request.urlopen(f"{url}/api/tasks/{task_id}", timeout=5) as resp:
                data = json.loads(resp.read())
            task = data.get("data", {})
            status = task.get("status", "UNKNOWN")
            print(json.dumps({"task_id": task_id, "status": status}, ensure_ascii=False))
            if status in ("DONE", "FAILED", "CANCELLED"):
                return 0 if status == "DONE" else 1
        except Exception as exc:
            print(json.dumps({"task_id": task_id, "error": str(exc)}))
    return 1  # timeout


_COMMANDS = {
    "serve": _cmd_serve,
    "agent": _cmd_agent,
    "ai-config": _cmd_ai_config,
    "parse": _cmd_parse,
    "summarize": _cmd_summarize,
    "diagnose-local": _cmd_diagnose_local,
    "diff-top": _cmd_diff_top,
    "ci-check": _cmd_ci_check,
    "alert": _cmd_alert,
    "batch-diagnose": _cmd_batch_diagnose,
    "export-summary": _cmd_export_summary,
    "watch-task": _cmd_watch_task,
    "keywords": _cmd_keywords,
    "suggest": _cmd_suggest,
    "completion": _cmd_completion,
    "collect": _cmd_collect,
    "status": _cmd_status,
    "perf-top": _cmd_perf_top,
    "version": _cmd_version,
    "task-cancel": _cmd_task_cancel,
    "diagnose-remote": _cmd_diagnose_remote,
    "report": _cmd_report,
    "feedback-stats": _cmd_feedback_stats,
    "storage-ls": _cmd_storage_ls,
    "storage-prune": _cmd_storage_prune,
    "agent-exec": _cmd_agent_exec,
    "install-check": _cmd_install_check,
}


_KEYWORDS = {
    "commands": sorted(_COMMANDS.keys()),
    "collectors": ["perf_cpu", "ebpf_io", "pyspy", "continuous_perf", "java_async", "go_pprof", "memory_smaps", "sys_metrics"],
    "causes": [
        "cpu_hotspot_recursive",
        "io_wait_high",
        "python_userland_hotspot",
        "agent_overhead",
        "collector_permission_denied",
        "target_pid_invalid",
        "insufficient_data",
        "fd_leak",
        "thread_leak",
        "memory_growth",
        "network_high",
        "sys_cpu_high",
    ],
    "fields": [
        "top_functions",
        "top_functions[0].name",
        "top_functions[0].percent",
        "ebpf_metrics.io_latency_us",
        "baseline_diff",
        "agent_stats.max_cpu_percent",
        "sys_metrics.summary.fd_trend",
        "sys_metrics.summary.thread_trend",
        "task_metadata.status",
        "failure_events",
        "tool_results.get_flamegraph_top",
        "repair_plan.actions",
    ],
    "report_formats": ["json", "markdown"],
}


if __name__ == "__main__":
    raise SystemExit(main())
