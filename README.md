# prod-readiness

A seven-lens production readiness audit for Claude Code. One orchestrator skill,
seven read-only specialist agents, and five scripts that make the audit's
central claim checkable rather than merely asserted.

## Install

```bash
# try it without installing
claude --plugin-dir /path/to/prod-readiness

# or keep it loaded automatically
cp -r prod-readiness ~/.claude/skills/prod-readiness   # loads as prod-readiness@skills-dir
```

Then, in any repository:

> is this ready for production?

The skill triggers on launch, deploy, go/no-go, and "what's missing" phrasing.
You can also invoke a single lens directly with
`@agent-prod-readiness:lens-security`.

## What it does

```
Stage 0  preflight        git ref, dirty check, resume-or-restart
Stage 1  context          criticality, RTO/RPO, scale, threat model, scope
Stage 2  evidence         evidence_scan.py + absence_probe.py + a semantic map
Stage 3  lenses           two waves of read-only agents over that one evidence body
Stage 4  validation       validate_findings.py blocks the report on rule breaks
Stage 5  report           assemble_report.py, then the narrative sections
```

Everything persists under `.readiness-audit/`, so the run survives `/clear`, a
crash, or a week-long gap.

## The idea worth explaining

The hardest rule in a "find what's missing" audit is the one a language model
will quietly break: telling apart what it proved, what it searched for and did
not find, and what it cannot see at all. Asked to find absences, a model
produces confident ones.

So absence is made mechanical. `absence_probe.py` runs about ninety
deterministic control probes and records, per control, the patterns searched,
the hit count, and the paths. A `[NOT FOUND]` finding is only valid if it cites
a ledger row with zero hits — `validate_findings.py` rejects it otherwise.

The ledger also decides *which* state a miss deserves. Controls that normally
live outside a repository (backups, PITR, alert routing) prove nothing by being
absent from source, so they default to `[UNVERIFIED]` — unless the repo ships
infrastructure-as-code, in which case the repo is the right place to look and
the same silence becomes a real `[NOT FOUND]`.

A second table handles proportionality: controls that depend on something the
system does not have are marked not-applicable rather than missing. No broker
means no dead-letter-queue finding. That is what keeps the report from filling
up with demands for machinery the system has no use for.

## Design choices worth knowing

**One evidence pass, seven evaluations.** Stage 2 scans once; the lenses consume
the pack. Seven agents each scanning the repository would cost seven times as
much for a worse result, since they would also disagree about what they saw.

**Two waves, not seven in parallel.** Agents cannot see each other. Security,
backend, and database run first because they own most cross-lens findings; the
rest run second and reference wave-1 findings by ID instead of duplicating them.

**No agent memory.** Subagents support persistent memory and it is tempting
here. It is deliberately off: an audit that remembers "this repo has rate
limiting" from three months ago will assert it again without looking. Fresh eyes
every run is the point.

**Agents get `Write` but not `Edit`.** They write only their own findings file.
Plugin subagents ignore `hooks`, `mcpServers`, and `permissionMode`, so the
read-only rule is enforced by prompt rather than by policy — if you want it
enforced hard, copy the agent files into `.claude/agents/` and add a `PreToolUse`
hook.

**It audits and stops.** No source file is ever modified. For remediation with
tests proving each fix, hand off to a separate skill.

## Layout

```
.claude-plugin/plugin.json
agents/lens-{security,backend,frontend,devops,qa,database,ai-security}.md
skills/production-readiness-audit/
  SKILL.md
  references/{context-intake,lens-dispatch,finding-format,report-writing}.md
scripts/
  audit_state.py        stage pointer, git ref, lens run/skip decisions
  evidence_scan.py      what exists: manifests, entry points, IaC, tests, migrations
  absence_probe.py      what was searched for: the absence ledger
  validate_findings.py  the quality gate
  assemble_report.py    Sections A-K, arithmetic filled, judgement left as FILL markers
```

## Tuning

Add a control: append a `C(...)` entry to `CONTROLS` in `absence_probe.py`, and
add it to `REQUIRES` if it only applies when something else is present. Every
lens picks it up automatically through the ledger.

Pin models per lens by setting `model: haiku` or `model: sonnet` in an agent's
frontmatter; the default is `inherit`.
