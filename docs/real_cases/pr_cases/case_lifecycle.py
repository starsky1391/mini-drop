from __future__ import annotations

import os
import threading
import time
from pathlib import Path


class CaseLifecycle:
    def __init__(self, evidence: Path):
        self.evidence = evidence
        self.initial_duration_sec = max(
            30, int(os.environ.get("CASE_DURATION_SEC", "180"))
        )
        self.initial_deadline = time.monotonic() + self.initial_duration_sec
        self.release_path = evidence / "runner-release.json"
        self.initial_window_path = evidence / "initial-window-complete"
        self._initial_window_announced = False
        self._lock = threading.Lock()

    def tick(self, emit) -> str:
        with self._lock:
            if not self._initial_window_announced and time.monotonic() >= self.initial_deadline:
                emit("initial_window_complete", duration_sec=self.initial_duration_sec)
                self.initial_window_path.touch()
                self._initial_window_announced = True
        if self.release_path.exists() and self.initial_window_path.exists():
            return "released"
        return "sustain" if self._initial_window_announced else "initial"
