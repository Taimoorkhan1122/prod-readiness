# Production Readiness Audit

A Claude Code plugin that audits a local codebase from seven angles and produces
an evidence-tagged go/no-go verdict, plus a local dashboard for watching the
audit and exporting its results.

## The audit

**Lens**:
One of the seven specialist perspectives (security, backend, frontend, devops,
qa, database, ai-security) that evaluates the shared evidence body and authors
findings. Also referred to as a *reviewer*, since each lens stands in for a
senior engineer reviewing the system.
_Avoid_: Agent, auditor, checker

**Evidence pass**:
The single scan of the repository, run once before any lens, whose output every
lens consumes. A lens that rescans the repository has broken the design.
_Avoid_: Scan phase, discovery

**Absence ledger**:
The record of every control that was searched for, the patterns used, the hit
count, and whether zero hits should be reported as NOT_FOUND or UNVERIFIED.
_Avoid_: Negative results, gap list

**Evidence state**:
One of CONFIRMED (proved, cites file:line), NOT_FOUND (searched for, cites a
zero-hit ledger row), or UNVERIFIED (not visible from here). Every finding
carries exactly one.
_Avoid_: Status, confidence

**Factors**:
The closed enums a lens supplies for a finding — exposure, data class, blast
radius, and compensating control — from which severity is derived. A lens
supplies factors; it never supplies a severity.
_Avoid_: Risk inputs, attributes

**Derived severity**:
The P0–P3 rating computed from a finding's factors by a fixed rubric. Never
authored by a lens.
_Avoid_: Priority, rating, score

**Decision**:
The computed go/no-go call — SHIP, FIX_THEN_SHIP, or HOLD — a pure function of
the validated findings. Distinct from the *headline* and *summary*, which are
prose a lens author writes.
_Avoid_: Verdict (the verdict is the decision plus its prose), recommendation

## The dashboard

**Handshake file**:
`.readiness-audit/dashboard.json`, written atomically by the dashboard server
once it is listening, carrying the URL, port, PID, resolved audit root, and
start time. It is a cache, not a source of truth — the running server is.
_Avoid_: Lock file, state file, PID file

**Heartbeat**:
A single progress event a lens appends to its own progress log while it works,
carrying a phase and an optional free-text note.
_Avoid_: Progress update, log line, trace

**Phase**:
The stage of a lens's own work, drawn from a closed vocabulary: started,
evidence-read, analyzing, writing-findings, done. Distinct from an audit
*stage*, which describes the whole run.
_Avoid_: Step, status

**No signal**:
How the dashboard renders a lens that has not emitted a heartbeat within the
silence threshold. Never rendered as progress.
_Avoid_: Stalled, unknown, idle

**Export**:
A derived, shareable document set generated from a snapshot of the audit —
a combined report plus one per-lens report. Exports are written once and never
overwritten.
_Avoid_: Report (the report is an audit artifact; an export is a shareable
rendering of it), download
