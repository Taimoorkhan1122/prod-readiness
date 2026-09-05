---
name: production-readiness-dashboard
disable-model-invocation: true
description: Watch a production-readiness audit in a local browser dashboard.
---

# Production readiness dashboard

Use this optional, read-only, local-only dashboard to watch an existing audit.
It does not interrupt, change, or rerun the audit.

Run `/prod-readiness:production-readiness-dashboard` from the project being
audited.

Run this command with `<target-project-root>` replaced by the open project
path:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/readiness_dashboard.py" <target-project-root>
```

The command prints a local URL such as `http://127.0.0.1:<port>/` and exits. It
opens that URL in a browser where one is available; open it yourself if not.

The server runs detached, so it stays up after the session that started it and
Ctrl-C does not reach it. It closes after an hour with no reader. To close it
now:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/readiness_dashboard.py" <target-project-root> --stop
```

Running the command again while a dashboard is already serving that project
reuses the running server instead of starting a second one.
