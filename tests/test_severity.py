"""
Tests for deterministic severity and the computed go/no-go decision.

Severity used to be a lens's judgement call, so the same gap could be graded
P1 one run and P3 the next. It is now a pure function of four closed factors
(scripts/severity.py). The go/no-go decision was the same kind of problem -
the model wrote verdict.json by hand even though the rule was always
mechanical - so it is now computed from the validated findings too
(scripts/validate_findings.py and scripts/assemble_report.py).

This file covers all three: the rubric in severity.py, the validator's
rejection of hand-set severity and invalid/missing factors, and the three
decision outcomes including the disagreement-with-verdict.json error.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.severity import (
    FACTOR_KEYS,
    FACTORS,
    FactorError,
    compute_severity,
    score_factors,
    severity_for_score,
    validate_factors,
)
from scripts.finding_store import FINDING_SCHEMA, compute_decision, load_lens, normalise_finding, FindingError
from scripts.validate_findings import validate


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def factors(exposure="internal", data_class="none", blast_radius="single-user",
            compensating_control="absent"):
    return {
        "exposure": exposure,
        "data_class": data_class,
        "blast_radius": blast_radius,
        "compensating_control": compensating_control,
    }


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def base_finding(**overrides) -> dict:
    """A finding that passes every validator rule except what a test overrides."""
    finding = {
        "id": "PRA-SEC-001",
        "title": "Tenant identifier is read from the request body on order writes",
        "impact": "Any logged-in customer can read and change another company's orders.",
        "state": "CONFIRMED",
        "owner": "security",
        "cross_lens": [],
        "evidence": ["src/orders/orders.service.ts:88"],
        "probe": None,
        "failure_path": "OrdersController accepts tenantId in the POST body.",
        "compensating": "none found",
        "fix": "Derive tenantId from the authenticated principal.",
        "resolve": None,
        "see": None,
        "factors": factors(exposure="internet", data_class="pii",
                            blast_radius="multi-tenant", compensating_control="absent"),
    }
    finding.update(overrides)
    return finding


def write_lens_file(root: Path, lens: str, findings: list) -> Path:
    path = root / ".readiness-audit" / "findings" / f"{lens}.json"
    write_json(path, {"schema": FINDING_SCHEMA, "lens": lens, "findings": findings})
    return path


def write_ledger(root: Path, controls: dict | None = None) -> None:
    write_json(root / ".readiness-audit" / "evidence" / "absence-ledger.json",
               {"controls": controls or {}})


def write_verdict(root: Path, decision: str | None = None) -> None:
    payload = {}
    if decision is not None:
        payload["decision"] = decision
    write_json(root / ".readiness-audit" / "verdict.json", payload)


class TempRoot:
    """A throwaway .readiness-audit/ tree, with the ledger pre-seeded so
    validate() never fails for reasons unrelated to what a test checks."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write_ledger(self.root)
        return self.root

    def __exit__(self, *exc):
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# rubric: boundaries, subtraction, floor, UNVERIFIED cap
# ---------------------------------------------------------------------------

class SeverityForScoreBoundaryTests(unittest.TestCase):
    """Every threshold, both sides, expressed directly against the totals in
    the spec: total>=8 P0, 6-7 P1, 3-5 P2, <=2 P3."""

    def test_seven_is_p1_not_p0(self):
        self.assertEqual(severity_for_score(7), "P1")

    def test_eight_is_p0(self):
        self.assertEqual(severity_for_score(8), "P0")

    def test_five_is_p2_not_p1(self):
        self.assertEqual(severity_for_score(5), "P2")

    def test_six_is_p1(self):
        self.assertEqual(severity_for_score(6), "P1")

    def test_two_is_p3_not_p2(self):
        self.assertEqual(severity_for_score(2), "P3")

    def test_three_is_p2(self):
        self.assertEqual(severity_for_score(3), "P2")

    def test_zero_is_p3(self):
        self.assertEqual(severity_for_score(0), "P3")

    def test_maximum_possible_total_is_p0(self):
        # internet(3) + secrets/pii/financial(3) + systemic(3) = 9
        self.assertEqual(severity_for_score(9), "P0")


