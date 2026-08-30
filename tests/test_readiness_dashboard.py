import json
import tempfile
import unittest
from pathlib import Path

from scripts.readiness_dashboard import build_snapshot


class SnapshotTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
