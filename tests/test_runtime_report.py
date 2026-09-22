"""
Tests for the runtime report, dashboard, and export slice (issue #25).

The runtime results are readable where the rest of the audit is
readable. The dashboard shows the eighth lens and its evidence. The SQA
markdown report carries every required section and a release call. The
bug CSV holds one row per validated bug with a fixed header. The export
carries the runtime documents. Secrets reach none of these files.
"""
import csv
import json
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from scripts import runtime_report
from scripts.export_report import export
from scripts.finding_store import FINDING_SCHEMA
from scripts.readiness_dashboard import DASHBOARD_HTML, build_snapshot
from scripts.runtime_context import set_runtime_context
from scripts.runtime_walk import run_walk
from scripts.validate_findings import (
    RUNTIME_OBSERVATION,
    RUNTIME_PAGE,
    RUNTIME_VIEWPORT,
)

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "sample-live-app"

SECRET = "hunter2-secret-abc-2500"

P0_FACTORS = {"exposure": "internet", "data_class": "secrets",
              "blast_radius": "systemic",
              "compensating_control": "absent"}
P1_FACTORS = {"exposure": "internet", "data_class": "business",
              "blast_radius": "multi-tenant",
              "compensating_control": "absent"}

SQA_HEADINGS = [
    "## Executive Summary",
    "## Scope",
    "## Screens Tested",
    "## Responsive Findings",
    "## Data Findings",
    "## API Findings",
    "## Console Findings",
    "## UI Findings",
    "## Accessibility Findings",
    "## Bug Summary",
    "## Limitations",
    "## Risk Assessment",
    "## Release Call",
]


class QuietHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_server():
    server = HTTPServer(("127.0.0.1", 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def make_root(server):
    root = Path(tempfile.mkdtemp())
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    set_runtime_context(root, url, "staging", "readonly-auditor",
                        "vault:staging/readonly-user")
    (root / ".readiness-audit" / "evidence").mkdir(parents=True,
                                                   exist_ok=True)
    (root / ".readiness-audit" / "evidence" / "absence-ledger.json").write_text(
        json.dumps({"controls": {}}), encoding="utf-8")
    return root


def walk_fixture(root, **kwargs):
    params = {"console_entries": [
        {"page": "/checkout", "viewport": "1280x800", "level": "error",
         "message": "TypeError: cart total is not a function"}],
        "network_entries": [
        {"method": "GET", "url": "https://staging.example.com/checkout"}]}
    params.update(kwargs)
    return run_walk(root, str(FIXTURE), **params)


def manual_finding(fid="PRA-RT-001", severity_factors=None, **overrides):
    finding = {
        "id": fid,
        "title": "Checkout page shows an empty cart after reload",
        "impact": "Shoppers see an empty cart after reload and leave.",
        "state": "CONFIRMED",
        "owner": "runtime",
        "cross_lens": [],
        "evidence": ["Opened /checkout at viewport 1280x800, observed "
                     "an empty cart after reload. Console and network logs "
                     "show read-only checks."],
        "probe": None,
        "failure_path": "The lens observed this on /checkout at 1280x800: "
                        "the cart reload path drops the session token.",
        "compensating": "none found",
        "fix": "Persist the cart token across reloads.",
        "resolve": None,
        "see": None,
        "factors": dict(severity_factors or P1_FACTORS),
    }
    finding.update(overrides)
    return finding


def write_runtime_findings(root, findings):
    path = root / ".readiness-audit" / "findings" / "runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"schema": FINDING_SCHEMA, "lens": "runtime",
         "findings": findings}, indent=2) + "\n", encoding="utf-8")


def write_state(root, **overrides):
    state = {"stage": "3-lenses", "stage_status": "in_progress",
             "lenses_to_run": ["runtime"], "lenses_skipped": {}}
    state.update(overrides)
    path = root / ".readiness-audit" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def no_compiler():
    return mock.patch("scripts.export_report._find_tex_compiler",
                      return_value=None)


class DashboardRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.server = start_server()
        self.addCleanup(self.server.shutdown)

    def test_snapshot_contains_runtime_lens_cell_with_findings(self):
        root = make_root(self.server)
        summary = walk_fixture(root)

        snapshot = build_snapshot(root)
        by_id = {lens["id"]: lens for lens in snapshot["lenses"]}

        self.assertEqual(len(snapshot["lenses"]), 8)
        self.assertIn("runtime", by_id)
        self.assertEqual(by_id["runtime"]["label"], "Runtime")
        self.assertEqual(by_id["runtime"]["status"], "complete")
        self.assertEqual(by_id["runtime"]["counts"]["total"],
                         len(summary["findings"]))

        runtime_findings = [f for f in snapshot["findings"]
                            if f["lens"] == "runtime"]
        self.assertEqual(len(runtime_findings), len(summary["findings"]))
        self.assertGreater(len(runtime_findings), 0)

    def test_snapshot_runtime_findings_carry_live_evidence(self):
        root = make_root(self.server)
        walk_fixture(root)

        snapshot = build_snapshot(root)
        runtime_findings = [f for f in snapshot["findings"]
                            if f["lens"] == "runtime"]
        self.assertGreater(len(runtime_findings), 0)
        for finding in runtime_findings:
            with self.subTest(finding=finding["id"]):
                blob = " ".join(finding["evidence"])
                self.assertTrue(RUNTIME_PAGE.search(blob),
                                "finding lacks a page: %r" % blob)
                self.assertTrue(RUNTIME_VIEWPORT.search(blob),
                                "finding lacks a viewport: %r" % blob)
                self.assertTrue(RUNTIME_OBSERVATION.search(blob),
                                "finding lacks observed behavior: %r" % blob)

    def test_drawer_renders_per_finding_evidence_list(self):
        self.assertIn("evidence-list", DASHBOARD_HTML)
        self.assertIn("finding.evidence", DASHBOARD_HTML)

    def test_skipped_runtime_cell_carries_recorded_reason(self):
        root = make_root(self.server)
        write_state(root, lenses_skipped={
            "runtime": "no live URL provided at intake"})

        snapshot = build_snapshot(root)
        by_id = {lens["id"]: lens for lens in snapshot["lenses"]}

        self.assertEqual(by_id["runtime"]["status"], "skipped")
        self.assertEqual(by_id["runtime"]["skippedReason"],
                         "no live URL provided at intake")