class ScoreFactorsTests(unittest.TestCase):
    def test_sums_the_three_axes(self):
        f = factors(exposure="internet", data_class="secrets",
                     blast_radius="systemic", compensating_control="absent")
        self.assertEqual(score_factors(f), 9)

    def test_compensating_control_present_subtracts_two(self):
        f = factors(exposure="internet", data_class="secrets",
                     blast_radius="systemic", compensating_control="present")
        self.assertEqual(score_factors(f), 7)

    def test_compensating_control_absent_leaves_total_unchanged(self):
        f = factors(exposure="internet", data_class="secrets",
                     blast_radius="systemic", compensating_control="absent")
        self.assertEqual(score_factors(f), 9)

    def test_floor_at_zero(self):
        # local(0) + none(0) + single-user(0), then subtract 2 -> floored to 0
        f = factors(exposure="local", data_class="none",
                     blast_radius="single-user", compensating_control="present")
        self.assertEqual(score_factors(f), 0)

    def test_lowest_score_is_p3(self):
        f = factors(exposure="local", data_class="none",
                     blast_radius="single-user", compensating_control="present")
        self.assertEqual(compute_severity(f, "CONFIRMED"), "P3")

    def test_all_factor_point_tables_agree_with_the_published_rubric(self):
        self.assertEqual(FACTORS["exposure"],
                          {"internet": 3, "authenticated": 2, "internal": 1, "local": 0})
        self.assertEqual(FACTORS["data_class"],
                          {"secrets": 3, "pii": 3, "financial": 3, "business": 1, "none": 0})
        self.assertEqual(FACTORS["blast_radius"],
                          {"systemic": 3, "multi-tenant": 2, "single-tenant": 1, "single-user": 0})
        self.assertEqual(set(FACTOR_KEYS),
                          {"exposure", "data_class", "blast_radius", "compensating_control"})


class ComputeSeverityUnverifiedCapTests(unittest.TestCase):
    """A finding whose evidence state is UNVERIFIED can never be P0."""

    def test_unverified_finding_that_would_score_p0_is_capped_to_p1(self):
        f = factors(exposure="internet", data_class="secrets",
                     blast_radius="systemic", compensating_control="absent")
        self.assertEqual(score_factors(f), 9)  # would be P0 if CONFIRMED
        self.assertEqual(compute_severity(f, "UNVERIFIED"), "P1")

    def test_confirmed_finding_with_the_same_factors_is_p0(self):
        f = factors(exposure="internet", data_class="secrets",
                     blast_radius="systemic", compensating_control="absent")
        self.assertEqual(compute_severity(f, "CONFIRMED"), "P0")

    def test_not_found_finding_is_not_capped(self):
        # the cap is specific to UNVERIFIED; NOT_FOUND is an established absence
        f = factors(exposure="internet", data_class="secrets",
                     blast_radius="systemic", compensating_control="absent")
        self.assertEqual(compute_severity(f, "NOT_FOUND"), "P0")

    def test_unverified_finding_below_p0_is_unaffected(self):
        f = factors(exposure="authenticated", data_class="business",
                     blast_radius="single-tenant", compensating_control="absent")
        self.assertEqual(severity_for_score(score_factors(f)), "P2")
        self.assertEqual(compute_severity(f, "UNVERIFIED"), "P2")

    def test_state_matching_is_case_and_whitespace_insensitive(self):
        f = factors(exposure="internet", data_class="secrets",
                     blast_radius="systemic", compensating_control="absent")
        self.assertEqual(compute_severity(f, " unverified "), "P1")


# ---------------------------------------------------------------------------
# validate_factors: missing factors, each invalid enum value
# ---------------------------------------------------------------------------

