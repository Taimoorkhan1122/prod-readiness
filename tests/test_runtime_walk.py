"""
Tests for the runtime lens core live walk (issue #23).

The lens walks the live app like a senior tester: main screens in route
order, each at a desktop and a mobile viewport, plus navigation and the
browser console. The walk is read-only and records clean screens as
covered, so absence of findings never reads as skipped.
"""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from scripts.runtime_context import set_runtime_context
from scripts.runtime_walk import (
    WalkError,
    build_resolve,
    discover_screens,
    run_walk,
)
from scripts.validate_findings import (
    RUNTIME_OBSERVATION,
    RUNTIME_PAGE,
    RUNTIME_VIEWPORT,
    validate,
)

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "sample-live-app"


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


CONSOLE_ENTRIES = [
    {"page": "/checkout", "viewport": "1280x800", "level": "error",
     "message": "TypeError: cart total is not a function"},
]

NETWORK_ENTRIES = [
    {"method": "GET", "url": "https://staging.example.com/checkout"},
    {"method": "GET", "url": "https://staging.example.com/cart"},
]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class WalkOrderTests(unittest.TestCase):
    def test_discovers_fixture_screens_in_route_order(self):
        screens = discover_screens(str(FIXTURE))
        self.assertEqual([s["screen"] for s in screens],
                         ["/", "/about", "/checkout", "/pricing"])


class FullWalkTests(unittest.TestCase):
    def setUp(self):
        self.server = start_server()
        self.addCleanup(self.server.shutdown)
        self.root = make_root(self.server)

    def walk(self, **kwargs):
        params = {"console_entries": CONSOLE_ENTRIES,
                  "network_entries": NETWORK_ENTRIES}
        params.update(kwargs)
        return run_walk(self.root, str(FIXTURE), **params)

    def test_full_walk_produces_findings_file_that_passes_validate(self):
        summary = self.walk()
        self.assertGreaterEqual(len(summary["findings"]), 3)
        kinds = {f["id"].split("-")[1] for f in summary["findings"]}
        self.assertEqual(kinds, {"RT"})
        errors, _, stats = validate(self.root)
        self.assertEqual(errors, [], errors)
        self.assertEqual(stats["by_lens"].get("runtime"),
                         len(summary["findings"]))

    def test_each_finding_carries_page_viewport_and_behavior(self):
        summary = self.walk()
        for finding in summary["findings"]:
            with self.subTest(finding=finding["id"]):
                blob = " ".join(finding["evidence"])
                self.assertTrue(RUNTIME_PAGE.search(blob),
                                "finding lacks a page: %r" % blob)
                self.assertTrue(RUNTIME_VIEWPORT.search(blob),
                                "finding lacks a viewport: %r" % blob)
                self.assertTrue(RUNTIME_OBSERVATION.search(blob),
                                "finding lacks observed behavior: %r" % blob)

    def test_layout_nav_and_console_categories_are_covered(self):
        summary = self.walk()
        blobs = [(f["id"], " ".join(f["evidence"] + [f["title"]]).lower())
                 for f in summary["findings"]]
        self.assertTrue(any("pricing" in blob for _, blob in blobs))
        self.assertTrue(any("order-confirmation" in blob
                            for _, blob in blobs))
        self.assertTrue(any("typeerror" in blob for _, blob in blobs))

    def test_revisit_or_resolve_holds_for_every_finding(self):
        summary = self.walk()
        stored = read_json(self.root / ".readiness-audit"
                           / "findings" / "runtime.json")["findings"]
        self.assertEqual(len(stored), len(summary["findings"]))
        for finding in stored:
            with self.subTest(finding=finding["id"]):
                record = finding.get("reproduce") or {}
                self.assertGreaterEqual(record.get("revisits", 0), 2)
                self.assertIsInstance(record.get("reproduced"), bool)
                if not record["reproduced"]:
                    resolve = (finding.get("resolve") or "").strip()
                    self.assertTrue(resolve,
                                    "non-reproduced finding needs resolve")
                    self.assertIn("revisit", resolve.lower())

    def test_non_reproduced_observation_becomes_unverified_with_resolve(self):
        summary = self.walk(observations=[{
            "category": "layout", "page": "/about",
            "viewport": "390x844",
            "detail": "hero text may clip the button",
            "observed": "hero text may clip the button",
            "reproduced": False}])
        extra = [f for f in summary["findings"]
                 if f["state"] == "UNVERIFIED"]
        self.assertEqual(len(extra), 1)
        self.assertIn("revisit", extra[0]["resolve"].lower())
        self.assertIn("/about", extra[0]["resolve"])
        errors, _, _ = validate(self.root)
        self.assertEqual(errors, [], errors)

    def test_clean_screens_appear_as_covered_in_coverage_file(self):
        summary = self.walk()
        coverage = read_json(self.root / ".readiness-audit"
                             / "runtime-coverage.json")["coverage"]
        self.assertEqual(len(summary["coverage"]), 8)
        by_key = {(row["screen"], row["viewport"]): row
                  for row in coverage}
        self.assertEqual(by_key[("/", "1280x800")]["status"], "covered")
        self.assertEqual(by_key[("/", "390x844")]["status"], "covered")
        self.assertEqual(by_key[("/about", "1280x800")]["status"],
                         "covered")
        self.assertEqual(by_key[("/pricing", "390x844")]["status"],
                         "finding")
        self.assertEqual(by_key[("/checkout", "1280x800")]["status"],
                         "finding")
        for row in coverage:
            self.assertIn(row["status"], ("covered", "finding"))
            self.assertTrue(row["notes"])

    def test_resolve_states_exact_settle_steps(self):
        text = build_resolve("layout", "/pricing", "390x844")
        self.assertIn("/pricing", text)
        self.assertIn("390x844", text)
        self.assertIn("twice", text)


class WalkGuardTests(unittest.TestCase):
    def test_missing_context_stops_the_walk(self):
        root = Path(tempfile.mkdtemp())
        with self.assertRaises(WalkError) as ctx:
            run_walk(root, str(FIXTURE))
        self.assertIn("missing", str(ctx.exception).lower())

    def test_write_in_network_log_stops_the_walk(self):
        server = start_server()
        try:
            root = make_root(server)
            with self.assertRaises(WalkError) as ctx:
                run_walk(root, str(FIXTURE), network_entries=[
                    {"method": "GET",
                     "url": "https://staging.example.com/"},
                    {"method": "POST",
                     "url": "https://staging.example.com/cart"}])
            self.assertIn("read-only", str(ctx.exception).lower())
            self.assertFalse((root / ".readiness-audit"
                              / "findings" / "runtime.json").exists())
        finally:
            server.shutdown()

    def test_secret_in_console_message_never_reaches_disk(self):
        server = start_server()
        try:
            root = make_root(server)
            run_walk(root, str(FIXTURE), console_entries=[{
                "page": "/checkout", "level": "error",
                "message": "login failed with password=hunter2-secret-abc"}],
                network_entries=NETWORK_ENTRIES)
            raw = (root / ".readiness-audit"
                   / "findings" / "runtime.json").read_text(
                       encoding="utf-8")
            self.assertNotIn("hunter2-secret-abc", raw)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