class SqaReportTests(unittest.TestCase):
    def setUp(self):
        self.server = start_server()
        self.addCleanup(self.server.shutdown)

    def test_sqa_contains_every_required_section(self):
        root = make_root(self.server)
        walk_fixture(root)
        summary = runtime_report.generate(root)

        text = Path(summary["sqa_path"]).read_text(encoding="utf-8")
        lowered = text.lower()
        for required in ("executive summary", "scope", "screens tested",
                         "responsive", "data", "api", "console", "ui",
                         "accessibility", "bug summary", "limitations",
                         "risk assessment"):
            self.assertIn(required, lowered, "missing: %s" % required)
        for heading in SQA_HEADINGS:
            self.assertIn(heading, text, "missing heading: %s" % heading)

    def test_sqa_call_is_valid_and_matches_severity_rule(self):
        root = make_root(self.server)
        walk_fixture(root)
        summary = runtime_report.generate(root)

        text = Path(summary["sqa_path"]).read_text(encoding="utf-8")
        match = re.search(r"^Release call:\s*(.+?)\s*$", text, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertIn(match.group(1), runtime_report.VALID_CALLS)
        self.assertEqual(summary["call"], match.group(1))

    def test_call_derivation_covers_all_three_calls(self):
        self.assertEqual(runtime_report.derive_call(
            [{"severity": "P0"}]), "NO-GO")
        self.assertEqual(runtime_report.derive_call(
            [{"severity": "P1"}, {"severity": "P2"}]), "GO WITH KNOWN RISKS")
        self.assertEqual(runtime_report.derive_call(
            [{"severity": "P2"}]), "GO")
        self.assertEqual(runtime_report.derive_call([]), "GO")

    def test_p0_findings_give_no_go_end_to_end(self):
        root = make_root(self.server)
        write_runtime_findings(root, [manual_finding(
            severity_factors=P0_FACTORS)])
        summary = runtime_report.generate(root)

        text = Path(summary["sqa_path"]).read_text(encoding="utf-8")
        self.assertEqual(summary["call"], "NO-GO")
        self.assertIn("Release call: NO-GO", text)

    def test_p1_findings_give_known_risks_end_to_end(self):
        root = make_root(self.server)
        write_runtime_findings(root, [manual_finding(
            severity_factors=P1_FACTORS)])
        summary = runtime_report.generate(root)

        text = Path(summary["sqa_path"]).read_text(encoding="utf-8")
        self.assertEqual(summary["call"], "GO WITH KNOWN RISKS")
        self.assertIn("Release call: GO WITH KNOWN RISKS", text)

    def test_screens_tested_come_from_coverage_file(self):
        root = make_root(self.server)
        walk_fixture(root)
        summary = runtime_report.generate(root)

        text = Path(summary["sqa_path"]).read_text(encoding="utf-8")
        coverage = json.loads(
            (root / ".readiness-audit" / "runtime-coverage.json").read_text(
                encoding="utf-8"))["coverage"]
        self.assertGreater(len(coverage), 0)
        for row in coverage[:3]:
            self.assertIn(row["screen"], text)
            self.assertIn(row["viewport"], text)

    def test_secret_never_reaches_sqa_or_findings(self):
        root = make_root(self.server)
        walk_fixture(root, console_entries=[{
            "page": "/checkout", "level": "error",
            "message": "login failed with password=" + SECRET}])
        summary = runtime_report.generate(root)

        findings_raw = (root / ".readiness-audit" / "findings"
                        / "runtime.json").read_text(encoding="utf-8")
        sqa_text = Path(summary["sqa_path"]).read_text(encoding="utf-8")
        self.assertNotIn(SECRET, findings_raw)
        self.assertNotIn(SECRET, sqa_text)


class BugCsvTests(unittest.TestCase):
    def setUp(self):
        self.server = start_server()
        self.addCleanup(self.server.shutdown)

    def test_header_order_is_exact_and_one_row_per_bug(self):
        root = make_root(self.server)
        walk_summary = walk_fixture(root)
        summary = runtime_report.generate(root)

        with open(summary["bugs_path"], encoding="utf-8",
                  newline="") as handle:
            rows = list(csv.reader(handle))

        self.assertGreater(len(rows), 1)
        self.assertEqual(rows[0], list(runtime_report.BUG_CSV_COLUMNS))
        self.assertEqual(len(rows) - 1, len(walk_summary["findings"]))
        for row in rows[1:]:
            self.assertEqual(len(row), len(runtime_report.BUG_CSV_COLUMNS))

    def test_empty_results_still_write_header(self):
        root = Path(tempfile.mkdtemp())
        summary = runtime_report.generate(root)

        with open(summary["bugs_path"], encoding="utf-8",
                  newline="") as handle:
            rows = list(csv.reader(handle))

        self.assertEqual(rows, [list(runtime_report.BUG_CSV_COLUMNS)])
        self.assertEqual(summary["findings"], 0)
        self.assertEqual(summary["call"], "GO")

    def test_no_credentials_in_any_cell(self):
        root = make_root(self.server)
        walk_fixture(root, console_entries=[{
            "page": "/checkout", "level": "error",
            "message": "login failed with password=" + SECRET}])
        summary = runtime_report.generate(root)

        raw = Path(summary["bugs_path"]).read_text(encoding="utf-8")
        self.assertNotIn(SECRET, raw)
        with open(summary["bugs_path"], encoding="utf-8",
                  newline="") as handle:
            rows = list(csv.reader(handle))
        for row in rows[1:]:
            for cell in row:
                self.assertNotIn(SECRET, cell)


class ExportRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.server = start_server()
        self.addCleanup(self.server.shutdown)

    def test_export_includes_runtime_section_and_per_lens_doc(self):
        root = make_root(self.server)
        walk_fixture(root)
        runtime_report.generate(root)
        write_state(root, stage_status="complete")

        with no_compiler():
            out_dir = export(root)

        combined = (out_dir / "report.tex").read_text(encoding="utf-8")
        self.assertIn("PRA-RT-", combined)
        self.assertIn("Runtime", combined)

        per_lens = (out_dir / "report-runtime.tex").read_text(
            encoding="utf-8")
        self.assertIn("PRA-RT-", per_lens)

    def test_export_copies_sqa_and_bugs_when_present(self):
        root = make_root(self.server)
        walk_fixture(root)
        runtime_report.generate(root)

        with no_compiler():
            out_dir = export(root)

        self.assertTrue((out_dir / "runtime-sqa.md").exists())
        self.assertTrue((out_dir / "runtime-bugs.csv").exists())
        copied = (out_dir / "runtime-bugs.csv").read_text(encoding="utf-8")
        self.assertIn(",".join(runtime_report.BUG_CSV_COLUMNS),
                      copied.splitlines()[0])

    def test_export_without_runtime_files_still_covers_lens(self):
        root = make_root(self.server)
        write_state(root, stage_status="complete",
                    lenses_skipped={"runtime": "no live URL at intake"})

        with no_compiler():
            out_dir = export(root)

        self.assertTrue((out_dir / "report-runtime.tex").exists())
        combined = (out_dir / "report.tex").read_text(encoding="utf-8")
        self.assertIn("Runtime", combined)


class RuntimeDocsTests(unittest.TestCase):
    def test_docs_state_when_lens_runs_and_when_it_skips(self):
        dispatch = (ROOT / "skills" / "production-readiness-audit"
                    / "references" / "lens-dispatch.md").read_text(
                        encoding="utf-8")
        intake = (ROOT / "skills" / "production-readiness-audit"
                  / "references" / "context-intake.md").read_text(
                      encoding="utf-8")
        writing = (ROOT / "skills" / "production-readiness-audit"
                   / "references" / "report-writing.md").read_text(
                       encoding="utf-8")
        blob = "\n".join([dispatch, intake, writing]).lower()

        self.assertIn("live url", blob)
        self.assertIn("reachable", blob)
        self.assertIn("skip", blob)
        self.assertIn("abort", blob)
        self.assertIn("runtime-sqa.md", writing)
        self.assertIn("runtime-bugs.csv", writing)


if __name__ == "__main__":
    unittest.main()
