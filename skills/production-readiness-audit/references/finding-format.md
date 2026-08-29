# Finding format, evidence states, and severity

Every lens writes to this format. It is markdown so the trail stays readable,
and strict enough that `validate_findings.py` can check it mechanically. A
finding that fails validation does not reach the report.

## The block

Append blocks to `.readiness-audit/findings/<your-lens>.md`. One block per
finding, no prose between them.

```
### PRA-SEC-003 | Tenant identifier is read from the request body on order writes
state: CONFIRMED
severity: P0
owner: security
cross-lens: backend, database
evidence: src/orders/orders.service.ts:88
probe: -
failure-path: OrdersController accepts tenantId in the POST body and passes it straight to the repository. Any authenticated user of tenant A can set tenantId to B and read or mutate B's orders. No guard re-derives tenant from the session.
compensating: none found - the JWT does carry a tenant claim, but nothing compares it to the body value
fix: Derive tenantId from the authenticated principal inside TenantGuard, strip it from CreateOrderDto, and add a repository-level scope filter so the field cannot be supplied by a caller at all.
resolve: -
see: -
```

### Fields

| Field | Meaning |
| --- | --- |
| `### PRA-<PREFIX>-<NNN>` | ID. Prefixes: `SEC`, `BE`, `FE`, `OPS`, `QA`, `DB`, `AI`. Number within your own lens, from 001. |
| `state` | `CONFIRMED`, `NOT_FOUND`, or `UNVERIFIED`. Exactly one. |
| `severity` | `P0`, `P1`, `P2`, or `P3`. |
| `owner` | Your lens, or the lens that owns it if you are cross-referencing. |
| `cross-lens` | Other lenses this touches, comma separated. `-` if none. |
| `evidence` | `path/to/file.ts:120` for CONFIRMED. For NOT_FOUND, the phrase "searched, not found in scope". |
| `probe` | Absence-ledger control id. Required for NOT_FOUND. `-` otherwise. |
| `failure-path` | The specific articulable path to harm. Required for P0. |
| `compensating` | The mitigating control, or "none found". Required for P0. |
| `fix` | Concrete remediation. Always required. |
| `resolve` | What evidence would settle it. Required for UNVERIFIED. |
| `see` | ID of the owning finding, when you are deferring to another lens. |

Use `-` for fields that do not apply. Never leave a required field blank.

## Evidence states

The distinction between these three is the single thing this audit is built to
get right. A confident wrong absence does more damage than a missed finding,
because it sends a team to build something they already have, or worse, tells
them a gap is closed when nobody checked.

**`CONFIRMED`** - you read the code and the problem is there. Cite `file:line`.
This is the only state that may be stated as fact.

**`NOT_FOUND`** - you searched within the scope you actually had and the control
was not there. You must cite a `probe` id from `.readiness-audit/evidence/
absence-ledger.json` whose hit count is zero and whose `supports_state` is
`NOT_FOUND`. Phrase it as *"No rate limiting found in reviewed scope"* - never
*"the system has no rate limiting"*, which claims knowledge of a runtime you
never saw. The validator rejects the second phrasing.

**`UNVERIFIED`** - the answer lives somewhere you cannot see: the CI pipeline,
the cloud console, a separate infrastructure repository, a runtime dashboard.
Say precisely what would resolve it. An UNVERIFIED item may be flagged as a
potential P0 or P1 **risk**, but never written as an established defect.

The ledger already decides which of the last two applies for each control. If a
control normally lives outside a repository and this repo ships no IaC, absence
in the repo proves nothing and the ledger says `UNVERIFIED`. If the repo *does*
ship IaC, the repo is now the right place to look, and the same silence becomes
a real `NOT_FOUND`. Follow the ledger rather than your instinct here.

## Severity

**P0 - production blocker.** A credible, exploitable path to catastrophic
security compromise, major data loss, financial loss, regulatory exposure, or
widespread outage, with no adequate compensating control. "Credible" means you
can write the failure path down concretely - which is why `failure-path` is
mandatory. If you cannot articulate it, it is not a P0. If a compensating
control plausibly mitigates it, it is a P1.

**P1 - serious risk.** High likelihood or high impact against production
reliability, security, scalability, or operability. Required controls that are
absent within scope land here. So do implied RPO/RTO violations: nightly-only
snapshots on a system whose criticality implies a one-hour RPO is a P1 even
though nothing is technically broken.

**P2/P3 - technical debt.** Shortcuts, TODOs, maintainability. No ego, no noise.
If it would not change a decision, leave it out.

Uncertainty never raises severity. A compensating control always lowers it.

## Proportionality

Before you flag a missing control, run it against the operating context in
`.readiness-audit/context.md`:

1. Does the scale envelope make this control necessary?
2. Does the threat model expose this attack surface?
3. Does business criticality justify the cost?
4. Does regulatory exposure mandate it?
5. Is there a simpler compensating control already doing the job?

Three outcomes. **Necessary** - write the finding at full severity.
**Proportionate but not yet required** - do not write a finding; add a line to
`.readiness-audit/deferred.md` naming the concrete trigger ("needed when: more
than one write replica" / "needed when: PCI scope"). **Over-engineering here** -
also add it to `deferred.md` marked `considered: not needed`, so the reader can
see it was weighed rather than overlooked. Negative space is part of the
product.

A multi-region failover finding on an internal tool with forty users is not
rigour. It is noise that buries the finding that matters.

## Cross-lens ownership

Some failures span lenses. To keep one issue from appearing three times under
three headings, ownership is fixed in advance:

| Issue | Owner | Tags |
| --- | --- | --- |
| Migration / deploy sequencing, expand-contract, old-new coexistence | devops | backend, database |
| Backups, PITR, restore drills | database | devops |
| Cross-tenant cache key leakage | security | backend |
| Cache stampede, invalidation, cache as SPOF | backend | devops |
| Event replay safety and consumer idempotency | backend | qa |
| DLQ drain and consumer lag alerting | backend | devops |
| Post-deploy smoke tests | devops | qa |
| Client-side-only validation | security | frontend |
| Real PII or production credentials in test data | qa | security, database |
| Agent or tool calls that fetch URLs from model output | ai-security | security |
| Secrets handling in CI | devops | security |

If you are the owner, write the finding fully and tag the others in
`cross-lens`. If you are not, and you would otherwise have raised it, write a
short block with `see: <owner's id>` and no duplicate detail - or, if the lenses
ran in parallel and you cannot see the owner's ID yet, use
`see: owned-by-<lens>` and the orchestrator will reconcile. Silently dropping a
shared finding because "that is the other lens's job" is the failure this table
exists to prevent, so when in doubt, write the block with a `see:` line.
