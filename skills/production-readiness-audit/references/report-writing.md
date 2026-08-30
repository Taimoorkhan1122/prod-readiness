# Stage 5 - writing the parts a script cannot

`assemble_report.py` generates every section that is arithmetic: the P0 and P1
blocks, the missing-systems inventory, the recovery posture table, the debt
register, and the evidence-to-obtain list. It leaves `<!-- FILL: ... -->`
markers where judgement is required. Replace every one of them. A report shipped
with FILL markers still in it tells the reader the audit was abandoned halfway.

## Language - write in ASD-STE100

Write every section you fill in ASD-STE100 (Simplified Technical English). The
reader may be tired, non-technical, or reading in a second language, and the
verdict must survive all three.

- One idea per sentence. 20 words or fewer for descriptive text, 25 or fewer for
  instructions.
- Active voice, with the actor named.
- One word for one meaning across the whole report.
- Simple verbs and simple tenses.
- No noun cluster longer than three words. Keep the articles.
- No metaphor, no idiom, no humour, no hedging. State the fact, or mark it
  UNVERIFIED.
- Code, identifiers, paths, error strings, and severity labels stay verbatim.

## Section B - the verdict

Three sentences of context, then the call. A CTO reads this and nothing else
before deciding, so it has to carry the weight honestly.

Pick one: **SHIP**, **FIX THEN SHIP**, or **HOLD - DO NOT DEPLOY**. The rule is
mechanical: any P0 means HOLD. P1s without P0s means FIX THEN SHIP. Neither
means SHIP.

Then state how much of the verdict rests on things you could not see. This is
the sentence most audits omit and the one that decides whether the reader trusts
the rest. If eleven of nineteen findings are UNVERIFIED because the
infrastructure lives in a repository you were not given, say exactly that: the
verdict is provisional on evidence nobody has produced yet, and the fastest path
to a real answer is handing over the CI config and the backup policy.

Do not hedge a P0 to sound balanced, and do not harden an UNVERIFIED to sound
decisive. Precision is the product.

## Section G - the RPO/RTO judgement

The generated table leaves two columns blank per row. Fill them against the
numbers in `context.md`:

*Meets stated RPO/RTO?* is yes, no, or unknown - not a paragraph. Do the
arithmetic out loud in the *Gap* column: nightly snapshots against a stated
four-hour RPO means up to twenty hours of data loss in the worst case, so the
answer is no and the gap is sixteen hours.

An untested backup is a hypothesis, not a recovery capability. If nothing in
scope shows a restore has ever been executed and validated, the restore-drill
row says so plainly, whatever the backup row says.

## Section H - what breaks first

Order by failure sequence, not by severity. The question is which thing gives
way at 10x and which at 100x, relative to the scale envelope in Section A. Name
the mechanism, not the symptom: "the per-request permissions query has no cache,
so at 10x it is 4,000 qps against a single primary" beats "database may struggle
under load".

Include the data-growth projection when the database lens produced one, and any
cache-stampede scenario the backend lens raised. If the scale envelope is small
and nothing plausibly breaks at 100x, say that - it is a legitimate finding and
it stops the reader from imagining a problem that is not there.

## Section K - the closing lines

Each lens ends with one sentence:

> The scariest thing this system is missing is ___ (and I know / suspect /
> cannot determine this because ___).

Use the lens's own returned summary for this rather than inventing one. The
know/suspect/cannot-determine choice must match the evidence state of the
finding being referenced - a lens that says "I know" about an UNVERIFIED item
has broken the discipline the whole audit rests on. This section is where a
reader checks whether the panel was honest, so it is worth getting exactly
right.

## Before you hand it over

- No `<!-- FILL` markers remain (`assemble_report.py` reports the count).
- The verdict follows the P0/P1 rule above.
- Every P0 has a failure path a reader could reproduce.
- Section A's scope boundary is stated before anyone reads a finding.

Then tell the user where the trail lives, and that `.readiness-audit/` should
probably be gitignored unless they want the audit in version control.

## Handing off to remediation

This skill audits and stops. It never edits source. If the user wants the
security findings fixed with tests proving each fix, the `security-audit-hardening`
skill picks up from a findings list and runs an approval-gated remediation
cycle. Say so once, at the end, and let them decide.
