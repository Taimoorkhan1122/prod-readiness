# Stage 1 - operating context and scope

Nothing below this stage means anything without it. A control that is mandatory
at one scale is over-engineering at another, and severity is a function of
context, not of a checklist. Write both files before any lens runs.

## context.md

Ask the user for what you cannot infer. Infer the rest from the code and say so.
Every inferred value gets marked `assumed` - a reader must be able to tell which
numbers came from the business and which came from you, because a wrong
assumption here silently mis-severities the whole report.

Cover:

**Business criticality.** What happens if this is down for an hour? Is there a
money path? Regulated data? Anything touching human safety? "Internal tool, an
hour of downtime annoys forty people" and "checkout for a marketplace" produce
completely different reports from identical code.

**Recovery objectives.** State RTO (maximum acceptable downtime) and RPO
(maximum acceptable data loss) explicitly, in hours or minutes. If the user has
not set them, derive them from the criticality answer and mark them `assumed`.
Every recovery finding is judged against these two numbers, so leaving them
vague makes Section G unfalsifiable.

**Scale envelope.** Current and expected users, requests per second, data
volume, and - the one most audits skip - the data *growth rate* in records per
year. Growth rate is what turns "this table is fine" into "this table is fine
for fourteen more months".

**Threat model.** Internet-facing and unauthenticated? B2B with authenticated
tenants? Internal-only behind a VPN? This decides whether an IDOR is a P0 or a
P2 and whether a WAF is a finding or a distraction.

**Regulatory exposure.** GDPR, HIPAA, SOC2, PCI, or none apparent. Retention
and deletion findings are mandatory when PII is in scope and optional when it
is not.

**System maturity.** Greenfield, established, or legacy under active change.
A greenfield service gets latitude on observability that a five-year-old
revenue system does not.

Close the file with an **Assumptions that would change findings** list: each
assumption, and what would move if it were wrong. For example: "assumed single
tenant per deployment - if this is shared-database multi-tenant, every read path
needs tenant-scope review and PRA-SEC findings escalate."

If the user is available, confirm the criticality, RTO/RPO, and threat model
answers with them before dispatching lenses. Those three drive most severity
decisions and are the cheapest to get right up front. Do not block on it - state
the assumptions, proceed, and flag them prominently.

## scope.md

List what you can see and what you cannot. Every finding inherits this boundary,
and the honesty of the whole report rests on it.

Typical gaps worth naming explicitly, because their absence is otherwise easy to
misread as a defect:

- Infrastructure-as-code, CI/CD pipeline config, cloud console configuration
- Runtime environment, deployed secret management, backup and retention policy
- Ticketing, runbooks, incident history, postmortems
- Other repositories - frontend, backend, and platform-infra are often split
- Test environments and staging data pipelines
- Anything the evidence scan truncated because the repository exceeded its cap

Record which of these you *do* have. If the repository ships Terraform, then
infrastructure is in scope and a missing backup resource is a real `NOT_FOUND`
rather than an `UNVERIFIED` - `absence_probe.py` applies exactly this rule, so
scope.md and the ledger should agree.

Also record the git ref the audit ran against and whether the tree was dirty.
`audit_state.py init` captures both; copy them in so the report is reproducible.

## Runtime intake - the live target the audit may touch

Complete this intake before any lens runs. A lens never starts without it.

Record five items in `.readiness-audit/runtime-context.json` with
`scripts/runtime_context.py`:

- URL of the live target.
- Environment name, such as staging or prod.
- Role the audit uses, such as readonly auditor.
- Credential reference, such as the vault path or variable name. Store the
  name only. Store no value.
- Scope notes, such as pages in scope and areas to avoid.

Apply the fail-fast gate before lenses start. Run `check_runtime_ready`.
It fails when the target is missing or unreachable.

- Missing target means no runtime lens. Record the skip with a reason and
  run the other lenses.
- Unreachable target means abort. Report the message and start no lens.

Keep secrets out of the trail. Findings, logs, screenshots, reports, and
exports carry the credential reference only. The write path redacts values
that look like a password, token, secret, or API key and stores
`[REDACTED]` instead.

Stay read-only after login. The network log may show GET, HEAD, OPTIONS,
and one POST login. Any other POST, PUT, PATCH, or DELETE is a violation.
Inspect mutating controls for presence and state only. Record the
inspection with `mark_inspected_not_activated`. Activate nothing.
