"""Tests for Go pprof collector."""

from __future__ import annotations

import os
import json
from unittest import mock

from agent.mini_drop_agent.collectors.base import CollectorTask
from agent.mini_drop_agent.collectors.pprof import PprofCollector


class TestPprofCollector:
    @staticmethod
    def _task(**kwargs) -> CollectorTask:
        return CollectorTask(
            id="pprof_test_001",
            collector_type="go_pprof",
            target_pid=1234,  # not used for HTTP-based pprof
            sample_rate=99,
            duration_sec=10,
            options=kwargs.get("options", {}),
        )

    def test_http_connection_error(self, tmp_path):
        collector = PprofCollector()
        collector.OUTPUT_BASE = str(tmp_path)
        with mock.patch("urllib.request.urlopen", side_effect=OSError("Connection refused")):
            result = collector.collect(self._task())
        assert result.ok is False
        assert "连接失败" in result.reason or "pprof" in result.reason.lower()

    def test_http_404_returns_failure(self, tmp_path):
        collector = PprofCollector()
        collector.OUTPUT_BASE = str(tmp_path)
        mock_error = mock.MagicMock()
        mock_error.code = 404
        mock_error.read.return_value = b"not found"
        with mock.patch("urllib.request.urlopen", side_effect=Exception("HTTP 404")):
            result = collector.collect(self._task())
        assert result.ok is False

    def test_empty_response(self, tmp_path):
        collector = PprofCollector()
        collector.OUTPUT_BASE = str(tmp_path)
        mock_resp = mock.MagicMock()
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_resp.read.return_value = b""
        with mock.patch("urllib.request.urlopen", return_value=mock_resp):
            result = collector.collect(self._task())
        assert result.ok is False
        assert "空数据" in result.reason

    def test_successful_collection(self, tmp_path):
        collector = PprofCollector()
        collector.OUTPUT_BASE = str(tmp_path)
        os.makedirs(os.path.join(str(tmp_path), "pprof_test_001"), exist_ok=True)
        pprof_data = b"mock pprof gzip data" * 100

        mock_resp = mock.MagicMock()
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_resp.read.return_value = pprof_data

        with mock.patch("urllib.request.urlopen", return_value=mock_resp), \
             mock.patch.object(PprofCollector, "_pprof_to_svg", return_value=False):
            result = collector.collect(self._task())
        assert result.ok is True
        assert len(result.artifacts) >= 1
        raw = [a for a in result.artifacts if a["artifact_type"] == "pprof_raw"]
        assert len(raw) == 1

    def test_go_not_installed_skips_svg(self, tmp_path):
        collector = PprofCollector()
        collector.OUTPUT_BASE = str(tmp_path)
        os.makedirs(os.path.join(str(tmp_path), "pprof_test_001"), exist_ok=True)

        mock_resp = mock.MagicMock()
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_resp.read.return_value = b"pprof data"

        with mock.patch("urllib.request.urlopen", return_value=mock_resp), \
             mock.patch.object(PprofCollector, "_pprof_to_svg", return_value=False):
            result = collector.collect(self._task())
        assert result.ok is True
        assert "go 未安装" in result.reason or "跳过" in result.reason

    def test_heap_collection_generates_structured_hotspots(self, tmp_path):
        collector = PprofCollector()
        collector.OUTPUT_BASE = str(tmp_path)
        pprof_data = b"mock heap pprof gzip data" * 100
        pprof_top = """
File: service
Type: inuse_space
Showing nodes accounting for 12MB, 100% of 12MB total
      flat  flat%   sum%        cum   cum%
      8MB 66.67% 66.67%       10MB 83.33%  /app/cache/cache.go:42
      4MB 33.33%   100%        4MB 33.33%  /app/api/handler.go:88
"""

        mock_resp = mock.MagicMock()
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_resp.read.return_value = pprof_data
        proc = mock.MagicMock(returncode=0, stdout=pprof_top, stderr="")

        with mock.patch("urllib.request.urlopen", return_value=mock_resp) as urlopen, \
             mock.patch.object(PprofCollector, "_find_go", return_value="/usr/bin/go"), \
             mock.patch("subprocess.run", return_value=proc):
            result = collector.collect(self._task(options={"profile_kind": "heap"}))

        assert result.ok is True
        assert "gc=1" in urlopen.call_args.args[0].full_url
        assert any(item["artifact_type"] == "pprof_raw" and item["filename"] == "heap.pb.gz" for item in result.artifacts)
        artifact = next(item for item in result.artifacts if item["artifact_type"] == "go_heap_profile_json")
        payload = artifact["metadata"]["data"]
        assert payload["evidence_validity"]["evidence_status"] == "valid"
        assert payload["hotspots"][0]["file"] == "/app/cache/cache.go"
        assert payload["hotspots"][0]["line"] == 42
        assert payload["line_candidates"][0]["file"] == "/app/cache/cache.go"
        on_disk = json.load(open(artifact["local_path"], encoding="utf-8"))
        assert on_disk["summary"]["hotspot_count"] == 2

    def test_heap_collection_missing_go_emits_blocked_json(self, tmp_path):
        collector = PprofCollector()
        collector.OUTPUT_BASE = str(tmp_path)

        mock_resp = mock.MagicMock()
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_resp.read.return_value = b"pprof data"

        with mock.patch("urllib.request.urlopen", return_value=mock_resp), \
             mock.patch.object(PprofCollector, "_find_go", return_value=None):
            result = collector.collect(self._task(options={"profile_kind": "heap"}))

        assert result.ok is True
        artifact = next(item for item in result.artifacts if item["artifact_type"] == "go_heap_profile_json")
        payload = artifact["metadata"]["data"]
        assert payload["evidence_validity"]["evidence_status"] == "blocked"
        assert payload["hotspots"] == []