class ValidateFactorsTests(unittest.TestCase):
    def test_valid_factors_produce_no_errors(self):
        self.assertEqual(validate_factors(factors()), [])

    def test_missing_factors_object_is_one_error_naming_all_keys(self):
        errors = validate_factors(None)
        self.assertEqual(len(errors), 1)
        for key in FACTOR_KEYS:
            self.assertIn(key, errors[0])

    def test_factors_that_is_not_an_object_is_rejected(self):
        errors = validate_factors("internet")
        self.assertEqual(len(errors), 1)
        self.assertIn("factors", errors[0])

    def test_missing_exposure_key_names_exposure_and_lists_allowed_values(self):
        bad = factors()
        del bad["exposure"]
        errors = validate_factors(bad)
        self.assertEqual(len(errors), 1)
        self.assertIn("factors.exposure", errors[0])
        for value in ("internet", "authenticated", "internal", "local"):
            self.assertIn(value, errors[0])

    def test_invalid_exposure_value_names_the_key_and_the_bad_value(self):
        errors = validate_factors(factors(exposure="offsite"))
        self.assertEqual(len(errors), 1)
        self.assertIn("factors.exposure", errors[0])
        self.assertIn("offsite", errors[0])

    def test_invalid_data_class_value_names_the_key_and_lists_allowed_values(self):
        errors = validate_factors(factors(data_class="health"))
        self.assertEqual(len(errors), 1)
        self.assertIn("factors.data_class", errors[0])
        for value in ("secrets", "pii", "financial", "business", "none"):
            self.assertIn(value, errors[0])

    def test_invalid_blast_radius_value_names_the_key_and_lists_allowed_values(self):
        errors = validate_factors(factors(blast_radius="global"))
        self.assertEqual(len(errors), 1)
        self.assertIn("factors.blast_radius", errors[0])
        for value in ("systemic", "multi-tenant", "single-tenant", "single-user"):
            self.assertIn(value, errors[0])

    def test_invalid_compensating_control_value_names_the_key_and_lists_allowed_values(self):
        errors = validate_factors(factors(compensating_control="partial"))
        self.assertEqual(len(errors), 1)
        self.assertIn("factors.compensating_control", errors[0])
        self.assertIn("present", errors[0])
        self.assertIn("absent", errors[0])

    def test_empty_string_value_is_treated_as_missing(self):
        errors = validate_factors(factors(exposure=""))
        self.assertEqual(len(errors), 1)
        self.assertIn("factors.exposure", errors[0])

    def test_multiple_bad_keys_produce_one_message_each(self):
        errors = validate_factors(factors(exposure="offsite", data_class="health"))
        self.assertEqual(len(errors), 2)

    def test_compute_severity_raises_factor_error_on_invalid_input(self):
        with self.assertRaises(FactorError):
            compute_severity(factors(exposure="offsite"), "CONFIRMED")


# ---------------------------------------------------------------------------
# finding_store.normalise_finding: schema, hand-set severity, factors
# ---------------------------------------------------------------------------

class NormaliseFindingTests(unittest.TestCase):
    def test_valid_finding_derives_severity_from_factors(self):
        finding = normalise_finding(base_finding(), "security")
        # internet(3) + pii(3) + multi-tenant(2) = 8 -> P0
        self.assertEqual(finding["severity"], "P0")
        self.assertEqual(finding["factors"], base_finding()["factors"])

    def test_hand_set_severity_is_rejected(self):
        with self.assertRaises(FindingError) as ctx:
            normalise_finding(base_finding(severity="P0"), "security")
        self.assertIn("severity", str(ctx.exception))

    def test_missing_factors_is_rejected(self):
        raw = base_finding()
        del raw["factors"]
        with self.assertRaises(FindingError) as ctx:
            normalise_finding(raw, "security")
        self.assertIn("factors", str(ctx.exception))

    def test_invalid_factor_enum_is_rejected(self):
        with self.assertRaises(FindingError) as ctx:
            normalise_finding(base_finding(factors=factors(exposure="offsite")), "security")
        self.assertIn("factors.exposure", str(ctx.exception))

    def test_unverified_finding_is_capped_below_p0(self):
        finding = normalise_finding(
            base_finding(state="UNVERIFIED", resolve="check the cloud console"),
            "security",
        )
        self.assertEqual(finding["severity"], "P1")

    def test_load_lens_rejects_pre_derived_severity_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "security.json"
            write_json(path, {
                "schema": 1,
                "lens": "security",
                "findings": [{**base_finding(), "severity": "P0", "factors": None}],
            })
            with self.assertRaises(FindingError) as ctx:
                load_lens(path)
            message = str(ctx.exception)
            self.assertIn("schema", message)
            self.assertIn("re-run", message.lower())

    def test_load_lens_accepts_current_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "security.json"
            write_json(path, {"schema": FINDING_SCHEMA, "lens": "security",
                               "findings": [base_finding()]})
            findings = load_lens(path)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["severity"], "P0")


