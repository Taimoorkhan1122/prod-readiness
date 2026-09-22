"""
Tests for the runtime context intake and safety rails (issue #22).

The audit records which live system it may touch and enforces read-only
behavior: one login to get in, reads only afterwards, secrets never written
down. These tests cover the four acceptance bullets: fail-fast gate, secret
hygiene, read-only log proof, and mutating-control inspection.
"""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from scripts.export_report import export
from scripts.finding_store import FINDING_SCHEMA, load_lens, render_markdown
from scripts.runtime_context import (
    check_runtime_ready,
    is_read_only_log,
    load_inspections,
    load_runtime_context,
    mark_inspected_not_activated,
    redact,
    redact_text,
    set_runtime_context,
    verify_target_reachable,
)

FAKE_SECRET = "hunter2-secret-value-abc123"


def make_root():
    return Path(tempfile.mkdtemp())


def write_state(audit: Path, **overrides):
    state = {"stage": "3-lenses", "stage_status": "in_progress",
             "lenses_to_run": [], "lenses_skipped": {}}
    state.update(overrides)
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "state.json").write_text(json.dumps(state), encoding="utf-8")


def write_findings(audit: Path, lens: str, findings: list):
    (audit / "findings").mkdir(parents=True, exist_ok=True)
    (audit / "findings" / f"{lens}.json").write_text(
        json.dumps({"schema": FINDING_SCHEMA, "lens": lens, "findings": findings}),
        encoding="utf-8")


def runtime_finding(**overrides):
    base = {
        "id": "PRA-RT-001",
        "title": "Checkout cart reads empty after reload",
        "impact": "Shoppers lose the cart content after reload.",
        "state": "CONFIRMED",
        "owner": "runtime",
        "cross_lens": [],
        "evidence": ["Opened https://staging.example.com/checkout at 1280x800, "
                     "network shows the cart call returns empty"],
        "probe": None,
        "failure_path": "The reload path drops the cart token.",
        "compensating": "none found",
        "fix": "Persist the cart token across reloads.",
        "resolve": None,
        "see": None,
        "factors": {"exposure": "internal", "data_class": "business",
                    "blast_radius": "single-tenant",
                    "compensating_control": "absent"},
    }
    base.update(overrides)
    return base


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


class FailFastGateTests(unittest.TestCase):
    def test_missing_context_file_fails_fast_with_clear_message(self):
        root = make_root()
        ready, message = check_runtime_ready(root, timeout=2)
        self.assertFalse(ready)
        self.assertIn("missing", message.lower())
        self.assertIn("runtime", message.lower())
        self.assertIn("lens", message.lower())

    def test_context_without_url_fails_fast(self):
        root = make_root()
        audit = root / ".readiness-audit"
        audit.mkdir(parents=True, exist_ok=True)
        (audit / "runtime-context.json").write_text(
            json.dumps({"url": "", "environment": "staging"}), encoding="utf-8")
        ready, message = check_runtime_ready(root, timeout=2)
        self.assertFalse(ready)
        self.assertIn("missing", message.lower())
        self.assertIn("URL", message)

    def test_malformed_url_fails_without_network(self):
        ready, message = verify_target_reachable("not-a-url", timeout=2)
        self.assertFalse(ready)
        self.assertIn("not valid", message)

    def test_unreachable_target_fails_fast_with_clear_message(self):
        root = make_root()
        set_runtime_context(root, "http://127.0.0.1:9/", "staging",
                            "readonly-auditor", "vault:staging/readonly-user")
        ready, message = check_runtime_ready(root, timeout=2)
        self.assertFalse(ready)
        self.assertIn("unreachable", message.lower())
        self.assertIn("before", message.lower())

    def test_reachable_target_passes(self):
        server = start_server()
        try:
            url = "http://127.0.0.1:%d/" % server.server_address[1]
            root = make_root()
            set_runtime_context(root, url, "staging", "readonly-auditor",
                                "vault:staging/readonly-user")
            ready, message = check_runtime_ready(root, timeout=5)
            self.assertTrue(ready, message)
            self.assertIn("ready", message.lower())
        finally:
            server.shutdown()

    def test_verify_reports_http_error_status_as_reachable(self):
        with mock.patch("scripts.runtime_context.urllib.request.urlopen") as urlopen:
            import urllib.error
            urlopen.side_effect = urllib.error.HTTPError(
                "http://x/", 404, "Not Found", {}, None)
            reachable, message = verify_target_reachable("http://x/", timeout=2)
        self.assertTrue(reachable)
        self.assertIn("404", message)


