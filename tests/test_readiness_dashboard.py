import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from scripts.finding_store import FINDING_SCHEMA
from scripts.readiness_dashboard import DASHBOARD_HTML, build_snapshot, create_server, startup_url


def write_findings(audit, lens, findings):
    (audit / "findings").mkdir(parents=True, exist_ok=True)
    (audit / "findings" / f"{lens}.json").write_text(
        json.dumps({"schema": FINDING_SCHEMA, "lens": lens, "findings": findings}),
        encoding="utf-8")


# A lens supplies factors, not a severity. These sets are chosen so the rubric
# in severity.py produces each band, which keeps the tests readable: they still
# say "give me a P0" while exercising the real derivation.
FACTORS_FOR_SEVERITY = {
    "P0": {"exposure": "internet", "data_class": "secrets",
           "blast_radius": "systemic", "compensating_control": "absent"},
    "P1": {"exposure": "internet", "data_class": "business",
           "blast_radius": "multi-tenant", "compensating_control": "absent"},
    "P2": {"exposure": "internal", "data_class": "business",
           "blast_radius": "single-tenant", "compensating_control": "absent"},
    "P3": {"exposure": "local", "data_class": "none",
           "blast_radius": "single-user", "compensating_control": "absent"},
}


def finding(**overrides):
    severity = overrides.pop("severity", "P0")
    base = {
        "id": "PRA-SEC-001",
        "title": "Tenant id is read from the request body",
        "impact": "Any logged-in customer can read another company's orders.",
        "state": "CONFIRMED",
        "factors": dict(FACTORS_FOR_SEVERITY[severity]),
        "evidence": ["src/orders/orders.service.ts:88"],
        "failure_path": "The controller trusts a client-supplied tenant id.",
        "compensating": "none found",
        "fix": "Derive the tenant from the session.",
    }
    base.update(overrides)
    return base


class DashboardClientContractTests(unittest.TestCase):
    def test_client_has_only_the_locked_navigation_destinations(self):
        self.assertIn("const ROUTES = ['overview', 'findings', 'evidence', 'report']", DASHBOARD_HTML)
        self.assertIn(
            "[['overview', 'Overview'], ['findings', 'Findings'], "
            "['evidence', 'Evidence'], ['report', 'Report']]",
            DASHBOARD_HTML)

    def test_client_polls_snapshot_every_two_seconds_while_running(self):
        self.assertIn("/api/snapshot", DASHBOARD_HTML)
        self.assertIn("setTimeout(refresh, 2000)", DASHBOARD_HTML)
        self.assertIn("snapshot.status === 'running'", DASHBOARD_HTML)

    def test_client_escapes_content_and_has_no_remote_assets(self):
        self.assertIn("function escapeHtml", DASHBOARD_HTML)
        self.assertNotIn("https://", DASHBOARD_HTML)
        self.assertNotIn("http://", DASHBOARD_HTML)

    def test_client_stops_polling_after_a_snapshot_request_failure(self):
        refresh_function = DASHBOARD_HTML.split("async function refresh() {", 1)[1]
        failure_handler = refresh_function.split("} catch (error) {", 1)[1]

        self.assertNotIn("setTimeout(refresh, 2000)", failure_handler)

    def test_client_renders_no_markdown(self):
        """The dashboard's job is to answer without making anyone read the trail."""
        self.assertNotIn("renderMarkdown", DASHBOARD_HTML)
        self.assertNotIn("reportHtml", DASHBOARD_HTML)

    def test_finding_rows_lead_with_impact_not_evidence(self):
        row = DASHBOARD_HTML.split("function findingRow(", 1)[1].split("function ", 1)[0]
        self.assertIn("row-impact", row)
        self.assertIn("finding.impact", row)
        self.assertNotIn("finding.evidence", row)

    def test_dialog_takes_and_returns_focus(self):
        self.assertIn('role="dialog"', DASHBOARD_HTML)
        self.assertIn("panel.focus()", DASHBOARD_HTML)
        self.assertIn("returnFocusTo", DASHBOARD_HTML)

    def test_control_state_words_differ_from_finding_state_words(self):
        """'Confirmed' on a control means 'the repo has one', which is not what
        it means on a finding. Different words keep the two from being read
        as the same claim."""
        self.assertIn("const CONTROL_LABEL = { CONFIRMED: 'Found'", DASHBOARD_HTML)