# ---------------------------------------------------------------------------
# validate_findings.validate(): the same rules, end to end
# ---------------------------------------------------------------------------

class ValidateFindingsIntegrationTests(unittest.TestCase):
    def test_clean_finding_produces_no_errors(self):
        with TempRoot() as root:
            write_lens_file(root, "security", [base_finding()])
            errors, warnings, stats = validate(root)
            self.assertEqual(errors, [])
            self.assertEqual(stats["by_severity"].get("P0"), 1)

    def test_hand_set_severity_is_a_named_error(self):
        with TempRoot() as root:
            write_lens_file(root, "security", [base_finding(severity="P1")])
            errors, _, _ = validate(root)
            messages = [msg for _, _, msg in errors]
            self.assertTrue(any("severity" in m and "must not be set" in m for m in messages))

    def test_missing_factors_is_a_named_error(self):
        with TempRoot() as root:
            raw = base_finding()
            del raw["factors"]
            write_lens_file(root, "security", [raw])
            errors, _, _ = validate(root)
            messages = [msg for _, _, msg in errors]
            self.assertTrue(any("factors" in m for m in messages))

    def test_invalid_enum_value_names_offending_key_and_allowed_values(self):
        with TempRoot() as root:
            write_lens_file(root, "security", [
                base_finding(factors=factors(blast_radius="everywhere"))
            ])
            errors, _, _ = validate(root)
            messages = [msg for _, _, msg in errors]
            match = [m for m in messages if "factors.blast_radius" in m]
            self.assertEqual(len(match), 1)
            for value in ("systemic", "multi-tenant", "single-tenant", "single-user"):
                self.assertIn(value, match[0])

    def test_old_schema_file_fails_with_a_clear_message(self):
        with TempRoot() as root:
            path = root / ".readiness-audit" / "findings" / "security.json"
            write_json(path, {
                "schema": 1, "lens": "security",
                "findings": [{**base_finding(), "severity": "P0"}],
            })
            errors, _, _ = validate(root)
            messages = [msg for _, _, msg in errors]
            self.assertTrue(any("schema" in m and "re-run" in m.lower() for m in messages))

    def test_unverified_finding_reaching_p0_score_is_recorded_as_p1(self):
        with TempRoot() as root:
            write_lens_file(root, "security", [
                base_finding(
                    state="UNVERIFIED",
                    resolve="check the backup provider's console",
                    factors=factors(exposure="internet", data_class="secrets",
                                     blast_radius="systemic", compensating_control="absent"),
                )
            ])
            errors, _, stats = validate(root)
            self.assertEqual(stats["by_severity"].get("P0"), None)
            self.assertEqual(stats["by_severity"].get("P1"), 1)


# ---------------------------------------------------------------------------
# compute_decision: all three outcomes
# ---------------------------------------------------------------------------

class ComputeDecisionTests(unittest.TestCase):
    def test_no_findings_is_ship(self):
        self.assertEqual(compute_decision([]), "SHIP")

    def test_only_p2_and_p3_is_ship(self):
        findings = [{"severity": "P2"}, {"severity": "P3"}]
        self.assertEqual(compute_decision(findings), "SHIP")

    def test_any_p1_with_no_p0_is_fix_then_ship(self):
        findings = [{"severity": "P2"}, {"severity": "P1"}]
        self.assertEqual(compute_decision(findings), "FIX_THEN_SHIP")

    def test_any_p0_is_hold_even_alongside_p1(self):
        findings = [{"severity": "P1"}, {"severity": "P0"}, {"severity": "P3"}]
        self.assertEqual(compute_decision(findings), "HOLD")

    def test_p0_outranks_everything(self):
        findings = [{"severity": "P0"}]
        self.assertEqual(compute_decision(findings), "HOLD")


