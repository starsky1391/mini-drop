#!/usr/bin/env python3
"""Attach Memray to a container process from a host-pid Agent namespace."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import uuid
from pathlib import Path


def _container_pid(host_pid: int) -> int:
    status_path = Path(f"/proc/{host_pid}/status")
    for line in status_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("NSpid:"):
            values = [int(value) for value in line.split()[1:]]
            if values:
                return values[-1]
    return host_pid


def _target_python_runtime(nsenter: str, host_pid: int) -> dict[str, str]:
    target_exe = os.path.realpath(f"/proc/{host_pid}/exe")
    if not target_exe.startswith("/"):
        raise RuntimeError("target_python_executable_unavailable")
    probe = (
        "import json,sys,sysconfig; "
        "print(json.dumps({"
        "'version':sys.version_info[:2],"
        "'purelib':sysconfig.get_path('purelib'),"
        "'executable':sys.executable"
        "}))"
    )
    result = subprocess.run(
        [
            nsenter,
            "-t",
            str(host_pid),
            "-m",
            "-p",
            "--",
            target_exe,
            "-c",
            probe,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "target_python_runtime_unavailable:"
            + (result.stderr or result.stdout)[-500:]
        )
    try:
        value = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise RuntimeError("target_python_runtime_unparseable") from exc
    version = value.get("version")
    purelib = str(value.get("purelib") or "")
    executable = str(value.get("executable") or target_exe)
    if not isinstance(version, list) or len(version) != 2 or not purelib.startswith("/"):
        raise RuntimeError("target_python_runtime_incomplete")
    return {
        "major": str(version[0]),
        "minor": str(version[1]),
        "purelib": purelib,
        "executable": executable,
    }


def _requirement_name(requirement: str) -> str:
    return re.split(r"[<>=!~;[\s]", requirement, maxsplit=1)[0].strip()


def _memray_distributions() -> list[str]:
    names: list[str] = []
    pending = ["memray"]
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        key = name.lower().replace("-", "_")
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
        try:
            requirements = importlib.metadata.requires(name) or []
        except importlib.metadata.PackageNotFoundError:
            continue
        for requirement in requirements:
            if ";" in requirement:
                expression = requirement.split(";", 1)[1].strip()
                if "extra" in expression:
                    continue
            dependency = _requirement_name(requirement)
            if dependency:
                pending.append(dependency)
    return names


def _stage_memray_runtime(host_pid: int, purelib: str) -> list[Path]:
    agent_purelib = Path(sysconfig.get_path("purelib"))
    target_root = Path(f"/proc/{host_pid}/root")
    target_purelib = target_root / purelib.lstrip("/")
    staged: list[Path] = []
    for distribution_name in _memray_distributions():
        try:
            distribution = importlib.metadata.distribution(distribution_name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError(
                f"agent_memray_dependency_missing:{distribution_name}"
            ) from exc
        for file in distribution.files or []:
            relative = Path(str(file))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            source = agent_purelib / relative
            destination = target_purelib / relative
            if not source.is_file() or destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            staged.append(destination)
    if not (target_purelib / "memray" / "__init__.py").is_file():
        raise RuntimeError("target_memray_runtime_not_staged")
    return staged


def _remove_staged_files(staged: list[Path]) -> None:
    for path in reversed(staged):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            pass


def _target_capture_path(host_pid: int) -> tuple[Path, Path]:
    target_root = Path(f"/proc/{host_pid}/root")
    relative = Path("tmp") / f"mini-drop-memray-{uuid.uuid4().hex}.bin"
    return target_root / relative, Path("/") / relative


def _run_memray_attach(
    *,
    nsenter: str,
    host_pid: int,
    target_capture: Path,
    target_source: Path,
    output: Path,
    duration: int,
    target_purelib: str,
) -> subprocess.CompletedProcess:
    import memray._memray as native_module

    native_candidates = sorted(Path(native_module.__file__).parent.glob("_memray*.so"))
    if not native_candidates:
        return subprocess.CompletedProcess(
            ["memray"],
            127,
            stdout=b"",
            stderr=b"agent_memray_native_module_missing",
        )
    target_native = Path("/") / target_purelib.lstrip("/") / "memray" / native_candidates[0].name
    script = Path(tempfile.mkstemp(prefix="mini-drop-memray-attach-", suffix=".py")[1])
    script.write_text(
        "\n".join(
            [
                "import memray",
                "import memray._memray as _native",
                "import sys",
                "from memray.commands import main",
                f"_native.__file__ = {str(target_native)!r}",
                "sys.argv = [",
                "  'memray', 'attach', '--method', 'gdb', '--verbose', '--force',",
                "  '--no-compress', '--output',",
                f"  {str(target_capture)!r}, '--duration', {str(duration)!r},",
                f"  {str(host_pid)!r},",
                "]",
                "raise SystemExit(main())",
            ]
        ),
        encoding="utf-8",
    )
    process: subprocess.Popen | None = None

    def _terminate_child(_signum, _frame):
        try:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        raise SystemExit(143)

    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, _terminate_child)
    signal.signal(signal.SIGINT, _terminate_child)
    try:
        process = subprocess.Popen(
            [
                nsenter,
                "-t",
                str(host_pid),
                "-n",
                "--",
                sys.executable,
                str(script),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=duration + 45)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            result = subprocess.CompletedProcess(
                process.args,
                124,
                stdout=stdout,
                stderr=stderr + b"\nmemray_attach_process_timeout",
            )
        else:
            result = subprocess.CompletedProcess(
                process.args,
                process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        if result.returncode == 0:
            source = target_source
            if source.is_file() and source.stat().st_size > 0:
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, output)
        return result
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
        try:
            script.unlink()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True, help="host-visible target PID")
    parser.add_argument("--duration", type=int, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.pid <= 0 or not Path(f"/proc/{args.pid}").is_dir():
        print("target_process_missing", flush=True)
        return 2
    if args.duration <= 0:
        print("duration_must_be_positive", flush=True)
        return 2
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("memray_namespace_helper_requires_root", flush=True)
        return 3

    nsenter = shutil.which("nsenter")
    memray = shutil.which("memray")
    if not nsenter:
        print("nsenter_not_installed", flush=True)
        return 4
    if not memray:
        print("memray_not_installed", flush=True)
        return 5

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    target_capture: Path | None = None
    try:
        runtime = _target_python_runtime(nsenter, args.pid)
        if (
            runtime["major"] != str(sys.version_info.major)
            or runtime["minor"] != str(sys.version_info.minor)
        ):
            print("incompatible_python_runtime", flush=True)
            return 6
        staged = _stage_memray_runtime(args.pid, runtime["purelib"])
        target_capture, visible_capture = _target_capture_path(args.pid)
        result = _run_memray_attach(
            nsenter=nsenter,
            host_pid=args.pid,
            target_capture=visible_capture,
            target_source=target_capture,
            output=output,
            duration=args.duration,
            target_purelib=runtime["purelib"],
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), flush=True)
        return 7
    finally:
        _remove_staged_files(staged)
        if target_capture is not None:
            try:
                target_capture.unlink()
            except OSError:
                pass
    if result.returncode != 0:
        if result.stdout:
            print(
                "memray_attach_stdout:\n"
                + result.stdout.decode("utf-8", errors="replace")[-2000:],
                flush=True,
            )
        if result.stderr:
            print(
                "memray_attach_stderr:\n"
                + result.stderr.decode("utf-8", errors="replace")[-2000:],
                flush=True,
            )
        return result.returncode or 8
    if not output.is_file() or output.stat().st_size <= 0:
        print("memray_attach_output_missing", flush=True)
        return 9
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
