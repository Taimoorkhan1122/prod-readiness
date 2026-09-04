"""Tests for the dashboard's launch protocol.

The launch used to depend on a person or an agent reading a URL out of a log
line. These tests cover the replacement: a handshake file, a health check that
decides reuse, and a detached server process. The reuse decision is where the
bugs live, so most of this is unit-level against a stubbed health check; one
integration test exists because only a real spawn exercises the platform
process flags.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import readiness_dashboard as dash  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "readiness_dashboard.py"


class HandshakeFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_write_then_read_round_trip(self):
        dash.write_handshake(self.root, "http://127.0.0.1:9/", 9)
        record = dash.read_handshake(self.root)
        self.assertEqual(record["url"], "http://127.0.0.1:9/")
        self.assertEqual(record["port"], 9)
        self.assertEqual(record["pid"], os.getpid())
        self.assertEqual(record["audit_root"], str(dash.audit_root_for(self.root)))

    def test_write_creates_the_audit_directory(self):
        self.assertFalse((self.root / ".readiness-audit").exists())
        dash.write_handshake(self.root, "http://127.0.0.1:9/", 9)
        self.assertTrue((self.root / ".readiness-audit").is_dir())

    def test_missing_file_reads_as_absent(self):
        self.assertIsNone(dash.read_handshake(self.root))

    def test_corrupt_json_reads_as_absent(self):
        path = dash.handshake_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(dash.read_handshake(self.root))

    def test_unknown_schema_reads_as_absent(self):
        dash.write_handshake(self.root, "http://127.0.0.1:9/", 9)
        path = dash.handshake_path(self.root)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["schema"] = dash.HANDSHAKE_SCHEMA + 99
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertIsNone(dash.read_handshake(self.root))

    def test_incomplete_record_reads_as_absent(self):
        path = dash.handshake_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": dash.HANDSHAKE_SCHEMA, "pid": 1}), encoding="utf-8")
        self.assertIsNone(dash.read_handshake(self.root))


class ReuseDecisionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_healthy_record_is_reused(self):
        dash.write_handshake(self.root, "http://127.0.0.1:9/", 9)
        with mock.patch.object(dash, "health_check", return_value=True):
            self.assertIsNotNone(dash.find_running(self.root))

    def test_unhealthy_record_is_not_reused(self):
        dash.write_handshake(self.root, "http://127.0.0.1:9/", 9)
        with mock.patch.object(dash, "health_check", return_value=False):
            self.assertIsNone(dash.find_running(self.root))

    def test_record_for_another_audit_is_not_reused(self):
        dash.write_handshake(self.root, "http://127.0.0.1:9/", 9)
        path = dash.handshake_path(self.root)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["audit_root"] = str(Path(record["audit_root"]).parent / "somewhere-else")
        path.write_text(json.dumps(record), encoding="utf-8")
        with mock.patch.object(dash, "health_check", return_value=True) as health:
            self.assertIsNone(dash.find_running(self.root))
            health.assert_not_called()

    def test_launch_reuses_instead_of_spawning(self):
        dash.write_handshake(self.root, "http://127.0.0.1:9/", 9)
        with mock.patch.object(dash, "health_check", return_value=True), \
                mock.patch.object(dash, "spawn_detached") as spawn:
            record = dash.launch(self.root, open_in_browser=False)
        spawn.assert_not_called()
        self.assertEqual(record["url"], "http://127.0.0.1:9/")

    def test_launch_reports_failure_when_no_handshake_appears(self):
        with mock.patch.object(dash, "spawn_detached"), \
                mock.patch.object(dash, "health_check", return_value=False):
            self.assertIsNone(dash.launch(self.root, open_in_browser=False, timeout=0.2))


class HealthCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _response(self, payload):
        body = json.dumps(payload).encode("utf-8")
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = body
        response.__enter__.return_value = response
        return response

    def test_matching_audit_root_is_healthy(self):
        payload = {"auditRoot": str(dash.audit_root_for(self.root))}
        with mock.patch.object(urllib.request, "urlopen", return_value=self._response(payload)):
            self.assertTrue(dash.health_check("http://127.0.0.1:9/", dash.audit_root_for(self.root)))

    def test_different_audit_root_is_not_healthy(self):
        with mock.patch.object(urllib.request, "urlopen",
                               return_value=self._response({"auditRoot": "/somewhere/else"})):
            self.assertFalse(dash.health_check("http://127.0.0.1:9/", dash.audit_root_for(self.root)))

    def test_connection_failure_is_not_healthy(self):
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("refused")):
            self.assertFalse(dash.health_check("http://127.0.0.1:9/", dash.audit_root_for(self.root)))


class StopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_stale_record_is_deleted_and_nothing_is_signalled(self):
        dash.write_handshake(self.root, "http://127.0.0.1:9/", 9)
        with mock.patch.object(dash, "health_check", return_value=False), \
                mock.patch.object(os, "kill") as kill:
            self.assertFalse(dash.stop(self.root))
        kill.assert_not_called()
        self.assertFalse(dash.handshake_path(self.root).exists())

    def test_healthy_record_is_signalled(self):
        dash.write_handshake(self.root, "http://127.0.0.1:9/", 9)
        with mock.patch.object(dash, "health_check", return_value=True), \
                mock.patch.object(os, "kill") as kill:
            self.assertTrue(dash.stop(self.root))
        kill.assert_called_once()
        self.assertFalse(dash.handshake_path(self.root).exists())

    def test_stop_without_a_record_is_not_an_error(self):
        self.assertFalse(dash.stop(self.root))


class BrowserTests(unittest.TestCase):
    def test_remote_sessions_do_not_attempt_to_open_a_browser(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_REMOTE": "1"}), \
                mock.patch.object(dash.webbrowser, "open") as opener:
            self.assertFalse(dash.open_browser("http://127.0.0.1:9/"))
        opener.assert_not_called()

    def test_a_failing_browser_never_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(dash.webbrowser, "open", side_effect=OSError("no display")):
            self.assertFalse(dash.open_browser("http://127.0.0.1:9/"))


class DetachedLaunchIntegrationTests(unittest.TestCase):
    """One real spawn, because the platform process flags cannot be faked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._terminate)

    def _terminate(self):
        record = dash.read_handshake(self.root)
        if record:
            try:
                os.kill(int(record["pid"]), 9)
            except OSError:
                pass

    def test_launcher_starts_a_server_that_answers(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.root), "--no-open"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        url = result.stdout.strip().splitlines()[-1]
        self.assertTrue(url.startswith("http://127.0.0.1:"), url)

        record = dash.read_handshake(self.root)
        self.assertIsNotNone(record)
        self.assertEqual(record["url"], url)
        self.assertTrue(dash.health_check(url, dash.audit_root_for(self.root), timeout=5))

        with urllib.request.urlopen(url + "api/ping", timeout=5) as response:
            self.assertEqual(json.loads(response.read().decode("utf-8")), {"ok": True})

        again = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.root), "--no-open"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(again.stdout.strip().splitlines()[-1], url,
                         "a second launch must reuse the running server")

        stopped = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.root), "--stop"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(stopped.returncode, 0, stopped.stderr)


if __name__ == "__main__":
    unittest.main()
