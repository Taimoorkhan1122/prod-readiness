import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.export_report import export, latex_escape
from scripts.finding_store import FINDING_SCHEMA

# Severity is derived from `factors` (see scripts/severity.py), not authored
# directly, so each nominal severity used by these fixtures maps to a rubric
# combination that scores into that band. Values chosen comfortably inside
# each band's threshold so an unrelated change to the rubric's boundaries is
# unlikely to shift these into a different severity.
_SEVERITY_FACTORS = {
    "P0": {"exposure": "internet", "data_class": "secrets",
           "blast_radius": "systemic", "compensating_control": "absent"},
    "P1": {"exposure": "authenticated", "data_class": "pii",
           "blast_radius": "multi-tenant", "compensating_control": "absent"},
    "P2": {"exposure": "internal", "data_class": "business",
           "blast_radius": "single-tenant", "compensating_control": "absent"},
    "P3": {"exposure": "local", "data_class": "none",
           "blast_radius": "single-user", "compensating_control": "absent"},
}


def _write_state(audit: Path, **overrides):
    state = {"stage": "3-lenses", "stage_status": "in_progress",
              "lenses_to_run": [], "lenses_skipped": {}}
    state.update(overrides)
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _write_findings(audit: Path, lens: str, findings: list[dict]):
    (audit / "findings").mkdir(parents=True, exist_ok=True)
    (audit / "findings" / f"{lens}.json").write_text(
        json.dumps({"schema": FINDING_SCHEMA, "lens": lens, "findings": findings}),
        encoding="utf-8")


def _finding(**overrides):
    severity = overrides.pop("severity", "P0")
    base = {
        "id": "PRA-SEC-001",
        "title": "Tenant id is read from the request body",
        "impact": "Any logged-in customer can read another company's orders.",
        "state": "CONFIRMED",
        "factors": dict(_SEVERITY_FACTORS[severity]),
        "evidence": ["src/orders/orders_service.ts:88"],
        "failure_path": "The controller trusts a client-supplied tenant id.",
        "compensating": "none found",
        "fix": "Derive the tenant from the session.",
    }
    base.update(overrides)
    return base


def _no_compiler():
    """Force the "no TeX compiler on PATH" branch, so tests never depend on
    whatever happens to be installed on the machine running them."""
    return mock.patch("scripts.export_report._find_tex_compiler", return_value=None)


class LatexEscapeTests(unittest.TestCase):
    """Every character LaTeX treats as syntax must come out escaped. A
    mangled file path in a security finding is worse than no finding, so
    these are exact-match, one character at a time."""

    def test_none_and_empty_string_escape_to_empty_string(self):
        self.assertEqual(latex_escape(None), "")
        self.assertEqual(latex_escape(""), "")

    def test_backslash(self):
        self.assertEqual(latex_escape("\\"), r"\textbackslash{}")

    def test_open_brace(self):
        self.assertEqual(latex_escape("{"), r"\{")

    def test_close_brace(self):
        self.assertEqual(latex_escape("}"), r"\}")

    def test_dollar(self):
        self.assertEqual(latex_escape("$"), r"\$")

    def test_percent(self):
        self.assertEqual(latex_escape("%"), r"\%")

    def test_ampersand(self):
        self.assertEqual(latex_escape("&"), r"\&")

    def test_hash(self):
        self.assertEqual(latex_escape("#"), r"\#")

    def test_underscore(self):
        self.assertEqual(latex_escape("_"), r"\_")

    def test_caret(self):
        self.assertEqual(latex_escape("^"), r"\textasciicircum{}")

    def test_tilde(self):
        self.assertEqual(latex_escape("~"), r"\textasciitilde{}")

    def test_file_path_with_underscores_survives_intact(self):
        self.assertEqual(
            latex_escape("src/orders_service/handler_v2.py"),
            r"src/orders\_service/handler\_v2.py")

    def test_every_special_character_together_in_one_string(self):
        raw = "100% & $5 #1 ~caret^ back\\slash {brace}_underscore"
        expected = (
            r"100\% \& \$5 \#1 \textasciitilde{}caret\textasciicircum{} "
            r"back\textbackslash{}slash \{brace\}\_underscore"
        )
        self.assertEqual(latex_escape(raw), expected)

    def test_plain_text_with_no_special_characters_is_unchanged(self):
        self.assertEqual(latex_escape("nothing special here"), "nothing special here")

    def test_non_string_values_are_stringified_first(self):
        self.assertEqual(latex_escape(42), "42")


