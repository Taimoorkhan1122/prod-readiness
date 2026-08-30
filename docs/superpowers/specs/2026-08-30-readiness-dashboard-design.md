# Production Readiness Dashboard Design

## Goal

Add an optional, local-only dashboard that lets a Claude Code user follow a
running production-readiness audit and review its completed artifacts without
interrupting the audit conversation.

## Scope

The first implementation is for Claude Code. The production-readiness audit
starts the dashboard automatically during preflight. Any other agent can start
the same dashboard manually if it can run Python 3 and has a
`.readiness-audit/` directory.

The dashboard is read-only. It reads only `.readiness-audit/` in the target
project and never sends audit data across the network, modifies the project,
or invokes a lens agent.

## Runtime

`scripts/readiness_dashboard.py` is a Python 3 standard-library HTTP server.

- It binds exclusively to `127.0.0.1`.
- Port `0` is the default, so the operating system chooses a free ephemeral
  port.
- It prints one exact local URL such as `http://127.0.0.1:43123/` to standard
  output before serving requests.
- It serves a self-contained HTML, CSS, and JavaScript client at `/` and a
  JSON snapshot at `/api/snapshot`.
- It reads the target path supplied on the command line. It rejects an audit
  root that does not exist rather than falling back to the server's working
  directory.
- It supports a manual foreground stop with Ctrl-C. Claude starts it as a
  managed background Bash task, so its normal lifetime is the active Claude
  session and post-audit review.

No Node runtime, package manager, file-watcher library, or external web asset
is required.

## Data contract

The server normalizes the audit directory into one snapshot. Individual files
may appear while the audit is running, so every field is optional and missing
data has an explicit state instead of a fabricated value.

```json
{
  "status": "running | complete | unavailable",
  "auditRoot": "/absolute/path/.readiness-audit",
  "updatedAt": "ISO-8601 timestamp or null",
  "stage": { "name": "3-lenses", "status": "in_progress", "note": null },
  "executionMode": "parallel | sequential | null",
  "lenses": [
    { "id": "security", "label": "Security", "status": "complete | running | waiting | skipped", "findingPath": "findings/security.md" }
  ],
  "summary": { "p0": 0, "p1": 0, "p2": 0, "unverified": 0, "verdict": null },
  "artifacts": {
    "report": { "available": false, "content": null },
    "findings": [],
    "evidenceLedger": { "available": false, "content": null }
  },
  "message": "Human-readable empty or partial-state guidance"
}
```

`state.json` provides the stage, execution mode, selected lenses, and skipped
lenses. Finding files and `report.md` are read as text. The server derives
only lightweight counts from finding headings/severity labels; it never
reclassifies severity or generates a verdict. When a report exists, its text
is the authoritative displayed report.

## User experience

The single-page dashboard has four stable destinations:

1. **Overview** is the Audit Story. During a run it shows an aligned stage
   timeline, current stage, lens progress, latest evidence signal, and a clear
   statement of whether the user should wait or make a release decision. On a
   completed audit it foregrounds the verdict and first blocker.
2. **Findings** presents the available findings as an ordered triage list.
3. **Evidence** opens a contextual right-side drawer for the evidence ledger.
4. **Report** renders the audit's `report.md` inside the dashboard.

Lens cards open an agent-run detail view. Each detail view identifies the lens,
its state, and the paths to its available output. The top navigation remains
available on every destination; there is no prototype comparison switcher and
no "Back to dashboard" control.

The browser fetches `/api/snapshot` every two seconds while `status` is
`running`. It stops polling after `complete` or `unavailable`. If a file is
missing or in the middle of being written, the view keeps its last valid
snapshot and describes the output as pending rather than displaying an error
or invented audit result.

## Claude integration

Add `skills/production-readiness-dashboard/SKILL.md` for explicit manual use:

```text
/prod-readiness:production-readiness-dashboard
```

It starts the server against the currently open project, reports the URL, and
does not alter project files.

Update `production-readiness-audit` Stage 0 to start the same server in a
managed background Bash task immediately after audit initialization. It must
state that the audit still runs in its normal two-wave parallel mode by
default. The dashboard process only watches files and must never serialize,
block, or replace lens execution.

If browser opening is unavailable, the printed URL is the successful fallback.
The audit continues if the dashboard cannot start; it reports the non-fatal
failure and proceeds with the audit.

## Error handling and safety

- Reject any request outside `/` and `/api/snapshot` with a normal 404.
- Bind only to loopback; never accept `0.0.0.0` as a default or documented
  option.
- Escape all artifact text before adding it to the HTML client. Markdown is
  displayed as preformatted text in this first version rather than parsed as
  arbitrary HTML.
- Handle malformed JSON, absent directories, absent artifact files, and files
  that change during a read as an `unavailable` or partial snapshot with a
  human-readable message.
- Do not log artifact contents, credentials, or audit findings to the server
  console; log only startup URL and operational errors.

## Tests

Use Python `unittest` with temporary audit directories.

- Snapshot builder: unavailable audit, initialized audit, running audit,
  completed audit, skipped lenses, missing artifacts, malformed `state.json`,
  and partially readable artifacts.
- HTTP server: localhost bind, ephemeral-port URL announcement, root page,
  snapshot endpoint, and unknown-path 404.
- Client contract: static checks confirm the client uses the snapshot endpoint,
  the four stable destinations, two-second active polling, and HTML escaping.
- Documentation/skill checks: confirm manual startup, automatic Claude launch,
  non-fatal dashboard failure, and preservation of parallel-by-default audit
  execution.

## Out of scope

- Remote access, public hosting, authentication, collaboration, persistence
  beyond `.readiness-audit/`, and editing audit findings from the UI.
- Automatic launch adapters for Codex, OpenCode, Pi, Antigravity, or other
  non-Claude agents. They may use the manual server command.
- WebSocket/SSE updates, filesystem watcher dependencies, charting, and
  Markdown-to-HTML rendering.
