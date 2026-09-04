#!/usr/bin/env python3
"""Start the readiness dashboard as soon as an audit begins.

The audit skill also asks the model to start the dashboard, so that the URL
reaches the transcript. This hook exists because that instruction is advice to
a language model, and advice is sometimes skipped. The launcher is idempotent,
so the two paths together start exactly one server.

The hook returns immediately. It never waits for the server to finish starting
and it never fails an audit: a dashboard is a convenience, and the audit is
the work.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import readiness_dashboard as dashboard  # noqa: E402

# Matches the two commands that mean "an audit is now under way": the initial
# state write, and a resume of an existing one.
AUDIT_START = re.compile(r"audit_state\.py.*\b(init|set-stage)\b")


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    command = str((event.get("tool_input") or {}).get("command", ""))
    if not AUDIT_START.search(command):
        return 0

    root = Path(event.get("cwd") or ".")
    try:
        if dashboard.find_running(root) is None:
            dashboard.spawn_detached(root, 0)
    except Exception:
        # A dashboard that cannot start is reported by the audit skill, which
        # has somewhere to say it. A hook has nowhere useful to complain.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
