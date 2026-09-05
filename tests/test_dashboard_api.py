"""Tests for the dashboard's HTTP surface beyond the snapshot.

These cover the two additions that changed what the dashboard is allowed to do:
it now reports live lens progress, and it writes one kind of file.
"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import progress  # noqa: E402
import readiness_dashboard as dash  # noqa: E402


class SnapshotProgressTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".readiness-audit" / "findings").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def test_snapshot_carries_progress_for_every_lens(self):
        progress.append_note(self.root, "security", "analyzing", "Reading the auth paths.")
        snapshot = dash.build_snapshot(self.root)
        self.assertIn("progress", snapshot)
        self.assertEqual(snapshot["progress"]["security"]["events"][-1]["note"],
                         "Reading the auth paths.")

    def test_a_lens_that_said_nothing_has_no_invented_progress(self):
        snapshot = dash.build_snapshot(self.root)
        backend = snapshot["progress"]["backend"]
        self.assertEqual(backend["events"], [])
        self.assertEqual(backend["signal"], "no-signal")

    def test_project_without_an_audit_still_reports_a_progress_key(self):
        empty = Path(tempfile.mkdtemp(dir=self._tmp.name))
        snapshot = dash.build_snapshot(empty)
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertEqual(snapshot["progress"], {})


class ServerRouteTests(unittest.TestCase):
    """Runs a real server on a loopback port, in-process."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".readiness-audit" / "findings").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

        self.server = dash.create_server(self.root, 0)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = dash.startup_url(self.server)

    def _get(self, path):
        with urllib.request.urlopen(self.url.rstrip("/") + path, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_ping_is_cheap_and_counts_as_activity(self):
        self.server.last_activity -= 1000
        status, payload = self._get("/api/ping")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True})
        self.assertLess(self.server.seconds_idle(), 5)

    def test_unknown_route_is_not_found(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get("/api/nope")
        self.assertEqual(caught.exception.code, 404)

    def test_export_writes_a_directory_and_reports_its_files(self):
        request = urllib.request.Request(self.url.rstrip("/") + "/api/export", method="POST")
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"], payload)
        directory = Path(payload["directory"])
        self.assertTrue(directory.is_dir())
        self.assertIn("report.tex", payload["files"])
        expected_parent = (self.root / ".readiness-audit" / "export").resolve()
        self.assertEqual(directory.resolve().parent, expected_parent)

    def test_export_rejects_any_other_post_route(self):
        request = urllib.request.Request(self.url.rstrip("/") + "/api/snapshot", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
