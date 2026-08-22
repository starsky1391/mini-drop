from pathlib import Path
import json, os, time

EVIDENCE = Path(os.environ.get("CASE_EVIDENCE_ROOT", "/evidence"))
PATTERN = os.environ.get("TARGET_PROCESS_PATTERN", "")
DEADLINE = time.monotonic() + max(60, int(os.environ.get("CASE_DURATION_SEC", "180")))

def pid():
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        try:
            cmd = (item / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if PATTERN and PATTERN in cmd:
            return int(item.name)
    return None

def rss(value):
    try:
        for line in Path(f"/proc/{value}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None

EVIDENCE.mkdir(parents=True, exist_ok=True)
while time.monotonic() < DEADLINE:
    target = pid()
    with (EVIDENCE / "worker_observations.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "rss_sample", "observed_at": time.time(), "pid": target, "rss_bytes": rss(target) if target else None}) + "\n")
    time.sleep(1)