class SecretHygieneTests(unittest.TestCase):
    def test_redact_strips_password_token_secret_api_key_patterns(self):
        cases = [
            "password=hunter2-secret-value-abc123",
            "token: hunter2-secret-value-abc123",
            "api_key = hunter2-secret-value-abc123",
            "api key: hunter2-secret-value-abc123",
            "client_secret=hunter2-secret-value-abc123",
            "Bearer hunter2-secret-value-abc123",
        ]
        for text in cases:
            with self.subTest(text=text):
                cleaned = redact_text(text)
                self.assertNotIn(FAKE_SECRET, cleaned)
                self.assertIn("[REDACTED]", cleaned)

    def test_redact_dict_values_under_secret_keys(self):
        cleaned = redact({"password": FAKE_SECRET, "name": "checkout"})
        self.assertEqual(cleaned["password"], "[REDACTED]")
        self.assertEqual(cleaned["name"], "checkout")

    def test_planted_secret_never_lands_in_stored_json(self):
        root = make_root()
        record = set_runtime_context(
            root, "https://staging.example.com/", "staging",
            "readonly-auditor", "vault:staging/readonly-user",
            scope_notes="Login uses password=%s on the checkout page" % FAKE_SECRET)
        self.assertNotIn(FAKE_SECRET, json.dumps(record))
        raw = (root / ".readiness-audit" / "runtime-context.json").read_text(
            encoding="utf-8")
        self.assertNotIn(FAKE_SECRET, raw)
        self.assertIn("[REDACTED]", raw)
        stored = load_runtime_context(root)
        self.assertNotIn(FAKE_SECRET, json.dumps(stored))
        self.assertEqual(stored["credential_source"], "reference")

    def test_credential_value_passed_as_reference_is_redacted(self):
        root = make_root()
        blob = "sk-abcdefghij1234567890ABCDEFGH123456"
        record = set_runtime_context(root, "https://staging.example.com/",
                                     "staging", "readonly-auditor", blob)
        self.assertEqual(record["credential_ref"], "[REDACTED]")
        raw = (root / ".readiness-audit" / "runtime-context.json").read_text(
            encoding="utf-8")
        self.assertNotIn(blob, raw)

    def test_secret_never_reaches_markdown_or_export(self):
        root = make_root()
        audit = root / ".readiness-audit"
        write_state(audit, stage_status="complete")
        set_runtime_context(
            root, "https://staging.example.com/", "staging",
            "readonly-auditor", "vault:staging/readonly-user",
            scope_notes="password=%s must not leak" % FAKE_SECRET)
        stored = load_runtime_context(root)
        self.assertNotIn(FAKE_SECRET, json.dumps(stored))
        finding = runtime_finding(
            evidence=["Opened %scheckout at 1280x800 with credential ref %s, "
                      "cart reads empty" % (stored["url"],
                                            stored["credential_ref"])])
        write_findings(audit, "runtime", [finding])
        findings = load_lens(audit / "findings" / "runtime.json")
        markdown = render_markdown(findings)
        self.assertNotIn(FAKE_SECRET, markdown)
        self.assertIn("vault:staging/readonly-user", markdown)
        with mock.patch("scripts.export_report._find_tex_compiler",
                        return_value=None):
            out_dir = export(root)
        tex = (out_dir / "report.tex").read_text(encoding="utf-8")
        self.assertNotIn(FAKE_SECRET, tex)
        self.assertIn("vault:staging/readonly-user", tex)


class ReadOnlyLogTests(unittest.TestCase):
    def test_reads_only_log_passes(self):
        entries = [
            {"method": "GET", "url": "https://staging.example.com/checkout"},
            {"method": "HEAD", "url": "https://staging.example.com/cart"},
            {"method": "OPTIONS", "url": "https://staging.example.com/cart"},
        ]
        ok, violations = is_read_only_log(entries)
        self.assertTrue(ok)
        self.assertEqual(violations, [])

    def test_reads_plus_one_login_post_passes(self):
        entries = [
            {"method": "POST", "url": "https://staging.example.com/login",
             "login": True},
            {"method": "GET", "url": "https://staging.example.com/checkout"},
            {"method": "GET", "url": "https://staging.example.com/cart"},
        ]
        ok, violations = is_read_only_log(entries)
        self.assertTrue(ok, violations)

    def test_login_by_url_without_flag_passes_once(self):
        entries = [
            {"method": "POST", "url": "https://staging.example.com/auth"},
            {"method": "GET", "url": "https://staging.example.com/pricing"},
        ]
        ok, violations = is_read_only_log(entries)
        self.assertTrue(ok, violations)

    def test_post_login_post_fails(self):
        entries = [
            {"method": "POST", "url": "https://staging.example.com/login",
             "login": True},
            {"method": "GET", "url": "https://staging.example.com/cart"},
            {"method": "POST", "url": "https://staging.example.com/cart"},
        ]
        ok, violations = is_read_only_log(entries)
        self.assertFalse(ok)
        self.assertTrue(any("POST" in v for v in violations))

    def test_put_patch_delete_fail(self):
        for method in ("PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                entries = [
                    {"method": "GET",
                     "url": "https://staging.example.com/items"},
                    {"method": method,
                     "url": "https://staging.example.com/items/1"},
                ]
                ok, violations = is_read_only_log(entries)
                self.assertFalse(ok)
                self.assertTrue(any(method in v for v in violations))

    def test_second_login_post_fails(self):
        entries = [
            {"method": "POST", "url": "https://staging.example.com/login",
             "login": True},
            {"method": "POST", "url": "https://staging.example.com/login",
             "login": True},
        ]
        ok, violations = is_read_only_log(entries)
        self.assertFalse(ok)
        self.assertTrue(any("one login" in v.lower() for v in violations))


class MutatingControlTests(unittest.TestCase):
    def test_inspection_records_state_without_activation(self):
        root = make_root()
        record = mark_inspected_not_activated(
            "delete-account-button", state="enabled",
            note="Button is visible on the settings page.", root=root)
        self.assertEqual(record["control_id"], "delete-account-button")
        self.assertEqual(record["state"], "enabled")
        self.assertFalse(record["activated"])
        stored = load_inspections(root)
        self.assertEqual(len(stored), 1)
        self.assertFalse(stored[0]["activated"])

    def test_inspection_without_root_still_returns_record(self):
        record = mark_inspected_not_activated("refund-order-button")
        self.assertFalse(record["activated"])
        self.assertEqual(record["action"], "inspected only")

    def test_inspection_note_is_redacted(self):
        record = mark_inspected_not_activated(
            "cancel-subscription", note="Form posts token=%s" % FAKE_SECRET)
        self.assertNotIn(FAKE_SECRET, record["note"])
        self.assertIn("[REDACTED]", record["note"])

    def test_missing_control_id_raises_clear_error(self):
        with self.assertRaises(ValueError):
            mark_inspected_not_activated("")


if __name__ == "__main__":
    unittest.main()
