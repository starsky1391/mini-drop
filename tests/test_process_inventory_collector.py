import json
from unittest import mock

from agent.mini_drop_agent.collectors.base import CollectorTask
from agent.mini_drop_agent.collectors.process_inventory import ProcessInventoryCollector


REAL_OPEN = open


def test_process_inventory_collector_emits_structured_json(tmp_path):
    collector = ProcessInventoryCollector()
    collector.OUTPUT_BASE = str(tmp_path)
    files = {
        "/proc/stat": "cpu  1 0 1 98 0 0 0\nbtime 1000\n",
        "/proc/123/stat": "123 (python) S 1 1 1 0 -1 0 0 0 0 0 200 100 0 0 20 0 8 0 500 0 1024\n",
        "/proc/123/status": "Name:\tpython\nUid:\t1000\t1000\t1000\t1000\nThreads:\t8\n",
        "/proc/123/cmdline": "python\x00app.py\x00--service\x00order-service\x00",
        "/proc/123/comm": "python\n",
    }

    def fake_open(path, *args, **kwargs):
        if path in files:
            return mock.mock_open(read_data=files[path]).return_value
        return REAL_OPEN(path, *args, **kwargs)

    with mock.patch("os.path.isdir", side_effect=lambda path: path in {"/proc", "/proc/123"}), \
         mock.patch("os.listdir", return_value=["123", "self", "net"]), \
         mock.patch("builtins.open", side_effect=fake_open), \
         mock.patch("time.time", return_value=1600.0):
        result = collector.collect(CollectorTask(
            id="task_proc",
            collector_type="process_inventory",
            target_pid=1,
            sample_rate=1,
            duration_sec=1,
        ))

    assert result.ok is True
    artifact = result.artifacts[0]
    assert artifact["artifact_type"] == "process_inventory_json"
    data = json.loads(open(artifact["local_path"], encoding="utf-8").read())
    assert data["collector_type"] == "process_inventory"
    assert data["processes"][0]["pid"] == 123
    assert data["processes"][0]["comm"] == "python"
    assert data["processes"][0]["service_guess"] == "order-service"
