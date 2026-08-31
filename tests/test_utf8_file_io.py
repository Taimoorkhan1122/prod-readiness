import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AuditScriptEncodingTests(unittest.TestCase):
    def test_audit_text_io_declares_utf8(self):
        """Windows must not choose its system text encoding for audit files."""
        missing = []

        for path in sorted((ROOT / "scripts").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in {"read_text", "write_text"}:
                    continue
                encoding = next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "encoding"),
                    None,
                )
                if not isinstance(encoding, ast.Constant) or encoding.value != "utf-8":
                    missing.append(f"{path.name}:{node.lineno}")

        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