class ValidateFindingsDecisionCrossCheckTests(unittest.TestCase):
    """verdict.json's decision, when present, must agree with the one
    computed from the validated findings - disagreement is an error, not a
    silent override."""

    def test_agreeing_verdict_produces_no_decision_error(self):
        with TempRoot() as root:
            write_lens_file(root, "security", [base_finding()])  # scores P0
            write_verdict(root, "HOLD")
            errors, _, _ = validate(root)
            messages = [msg for _, _, msg in errors]
            self.assertFalse(any("decision" in m for m in messages))

    def test_disagreeing_verdict_is_a_validation_error(self):
        with TempRoot() as root:
            write_lens_file(root, "security", [base_finding()])  # scores P0 -> HOLD
            write_verdict(root, "SHIP")
            errors, _, _ = validate(root)
            messages = [msg for _, _, msg in errors]
            match = [m for m in messages if "decision" in m]
            self.assertEqual(len(match), 1)
            self.assertIn("SHIP", match[0])
            self.assertIn("HOLD", match[0])

    def test_no_verdict_file_produces_no_decision_error(self):
        with TempRoot() as root:
            write_lens_file(root, "security", [base_finding()])
            errors, _, _ = validate(root)
            messages = [msg for _, _, msg in errors]
            self.assertFalse(any("decision" in m for m in messages))

    def test_fix_then_ship_agreement(self):
        with TempRoot() as root:
            write_lens_file(root, "security", [
                base_finding(id="PRA-SEC-002",
                             factors=factors(exposure="authenticated", data_class="business",
                                              blast_radius="multi-tenant", compensating_control="absent"))
            ])  # 2+1+2=5 -> P2, not enough for FIX_THEN_SHIP; use a P1 instead
            write_lens_file(root, "backend", [
                base_finding(id="PRA-BE-001", owner="backend",
                             factors=factors(exposure="authenticated", data_class="financial",
                                              blast_radius="single-tenant", compensating_control="absent"))
            ])  # 2+3+1=6 -> P1
            write_verdict(root, "FIX_THEN_SHIP")
            errors, _, stats = validate(root)
            messages = [msg for _, _, msg in errors]
            self.assertFalse(any("decision" in m for m in messages))
            self.assertIsNone(stats["by_severity"].get("P0"))
            self.assertEqual(stats["by_severity"].get("P1"), 1)


ASSEMBLE_SCRIPT = Path(__file__).parents[1] / "scripts" / "assemble_report.py"


def run_assemble(root: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ASSEMBLE_SCRIPT), str(root), *extra_args],
        text=True, capture_output=True, check=False,
    )


class AssembleReportDecisionTests(unittest.TestCase):
    """End to end: the report's decision line is computed from the findings,
    never copied verbatim from verdict.json."""

    def test_hold_is_rendered_for_a_p0_finding(self):
        with TempRoot() as root:
            write_lens_file(root, "security", [base_finding()])  # scores P0
            result = run_assemble(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = (root / ".readiness-audit" / "report.md").read_text(encoding="utf-8")
            self.assertIn("HOLD", report)

    def test_ship_is_rendered_when_no_p0_or_p1_findings_exist(self):
        with TempRoot() as root:
            write_lens_file(root, "security", [
                base_finding(factors=factors(exposure="local", data_class="none",
                                              blast_radius="single-user",
                                              compensating_control="present"))
            ])  # scores P3
            result = run_assemble(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = (root / ".readiness-audit" / "report.md").read_text(encoding="utf-8")
            self.assertIn("**SHIP**", report)

    def test_disagreeing_verdict_blocks_assembly_without_force(self):
        with TempRoot() as root:
            write_lens_file(root, "security", [base_finding()])  # scores P0 -> HOLD
            write_verdict(root, "SHIP")
            result = run_assemble(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / ".readiness-audit" / "report.md").exists())

    def test_disagreeing_verdict_with_force_still_uses_the_computed_decision(self):
        with TempRoot() as root:
            write_lens_file(root, "security", [base_finding()])  # scores P0 -> HOLD
            write_verdict(root, "SHIP")
            result = run_assemble(root, "--force")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = (root / ".readiness-audit" / "report.md").read_text(encoding="utf-8")
            self.assertIn("HOLD", report)
            self.assertNotIn("**SHIP**", report)


if __name__ == "__main__":
    unittest.main()
