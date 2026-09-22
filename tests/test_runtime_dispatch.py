"""
Tests for runtime lens dispatch wiring (issue #21).

The runtime lens runs in a third wave when a live URL is present, and is
skipped with a recorded reason when there is not. Skipping it keeps the
verdict valid, the dashboard shows it like every other lens, and its
findings file uses the RT prefix.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import audit_state
from scripts import progress
from scripts.finding_store import FINDING_SCHEMA, build_report
from scripts.validate_findings import validate

ROOT = Path(__file__).parents[1]
AUDIT_STATE_SCRIPT = ROOT / "scripts" / "audit_state.py"

CHECKPOINTS = ["started", "evidence-read", "analyzing", "writing-findings", "done"]


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def make_root() -> Path:
    return Path(tempfile.mkdtemp())


def write_ledger(root: Path) -> None:
    write_json(root / ".readiness-audit" / "evidence" / "absence-ledger.json",
               {"controls": {}})


def write_state(root: Path, **overrides) -> None:
    state = {"stage": "3-lenses", "stage_status": "in_progress",
             "lenses_to_run": ["security", "runtime"],
             "lenses_skipped": {}}
    state.update(overrides)
    write_json(root / ".readiness-audit" / "state.json", state)


def runtime_finding(**overrides) -> dict:
    finding = {
        "id": "PRA-RT-001",
        "title": "Checkout page shows an empty cart after reload",
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
        "factors": {"exposure": "local", "data_class": "none",
                    "blast_radius": "single-user", "compensating_control": "absent"},
    }
    finding.update(overrides)
    return finding


class RuntimeInLensListsTests(unittest.TestCase):
    def test_runtime_in_audit_state_lenses(self):
        self.assertIn("runtime", audit_state.LENSES)

    def test_runtime_in_progress_lenses(self):
        self.assertIn("runtime", progress.LENSES)

    def test_progress_phases_unchanged(self):
        self.assertEqual(progress.PHASES,
                         ["started", "evidence-read", "analyzing",
                          "writing-findings", "done"])

    def test_progress_accepts_all_runtime_checkpoints(self):
        root = make_root()
        for phase in CHECKPOINTS:
            event = progress.append_note(root, "runtime", phase, f"note for {phase}")
            self.assertEqual(event["lens"], "runtime")
            self.assertEqual(event["phase"], phase)
        snapshot = progress.read_progress(root)
        self.assertEqual([e["phase"] for e in snapshot["runtime"]["events"]],
                         CHECKPOINTS)


class RuntimeSkipTests(unittest.TestCase):
    def test_set_lenses_skip_runtime_records_reason(self):
        root = make_root()
        init = subprocess.run(
            [sys.executable, str(AUDIT_STATE_SCRIPT), "init", str(root)],
            text=True, capture_output=True, check=False)
        self.assertEqual(init.returncode, 0, init.stderr)
        skip = subprocess.run(
            [sys.executable, str(AUDIT_STATE_SCRIPT), "set-lenses", str(root),
             "--run", "security,backend,database,devops,qa,frontend,ai-security",
             "--skip", "runtime=no live URL provided at intake"],
            text=True, capture_output=True, check=False)
        self.assertEqual(skip.returncode, 0, skip.stderr)
        state = json.loads((root / ".readiness-audit" / "state.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(state["lenses_skipped"]["runtime"],
                         "no live URL provided at intake")

    def test_skip_with_reason_keeps_verdict_valid(self):
        root = make_root()
        write_ledger(root)
        write_state(root, stage="5-report", stage_status="complete",
                    lenses_to_run=["security"],
                    lenses_skipped={"runtime": "no live URL provided at intake"})
        write_json(root / ".readiness-audit" / "verdict.json",
                   {"headline": "No blockers found in the reviewed scope.",
                    "summary": "The runtime lens was skipped because no live URL "
                               "was provided at intake."})
        write_json(root / ".readiness-audit" / "findings" / "security.json",
                   {"schema": FINDING_SCHEMA, "lens": "security", "findings": [
                       {"id": "PRA-SEC-001",
                        "title": "Health check endpoint is not authenticated",
                        "impact": "Anyone on the internet can see if the service is up.",
                        "state": "CONFIRMED",
                        "owner": "security",
                        "cross_lens": [],
                        "evidence": ["src/app/health.ts:12"],
                        "probe": None,
                        "failure_path": None,
                        "compensating": None,
                        "fix": "Remove the endpoint or put it behind the network gate.",
                        "resolve": None,
                        "see": None,
                        "factors": {"exposure": "local", "data_class": "none",
                                    "blast_radius": "single-user",
                                    "compensating_control": "absent"}}]})

        errors, _, _ = validate(root)
        self.assertEqual(errors, [])

        report = build_report(root)
        by_id = {lens["id"]: lens for lens in report["lenses"]}
        self.assertEqual(by_id["runtime"]["status"], "skipped")
        self.assertEqual(by_id["runtime"]["skippedReason"],
                         "no live URL provided at intake")
        self.assertEqual(report["verdict"]["decision"], "SHIP")
        self.assertEqual(report["errors"], [])


class RuntimeAgentFileTests(unittest.TestCase):
    def _text(self) -> str:
        return (ROOT / "agents" / "lens-runtime.md").read_text(encoding="utf-8")

    def test_agent_file_exists(self):
        self.assertTrue((ROOT / "agents" / "lens-runtime.md").exists())

    def test_agent_file_has_all_five_checkpoints_for_runtime(self):
        text = self._text()
        for phase in CHECKPOINTS:
            self.assertIn(f"`{phase}`", text)
        self.assertIn("note <root> runtime <phase>", text)

    def test_agent_file_uses_rt_prefix_and_findings_path(self):
        text = self._text()
        self.assertIn("PRA-RT-001", text)
        self.assertIn(".readiness-audit/findings/runtime.json", text)

    def test_agent_file_requires_reading_static_waves_and_see_refs(self):
        text = self._text()
        self.assertIn("see:", text)
        self.assertIn("wave 1", text.lower())
        self.assertIn("wave 2", text.lower())


class RuntimeFindingsValidationTests(unittest.TestCase):
    def test_sample_runtime_findings_pass_validation(self):
        root = make_root()
        write_ledger(root)
        write_json(root / ".readiness-audit" / "findings" / "runtime.json",
                   {"schema": FINDING_SCHEMA, "lens": "runtime",
                    "findings": [runtime_finding()]})

        errors, _, stats = validate(root)
        self.assertEqual(errors, [])
        self.assertEqual(stats["by_lens"].get("runtime"), 1)


if __name__ == "__main__":
    unittest.main()
