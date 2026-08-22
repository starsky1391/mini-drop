from pathlib import Path

from deploy.collectors.memray_live.memray_attach_helper import (
    _attach_namespace_command,
    _attach_script_content,
)


def test_memray_attach_uses_target_namespace_and_executable():
    command = _attach_namespace_command(
        nsenter="/usr/bin/nsenter",
        host_pid=9001,
        target_executable="/usr/local/bin/python3",
        visible_script=Path("/tmp/mini-drop-attach.py"),
    )

    assert [value.replace("\\", "/") for value in command] == [
        "/usr/bin/nsenter",
        "-t",
        "9001",
        "-m",
        "-p",
        "-n",
        "--",
        "/usr/local/bin/python3",
        "/tmp/mini-drop-attach.py",
    ]


def test_memray_attach_script_uses_container_pid_not_host_pid():
    script = _attach_script_content(
        target_capture=Path("/tmp/memray.bin"),
        target_pid=17,
        duration=5,
        target_native=Path("/usr/local/lib/python3.11/site-packages/memray/_memray.so"),
    )

    assert "'17'," in script
    assert "memray', 'attach'" in script
    assert "'9001'," not in script
    assert "--duration', '5'" in script
