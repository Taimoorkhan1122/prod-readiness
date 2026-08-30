---
name: production-readiness-audit
description: Run a seven-lens production readiness audit over a local codebase - one shared evidence pass, then seven read-only specialist agents (security, backend, frontend, devops, QA, database, AI security) that judge whether the system deserves real users, real attackers, and real load. Produces a persistent trail under .readiness-audit/ with an evidence ledger, evidence-tagged findings (CONFIRMED / NOT FOUND / UNVERIFIED), and a CTO-readable go/no-go report. Use this whenever someone asks "is this ready for production", "audit this repo before launch", "what's missing before we ship", "production readiness review", "review this codebase end to end", "what breaks at 10x", "are we safe to deploy", or asks for a go/no-go call on a system - and use it proactively when a launch, deploy, or scale-up decision is being discussed and nobody has checked what the system is missing. Not for reviewing a single PR or diff, and not for fixing what it finds - it audits and stops.
---

# Production readiness audit

Seven senior engineers looking at the same system from seven angles. The job is
not code style. It is whether this thing survives contact with real users, real
attackers, and real load.

The most dangerous defects in a codebase are the systems that were never built:
no monitoring, no backups, no rate limiting, no rollback path. Absence is a
finding. But an absence you assert without having looked is worse than one you
miss, because it sends the team to build something they already have - or tells
them a gap is closed when nobody checked. So the whole design of this skill is
built around one distinction: what you proved, what you searched for and did not
find, and what you simply cannot see from here.

## Invariants

1. **Read-only.** No source file, config, test, or dependency is modified. The
   only writes are under `.readiness-audit/`. If the user wants fixes, hand off
   at the end.
2. **One evidence pass, seven evaluations.** Stage 2 scans; the lenses consume.
   A lens that re-scans the repository wholesale has burned the budget the
   isolation was meant to save.
3. **Every finding carries an evidence state**, and `NOT_FOUND` cites a ledger
   row. `validate_findings.py` enforces this; do not route around it.
4. **Uncertainty never escalates severity. Compensating controls always
   demote it.**
5. **Secrets are reported by location and kind only** - never the value, not
   even truncated, not inside a quoted snippet.
6. **Each stage persists before the next begins.** If a stage produced nothing
   on disk, it is not done.

## Working directory

```
.readiness-audit/
├── state.json                      # stage pointer, git ref, lens decisions
├── context.md                      # Stage 1 - criticality, RTO/RPO, scale, threat model
├── scope.md                        # Stage 1 - what you can and cannot see
├── evidence/
│   ├── inventory.json              # Stage 2 - what exists
│   ├── absence-ledger.{json,md}    # Stage 2 - what was searched for
│   └── map.md                      # Stage 2 - the semantic map a script can't write
├── findings/<lens>.md              # Stage 3 - one file per lens
├── deferred.md                     # controls considered and not yet needed
└── report.md                       # Stage 5
```

`${CLAUDE_PLUGIN_ROOT}` is this plugin's own directory. Use it directly for all
bundled scripts. Resolve and pass its absolute value to the lens agents, since
they run in their own context.

## Execution mode

**Parallel is the default.** Unless the user explicitly asks for a sequential
audit, launch each wave's lenses concurrently. Do not silently choose serial
execution to simplify orchestration.

Use **sequential** only when the user says `sequential`, asks for one agent at a
time, or invokes `/prod-readiness:production-readiness-audit sequential`.
Record the selected mode during preflight and preserve it when resuming:

```bash
# Default: parallel lenses within each wave
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audit_state.py" init <root> \
  --execution-mode parallel

# Opt-in: one lens at a time
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audit_state.py" init <root> \
  --execution-mode sequential
```

## Stage 0 - preflight

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audit_state.py" status <root>
```

If state exists, tell the user which stage it stopped at and offer to resume or
restart (restart archives the old run rather than deleting it: `audit_state.py
archive`). Otherwise `init`, which records the git ref and whether the tree is
dirty and execution mode. If it is dirty, say what is uncommitted and let the
user decide before proceeding - an audit of an ambiguous working tree is hard
to act on later.

### Optional dashboard

Only after `audit_state.py init` succeeds or the user confirms a state resume,
start the following command as a managed background Bash task:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/readiness_dashboard.py" <root>
```

Wait only for the printed URL line or an immediate error. Best-effort open the
URL, report it to the user, and continue immediately to Stage 1.

