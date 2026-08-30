import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from scripts.readiness_dashboard import DASHBOARD_HTML, build_snapshot, create_server, startup_url


class DashboardClientContractTests(unittest.TestCase):
    def test_client_has_only_the_locked_navigation_destinations(self):
        for route in ("overview", "findings", "evidence", "report"):
            self.assertIn(f'data-route="{route}"', DASHBOARD_HTML)
        self.assertNotIn("switcher", DASHBOARD_HTML)
        self.assertNotIn("Back to dashboard", DASHBOARD_HTML)

    def test_client_polls_snapshot_every_two_seconds_while_running(self):
        self.assertIn("/api/snapshot", DASHBOARD_HTML)
        self.assertIn("setTimeout(refresh, 2000)", DASHBOARD_HTML)
        self.assertIn("snapshot.status === 'running'", DASHBOARD_HTML)

    def test_client_escapes_artifact_content_and_has_no_remote_assets(self):
        self.assertIn("function escapeHtml", DASHBOARD_HTML)
        self.assertNotIn("https://", DASHBOARD_HTML)
        self.assertNotIn("http://", DASHBOARD_HTML)


class SnapshotTests(unittest.TestCase):
    def _write_state(self, audit, **overrides):
        state = {
            "stage": "3-lenses",
            "stage_status": "in_progress",
            "lenses_to_run": [],
            "lenses_skipped": {},
            "notes": [],
        }
        state.update(overrides)
        (audit / "state.json").write_text(json.dumps(state))

    def test_missing_audit_returns_unavailable_snapshot(self):
        root = Path(tempfile.mkdtemp())

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["status"], "unavailable")
        self.assertEqual(snapshot["stage"], {"name": None, "status": None, "note": None})
        self.assertIn("No .readiness-audit directory", snapshot["message"])

    def test_running_audit_includes_stage_mode_and_lens_progress(self):
        root = Path(tempfile.mkdtemp())
        audit = root / ".readiness-audit"
        (audit / "findings").mkdir(parents=True)
        (audit / "state.json").write_text(json.dumps({
            "stage": "3-lenses",
            "stage_status": "in_progress",
            "execution_mode": "parallel",
            "notes": [{"stage": "3-lenses", "note": "Wave one complete"}],
            "lenses_to_run": ["security", "backend"],
            "lenses_skipped": {"ai-security": "No model calls"},
            "updated_at": "2026-08-30T10:00:00+00:00",
        }))
        (audit / "findings" / "security.md").write_text("# Security\n\n## P0\n")

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(snapshot["stage"], {"name": "3-lenses", "status": "in_progress", "note": "Wave one complete"})
        self.assertEqual(snapshot["executionMode"], "parallel")
        self.assertEqual(snapshot["lenses"][0]["status"], "complete")
        self.assertEqual(snapshot["lenses"][-1], {
            "id": "ai-security", "label": "AI security", "status": "skipped",
            "findingPath": "findings/ai-security.md",
        })

    def test_complete_audit_uses_report_and_counts_severity_labels(self):
        root = Path(tempfile.mkdtemp())
        audit = root / ".readiness-audit"
        (audit / "findings").mkdir(parents=True)
        (audit / "state.json").write_text(json.dumps({
            "stage": "5-report", "stage_status": "complete", "lenses_to_run": ["security"],
            "lenses_skipped": {}, "notes": []
        }))
        (audit / "findings" / "security.md").write_text("## P0\n## P1\nUNVERIFIED\n")
        (audit / "report.md").write_text("# Report\n\n## Verdict\nHOLD - DO NOT DEPLOY\n")

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["status"], "complete")
        self.assertEqual(snapshot["summary"], {"p0": 1, "p1": 1, "p2": 0, "unverified": 1, "verdict": "HOLD - DO NOT DEPLOY"})
        self.assertEqual(snapshot["artifacts"]["report"], {"available": True, "content": "# Report\n\n## Verdict\nHOLD - DO NOT DEPLOY\n"})

    def test_malformed_state_is_unavailable_instead_of_raising(self):
        root = Path(tempfile.mkdtemp())
        audit = root / ".readiness-audit"
        audit.mkdir()
        (audit / "state.json").write_text("{")

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIn("not readable", snapshot["message"])

    def test_missing_report_keeps_running_audit_partial(self):
        root = Path(tempfile.mkdtemp())
        audit = root / ".readiness-audit"
        audit.mkdir()
        (audit / "state.json").write_text(json.dumps({
            "stage": "4-validation", "stage_status": "in_progress", "lenses_to_run": [],
            "lenses_skipped": {}, "notes": []
        }))

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(snapshot["artifacts"]["report"], {"available": False, "content": None})

    def test_unrecognized_stage_status_is_unavailable(self):
        root = Path(tempfile.mkdtemp())
        audit = root / ".readiness-audit"
        audit.mkdir()
        self._write_state(audit, stage_status="paused")

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIn("stage status", snapshot["message"])

    def test_malformed_lens_configuration_is_unavailable(self):
        root = Path(tempfile.mkdtemp())
        audit = root / ".readiness-audit"
        audit.mkdir()
        self._write_state(audit, lenses_to_run="security")

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIn("lens configuration", snapshot["message"])

    def test_missing_lens_configuration_keys_are_unavailable(self):
        root = Path(tempfile.mkdtemp())
        audit = root / ".readiness-audit"
        audit.mkdir()
        (audit / "state.json").write_text(json.dumps({
            "stage": "3-lenses", "stage_status": "in_progress", "notes": []
        }))

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIn("lens configuration", snapshot["message"])

    def test_unreadable_finding_is_retained_as_unavailable(self):
        root = Path(tempfile.mkdtemp())
        audit = root / ".readiness-audit"
        (audit / "findings").mkdir(parents=True)
        self._write_state(audit, lenses_to_run=["security"])
        (audit / "findings" / "security.md").write_bytes(b"\xff")

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(snapshot["lenses"][0]["status"], "unavailable")
        self.assertEqual(snapshot["artifacts"]["findings"], [{
            "path": "findings/security.md", "available": False, "content": None,
        }])
        self.assertIn("unavailable", snapshot["message"])

    def test_unreadable_finding_overrides_skipped_lens_status(self):
        root = Path(tempfile.mkdtemp())
        audit = root / ".readiness-audit"
        (audit / "findings").mkdir(parents=True)
        self._write_state(audit, lenses_to_run=[], lenses_skipped={"security": "Not in scope"})
        (audit / "findings" / "security.md").write_bytes(b"\xff")

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["lenses"][0]["status"], "unavailable")


class DashboardHttpTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.server = create_server(self.root, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path):
        host, port = self.server.server_address
        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read().decode()
        connection.close()
        return response, body

    def test_server_binds_to_loopback_and_serves_snapshot(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        response, body = self.request("/api/snapshot")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(json.loads(body)["status"], "unavailable")

    def test_server_serves_dashboard_and_unknown_paths_are_404(self):
        root_response, root_body = self.request("/")
        missing_response, _ = self.request("/unexpected")
        self.assertEqual(root_response.status, 200)
        self.assertIn('id="app"', root_body)
        self.assertEqual(missing_response.status, 404)


class DashboardStartupTests(unittest.TestCase):
    def test_startup_url_uses_the_actual_ephemeral_port(self):
        server = create_server(Path(tempfile.mkdtemp()), port=0)
        host, port = server.server_address
        try:
            self.assertEqual(host, "127.0.0.1")
            self.assertGreater(port, 0)
            self.assertEqual(startup_url(server), f"http://127.0.0.1:{port}/")
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
