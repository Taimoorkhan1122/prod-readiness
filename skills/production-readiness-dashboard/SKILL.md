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

The server prints a local URL such as `http://127.0.0.1:<port>/`. Open that URL
if the browser does not open automatically. Press Ctrl-C to stop a manually
launched server.
