import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class DashboardSkillDocumentationTests(unittest.TestCase):
    def test_manual_dashboard_skill_uses_python_loopback_server(self):
        skill = (ROOT / "skills/production-readiness-dashboard/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("/prod-readiness:production-readiness-dashboard", skill)
        self.assertIn("readiness_dashboard.py", skill)
        self.assertIn("127.0.0.1", skill)
        self.assertIn("read-only", skill.lower())

    def test_audit_starts_dashboard_without_changing_parallel_default(self):
        audit_skill = (ROOT / "skills/production-readiness-audit/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("readiness_dashboard.py", audit_skill)
        self.assertIn("managed background Bash task", audit_skill)
        self.assertIn("Parallel is the default", audit_skill)
        self.assertIn("non-fatal", audit_skill)

    def test_readme_explains_automatic_and_manual_dashboard_use(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("production-readiness-dashboard", readme)
        self.assertIn("automatically", readme.lower())
        self.assertIn("127.0.0.1", readme)
