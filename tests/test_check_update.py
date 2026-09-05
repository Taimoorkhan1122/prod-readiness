import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPT = Path(__file__).parents[1] / "scripts" / "check_update.py"


def run_check(env_extra: dict, cache_dir: Path, plugin_root: Path):
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(cache_dir),
        "XDG_CACHE_HOME": str(cache_dir),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "PYTHONPATH": str(SCRIPT.parent),
    }
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def make_plugin_root(base: Path, version: str) -> Path:
    root = base / "plugin"
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "prod-readiness", "version": version}), encoding="utf-8"
    )
    return root


def write_cache(cache_dir: Path, version: str, age_seconds: float = 0.0) -> Path:
    path = cache_dir / "prod-readiness" / "update-check.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"checked_at": time.time() - age_seconds, "latest_version": version}),
        encoding="utf-8",
    )
    return path


class CheckUpdateTests(unittest.TestCase):
    def test_reports_when_cached_upstream_version_is_newer(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_plugin_root(base, "0.2.0")
            write_cache(base, "0.3.0")

            result = run_check({}, base, root)

            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            payload = output["hookSpecificOutput"]
            self.assertEqual(payload["hookEventName"], "SessionStart")
            self.assertIn("0.3.0", payload["additionalContext"])
            self.assertIn("0.2.0", payload["additionalContext"])
            self.assertIn("claude plugin update prod-readiness", payload["additionalContext"])

    def test_stays_silent_when_versions_match(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_plugin_root(base, "0.2.0")
            write_cache(base, "0.2.0")

            result = run_check({}, base, root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_stays_silent_when_cached_version_is_older(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_plugin_root(base, "0.10.0")
            write_cache(base, "0.9.9")

            result = run_check({}, base, root)

            self.assertEqual(result.stdout.strip(), "")

    def test_opt_out_env_skips_the_check_entirely(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_plugin_root(base, "0.2.0")
            write_cache(base, "9.9.9")

            result = run_check({"PROD_READINESS_NO_UPDATE_CHECK": "1"}, base, root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_network_failure_is_silent(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_plugin_root(base, "0.2.0")
            # No cache and an unroutable upstream: the fetch must fail quietly.
            result = run_check(
                {"http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9"},
                base,
                root,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_expired_cache_is_ignored(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_plugin_root(base, "0.2.0")
            write_cache(base, "9.9.9", age_seconds=48 * 60 * 60)

            result = run_check(
                {"https_proxy": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"},
                base,
                root,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_missing_manifest_is_silent(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "empty"
            root.mkdir()
            write_cache(base, "9.9.9")

            result = run_check({}, base, root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")


class HookRegistrationTests(unittest.TestCase):
    def test_session_start_hook_is_registered(self):
        hooks = json.loads(
            (Path(__file__).parents[1] / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )
        commands = [
            hook["command"]
            for entry in hooks["hooks"]["SessionStart"]
            for hook in entry["hooks"]
        ]
        self.assertTrue(any("check_update.py" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
