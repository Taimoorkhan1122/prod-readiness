# Stage 3 - dispatching the lenses

Seven agents, one evidence body. The whole point of Stage 2 was to make the
expensive scanning happen once, so a lens that re-reads the entire repository
has defeated the design. Each lens does targeted verification only: it reads the
evidence pack, forms hypotheses, and then opens the specific files it needs.

## Which lenses run

Read `lens_signals` from `absence-ledger.json` and decide:

| Signal | Effect |
| --- | --- |
| `frontend_present: false` | Skip the frontend lens. Record the skip. |
| `llm_present: false` | Skip ai-security. The report states CONFIRMED NOT PRESENT rather than inventing risks. |
| `broker_present: false` | Backend still runs; its event-driven section is declared not applicable. |
| `tests_present: false` | QA still runs - "no tests" is the finding, not a reason to skip. |
| `iac_present: false` | DevOps still runs, mostly producing UNVERIFIED findings. That is a legitimate outcome. |

Record every skip with its reason:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audit_state.py" set-lenses <root> \
  --run security,backend,devops,qa,database \
  --skip frontend="no frontend code found in repository" \
  --skip ai-security="no LLM or model provider SDK found in repository"
```

Skipping a lens because it has nothing to look at is proportionality. Skipping
one because the audit is running long is not - say so plainly if you have to
stop early, and mark the report incomplete.

## Two waves, not one

Dispatch in two waves rather than all seven at once. Agents run in isolated
context windows and cannot see each other, so the wave split is what makes the
cross-lens ownership table actually work:

**Wave 1 - security, backend, database.** These own most shared findings
(tenant isolation, cache leakage, event replay, backups). Run them together.

**Wave 2 - devops, qa, frontend, ai-security.** These read wave 1's findings
files before writing their own, so they can reference an existing ID with
`see:` instead of duplicating. Tell them explicitly that
`.readiness-audit/findings/*.md` already contains wave 1 output.

## The dispatch prompt

Each agent gets a task prompt that pins the paths and nothing else - the agent
definition carries its own mandate, so repeating the checklist here just burns
context:

```
Run your lens against this project.

Project root: /abs/path/to/repo
Audit directory: /abs/path/to/repo/.readiness-audit
Plugin root: /abs/path/to/plugin        (references/ and scripts/ live here)
Wave: 1 of 2                            (or: 2 of 2 - wave 1 findings are already
                                         in .readiness-audit/findings/)

Read in this order before touching source:
  .readiness-audit/context.md
  .readiness-audit/scope.md
  .readiness-audit/evidence/map.md
  .readiness-audit/evidence/inventory.json
  .readiness-audit/evidence/absence-ledger.md
  <plugin root>/skills/production-readiness-audit/references/finding-format.md

Write findings to .readiness-audit/findings/<your-lens>.md.
Return a summary of at most 10 lines: counts by severity, your single scariest
item, and anything you could not determine. Do not paste findings into the reply.
```

That last line matters. Seven agents each returning their full findings would
put the entire report back into the orchestrator's context window, which is the
cost the isolation was supposed to avoid. The files on disk are the deliverable;
the reply is a receipt.

## After each wave

Run the validator between waves, not only at the end:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/validate_findings.py" <root>
```

Catching a malformed block after three findings is cheap. Catching it after
forty means re-dispatching an agent. If a lens produced errors, send it back
with the specific validator output rather than fixing its findings yourself -
the lens has the context to know whether the fix is a rephrase or a downgrade,
and rewriting another agent's finding is how severity quietly drifts.
