"""
Tests for the runtime lens data, network, and responsive slice (issue #24).

The lens checks what the app shows. It compares displayed numbers with
API payloads. It compares totals across pages. It runs read-only table
actions. It checks key viewports. The walk stays read-only. It stores
no secrets.
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
    check_viewport,
    compare_cross_page_totals,
    compare_ui_vs_api,
    exercise_search_filter_sort_pagination,
    observation_to_finding,
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
API_CART = FIXTURE / "api" / "cart.json"
TABLE_ORDERS = FIXTURE / "tables" / "orders.json"

DATASET = [
    {"id": "o1", "plan": "Starter", "price": 9, "status": "active"},
    {"id": "o2", "plan": "Team", "price": 29, "status": "active"},
    {"id": "o3", "plan": "Team", "price": 29, "status": "trial"},
    {"id": "o4", "plan": "Scale", "price": 99, "status": "active"},
]

ACTIONS = [
    {"name": "search-team", "kind": "search", "query": "Team"},
    {"name": "filter-active", "kind": "filter",
     "field": "status", "value": "active"},
    {"name": "sort-price", "kind": "sort",
     "field": "price", "direction": "asc"},
    {"name": "page-one", "kind": "pagination", "page": 1, "per_page": 2},
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


def finding_ids(findings):
    return [item["id"] for item in findings]


class ApiMismatchTests(unittest.TestCase):
    def test_match_returns_none(self):
        obs = compare_ui_vs_api("/checkout", 42, {"total": 42},
                                "/api/cart", 200)
        self.assertIsNone(obs)

    def test_match_ignores_text_versus_number_shape(self):
        obs = compare_ui_vs_api("/checkout", "42", {"total": 42},
                                "/api/cart", 200)
        self.assertIsNone(obs)

    def test_mismatch_reports_endpoint_status_expected_actual_and_page(self):
        obs = compare_ui_vs_api("/checkout", 99, {"total": 42},
                                "/api/cart", 200)
        self.assertIsNotNone(obs)
        self.assertEqual(obs["category"], "data")
        self.assertEqual(obs["page"], "/checkout")
        blob = " ".join([str(obs["detail"]), str(obs["observed"]),
                         str(obs["endpoint"]), str(obs["status"]),
                         str(obs["expected"]), str(obs["actual"])])
        for token in ("/api/cart", "200", "42", "99", "/checkout"):
            self.assertIn(token, blob)

    def test_mismatch_finding_passes_validate(self):
        server = start_server()
        try:
            root = make_root(server)
            obs = compare_ui_vs_api("/checkout", 99, {"total": 42},
                                    "/api/cart", 200)
            finding = observation_to_finding(obs, 1)
            self.assertEqual(finding["id"], "PRA-RT-001")
            blob = " ".join(finding["evidence"])
            self.assertTrue(RUNTIME_PAGE.search(blob))
            self.assertTrue(RUNTIME_VIEWPORT.search(blob))
            self.assertTrue(RUNTIME_OBSERVATION.search(blob))
            for token in ("/api/cart", "200", "42", "99", "/checkout"):
                self.assertIn(token, blob + finding["failure_path"])
            (root / ".readiness-audit" / "findings").mkdir(
                parents=True, exist_ok=True)
            (root / ".readiness-audit" / "findings" / "runtime.json").write_text(
                json.dumps({"schema": 2, "lens": "runtime",
                            "findings": [finding]}), encoding="utf-8")
            errors, _, _ = validate(root)
            self.assertEqual(errors, [], errors)
        finally:
            server.shutdown()


class CrossPageTotalsTests(unittest.TestCase):
    def test_agreement_returns_empty_list(self):
        result = compare_cross_page_totals([
            {"page": "/pricing", "total": 99},
            {"page": "/checkout", "total": 99},
        ])
        self.assertEqual(result, [])

    def test_disagreement_cites_both_locations(self):
        result = compare_cross_page_totals([
            {"page": "/pricing", "total": 99},
            {"page": "/checkout", "total": 42},
        ])
        self.assertEqual(len(result), 1)
        obs = result[0]
        self.assertEqual(obs["category"], "data")
        blob = obs["detail"] + " " + obs["observed"]
        self.assertIn("/pricing", blob)
        self.assertIn("/checkout", blob)
        self.assertIn("99", blob)
        self.assertIn("42", blob)

    def test_disagreement_finding_passes_validate(self):
        server = start_server()
        try:
            root = make_root(server)
            obs = compare_cross_page_totals([
                {"page": "/pricing", "total": 99},
                {"page": "/checkout", "total": 42},
            ])[0]
            finding = observation_to_finding(obs, 1)
            blob = " ".join(finding["evidence"])
            self.assertTrue(RUNTIME_PAGE.search(blob))
            self.assertTrue(RUNTIME_VIEWPORT.search(blob))
            self.assertTrue(RUNTIME_OBSERVATION.search(blob))
            self.assertIn("/pricing", finding["failure_path"])
            self.assertIn("/checkout", finding["failure_path"])
            (root / ".readiness-audit" / "findings").mkdir(
                parents=True, exist_ok=True)
            (root / ".readiness-audit" / "findings" / "runtime.json").write_text(
                json.dumps({"schema": 2, "lens": "runtime",
                            "findings": [finding]}), encoding="utf-8")
            errors, _, _ = validate(root)
            self.assertEqual(errors, [], errors)
        finally:
            server.shutdown()


class TableBehaviorTests(unittest.TestCase):
    def test_search_filter_sort_pagination_all_run(self):
        outcome = exercise_search_filter_sort_pagination(DATASET, ACTIONS)
        by_name = {item["name"]: item for item in outcome["results"]}
        self.assertEqual(by_name["search-team"]["count"], 2)
        self.assertEqual(by_name["filter-active"]["count"], 3)
        prices = [row["price"] for row in by_name["sort-price"]["rows"]]
        self.assertEqual(prices, [9, 29, 29, 99])
        self.assertEqual(by_name["page-one"]["count"], 2)
        self.assertEqual(outcome["skipped"], [])

    def test_empty_and_no_result_states_are_covered(self):
        outcome = exercise_search_filter_sort_pagination(DATASET, ACTIONS)
        by_name = {item["name"]: item for item in outcome["results"]}
        self.assertIn("empty-dataset", by_name)
        self.assertEqual(by_name["empty-dataset"]["count"], 0)
        self.assertIn("no-result", by_name)
        self.assertEqual(by_name["no-result"]["count"], 0)
        self.assertEqual(by_name["no-result"]["rows"], [])

    def test_state_changing_action_is_skipped_not_run(self):
        actions = list(ACTIONS) + [
            {"name": "delete-row", "kind": "delete",
             "state_changing": True},
            {"name": "place-order", "kind": "search",
             "query": "Team", "state_changing": True},
        ]
        before = json.dumps(DATASET, sort_keys=True)
        outcome = exercise_search_filter_sort_pagination(DATASET, actions)
        self.assertEqual(json.dumps(DATASET, sort_keys=True), before)
        skipped_names = [item["name"] for item in outcome["skipped"]]
        self.assertIn("delete-row", skipped_names)
        self.assertIn("place-order", skipped_names)
        for item in outcome["skipped"]:
            self.assertTrue(item["reason"])
        result_names = [item["name"] for item in outcome["results"]]
        self.assertNotIn("delete-row", result_names)
        self.assertNotIn("place-order", result_names)

    def test_input_dataset_is_never_mutated(self):
        source = [dict(row) for row in DATASET]
        snapshot = json.dumps(source, sort_keys=True)
        exercise_search_filter_sort_pagination(source, ACTIONS)
        self.assertEqual(json.dumps(source, sort_keys=True), snapshot)


class ResponsiveCheckTests(unittest.TestCase):
    def test_clean_layout_returns_none(self):
        obs = check_viewport("/pricing", "1280x800",
                             "Content fits. No defect.")
        self.assertIsNone(obs)

    def test_each_breakage_class_is_detected(self):
        for breakage in ("overlap", "clipping", "off-screen",
                         "unusable-table"):
            with self.subTest(breakage=breakage):
                obs = check_viewport("/pricing", "390x844",
                                     "Layout shows %s at 390x844" % breakage)
                self.assertIsNotNone(obs)
                self.assertEqual(obs["category"], "responsive")
                self.assertIn(breakage, obs["detail"])
                self.assertIn("390x844", obs["detail"])

    def test_each_viewport_is_cited_exactly(self):
        for viewport in ("390x844", "768x1024", "1280x800"):
            with self.subTest(viewport=viewport):
                obs = check_viewport("/pricing", viewport,
                                     {"breakage": "overlap",
                                      "detail": "Header overlaps the hero."})
                self.assertIsNotNone(obs)
                self.assertEqual(obs["viewport"], viewport)
                self.assertIn(viewport, obs["detail"])
                self.assertIn("overlap", obs["detail"])

    def test_responsive_finding_passes_validate(self):
        server = start_server()
        try:
            root = make_root(server)
            obs = check_viewport("/pricing", "390x844",
                                 "unusable-table at 390x844, price table "
                                 "needs horizontal scroll")
            finding = observation_to_finding(obs, 1)
            blob = " ".join(finding["evidence"])
            self.assertTrue(RUNTIME_PAGE.search(blob))
            self.assertTrue(RUNTIME_VIEWPORT.search(blob))
            self.assertTrue(RUNTIME_OBSERVATION.search(blob))
            self.assertIn("390x844", blob + finding["failure_path"])
            self.assertIn("unusable-table", blob + finding["failure_path"])
            (root / ".readiness-audit" / "findings").mkdir(
                parents=True, exist_ok=True)
            (root / ".readiness-audit" / "findings" / "runtime.json").write_text(
                json.dumps({"schema": 2, "lens": "runtime",
                            "findings": [finding]}), encoding="utf-8")
            errors, _, _ = validate(root)
            self.assertEqual(errors, [], errors)
        finally:
            server.shutdown()


class FixtureAndWalkTests(unittest.TestCase):
    def test_fixture_files_exist(self):
        self.assertTrue(API_CART.exists())
        self.assertTrue(TABLE_ORDERS.exists())

    def test_fixture_api_payload_drives_mismatch(self):
        payload = json.loads(API_CART.read_text(encoding="utf-8"))
        rows = json.loads(TABLE_ORDERS.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(rows), 4)
        obs = compare_ui_vs_api("/checkout", 99, payload,
                                payload["endpoint"], payload["status"])
        self.assertIsNotNone(obs)
        outcome = exercise_search_filter_sort_pagination(rows, ACTIONS)
        self.assertTrue(any(item["count"] > 0
                            for item in outcome["results"]))

    def test_walk_with_new_inputs_keeps_sequence_and_passes_validate(self):
        server = start_server()
        try:
            root = make_root(server)
            payload = json.loads(API_CART.read_text(encoding="utf-8"))
            rows = json.loads(TABLE_ORDERS.read_text(encoding="utf-8"))
            summary = run_walk(
                root, str(FIXTURE),
                console_entries=[],
                network_entries=[
                    {"method": "GET",
                     "url": "https://staging.example.com/checkout"},
                ],
                api_checks=[{
                    "page": "/dashboard",
                    "displayed": 100,
                    "api_payload": {"total": 42},
                    "endpoint": "/api/cart",
                    "status": 200,
                }],
                totals_checks=[[
                    {"page": "/pricing", "total": 99},
                    {"page": "/checkout", "total": 42},
                ]],
                table_cases=[{"dataset": rows, "actions": ACTIONS + [
                    {"name": "drop-table", "kind": "delete",
                     "state_changing": True},
                ]}],
                viewport_reports=[{
                    "screen": "/pricing",
                    "viewport": "768x1024",
                    "layout_report": "clipping at 768x1024, hero text "
                                     "clips the button",
                }])
            ids = finding_ids(summary["findings"])
            self.assertEqual(ids, ["PRA-RT-%03d" % number
                                   for number in range(1, len(ids) + 1)])
            blobs = [finding["failure_path"] + " " +
                     " ".join(finding["evidence"])
                     for finding in summary["findings"]]
            joined = " ".join(blobs)
            self.assertIn("/api/cart", joined)
            self.assertIn("/pricing", joined)
            self.assertIn("/checkout", joined)
            self.assertIn("768x1024", joined)
            self.assertIn("clipping", joined)
            errors, _, stats = validate(root)
            self.assertEqual(errors, [], errors)
            self.assertEqual(stats["by_lens"].get("runtime"),
                             len(summary["findings"]))
        finally:
            server.shutdown()

    def test_fixture_markers_surface_in_walk(self):
        server = start_server()
        try:
            root = make_root(server)
            summary = run_walk(
                root, str(FIXTURE),
                network_entries=[
                    {"method": "GET",
                     "url": "https://staging.example.com/"},
                ])
            joined = " ".join(
                finding["failure_path"] + " " +
                " ".join(finding["evidence"])
                for finding in summary["findings"])
            self.assertIn("/api/cart", joined)
            self.assertIn("unusable-table", joined)
            self.assertIn("390x844", joined)
        finally:
            server.shutdown()

    def test_secret_in_data_check_never_reaches_disk(self):
        server = start_server()
        try:
            root = make_root(server)
            secret = "hunter2-secret-value-abc123"
            run_walk(
                root, str(FIXTURE),
                network_entries=[
                    {"method": "GET",
                     "url": "https://staging.example.com/"},
                ],
                api_checks=[{
                    "page": "/checkout",
                    "displayed": "password=%s" % secret,
                    "api_payload": {"total": 42},
                    "endpoint": "/api/cart",
                    "status": 200,
                }])
            raw = (root / ".readiness-audit" / "findings"
                   / "runtime.json").read_text(encoding="utf-8")
            self.assertNotIn(secret, raw)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
