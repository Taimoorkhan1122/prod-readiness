#!/usr/bin/env python3
"""
assemble_report.py - build report.md from the audit trail.

The sections that are arithmetic (which findings are P0, which controls the
ledger says are missing, which unknowns need evidence) are generated here so
they cannot drift from the findings files. The sections that are judgement
(the verdict, the scalability ordering, each lens's closing line) are left as
FILL markers for the orchestrator to write. That split exists because a report
whose counts disagree with its own appendix stops being believed.

Run validate_findings.py first - this script will refuse to assemble a report
from findings that do not pass the gate unless --force is given.

Usage:
    python3 assemble_report.py <project_root> [--force]
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from validate_findings import parse_file, validate, LENS_PREFIX  # noqa: E402
from finding_store import load_verdict, write_report  # noqa: E402

DECISION_TEXT = {
    "SHIP": "SHIP",
    "FIX_THEN_SHIP": "FIX THEN SHIP",
    "HOLD": "HOLD - DO NOT DEPLOY",
}

LENS_ORDER = ["security", "backend", "frontend", "devops", "qa", "database", "ai-security"]
LENS_TITLE = {
    "security": "Security Engineer", "backend": "Backend Architect",
    "frontend": "Frontend Engineer", "devops": "DevOps Engineer",
    "qa": "QA Engineer", "database": "Database Engineer",
    "ai-security": "AI Security Engineer",
}
RECOVERY_ROWS = [
    ("Backups", "backup_config"),
    ("Point-in-time recovery", "pitr"),
    ("Verified restore drill", "restore_drill"),
    ("Rollback path", "rollback_path"),
    ("Incident response", "runbook"),
    ("Event replay / DLQ drain", "dead_letter_queue"),
]
STATE_LABEL = {"CONFIRMED": "[CONFIRMED]", "NOT_FOUND": "[NOT FOUND]",
               "UNVERIFIED": "[UNVERIFIED]"}


def load_findings(root: Path):
    fdir = root / ".readiness-audit" / "findings"
    out = []
    if not fdir.exists():
        return out
    for f in sorted(fdir.glob("*.json")):
        try:
            parsed = parse_file(f)
        except ValueError:
            continue  # validate_findings.py reports this; the gate above blocks on it
        for fd in parsed:
            fd["lens_file"] = f.stem
            out.append(fd)
    return out


def fld(fd, key, default="-"):
    v = fd["fields"].get(key, "").strip()
    return v if v else default


def render_finding(fd):
    F = fd["fields"]
    state = F.get("state", "?").upper().replace(" ", "_")
    lines = [
        f"#### {fd['id']} | {fd['title']}",
        "",
        f"- **Lens**: {LENS_TITLE.get(fld(fd,'owner',fd['lens_file']), fld(fd,'owner'))}"
        + (f"  **[CROSS-LENS: {fld(fd,'cross-lens')}]**" if fld(fd, "cross-lens") != "-" else ""),
        f"- **Evidence state**: {STATE_LABEL.get(state, state)}",
        f"- **Evidence**: {fld(fd,'evidence')}"
        + (f"  (ledger probe `{fld(fd,'probe')}`)" if fld(fd, "probe") != "-" else ""),
    ]
    if fld(fd, "failure-path") != "-":
        lines.append(f"- **Why this severity**: {fld(fd,'failure-path')}")
    if fld(fd, "compensating") != "-":
        lines.append(f"- **Compensating control**: {fld(fd,'compensating')}")
    if fld(fd, "resolve") != "-":
        lines.append(f"- **Evidence that would resolve this**: {fld(fd,'resolve')}")
    if fld(fd, "see") != "-":
        lines.append(f"- **Owned by**: {fld(fd,'see')}")
    lines += [f"- **Fix**: {fld(fd,'fix')}", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project_root")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    d = root / ".readiness-audit"

    errors, warnings, _ = validate(root)
    if errors and not args.force:
        print(f"refusing to assemble: {len(errors)} validation errors. "
              "Run validate_findings.py, fix them, then retry (or pass --force).",
              file=sys.stderr)
        return 1

    findings = load_findings(root)
    ledger = {}
    lpath = d / "evidence" / "absence-ledger.json"
    ledger_meta = {}
    if lpath.exists():
        raw = json.loads(lpath.read_text())
        ledger = raw.get("controls", {})
        ledger_meta = {k: v for k, v in raw.items() if k != "controls"}

    state_file = d / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}

    def sev(fd):
        return fd["fields"].get("severity", "").strip().upper()

    p0 = [f for f in findings if sev(f) == "P0"]
    p1 = [f for f in findings if sev(f) == "P1"]
    debt = [f for f in findings if sev(f) in ("P2", "P3")]
    unverified = [f for f in findings if f["fields"].get("state", "").upper().replace(" ", "_") == "UNVERIFIED"]

    out = []
    A = out.append

    A("# Production Readiness Audit")
    A("")
    A(f"Repository: `{root}`  ")
    A(f"Git ref at audit start: `{state.get('git_ref') or 'unknown'}`  ")
    A(f"Findings: {len(findings)} ({len(p0)} P0, {len(p1)} P1, {len(debt)} P2/P3, "
      f"{len(unverified)} unverified)")
    A("")

    # ---- A ----
    A("## Section A - Scope & Context")
    A("")
    for name, heading in (("context.md", "Operating context"), ("scope.md", "Review scope")):
        p = d / name
        A(f"### {heading}")
        A("")
        A(p.read_text().strip() if p.exists()
          else f"<!-- FILL: {name} was not written; state the assumptions here -->")
        A("")
    if state.get("lenses_skipped"):
        A("### Lenses not run")
        A("")
        A("| Lens | Why it was skipped |")
        A("| --- | --- |")
        for lens, reason in state["lenses_skipped"].items():
            A(f"| {LENS_TITLE.get(lens, lens)} | {reason} |")
        A("")
    if ledger_meta.get("truncated"):
        A("> The evidence scan hit its file cap, so parts of this repository were not "
          "read. Every finding below inherits that boundary.")
        A("")

    # ---- B ----
    A("## Section B - Executive Verdict")
    A("")
    verdict, verdict_errors = load_verdict(root)
    for message in verdict_errors:
        print(message, file=sys.stderr)
    if verdict["decision"] or verdict["headline"]:
        A(f"**{DECISION_TEXT.get(verdict['decision'], verdict['decision'] or 'VERDICT')}**")
        A("")
        for paragraph in (verdict["headline"], verdict["summary"]):
            if paragraph:
                A(paragraph)
                A("")
    else:
        A("<!-- FILL: write .readiness-audit/verdict.json with a decision of SHIP / "
          "FIX_THEN_SHIP / HOLD, a headline, and a summary. State explicitly how much "
          f"of the verdict rests on UNVERIFIED areas - there are {len(unverified)} "
          "unverified findings. This section is generated from that file. -->")
        A("")

    # ---- C / D ----
    for label, group in (("Section C - Production Blockers (P0)", p0),
                         ("Section D - Serious Risks (P1)", p1)):
        A(f"## {label}")
        A("")
        if not group:
            A("None identified within the reviewed scope.")
            A("")
        else:
            for fd in sorted(group, key=lambda x: x["id"]):
                A(render_finding(fd))

    # ---- E ----
    A("## Section E - Missing Systems Inventory")
    A("")
    A("Generated from the absence ledger. *Necessity* is the lens's judgement under the "
      "proportionality rule; rows marked \"considered, not raised\" were searched for, not "
      "found, and judged not necessary at this scale by the lens that owns them.")
    A("")
    A("| Missing system | Lens | Evidence state | Ledger probe | Raised as | Necessity |")
    A("| --- | --- | --- | --- | --- | --- |")
    probe_to_finding = {}
    for fd in findings:
        pr = fd["fields"].get("probe", "").strip()
        if pr and pr != "-":
            probe_to_finding.setdefault(pr, []).append(fd)
    for cid, row in sorted(ledger.items()):
        if row["polarity"] != "control" or row["hit_count"] > 0:
            continue
        if row.get("supports_state") not in ("NOT_FOUND", "UNVERIFIED"):
            continue  # branch selector, or a control with nothing to apply to
        raised = probe_to_finding.get(cid, [])
        raised_txt = ", ".join(f"{f['id']} ({f['fields'].get('severity','?')})" for f in raised) or "not raised"
        if raised:
            necessity = "Necessary"
        elif row["lens"] in state.get("lenses_skipped", {}):
            necessity = "lens not run"
        else:
            necessity = "considered, not raised"
        st = "[NOT FOUND]" if row["supports_state"] == "NOT_FOUND" else "[UNVERIFIED]"
        A(f"| {row['label']} | {row['lens']} | {st} | `{cid}` | {raised_txt} | {necessity} |")
    A("")

    # ---- F ----
    A("## Section F - Deferred Controls")
    A("")
    dfile = d / "deferred.md"
    A(dfile.read_text().strip() if dfile.exists()
      else "<!-- FILL: controls considered and judged not yet necessary, each with the "
           "concrete trigger that should revisit it (\"needed when: >5k users / "
           "internet-facing / PCI scope\"). Also name controls deliberately deemed "
           "over-engineering here, so the reader knows they were considered. -->")
    A("")

    # ---- G ----
    A("## Section G - Recovery Posture")
    A("")
    A("| Dimension | Current implied state | Evidence state | Meets stated RPO/RTO? | Gap |")
    A("| --- | --- | --- | --- | --- |")
    for label, cid in RECOVERY_ROWS:
        row = ledger.get(cid)
        if not row:
            A(f"| {label} | not probed | [UNVERIFIED] | <!-- FILL --> | <!-- FILL --> |")
            continue
        if row.get("supports_state") == "none" and row["hit_count"] == 0:
            A(f"| {label} | not applicable - {row.get('note','')} | n/a | n/a | none |")
            continue
        if row["hit_count"] > 0:
            implied = f"signal in repo ({', '.join(h['path'] for h in row['hits'][:2])})"
            st = "[CONFIRMED] present - adequacy assessed by lens"
        elif row["supports_state"] == "NOT_FOUND":
            implied = "nothing found in reviewed scope"
            st = "[NOT FOUND]"
        else:
            implied = "configured outside this repository"
            st = "[UNVERIFIED]"
        A(f"| {label} | {implied} | {st} | <!-- FILL --> | <!-- FILL --> |")
    A("")
    applicable = [cid for _, cid in RECOVERY_ROWS
                  if ledger.get(cid, {}).get("supports_state") != "none"]
    unver_recovery = sum(1 for cid in applicable
                         if ledger.get(cid, {}).get("supports_state") == "UNVERIFIED")
    if unver_recovery >= 3:
        A(f"> {unver_recovery} of {len(applicable)} applicable recovery dimensions could not be "
          "verified from the repository alone. That is itself a finding: the team cannot "
          "currently demonstrate its own recovery posture from version control.")
        A("")

    # ---- H ----
    A("## Section H - Scalability Bottlenecks")
    A("")
    A("<!-- FILL: ordered by what breaks first at 10x then 100x, relative to the scale "
      "envelope in Section A. Include cache stampede scenarios and data-growth "
      "projections where the lenses raised them. -->")
    A("")

    # ---- I ----
    A("## Section I - Technical Debt Register (P2/P3)")
    A("")
    if not debt:
        A("None recorded.")
        A("")
    else:
        A("| ID | Severity | Lens | Finding | Fix |")
        A("| --- | --- | --- | --- | --- |")
        for fd in sorted(debt, key=lambda x: (x["fields"].get("severity", ""), x["id"])):
            A(f"| {fd['id']} | {fld(fd,'severity')} | {fd['lens_file']} | {fd['title']} | {fld(fd,'fix')} |")
        A("")

    # ---- J ----
    A("## Section J - 30/60/90 Remediation Plan")
    A("")
    A("<!-- FILL: prioritised plan. The evidence-to-obtain table below is generated from "
      "the unverified findings; fold it into the 30-day column, because resolving an "
      "unknown is remediation too. -->")
    A("")
    if unverified:
        A("### Evidence to obtain")
        A("")
        A("| Finding | Severity | What would resolve it |")
        A("| --- | --- | --- |")
        for fd in sorted(unverified, key=lambda x: x["id"]):
            A(f"| {fd['id']} - {fd['title']} | {fld(fd,'severity')} | {fld(fd,'resolve')} |")
        A("")

    # ---- K ----
    A("## Section K - Panel Closing")
    A("")
    ran = {fd["lens_file"] for fd in findings}
    for lens in LENS_ORDER:
        if lens in state.get("lenses_skipped", {}):
            continue
        if lens not in ran and findings:
            continue
        A(f"**{LENS_TITLE[lens]}** - <!-- FILL: \"The scariest thing this system is missing "
          "is ___ (and I know / suspect / cannot determine this because ___)\" -->")
        A("")

    if warnings:
        A("---")
        A("")
        A("<!-- Validation warnings carried into this draft:")
        for where, fid, msg in warnings:
            A(f"  {fid} [{where}]: {msg}")
        A("-->")
        A("")

    report = "\n".join(out)
    (d).mkdir(parents=True, exist_ok=True)
    (d / "report.md").write_text(report)
    # report.json is what the dashboard reads. Writing it here keeps the two
    # renderings of the same audit from ever drifting apart.
    report_json = write_report(root)

    fills = report.count("<!-- FILL")
    print(json.dumps({
        "written_to": str(d / "report.md"),
        "structured_report": str(report_json),
        "findings": len(findings), "p0": len(p0), "p1": len(p1),
        "debt": len(debt), "unverified": len(unverified),
        "fill_markers_remaining": fills,
        "validation_errors": len(errors), "validation_warnings": len(warnings),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
