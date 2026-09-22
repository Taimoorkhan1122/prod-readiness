"""
Tests for the runtime lens evidence contract (issue #20).

The audit accepts an eighth lens, `runtime` (id prefix `RT`), whose proof is
a live observation (page, viewport, console, read-only network check) instead
of a code location. The evidence gate is relaxed only for that lens: static
lenses keep the strict file:line rule, and the duplicate rule still catches
the same underlying issue raised by two lenses.
"""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.finding_store import FINDING_SCHEMA
from scripts.validate_findings import validate


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_lens_file(root: Path, lens: str, findings: list) -> Path:
    path = root / ".readiness-audit" / "findings" / f"{lens}.json"
    write_json(path, {"schema": FINDING_SCHEMA, "lens": lens, "findings": findings})
    return path


def write_ledger(root: Path) -> None:
    write_json(root / ".readiness-audit" / "evidence" / "absence-ledger.json",
               {"controls": {}})


def base_finding(**overrides) -> dict:
    finding = {
        "id": "PRA-RT-001",
        "title": "Cart shows empty after reload on the checkout page",
        "impact": "Shoppers see an empty cart after they reload and leave without paying.",
        "state": "CONFIRMED",
        "owner": "runtime",
        "cross_lens": [],
        "evidence": ["Opened https://staging.example.com/checkout at viewport "
                     "1280x800, console shows no errors, cart stays empty after reload"],
        "probe": None,
        "failure_path": "The cart reload path drops the session token.",
        "compensating": "none found",
        "fix": "Persist the cart token across reloads and re-read it on page open.",
        "resolve": None,
        "see": None,
        "factors": {"exposure": "internal", "data_class": "business",
                    "blast_radius": "single-tenant", "compensating_control": "absent"},
    }
    finding.update(overrides)
    return finding


class TempRoot:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write_ledger(self.root)
        return self.root

    def __exit__(self, *exc):
        self._tmp.cleanup()


def messages(errors):
    return [(fid, msg) for _, fid, msg in errors]


class RuntimeEvidenceContractTests(unittest.TestCase):
    def test_rt_confirmed_with_live_evidence_passes(self):
        with TempRoot() as root:
            write_lens_file(root, "runtime", [base_finding()])
            errors, _, _ = validate(root)
            self.assertEqual(errors, [])

    def test_each_live_marker_alone_satisfies_the_runtime_gate(self):
        variants = [
            ["Opened https://staging.example.com/checkout and saw an empty cart"],
            ["Observed an empty cart on the checkout page at 390x844"],
            ["Observed an empty cart on the checkout page at mobile viewport"],
            ["Reloaded the checkout page, network shows the cart call returns empty"],
            ["Took a screenshot of the empty cart on the checkout page"],
        ]
        for evidence in variants:
            with self.subTest(evidence=evidence):
                with TempRoot() as root:
                    write_lens_file(root, "runtime", [base_finding(evidence=evidence)])
                    errors, _, _ = validate(root)
                    self.assertEqual(errors, [])

    def test_rt_confirmed_with_no_evidence_fails(self):
        with TempRoot() as root:
            write_lens_file(root, "runtime", [base_finding(evidence=[])])
            errors, _, _ = validate(root)
            self.assertTrue(
                any(fid == "PRA-RT-001" and "CONFIRMED requires evidence" in msg
                    for fid, msg in messages(errors)))

    def test_rt_confirmed_with_file_line_only_fails(self):
        with TempRoot() as root:
            write_lens_file(root, "runtime",
                            [base_finding(evidence=["src/app/cart.tsx:42"])])
            errors, _, _ = validate(root)
            self.assertTrue(
                any(fid == "PRA-RT-001" and "live observation" in msg
                    for fid, msg in messages(errors)))

    def test_rt_prefix_in_wrong_file_fails(self):
        with TempRoot() as root:
            write_lens_file(root, "security", [base_finding(
                owner="security",
                evidence=["src/app/cart.tsx:42",
                          "Opened https://staging.example.com/checkout at 1280x800"])])
            errors, _, _ = validate(root)
            self.assertTrue(
                any(fid == "PRA-RT-001" and "does not match the file" in msg
                    for fid, msg in messages(errors)))

    def test_non_rt_prefix_in_runtime_file_fails(self):
        with TempRoot() as root:
            write_lens_file(root, "runtime", [base_finding(
                id="PRA-SEC-001", evidence=["src/app/cart.tsx:42"])])
            errors, _, _ = validate(root)
            self.assertTrue(
                any(fid == "PRA-SEC-001" and "does not match the file" in msg
                    for fid, msg in messages(errors)))

    def test_static_confirmed_with_live_only_evidence_still_fails(self):
        with TempRoot() as root:
            write_lens_file(root, "security", [base_finding(
                id="PRA-SEC-001", owner="security",
                evidence=["Opened https://staging.example.com/checkout at 1280x800, "
                          "console shows no errors, cart stays empty after reload"])])
            errors, _, _ = validate(root)
            match = [msg for fid, msg in messages(errors) if fid == "PRA-SEC-001"]
            self.assertTrue(any("must cite file:line" in msg for msg in match))
            self.assertFalse(any("live observation" in msg for msg in match))


class RuntimeDuplicateTests(unittest.TestCase):
    def test_duplicate_across_runtime_and_static_lens_is_flagged(self):
        with TempRoot() as root:
            write_lens_file(root, "frontend", [base_finding(
                id="PRA-FE-001", owner="frontend",
                evidence=["Observed empty cart on /checkout at mobile viewport, "
                          "reload keeps it empty"])])
            write_lens_file(root, "runtime", [base_finding(
                evidence=["Opened https://staging.example.com/checkout?ref=nav "
                          "at 1280x800, observed empty cart after reload"])])
            errors, _, _ = validate(root)
            dupes = [msg for _, _, msg in errors if "same underlying issue" in msg]
            self.assertEqual(len(dupes), 1)
            self.assertIn("live:/checkout", dupes[0])
            owners = [fid for _, fid, msg in errors if "same underlying issue" in msg]
            self.assertIn("PRA-RT-001", owners[0])
            self.assertIn("PRA-FE-001", owners[0])

    def test_different_live_pages_do_not_collide(self):
        with TempRoot() as root:
            write_lens_file(root, "frontend", [base_finding(
                id="PRA-FE-001", owner="frontend",
                evidence=["Observed empty cart on /checkout at mobile viewport"])])
            write_lens_file(root, "runtime", [base_finding(
                evidence=["Opened https://staging.example.com/pricing at 1280x800, "
                          "observed wrong plan total"])])
            errors, _, _ = validate(root)
            self.assertFalse(any("same underlying issue" in msg
                                 for _, _, msg in errors))


if __name__ == "__main__":
    unittest.main()