If dashboard launch fails, tell the user it is unavailable and continue the audit.
Do not wait for the dashboard process, use it as an audit agent, or change the selected parallel/sequential execution mode.

Dashboard launch is non-fatal.

Offer to add `.readiness-audit/` to `.gitignore`.

Confirm the target is a local directory the user owns or is authorised to review.

## Stage 1 - context and scope

Read `references/context-intake.md`. Write `context.md` and `scope.md`.

Do not skip this to get to the interesting part. Severity is a function of
context: the same missing rate limiter is a P0 on an unauthenticated public API
and a P3 on an internal tool behind a VPN. If the user is present, confirm
criticality, RTO/RPO, and threat model with them. If not, infer, mark every
inferred value `assumed`, and flag the assumptions that would change findings.

## Stage 2 - the evidence pass

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/evidence_scan.py" <root>     # what exists
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/absence_probe.py" <root>     # what was searched for
```

The second one is the load-bearing script. It runs roughly ninety deterministic
control probes and records, for each, the patterns searched, the hit count, and
whether zero hits should be reported as `NOT_FOUND` or as `UNVERIFIED`. That
second judgement follows a rule worth understanding: a control that normally
lives outside a repository - backups, PITR, alert routing - proves nothing by
being absent from source, *unless* the repository ships infrastructure-as-code,
in which case the repo is the right place to look and silence is real.

Then do the part no script can: read the entry points, follow the trust
boundaries, and write `evidence/map.md`. Cover the architecture (services, data
stores, brokers, caches, external dependencies), where authentication and
authorization actually happen, where money and mutable data flow, and the
hotspots worth a lens's attention - auth paths, write paths, external calls,
file and URL handling, infrastructure config. Facts and locations only; no
findings and no opinions yet. The lenses form the opinions.

Keep `map.md` tight. Seven agents will read it, so every wasted paragraph is
paid for seven times.

## Stage 3 - the lenses

Read `references/lens-dispatch.md`, then dispatch. In short: decide which lenses
have signal, record the skips with reasons, and run two waves - security,
backend, database first, then devops, qa, frontend, ai-security, so the second
wave can reference the first's findings instead of duplicating them.

Agent types are `prod-readiness:lens-security`, `lens-backend`, `lens-frontend`,
`lens-devops`, `lens-qa`, `lens-database`, `lens-ai-security`.

A lens with no signal is skipped and the skip is declared in the report. The AI
security lens in particular states CONFIRMED NOT PRESENT and stops rather than
inventing risks for a system with no model calls in it.

Run `validate_findings.py` between waves. If a lens produced errors, send that
lens back with the validator output rather than editing its findings yourself.

## Stage 4 - validation

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/validate_findings.py" <root>
```

Exit code 1 means the report is blocked. The checks are the rules that are easy
to state and easy to quietly break: CONFIRMED cites `file:line`; NOT_FOUND cites
a zero-hit ledger row that the ledger agrees supports NOT_FOUND; UNVERIFIED says
what would resolve it; P0 articulates a failure path and names its compensating
control; absence is phrased as "not found in reviewed scope"; and no two lenses
report the same underlying issue.

Fix by re-dispatching the owning lens. Resist the temptation to reword a finding
into compliance - if a NOT_FOUND cannot cite a ledger row, the honest fix is
usually that it should have been UNVERIFIED.

## Stage 5 - report

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/assemble_report.py" <root>
```

This generates every section that is arithmetic and leaves `<!-- FILL -->`
markers where judgement is needed: the verdict, the RPO/RTO gap column, the
scalability ordering, and each lens's closing line. Read
`references/report-writing.md` and fill them all. The script reports how many
remain; zero is the finish line.

The verdict rule is mechanical - any P0 is HOLD, P1s alone are FIX THEN SHIP,
neither is SHIP - but the sentence that matters most is the one saying how much
of the verdict rests on things you could not see.

## A note on judgement

The rigid parts here - the read-only rule, the ledger citation requirement, the
validator - exist because an audit that is persuasive but wrong is worse than a
shorter one that is honest. Everything else should flex to the system in front
of you. If the repository is a stack none of the lens checklists anticipated,
say so in `map.md` and reason about it directly rather than forcing it into the
closest-sounding pattern. And judge missing systems against this system's scale,
threat model, and criticality - not against a checklist written for someone
else's infrastructure.
