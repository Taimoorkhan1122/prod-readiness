import json
import subprocess
import sys
from pathlib import Path
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "route_readiness_prompt.py"


def run_router(prompt: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({"prompt": prompt}),
        text=True,
        capture_output=True,
        check=False,
    )


class ReadinessPromptRouterTests(unittest.TestCase):
    def test_routes_ready_for_production_prompt_to_the_audit_skill(self):
        result = run_router("Is this ready for production?")

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("prod-readiness:production-readiness-audit", context)
        self.assertIn("audit_state.py", context)

    def test_leaves_unrelated_prompts_untouched(self):
        result = run_router("Explain this TypeScript error")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
