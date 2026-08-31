import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "audit_state.py"


def initialize(mode: str | None = None) -> tuple[subprocess.CompletedProcess[str], Path]:
    root = Path(tempfile.mkdtemp())
    command = [sys.executable, str(SCRIPT), "init", str(root)]
    if mode:
        command.extend(["--execution-mode", mode])
    return (
        subprocess.run(command, text=True, capture_output=True, check=False),
        root,
    )


class AuditStateExecutionModeTests(unittest.TestCase):
    def test_init_defaults_to_parallel_execution(self):
        result, root = initialize()

        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((root / ".readiness-audit" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["execution_mode"], "parallel")

    def test_init_persists_requested_sequential_execution(self):
        result, root = initialize("sequential")

        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((root / ".readiness-audit" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["execution_mode"], "sequential")


if __name__ == "__main__":
    unittest.main()