class ExportOutputTests(unittest.TestCase):
    def _root(self):
        return Path(tempfile.mkdtemp())

    def test_missing_readiness_audit_directory_does_not_crash(self):
        root = self._root()
        self.assertFalse((root / ".readiness-audit").exists())

        with _no_compiler():
            out_dir = export(root)

        self.assertTrue(out_dir.is_dir())
        self.assertTrue((out_dir / "report.tex").exists())
        for lens in ("security", "backend", "frontend", "devops", "qa", "database", "ai-security"):
            self.assertTrue((out_dir / f"report-{lens}.tex").exists())
        # No report.md source existed, so none should have been copied.
        self.assertFalse((out_dir / "report.md").exists())
        # No compiler was available, so no PDF either - and that must not
        # have been treated as an error.
        self.assertFalse((out_dir / "report.pdf").exists())

    def test_missing_directory_output_is_placed_under_readiness_audit_export(self):
        root = self._root()

        with _no_compiler():
            out_dir = export(root)

        self.assertEqual(out_dir.parent.parent, root.resolve() / ".readiness-audit")
        self.assertEqual(out_dir.parent.name, "export")
        self.assertTrue(out_dir.name.startswith("nogit-") or "-" in out_dir.name)

    def test_output_directory_uses_short_git_ref_and_utc_timestamp(self):
        root = self._root()

        with _no_compiler():
            out_dir = export(root)

        # This repo checkout is not a git repository under a bare temp dir,
        # so the ref must fall back to "nogit", and the timestamp must be
        # 8 digits, "T", 6 digits, "Z".
        ref, _, timestamp = out_dir.name.partition("-")
        self.assertEqual(ref, "nogit")
        self.assertRegex(timestamp, r"^\d{8}T\d{6}Z(-\d+)?$")

    def test_two_exports_never_collide_or_overwrite(self):
        root = self._root()

        with _no_compiler():
            first = export(root)
            second = export(root)

        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        self.assertTrue((first / "report.tex").exists())
        self.assertTrue((second / "report.tex").exists())

    def test_report_md_is_copied_when_present(self):
        root = self._root()
        audit = root / ".readiness-audit"
        _write_state(audit, stage_status="complete")
        (audit / "report.md").write_text("# Production Readiness Audit\n", encoding="utf-8")

        with _no_compiler():
            out_dir = export(root)

        self.assertEqual((out_dir / "report.md").read_text(encoding="utf-8"),
                         "# Production Readiness Audit\n")

    def test_no_tex_compiler_present_exits_cleanly_and_keeps_tex_files(self):
        root = self._root()
        audit = root / ".readiness-audit"
        _write_state(audit, stage_status="complete")

        with _no_compiler(), mock.patch("builtins.print") as printed:
            out_dir = export(root)

        self.assertTrue((out_dir / "report.tex").exists())
        self.assertFalse((out_dir / "report.pdf").exists())
        # Exactly one explanatory line, telling the user how to compile it
        # themselves - missing TeX is never treated as an error.
        messages = [call.args[0] for call in printed.call_args_list]
        self.assertEqual(len(messages), 1)
        self.assertIn("tectonic", messages[0])
        self.assertIn("pdflatex", messages[0])
        self.assertIn(str(out_dir), messages[0])

    def test_compiler_is_invoked_when_one_is_found_on_path(self):
        root = self._root()
        _write_state(root / ".readiness-audit", stage_status="complete")

        with mock.patch("scripts.export_report._find_tex_compiler",
                        return_value=("tectonic", "/usr/bin/tectonic")), \
             mock.patch("scripts.export_report._compile_pdf", return_value=True) as compiled:
            export(root)

        compiled.assert_called_once()
        self.assertEqual(compiled.call_args[0][0], ("tectonic", "/usr/bin/tectonic"))


