# Finding format, evidence states, and severity

Every lens writes structured JSON. That file is the source of truth: the
dashboard renders it, the report is assembled from it, and the markdown trail a
fix agent reads is *generated* from it, so the two can never disagree. A finding
that fails validation does not reach the report.

## The file

Write `.readiness-audit/findings/<your-lens>.json`. One object, one `findings`
array. Do not write the `.md` file yourself - `finding_store.py render`
generates it from your JSON.

```json
{
  "schema": 1,
  "lens": "security",
  "findings": [
    {
      "id": "PRA-SEC-003",
      "title": "Tenant identifier is read from the request body on order writes",
      "impact": "Any logged-in customer can read and change another company's orders by editing one value in the request.",
      "state": "CONFIRMED",
      "factors": {
        "exposure": "authenticated",
        "data_class": "financial",
        "blast_radius": "systemic",
        "compensating_control": "absent"
      },
      "owner": "security",
      "cross_lens": ["backend", "database"],
      "evidence": ["src/orders/orders.service.ts:88"],
      "probe": null,
      "failure_path": "OrdersController accepts tenantId in the POST body and passes it straight to the repository. Any authenticated user of tenant A can set tenantId to B and read or mutate B's orders. No guard re-derives tenant from the session.",
      "compensating": "none found - the JWT does carry a tenant claim, but nothing compares it to the body value",
      "fix": "Derive tenantId from the authenticated principal inside TenantGuard, strip it from CreateOrderDto, and add a repository-level scope filter so the field cannot be supplied by a caller at all.",
      "resolve": null,
      "see": null
    }
  ]
}
```

### Fields

| Field | Meaning |
| --- | --- |
| `id` | `PRA-<PREFIX>-<NNN>`. Prefixes: `SEC`, `BE`, `FE`, `OPS`, `QA`, `DB`, `AI`. Number within your own lens, from 001. |
| `title` | One line naming the problem. Required. |
| `impact` | **What this costs a non-technical reader.** See below. Required. |
| `state` | `CONFIRMED`, `NOT_FOUND`, or `UNVERIFIED`. Exactly one. |
| `factors` | Object with `exposure`, `data_class`, `blast_radius`, `compensating_control`. A script derives `severity` from these four values - see Severity below. Do not set `severity` yourself. |
| `owner` | Your lens, or the lens that owns it if you are cross-referencing. |
| `cross_lens` | Array of other lenses this touches. `[]` if none. |
| `evidence` | Array of `path/to/file.ts:120` strings for CONFIRMED. One entry per location - never a prose sentence. For NOT_FOUND, `["searched, not found in scope"]`. |
| `probe` | Absence-ledger control id. Required for NOT_FOUND. `null` otherwise. |
| `failure_path` | The specific articulable path to harm. Required for P0. |
| `compensating` | The mitigating control, or `"none found"`. Required for P0. |
| `fix` | Concrete remediation. Always required. |
| `resolve` | What evidence would settle it. Required for UNVERIFIED. |
| `see` | ID of the owning finding, when you are deferring to another lens. |

Use `null` for fields that do not apply. Never leave a required field blank.

### Writing `impact`

`impact` is the only field a non-engineer reads. It is the headline of the
finding on the dashboard, and it is the difference between a report someone acts
on and a report someone closes.

One or two sentences. No file names, no class names, no function names, no
framework terms. Say what a user, the business, or the data loses - not what the
code does. `failure_path` is where the mechanism goes; keep them distinct rather
than writing the same sentence twice.

| Instead of | Write |
| --- | --- |
| "`OrdersController` accepts `tenantId` in the POST body" | "Any logged-in customer can read and change another company's orders." |
| "No rate limiting middleware on edge functions" | "One person can run up your API bill without an account, and nothing stops them." |
| "`stripe-webhook` has no test coverage" | "If billing breaks, nothing catches it - you would find out when a customer complains." |

For a `NOT_FOUND` or `UNVERIFIED` finding, `impact` describes what the missing
control would have protected against, phrased as exposure rather than fact:
"Nothing found that would restore this data after a bad deploy."

## Language - write in ASD-STE100

Write every prose field, in ASD-STE100
(Simplified Technical English). The goal is a report a tired reader
understands on the first pass, in a second language if necessary.

- One idea per sentence. Keep sentences to 20 words or fewer for descriptive
  text, and 25 words or fewer for instructions.
- Use the active voice. Name who does the thing: "An attacker reads the orders",
  not "The orders can be read".
- Use one word for one meaning. Do not call the same thing a "job", a "task" and
  a "worker" in three sentences.
- Use simple verbs and simple tenses. Prefer "the service stops" to "the service
  would end up being terminated".
- Do not use noun clusters of more than three words. Break
  "customer order export retry queue" into a phrase with a preposition.
- Do not drop articles. Write "the request", not "request".
- Do not use metaphor, idiom, humour, or hedging ("arguably", "somewhat",
  "a bit of a"). State the fact or mark it UNVERIFIED.
- Keep code, identifiers, error strings, file paths, and severity labels exactly
  as they are. ASD-STE100 applies to the prose around them, not to them.

This applies hardest to `impact`, which a non-engineer reads, and to
`recommendation`, which someone follows as an instruction.

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

A lens does not choose `P0`-`P3`. It answers four factual questions in
`factors`, and a script derives the severity from the answers. The same gap
then gets the same rating on every run, whoever wrote it.

| Factor | Values |
| --- | --- |
| `exposure` | `internet`, `authenticated`, `internal`, `local` |
| `data_class` | `secrets`, `pii`, `financial`, `business`, `none` |
| `blast_radius` | `systemic`, `multi-tenant`, `single-tenant`, `single-user` |
| `compensating_control` | `present`, `absent` |

The script scores each answer, sums the three factors, then subtracts for a
present compensating control and floors the result at zero:

    exposure:        internet 3, authenticated 2, internal 1, local 0
    data_class:      secrets 3, pii 3, financial 3, business 1, none 0
    blast_radius:    systemic 3, multi-tenant 2, single-tenant 1, single-user 0
    compensating_control present: subtract 2, total floored at 0

    total >= 8   -> P0
    total 6-7    -> P1
    total 3-5    -> P2
    total <= 2   -> P3

An `UNVERIFIED` finding is never `P0`. Its rubric result is capped at `P1`,
whatever the total.

A finding that sets `severity` itself, instead of `factors`, is rejected by
the validator. Answer the four questions honestly - do not pick values to
land on a severity you already had in mind.

`failure_path` and `compensating` are required whenever the factors score
`P0`: write the concrete path to harm, and name the mitigating control or
state `"none found"`.

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
