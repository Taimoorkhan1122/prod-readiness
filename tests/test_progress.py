import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.progress import (
    LENSES,
    PHASES,
    SILENCE_THRESHOLD_SECONDS,
    append_note,
    read_progress,
)

SCRIPT = Path(__file__).parents[1] / "scripts" / "progress.py"


def make_root() -> Path:
    return Path(tempfile.mkdtemp())


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def progress_file(root: Path, lens: str) -> Path:
    return root / ".readiness-audit" / "progress" / f"{lens}.jsonl"


class NoteCommandTests(unittest.TestCase):
    def test_valid_note_is_appended_and_readable(self):
        root = make_root()

        result = run_cli("note", str(root), "security", "started", "reading auth code")

        self.assertEqual(result.returncode, 0, result.stderr)
        path = progress_file(root, "security")
        self.assertTrue(path.exists())
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0])
        self.assertEqual(event["lens"], "security")
        self.assertEqual(event["phase"], "started")
        self.assertEqual(event["note"], "reading auth code")
        self.assertIn("ts", event)

        snapshot = read_progress(root)
        self.assertEqual(snapshot["security"]["latest_phase"], "started")
        self.assertEqual(len(snapshot["security"]["events"]), 1)

    def test_note_is_optional(self):
        root = make_root()

        result = run_cli("note", str(root), "backend", "done")

        self.assertEqual(result.returncode, 0, result.stderr)
        event = json.loads(progress_file(root, "backend").read_text(encoding="utf-8").strip())
        self.assertIsNone(event["note"])

    def test_unknown_lens_is_rejected_with_nonzero_exit(self):
        root = make_root()

        result = run_cli("note", str(root), "not-a-lens", "started")

        self.assertNotEqual(result.returncode, 0)
        for lens in LENSES:
            self.assertIn(lens, result.stderr)
        self.assertFalse(progress_file(root, "not-a-lens").exists())

    def test_unknown_phase_is_rejected_with_nonzero_exit(self):
        root = make_root()

        result = run_cli("note", str(root), "security", "not-a-phase")

        self.assertNotEqual(result.returncode, 0)
        for phase in PHASES:
            self.assertIn(phase, result.stderr)
        self.assertFalse(progress_file(root, "security").exists())

    def test_rejected_note_raises_value_error_via_import(self):
        root = make_root()

        with self.assertRaises(ValueError):
            append_note(root, "security", "bogus-phase", None)
        with self.assertRaises(ValueError):
            append_note(root, "bogus-lens", "started", None)


class TwoLensesAppendingTests(unittest.TestCase):
    def test_two_lenses_do_not_interfere(self):
        root = make_root()

        append_note(root, "security", "started", "sec note one")
        append_note(root, "database", "started", "db note one")
        append_note(root, "security", "analyzing", "sec note two")
        append_note(root, "database", "done", "db note two")

        sec_lines = progress_file(root, "security").read_text(encoding="utf-8").splitlines()
        db_lines = progress_file(root, "database").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(sec_lines), 2)
        self.assertEqual(len(db_lines), 2)

        snapshot = read_progress(root)
        self.assertEqual([e["phase"] for e in snapshot["security"]["events"]], ["started", "analyzing"])
        self.assertEqual([e["phase"] for e in snapshot["database"]["events"]], ["started", "done"])
        self.assertTrue(all(e["lens"] == "security" for e in snapshot["security"]["events"]))
        self.assertTrue(all(e["lens"] == "database" for e in snapshot["database"]["events"]))


class ReadProgressTests(unittest.TestCase):
    def test_lens_with_zero_events_has_no_fabricated_progress(self):
        root = make_root()

        snapshot = read_progress(root)

        self.assertEqual(set(snapshot.keys()), set(LENSES))
        for lens in LENSES:
            entry = snapshot[lens]
            self.assertEqual(entry["events"], [])
            self.assertIsNone(entry["latest_phase"])
            self.assertIsNone(entry["latest_ts"])
            self.assertIsNone(entry["seconds_since_latest"])
            self.assertEqual(entry["signal"], "no-signal")

    def test_recent_event_is_not_no_signal(self):
        root = make_root()
        append_note(root, "qa", "analyzing", None)

        snapshot = read_progress(root)

        self.assertNotEqual(snapshot["qa"]["signal"], "no-signal")
        self.assertLess(snapshot["qa"]["seconds_since_latest"], 5)

    def test_stale_event_below_done_reports_no_signal(self):
        root = make_root()
        path = progress_file(root, "frontend")
        path.parent.mkdir(parents=True, exist_ok=True)
        stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=SILENCE_THRESHOLD_SECONDS + 30)).isoformat()
        event = {"ts": stale_ts, "lens": "frontend", "phase": "analyzing", "note": None}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

        snapshot = read_progress(root)

        self.assertEqual(snapshot["frontend"]["signal"], "no-signal")
        self.assertGreater(snapshot["frontend"]["seconds_since_latest"], SILENCE_THRESHOLD_SECONDS)

    def test_stale_done_event_is_not_no_signal(self):
        root = make_root()
        path = progress_file(root, "devops")
        path.parent.mkdir(parents=True, exist_ok=True)
        stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=SILENCE_THRESHOLD_SECONDS + 30)).isoformat()
        event = {"ts": stale_ts, "lens": "devops", "phase": "done", "note": None}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

        snapshot = read_progress(root)

        self.assertEqual(snapshot["devops"]["latest_phase"], "done")
        self.assertNotEqual(snapshot["devops"]["signal"], "no-signal")

    def test_corrupt_line_is_skipped_not_fatal(self):
        root = make_root()
        append_note(root, "ai-security", "started", "first")
        path = progress_file(root, "ai-security")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("{not valid json\n")
            fh.write("\n")
        append_note(root, "ai-security", "done", "last")

        snapshot = read_progress(root)

        phases = [e["phase"] for e in snapshot["ai-security"]["events"]]
        self.assertEqual(phases, ["started", "done"])
        self.assertEqual(snapshot["ai-security"]["latest_phase"], "done")


class ReadCommandTests(unittest.TestCase):
    def test_read_command_prints_json_for_every_lens(self):
        root = make_root()
        append_note(root, "security", "started", None)

        result = run_cli("read", str(root))

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload.keys()), set(LENSES))
        self.assertEqual(payload["security"]["latest_phase"], "started")
        self.assertEqual(payload["backend"]["events"], [])


if __name__ == "__main__":
    unittest.main()
