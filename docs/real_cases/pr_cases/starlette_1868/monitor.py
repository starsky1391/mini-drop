from pathlib import Path
import json, os, time

EVIDENCE = Path(os.environ.get("CASE_EVIDENCE_ROOT", "/evidence"))
PATTERN = os.environ.get("TARGET_PROCESS_PATTERN", "")
def pid():
    for item in Path("/proc").iterdir():
        if item.name.isdigit():
            try:
                cmd = (item / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
                if PATTERN and PATTERN in cmd:
                    return int(item.name)
            except OSError:
                pass
    return None

def status(value):
    out = {"rss_bytes": None, "threads": None, "fd_count": None}
    try:
        for line in Path(f"/proc/{value}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                out["rss_bytes"] = int(line.split()[1]) * 1024
            elif line.startswith("Threads:"):
                out["threads"] = int(line.split()[1])
        out["fd_count"] = len(list(Path(f"/proc/{value}/fd").iterdir()))
    except (OSError, ValueError, IndexError):
        pass
    return out

EVIDENCE.mkdir(parents=True, exist_ok=True)
while not (EVIDENCE / "runner-release.json").exists():
    target = pid()
    with (EVIDENCE / "worker_observations.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "process_sample", "observed_at": time.time(), "pid": target, **(status(target) if target else {})}) + "\n")
    time.sleep(1)