class IncompleteAuditBannerTests(unittest.TestCase):
    def _root(self):
        return Path(tempfile.mkdtemp())

    def test_incomplete_audit_carries_a_banner_naming_the_stage(self):
        root = self._root()
        audit = root / ".readiness-audit"
        _write_state(audit, stage="3-lenses", stage_status="in_progress",
                     lenses_to_run=["security", "backend"])
        _write_findings(audit, "security", [_finding()])

        with _no_compiler():
            out_dir = export(root)

        text = (out_dir / "report.tex").read_text(encoding="utf-8")
        self.assertIn("AUDIT INCOMPLETE", text)
        self.assertIn("3-lenses", text)
        # Backend is running/waiting and every other unrun lens has not
        # reported; the banner must name them.
        self.assertIn("Frontend", text)
        self.assertIn("Backend", text)

    def test_complete_audit_carries_no_incomplete_banner(self):
        root = self._root()
        audit = root / ".readiness-audit"
        _write_state(audit, stage="5-report", stage_status="complete")
        for lens in ("security", "backend", "frontend", "devops", "qa", "database", "ai-security"):
            _write_findings(audit, lens, [])

        with _no_compiler():
            out_dir = export(root)

        text = (out_dir / "report.tex").read_text(encoding="utf-8")
        self.assertNotIn("AUDIT INCOMPLETE", text)

    def test_incomplete_banner_appears_on_every_per_lens_document_too(self):
        """An incomplete audit must never look complete, no matter which
        document a reviewer opens."""
        root = self._root()
        _write_state(root / ".readiness-audit", stage="2-evidence", stage_status="in_progress")

        with _no_compiler():
            out_dir = export(root)

        for lens in ("security", "backend", "frontend", "devops", "qa", "database", "ai-security"):
            text = (out_dir / f"report-{lens}.tex").read_text(encoding="utf-8")
            self.assertIn("AUDIT INCOMPLETE", text, f"missing banner in report-{lens}.tex")


class PerLensDocumentTests(unittest.TestCase):
    def _root(self):
        return Path(tempfile.mkdtemp())

    def test_per_lens_document_contains_only_that_lens_findings(self):
        root = self._root()
        audit = root / ".readiness-audit"
        _write_state(audit, stage_status="complete")
        _write_findings(audit, "security", [
            _finding(id="PRA-SEC-001", title="Security finding one")])
        _write_findings(audit, "backend", [
            _finding(id="PRA-BE-001", title="Backend finding one", severity="P1")])

        with _no_compiler():
            out_dir = export(root)

        security_doc = (out_dir / "report-security.tex").read_text(encoding="utf-8")
        backend_doc = (out_dir / "report-backend.tex").read_text(encoding="utf-8")

        self.assertIn("PRA-SEC-001", security_doc)
        self.assertNotIn("PRA-BE-001", security_doc)

        self.assertIn("PRA-BE-001", backend_doc)
        self.assertNotIn("PRA-SEC-001", backend_doc)

    def test_combined_document_contains_every_lens_finding(self):
        root = self._root()
        audit = root / ".readiness-audit"
        _write_state(audit, stage_status="complete")
        _write_findings(audit, "security", [_finding(id="PRA-SEC-001")])
        _write_findings(audit, "database", [
            _finding(id="PRA-DB-001", title="Missing PITR", severity="P0")])

        with _no_compiler():
            out_dir = export(root)

        combined = (out_dir / "report.tex").read_text(encoding="utf-8")
        self.assertIn("PRA-SEC-001", combined)
        self.assertIn("PRA-DB-001", combined)

    def test_combined_document_appendix_lists_every_unverified_finding(self):
        root = self._root()
        audit = root / ".readiness-audit"
        _write_state(audit, stage_status="complete")
        _write_findings(audit, "security", [
            _finding(id="PRA-SEC-002", state="UNVERIFIED", resolve="Check the console.")])
        _write_findings(audit, "qa", [
            _finding(id="PRA-QA-001", title="Untested checkout path",
                    state="UNVERIFIED", severity="P1", resolve="Run the checkout suite.")])

        with _no_compiler():
            out_dir = export(root)

        combined = (out_dir / "report.tex").read_text(encoding="utf-8")
        appendix = combined.split(r"\appendix", 1)[1]
        self.assertIn("PRA-SEC-002", appendix)
        self.assertIn("PRA-QA-001", appendix)

    def test_evidence_list_items_render_in_a_verbatim_block_not_escaped(self):
        root = self._root()
        audit = root / ".readiness-audit"
        _write_state(audit, stage_status="complete")
        _write_findings(audit, "security", [
            _finding(id="PRA-SEC-003", evidence=["src/db_client.py:42 -> query % 100"])])

        with _no_compiler():
            out_dir = export(root)

        combined = (out_dir / "report.tex").read_text(encoding="utf-8")
        self.assertIn(r"\begin{verbatim}", combined)
        # Verbatim content is not escaped: the raw evidence string must
        # appear untouched, backslash-percent-and-all.
        self.assertIn("src/db_client.py:42 -> query % 100", combined)


if __name__ == "__main__":
    unittest.main()
