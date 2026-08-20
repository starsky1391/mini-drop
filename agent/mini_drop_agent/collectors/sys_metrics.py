"""System & Process 多维指标采集器。

一次采集周期内从 /proc 读取以下维度的时间序列数据：

  /proc/stat          → 系统级 CPU 利用率（user/sys/iowait 占比）
  /proc/loadavg       → 系统负载（1m/5m/15m）
  /proc/<pid>/stat    → 进程状态、CPU utime/stime + 线程数
  /proc/<pid>/status  → 状态名称、线程数、自愿/非自愿上下文切换
  /proc/<pid>/fd      → 文件描述符计数（遍历 fd 目录）
  /proc/<pid>/io      → 进程磁盘 I/O（rchar/wchar/read_bytes/write_bytes）
  /proc/net/dev       → 系统网络吞吐（rx/tx bytes）

采集模式：
  - snapshot: 采集一次快照（duration_sec=1）
  - timeseries: 周期性采样，每 1 秒一次，产时间序列 JSON

产物：
  sys_metrics.json  — 包含所有维度的结构化时间序列数据
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask


class SysMetricsCollector:
    """多维系统指标采集器。"""

    OUTPUT_BASE = "/tmp/mini-drop"
    SAMPLE_INTERVAL_SEC = 1.0
    MAX_SAMPLES = 120  # 最多 2 分钟

    # ── 公共接口 ────────────────────────────────────────────

    def collect(self, task: CollectorTask) -> CollectorResult:
        """采集多维系统指标。

        若 task.options.mode == "snapshot" 则仅采集一次，
        否则按 duration_sec 周期采样（每 1s 一次）。
        """
        if not self._pid_exists(task.target_pid):
            return CollectorResult(
                ok=False,
                reason=f"目标 PID {task.target_pid} 不存在",
            )

        mode = task.options.get("mode", "timeseries")
        if mode not in ("snapshot", "timeseries"):
            return CollectorResult(ok=False, reason=f"无效的 mode: {mode}，支持 snapshot 或 timeseries")
        duration_sec = max(1, min(task.duration_sec, self.MAX_SAMPLES))

        output_dir = os.path.join(self.OUTPUT_BASE, task.id)
        os.makedirs(output_dir, exist_ok=True)

        samples: list[dict] = []
        prev_cpu = self._read_proc_stat_total()
        prev_process = self._read_process_metrics(task.target_pid)
        prev_workload = self._read_workload_metrics(task.target_pid, prev_process)
        prev_cgroup_cpu = self._read_cgroup_cpu_stat(prev_workload.get("cgroup_path"))
        prev_sample_ts = time.time()

        deadline = time.time() + duration_sec
        while time.time() < deadline:
            ts = time.time()
            if not self._pid_exists(task.target_pid):
                break

            curr_cpu = self._read_proc_stat_total()
            cpu_delta = {
                k: curr_cpu.get(k, 0) - prev_cpu.get(k, 0)
                for k in ("user", "system", "idle", "iowait")
                if k in curr_cpu and k in prev_cpu
            }
            total_delta = sum(cpu_delta.values())
            cpu_pct = {}
            if total_delta > 0:
                cpu_pct = {k: round(v / total_delta * 100, 1) for k, v in cpu_delta.items()}
            prev_cpu = curr_cpu

            process_metrics = self._read_process_metrics(task.target_pid)
            elapsed = max(ts - prev_sample_ts, 1e-6)
            clock_ticks = self._clock_ticks_per_second()
            cpu_interval_valid = elapsed >= min(self.SAMPLE_INTERVAL_SEC * 0.5, 0.5)
            process_metrics["cpu_user_pct"] = round(
                max(0, process_metrics.get("utime_ticks", 0) - prev_process.get("utime_ticks", 0))
                / clock_ticks / elapsed * 100,
                1,
            ) if cpu_interval_valid else 0.0
            process_metrics["cpu_sys_pct"] = round(
                max(0, process_metrics.get("stime_ticks", 0) - prev_process.get("stime_ticks", 0))
                / clock_ticks / elapsed * 100,
                1,
            ) if cpu_interval_valid else 0.0
            process_metrics["cpu_interval_valid"] = cpu_interval_valid
            workload_metrics = self._read_workload_metrics(task.target_pid, process_metrics)
            self._apply_workload_cpu_percent(
                workload_metrics,
                prev_workload,
                elapsed,
                clock_ticks,
                interval_valid=cpu_interval_valid,
            )
            cgroup_cpu = self._read_cgroup_cpu_stat(workload_metrics.get("cgroup_path"))
            workload_metrics["cgroup_cpu"] = self._cgroup_cpu_delta(cgroup_cpu, prev_cgroup_cpu, elapsed)
            prev_process = process_metrics
            prev_workload = workload_metrics
            prev_cgroup_cpu = cgroup_cpu
            prev_sample_ts = ts

            sample: dict = {
                "ts": ts,
                "cpu": cpu_pct,
                "load": self._read_loadavg(),
                "process": process_metrics,
                "workload": workload_metrics,
                "network": self._read_network_dev(),
            }

            samples.append(sample)
            if mode == "snapshot" or len(samples) >= self.MAX_SAMPLES:
                break

            remaining = deadline - time.time()
            if remaining > 0:
                time.sleep(min(self.SAMPLE_INTERVAL_SEC, remaining))

        if not samples:
            return CollectorResult(
                ok=False,
                reason="未能采集到系统指标样本",
            )

        # ── 汇总分析 ─────────────────────────────────────
        summary = self._compute_summary(samples)

        evidence_validity = {
            "execution_status": "completed",
            "artifact_status": "produced",
            "evidence_status": "valid",
            "reason": "workload-scoped system metrics collected",
        }
        output = {
            "task_id": task.id,
            "pid": task.target_pid,
            "mode": mode,
            "duration_sec": duration_sec,
            "sample_count": len(samples),
            "summary": summary,
            "samples": samples,
            "evidence_validity": evidence_validity,
        }

        output_path = os.path.join(output_dir, "sys_metrics.json")
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(output, fh, indent=2, ensure_ascii=False, default=str)

        return CollectorResult(
            ok=True,
            reason=(
                f"系统指标采集完成: {len(samples)} 个样本 | "
                f"CPU sys={summary.get('avg_cpu_sys_pct', 0):.1f}% | "
                f"线程={summary.get('thread_count', 0)} | "
                f"FD={summary.get('fd_count', 0)} | "
                f"网络 rx={summary.get('net_rx_kbps', 0):.1f}KB/s tx={summary.get('net_tx_kbps', 0):.1f}KB/s"
            ),
            artifacts=[{
                "artifact_type": "sys_metrics",
                "filename": "sys_metrics.json",
                "local_path": output_path,
                "content_type": "application/json",
                "size_bytes": os.path.getsize(output_path),
                "metadata": {**summary, "evidence_validity": evidence_validity},
            }],
        )

    # ── /proc 读取 ─────────────────────────────────────────

    @staticmethod
    def _read_proc_stat_total() -> dict[str, int]:
        """读取 /proc/stat 第一行（系统总计 CPU 时间）。"""
        try:
            with open("/proc/stat", "r") as fh:
                for line in fh:
                    if line.startswith("cpu "):
                        parts = line.split()
                        return {
                            "user": int(parts[1]),
                            "nice": int(parts[2]),
                            "system": int(parts[3]),
                            "idle": int(parts[4]),
                            "iowait": int(parts[5]) if len(parts) > 5 else 0,
                            "irq": int(parts[6]) if len(parts) > 6 else 0,
                            "softirq": int(parts[7]) if len(parts) > 7 else 0,
                        }
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            pass
        return {}

    @staticmethod
    def _read_loadavg() -> dict[str, float]:
        """读取系统负载平均值。"""
        try:
            with open("/proc/loadavg", "r") as fh:
                parts = fh.readline().split()
                return {
                    "load1m": round(float(parts[0]), 2),
                    "load5m": round(float(parts[1]), 2),
                    "load15m": round(float(parts[2]), 2),
                }
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            pass
        return {}

    @staticmethod
    def _read_process_metrics(pid: int) -> dict:
        """读取进程级指标：CPU ticks、线程数、FD 数、IO 字节、上下文切换。"""
        result: dict = {}

        # stat: utime, stime, num_threads, vsize, rss
        try:
            with open(f"/proc/{pid}/stat", "r") as fh:
                fields = fh.read().split()
                if len(fields) >= 22:
                    # fields: ...  [13]utime  [14]stime  [19]num_threads  [22]vsize  [23]rss
                    result["process_state"] = fields[2]
                    result["utime_ticks"] = int(fields[13])
                    result["stime_ticks"] = int(fields[14])
                    result["num_threads"] = int(fields[19])
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            pass

        # fd count
        try:
            fd_dir = f"/proc/{pid}/fd"
            result["fd_count"] = len(os.listdir(fd_dir))
        except (FileNotFoundError, PermissionError, OSError):
            pass

        # io: read_bytes, write_bytes
        try:
            with open(f"/proc/{pid}/io", "r") as fh:
                for line in fh:
                    if line.startswith("read_bytes:"):
                        result["disk_read_bytes"] = int(line.split(":")[1].strip())
                    elif line.startswith("write_bytes:"):
                        result["disk_write_bytes"] = int(line.split(":")[1].strip())
        except (FileNotFoundError, PermissionError, ValueError):
            pass

        # status: voluntary & nonvoluntary ctxt switches
        try:
            with open(f"/proc/{pid}/status", "r") as fh:
                for line in fh:
                    if line.startswith("State:"):
                        state_name = line.split(":", 1)[1].strip()
                        result["process_state_name"] = state_name
                        result["process_state"] = state_name[:1]
                    elif line.startswith("voluntary_ctxt_switches:"):
                        result["voluntary_switches"] = int(line.split(":")[1].strip())
                    elif line.startswith("nonvoluntary_ctxt_switches:"):
                        result["nonvoluntary_switches"] = int(line.split(":")[1].strip())
                    elif line.startswith("VmRSS:"):
                        result["vmrss_kb"] = int(line.split()[1])
        except (FileNotFoundError, PermissionError, ValueError):
            pass

        return result

    @classmethod
    def _read_workload_metrics(cls, target_pid: int, main_metrics: dict | None = None) -> dict:
        cgroup_path = cls._read_cgroup_path(target_pid)
        member_pids = cls._read_cgroup_pids(cgroup_path)
        scope_source = "cgroup" if member_pids else "process_tree"
        if not member_pids:
            member_pids = cls._read_process_tree_pids(target_pid)
        if target_pid not in member_pids:
            member_pids.insert(0, target_pid)

        members: list[dict] = []
        for pid in sorted(set(member_pids)):
            metrics = dict(main_metrics) if pid == target_pid and main_metrics is not None else cls._read_process_metrics(pid)
            if not metrics and not cls._pid_exists(pid):
                continue
            metrics["pid"] = pid
            members.append(metrics)
        return {
            "scope_source": scope_source,
            "cgroup_path": cgroup_path or "",
            "member_count": len(members),
            "member_pids": [item["pid"] for item in members],
            "members": members,
            **cls._aggregate_workload_members(members),
        }

    @staticmethod
    def _aggregate_workload_members(members: list[dict]) -> dict:
        summed_keys = (
            "utime_ticks", "stime_ticks", "num_threads", "fd_count", "vmrss_kb",
            "disk_read_bytes", "disk_write_bytes", "voluntary_switches", "nonvoluntary_switches",
        )
        return {
            key: sum(int(item.get(key, 0) or 0) for item in members)
            for key in summed_keys
        }

    @classmethod
    def _read_cgroup_path(cls, pid: int) -> str:
        try:
            lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        unified = next((line.split(":", 2)[2] for line in lines if line.startswith("0::")), "")
        return unified or (lines[0].split(":", 2)[-1] if lines else "")

    @staticmethod
    def _read_cgroup_pids(cgroup_path: str) -> list[int]:
        if not cgroup_path:
            return []
        try:
            values = (Path("/sys/fs/cgroup") / cgroup_path.lstrip("/") / "cgroup.procs").read_text(encoding="utf-8").split()
            return [int(value) for value in values if value.isdigit() and int(value) > 0]
        except OSError:
            return []

    @classmethod
    def _read_process_tree_pids(cls, root_pid: int) -> list[int]:
        found: list[int] = []
        pending = [root_pid]
        seen: set[int] = set()
        while pending and len(seen) < 4096:
            pid = pending.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            if cls._pid_exists(pid):
                found.append(pid)
            try:
                children = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8").split()
            except OSError:
                children = []
            pending.extend(int(value) for value in children if value.isdigit())
        return found

    @staticmethod
    def _apply_workload_cpu_percent(
        current: dict,
        previous: dict,
        elapsed: float,
        clock_ticks: int,
        *,
        interval_valid: bool = True,
    ) -> None:
        previous_by_pid = {
            int(item.get("pid")): item
            for item in previous.get("members", [])
            if item.get("pid") is not None
        }
        total_user = 0.0
        total_system = 0.0
        for member in current.get("members", []):
            old = previous_by_pid.get(int(member.get("pid", 0)))
            if old is None or not interval_valid:
                user_pct = 0.0
                system_pct = 0.0
            else:
                user_pct = max(0, int(member.get("utime_ticks", 0)) - int(old.get("utime_ticks", 0))) / clock_ticks / elapsed * 100
                system_pct = max(0, int(member.get("stime_ticks", 0)) - int(old.get("stime_ticks", 0))) / clock_ticks / elapsed * 100
            member["cpu_user_pct"] = round(user_pct, 1)
            member["cpu_sys_pct"] = round(system_pct, 1)
            member["cpu_total_pct"] = round(user_pct + system_pct, 1)
            total_user += user_pct
            total_system += system_pct
        current["cpu_user_pct"] = round(total_user, 1)
        current["cpu_sys_pct"] = round(total_system, 1)
        current["cpu_total_pct"] = round(total_user + total_system, 1)
        current["cpu_interval_valid"] = interval_valid
        current["top_cpu_members"] = sorted(
            (
                {
                    "pid": item.get("pid"),
                    "cpu_total_pct": item.get("cpu_total_pct", 0.0),
                    "process_state": item.get("process_state", ""),
                }
                for item in current.get("members", [])
            ),
            key=lambda item: float(item["cpu_total_pct"] or 0.0),
            reverse=True,
        )[:10]

    @staticmethod
    def _read_cgroup_cpu_stat(cgroup_path: str | None) -> dict[str, int]:
        if not cgroup_path:
            return {}
        try:
            lines = (Path("/sys/fs/cgroup") / str(cgroup_path).lstrip("/") / "cpu.stat").read_text(encoding="utf-8").splitlines()
        except OSError:
            return {}
        result: dict[str, int] = {}
        for line in lines:
            parts = line.split()
            if len(parts) == 2:
                try:
                    result[parts[0]] = int(parts[1])
                except ValueError:
                    continue
        return result

    @staticmethod
    def _cgroup_cpu_delta(current: dict[str, int], previous: dict[str, int], elapsed: float) -> dict[str, float | int]:
        throttled_usec = max(0, current.get("throttled_usec", 0) - previous.get("throttled_usec", 0))
        usage_usec = max(0, current.get("usage_usec", 0) - previous.get("usage_usec", 0))
        return {
            "usage_usec_delta": usage_usec,
            "throttled_usec_delta": throttled_usec,
            "nr_throttled_delta": max(0, current.get("nr_throttled", 0) - previous.get("nr_throttled", 0)),
            "throttled_time_pct": round(throttled_usec / max(elapsed * 1_000_000, 1) * 100, 2),
        }

    @staticmethod
    def _read_network_dev() -> dict[str, int]:
        """读取系统网络累计字节数（所有接口合计）。"""
        rx_total = 0
        tx_total = 0
        try:
            with open("/proc/net/dev", "r") as fh:
                for line in fh:
                    if ":" not in line:
                        continue
                    parts = line.split(":")[1].strip().split()
                    if len(parts) >= 10:
                        rx_total += int(parts[0])  # bytes received
                        tx_total += int(parts[8])  # bytes transmitted
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            pass
        return {"rx_bytes": rx_total, "tx_bytes": tx_total}

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        return os.path.isdir(f"/proc/{pid}")

    @staticmethod
    def _clock_ticks_per_second() -> int:
        try:
            return max(int(os.sysconf("SC_CLK_TCK")), 1)
        except (AttributeError, OSError, ValueError):
            return 100

    # ── 摘要计算 ───────────────────────────────────────────

    @staticmethod
    def _compute_summary(samples: list[dict]) -> dict:
        """从时间序列样本中提取关键汇总指标。"""
        proc_samples = [s.get("process", {}) for s in samples]
        workload_samples = [s.get("workload", {}) for s in samples]
        cpu_samples = [s.get("cpu", {}) for s in samples]
        net_samples = [s.get("network", {}) for s in samples]

        has_workload_metrics = any(
            item.get("member_count")
            or item.get("members")
            or "cpu_interval_valid" in item
            for item in workload_samples
        )
        metric_samples = workload_samples if has_workload_metrics else proc_samples
        thread_counts = [p.get("num_threads", 0) for p in metric_samples if "num_threads" in p]
        fd_counts = [p.get("fd_count", 0) for p in metric_samples if "fd_count" in p]
        vmrss_vals = [p.get("vmrss_kb", 0) / 1024 for p in metric_samples if "vmrss_kb" in p]
        ctx_vol = [p.get("voluntary_switches", 0) for p in metric_samples if "voluntary_switches" in p]
        ctx_nvol = [p.get("nonvoluntary_switches", 0) for p in metric_samples if "nonvoluntary_switches" in p]
        process_states = [str(p.get("process_state")) for p in proc_samples if p.get("process_state")]
        process_state_names = [str(p.get("process_state_name")) for p in proc_samples if p.get("process_state_name")]
        stopped_samples = sum(1 for state in process_states if state in {"T", "t"})

        valid_cpu_samples = [p for p in metric_samples if p.get("cpu_interval_valid", True)]
        process_user_pcts = [p.get("cpu_user_pct", 0) for p in valid_cpu_samples]
        process_sys_pcts = [p.get("cpu_sys_pct", 0) for p in valid_cpu_samples]
        host_sys_pcts = [c.get("system", 0) for c in cpu_samples]
        iowait_pcts = [c.get("iowait", 0) for c in cpu_samples]
        host_user_pcts = [c.get("user", 0) for c in cpu_samples]
        host_busy_pcts = [
            sum(float(c.get(key, 0) or 0) for key in ("user", "system", "iowait"))
            for c in cpu_samples
        ]
        throttled_pcts = [
            float((item.get("cgroup_cpu") or {}).get("throttled_time_pct", 0) or 0)
            for item in workload_samples
        ]
        throttled_counts = [
            int((item.get("cgroup_cpu") or {}).get("nr_throttled_delta", 0) or 0)
            for item in workload_samples
        ]

        # 网络速率 (KB/s) — 使用首尾差值
        net_rx_kbps = 0.0
        net_tx_kbps = 0.0
        if len(net_samples) >= 2:
            first = net_samples[0]
            last = net_samples[-1]
            dt = samples[-1]["ts"] - samples[0]["ts"]
            if dt > 0:
                net_rx_kbps = (last.get("rx_bytes", 0) - first.get("rx_bytes", 0)) / dt / 1024
                net_tx_kbps = (last.get("tx_bytes", 0) - first.get("tx_bytes", 0)) / dt / 1024

        # 线程趋势
        thread_trend = "stable"
        if len(thread_counts) >= 2:
            if thread_counts[-1] > thread_counts[0] * 1.05:
                thread_trend = "increasing"
            elif thread_counts[-1] < thread_counts[0] * 0.95:
                thread_trend = "decreasing"

        # FD 趋势
        fd_trend = "stable"
        if len(fd_counts) >= 2:
            if fd_counts[-1] > fd_counts[0] * 1.10:
                fd_trend = "increasing"  # 可能泄漏
            elif fd_counts[-1] < fd_counts[0] * 0.90:
                fd_trend = "decreasing"

        # 上下文切换速率
        ctx_vol_rate = 0.0
        ctx_nvol_rate = 0.0
        if len(ctx_vol) >= 2 and len(samples) >= 2:
            dt = samples[-1]["ts"] - samples[0]["ts"]
            if dt > 0:
                ctx_vol_rate = (ctx_vol[-1] - ctx_vol[0]) / dt
                ctx_nvol_rate = (ctx_nvol[-1] - ctx_nvol[0]) / dt

        latest_workload = workload_samples[-1] if workload_samples else {}
        avg_host_busy = round(sum(host_busy_pcts) / len(host_busy_pcts), 1) if host_busy_pcts else 0
        avg_throttled = round(sum(throttled_pcts) / len(throttled_pcts), 2) if throttled_pcts else 0
        return {
            "avg_cpu_user_pct": round(sum(process_user_pcts) / len(process_user_pcts), 1) if process_user_pcts else 0,
            "avg_cpu_sys_pct": round(sum(process_sys_pcts) / len(process_sys_pcts), 1) if process_sys_pcts else 0,
            "process_cpu_sample_count": len(process_user_pcts),
            "avg_host_cpu_user_pct": round(sum(host_user_pcts) / len(host_user_pcts), 1) if host_user_pcts else 0,
            "avg_host_cpu_sys_pct": round(sum(host_sys_pcts) / len(host_sys_pcts), 1) if host_sys_pcts else 0,
            "avg_host_cpu_busy_pct": avg_host_busy,
            "host_cpu_saturated": avg_host_busy >= 85.0,
            "avg_cpu_iowait_pct": round(sum(iowait_pcts) / len(iowait_pcts), 1) if iowait_pcts else 0,
            "workload_scope_source": latest_workload.get("scope_source", "main_pid"),
            "workload_cgroup_path": latest_workload.get("cgroup_path", ""),
            "workload_member_count": latest_workload.get("member_count", 1 if proc_samples else 0),
            "workload_member_pids": latest_workload.get("member_pids", []),
            "top_cpu_members": latest_workload.get("top_cpu_members", []),
            "avg_cgroup_throttled_pct": avg_throttled,
            "cgroup_nr_throttled_delta": sum(throttled_counts),
            "target_scheduling_pressure": avg_throttled > 0 or sum(throttled_counts) > 0,
            "thread_count": thread_counts[-1] if thread_counts else 0,
            "thread_trend": thread_trend,
            "fd_count": fd_counts[-1] if fd_counts else 0,
            "fd_trend": fd_trend,
            "fd_max": max(fd_counts) if fd_counts else 0,
            "vmrss_mb": round(vmrss_vals[-1], 1) if vmrss_vals else 0,
            "vmrss_mb_max": round(max(vmrss_vals), 1) if vmrss_vals else 0,
            "ctx_voluntary_rate": round(ctx_vol_rate, 1),
            "ctx_nonvoluntary_rate": round(ctx_nvol_rate, 1),
            "process_state": process_states[-1] if process_states else "",
            "process_state_name": process_state_names[-1] if process_state_names else "",
            "process_state_counts": {
                state: process_states.count(state)
                for state in sorted(set(process_states))
            },
            "stopped_sample_count": stopped_samples,
            "stopped_sample_ratio": round(stopped_samples / len(process_states), 3) if process_states else 0.0,
            "net_rx_kbps": round(net_rx_kbps, 1),
            "net_tx_kbps": round(net_tx_kbps, 1),
            "load1m": samples[0].get("load", {}).get("load1m", 0) if samples else 0,
        }
