#!/usr/bin/env python3
"""Inject the production-readiness workflow for matching Claude Code prompts."""

import json
import re
import sys


READINESS_REQUEST = re.compile(
    r"(?:\bready\s+for\s+production\b|\bproduction[- ]readiness\b|"
    r"\b(?:audit|review)\b.*\b(?:before\s+(?:launch|ship|deploy)|production)\b|"
    r"\bwhat(?:'s|\s+is)\s+missing\s+before\s+(?:we\s+)?ship\b|"
    r"\b(?:safe|ready)\s+to\s+deploy\b|\bgo[- ]?no[- ]?go\b|"
    r"\bwhat\s+breaks\s+at\s+10x\b)",
    re.IGNORECASE,
)

ROUTING_CONTEXT = """Production-readiness request detected. You MUST invoke and
follow the `prod-readiness:production-readiness-audit` skill before performing
any generic repository exploration. Do not substitute ad-hoc code review,
typecheck, test, or build commands for the audit. Start with the skill's
preflight using `${CLAUDE_PLUGIN_ROOT}/scripts/audit_state.py`, create or resume
the `.readiness-audit/` trail, and follow its staged evidence and lens workflow.
The audit is read-only except for `.readiness-audit/`."""


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    if not READINESS_REQUEST.search(str(event.get("prompt", ""))):
        return 0

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": ROUTING_CONTEXT,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