class SnapshotTests(unittest.TestCase):
    def _root(self):
        root = Path(tempfile.mkdtemp())
        (root / ".readiness-audit").mkdir()
        return root

    def _write_state(self, audit, **overrides):
        state = {"stage": "3-lenses", "stage_status": "in_progress",
                 "lenses_to_run": [], "lenses_skipped": {}}
        state.update(overrides)
        (audit / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def test_missing_audit_returns_unavailable_snapshot(self):
        snapshot = build_snapshot(Path(tempfile.mkdtemp()))

        self.assertEqual(snapshot["status"], "unavailable")
        self.assertEqual(snapshot["findings"], [])
        self.assertEqual(snapshot["counts"]["total"], 0)
        self.assertEqual(len(snapshot["lenses"]), 7)

    def test_running_audit_reports_stage_mode_and_lens_progress(self):
        root = self._root()
        audit = root / ".readiness-audit"
        (audit / "findings").mkdir()
        self._write_state(audit, execution_mode="parallel",
                          lenses_to_run=["security", "backend"],
                          lenses_skipped={"ai-security": "no AI surface"})
        write_findings(audit, "security", [finding()])

        snapshot = build_snapshot(root)
        by_id = {lens["id"]: lens for lens in snapshot["lenses"]}

        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(snapshot["executionMode"], "parallel")
        self.assertEqual(by_id["security"]["status"], "complete")
        self.assertEqual(by_id["backend"]["status"], "running")
        self.assertEqual(by_id["ai-security"]["status"], "skipped")
        self.assertEqual(by_id["ai-security"]["skippedReason"], "no AI surface")
        self.assertEqual(by_id["frontend"]["status"], "waiting")

    def test_counts_come_from_the_findings_themselves(self):
        root = self._root()
        audit = root / ".readiness-audit"
        self._write_state(audit, stage_status="complete")
        write_findings(audit, "security", [
            finding(id="PRA-SEC-001", severity="P0", state="CONFIRMED"),
            finding(id="PRA-SEC-002", severity="P1", state="UNVERIFIED", resolve="Check the console."),
        ])
        write_findings(audit, "qa", [finding(id="PRA-QA-001", severity="P2", state="NOT_FOUND", probe="tests")])

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["status"], "complete")
        self.assertEqual(snapshot["counts"],
                         {"total": 3, "p0": 1, "p1": 1, "p2": 1, "p3": 0,
                          "confirmed": 1, "notFound": 1, "unverified": 1})

    def test_findings_are_ordered_worst_first(self):
        root = self._root()
        audit = root / ".readiness-audit"
        self._write_state(audit)
        write_findings(audit, "security", [
            finding(id="PRA-SEC-009", severity="P2"),
            finding(id="PRA-SEC-002", severity="P0"),
            finding(id="PRA-SEC-005", severity="P1"),
        ])

        ids = [f["id"] for f in build_snapshot(root)["findings"]]

        self.assertEqual(ids, ["PRA-SEC-002", "PRA-SEC-005", "PRA-SEC-009"])

    def test_verdict_prose_is_authored_but_the_decision_is_computed(self):
        root = self._root()
        audit = root / ".readiness-audit"
        self._write_state(audit, stage_status="complete")
        (audit / "report.md").write_text("## Executive Verdict\n\n**SHIP**\n", encoding="utf-8")
        write_findings(audit, "security", [finding(severity="P0")])
        (audit / "verdict.json").write_text(json.dumps({
            "decision": "HOLD",
            "headline": "Two blockers make this unsafe to deploy.",
        }), encoding="utf-8")

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["verdict"]["decision"], "HOLD")
        self.assertEqual(snapshot["verdict"]["headline"],
                         "Two blockers make this unsafe to deploy.")
        self.assertEqual(snapshot["errors"], [])

    def test_an_authored_decision_that_disagrees_with_the_findings_is_reported(self):
        root = self._root()
        audit = root / ".readiness-audit"
        self._write_state(audit, stage_status="complete")
        write_findings(audit, "security", [finding(severity="P0")])
        (audit / "verdict.json").write_text(json.dumps({"decision": "SHIP"}), encoding="utf-8")

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["verdict"]["decision"], "HOLD")
        self.assertTrue(any("disagrees with the findings" in e for e in snapshot["errors"]))

    def test_a_running_audit_shows_no_decision_at_all(self):
        root = self._root()
        audit = root / ".readiness-audit"
        self._write_state(audit)
        write_findings(audit, "security", [finding(severity="P2")])

        self.assertIsNone(build_snapshot(root)["verdict"]["decision"])

    def test_unknown_verdict_decision_is_reported_not_guessed(self):
        root = self._root()
        audit = root / ".readiness-audit"
        self._write_state(audit)
        (audit / "verdict.json").write_text(
            json.dumps({"decision": "PROBABLY FINE"}), encoding="utf-8"
        )

        snapshot = build_snapshot(root)

        self.assertIsNone(snapshot["verdict"]["decision"])
        self.assertTrue(any("decision must be one of" in e for e in snapshot["errors"]))

    def test_malformed_findings_file_is_reported_without_losing_other_lenses(self):
        root = self._root()
        audit = root / ".readiness-audit"
        self._write_state(audit)
        write_findings(audit, "security", [finding()])
        (audit / "findings" / "qa.json").write_text("{ not json", encoding="utf-8")

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["counts"]["total"], 1)
        self.assertTrue(any("qa" in e and "not valid JSON" in e for e in snapshot["errors"]))

    def test_finding_missing_a_required_field_is_rejected_with_its_id(self):
        root = self._root()
        audit = root / ".readiness-audit"
        self._write_state(audit)
        broken = finding()
        del broken["fix"]
        write_findings(audit, "security", [broken])

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["counts"]["total"], 0)
        self.assertTrue(any("PRA-SEC-001" in e and "fix is required" in e for e in snapshot["errors"]))

    def test_malformed_state_degrades_instead_of_raising(self):
        root = self._root()
        audit = root / ".readiness-audit"
        (audit / "state.json").write_text("{ not json", encoding="utf-8")
        write_findings(audit, "security", [finding()])

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["counts"]["total"], 1)
        self.assertTrue(any("state.json" in e for e in snapshot["errors"]))

    def test_evidence_rows_carry_the_paths_that_justify_them(self):
        root = self._root()
        audit = root / ".readiness-audit"
        self._write_state(audit)
        (audit / "evidence").mkdir()
        (audit / "evidence" / "absence-ledger.json").write_text(json.dumps({"controls": {
            "rate_limiting": {"polarity": "control", "label": "Rate limiting",
                              "lens": "security", "hit_count": 0, "supports_state": "NOT_FOUND"},
            "backup_config": {"polarity": "control", "label": "Backups", "lens": "database",
                              "hit_count": 2, "supports_state": "NOT_FOUND",
                              "hits": [{"path": "infra/backup.tf"}, {"path": "README.md"}]},
            "a_branch_selector": {"polarity": "branch", "label": "ignored", "hit_count": 5},
        }}), encoding="utf-8")

        rows = {row["id"]: row for row in build_snapshot(root)["evidence"]}

        self.assertNotIn("a_branch_selector", rows)
        self.assertEqual(rows["rate_limiting"]["state"], "NOT_FOUND")
        self.assertEqual(rows["backup_config"]["state"], "CONFIRMED")
        self.assertEqual(rows["backup_config"]["paths"], ["infra/backup.tf", "README.md"])


class ServerTests(unittest.TestCase):
    def _serve(self, root):
        server = create_server(root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def _get(self, server, path):
        host, port = server.server_address
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def test_deep_links_with_a_query_string_still_serve_the_app(self):
        server = self._serve(Path(tempfile.mkdtemp()))

        self.assertEqual(self._get(server, "/")[0], 200)
        self.assertEqual(self._get(server, "/?view=findings&finding=PRA-SEC-001")[0], 200)

        status, body = self._get(server, "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "unavailable")

        self.assertEqual(self._get(server, "/nope")[0], 404)

    def test_server_binds_loopback_only(self):
        server = create_server(Path(tempfile.mkdtemp()))
        try:
            self.assertTrue(startup_url(server).startswith("http://127.0.0.1:"))
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
