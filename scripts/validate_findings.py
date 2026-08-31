#!/usr/bin/env python3
"""
validate_findings.py - the gate between "seven agents wrote things down" and
"this is a report someone can act on".

It enforces the rules that are easy to state and easy to quietly break:

  * a CONFIRMED finding cites file:line
  * a NOT FOUND finding cites an absence-ledger row that actually has zero hits
    and that the ledger says supports NOT FOUND rather than UNVERIFIED
  * an UNVERIFIED finding says what evidence would resolve it
  * a P0 articulates a specific failure path and names its compensating control
    (or states there is none)
  * absence is phrased as "not found in reviewed scope", never as "does not exist"
  * the same finding does not appear under two lenses

Errors block the report. Warnings are judgement calls worth a second look.

Usage:
    python3 validate_findings.py <project_root> [--json]
"""
import argparse
import json
import re
import sys
from pathlib import Path

STATES = {"CONFIRMED", "NOT_FOUND", "UNVERIFIED"}
SEVERITIES = {"P0", "P1", "P2", "P3"}
LENS_PREFIX = {
    "SEC": "security", "BE": "backend", "FE": "frontend", "OPS": "devops",
    "QA": "qa", "DB": "database", "AI": "ai-security",
}
LENS_TO_PREFIX = {v: k for k, v in LENS_PREFIX.items()}

HEADING = re.compile(r"^###\s+(PRA-[A-Z]+-\d+)\s*\|\s*(.+?)\s*$")
FIELD = re.compile(r"^([a-z][a-z-]*):\s*(.*)$")

OVERCLAIM = re.compile(
    r"\b(there is no|there are no|does not exist|do not exist|the system has no|"
    r"has never been|is never|no .{0,30} exists\b)", re.IGNORECASE)

EVIDENCE_LOC = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]+:\d+")

# A file path, a dotted symbol, or anything in backticks - the shapes that mean
# an `impact` line was written for an engineer rather than for the reader.
CODE_SHAPED = re.compile(r"`[^`]+`|[\w-]+/[\w./-]+|\b\w+\.(?:ts|tsx|js|jsx|py|go|rb|java|sql|json|yml|yaml|toml)\b")


# The authored JSON uses snake_case; the rules below were written against the
# markdown field names. Mapping once here keeps every rule untouched.
JSON_TO_FIELD = {
    "state": "state", "severity": "severity", "owner": "owner",
    "cross_lens": "cross-lens", "evidence": "evidence", "probe": "probe",
    "impact": "impact", "failure_path": "failure-path",
    "compensating": "compensating", "fix": "fix", "resolve": "resolve", "see": "see",
}


def parse_file(path: Path):
    """Load one findings/<lens>.json into the shape the rules below expect.

    Lenses author JSON, so there is nothing to parse out of prose - a malformed
    file is a hard error rather than a finding silently read as empty.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON "
                         f"(line {exc.lineno}, column {exc.colno})") from exc

    if isinstance(raw, list):
        raw = {"findings": raw}
    if not isinstance(raw, dict) or not isinstance(raw.get("findings", []), list):
        raise ValueError(f"{path.name} must be an object with a 'findings' list")

    findings = []
    for index, item in enumerate(raw.get("findings", []), 1):
        if not isinstance(item, dict):
            raise ValueError(f"{path.name}: finding #{index} is not an object")
        fields = {}
        for json_key, field_key in JSON_TO_FIELD.items():
            value = item.get(json_key)
            if isinstance(value, list):
                value = ", ".join(str(v).strip() for v in value if str(v).strip())
            fields[field_key] = "" if value is None else str(value).strip()
        findings.append({
            "id": str(item.get("id") or f"<finding #{index}>"),
            "title": str(item.get("title") or ""),
            "_line": index,
            "_file": path.name,
            "fields": fields,
        })
    return findings


def empty(v):
    return v is None or v.strip() in ("", "-", "n/a", "N/A", "none")


def validate(root: Path):
    d = root / ".readiness-audit"
    fdir = d / "findings"
    errors, warnings = [], []

    ledger = {}
    lpath = d / "evidence" / "absence-ledger.json"
    if lpath.exists():
        try:
            ledger = json.loads(lpath.read_text(encoding="utf-8")).get("controls", {})
        except json.JSONDecodeError:
            errors.append(("absence-ledger.json", "-", "ledger is not valid JSON; re-run absence_probe.py"))
    else:
        errors.append(("absence-ledger.json", "-",
                       "no absence ledger found; run absence_probe.py before validating findings"))

    if not fdir.exists():
        errors.append(("findings/", "-", "no findings directory; lenses have not run"))
        return errors, warnings, []

    all_findings = []
    seen_ids = {}
    for f in sorted(fdir.glob("*.json")):
        lens = f.stem
        try:
            parsed = parse_file(f)
        except ValueError as exc:
            errors.append((f.name, "-", str(exc)))
            continue
        for fd in parsed:
            fd["lens_file"] = lens
            all_findings.append(fd)

    for fd in all_findings:
        fid, F, where = fd["id"], fd["fields"], f"{fd['_file']}:{fd['_line']}"

        def err(msg):
            errors.append((where, fid, msg))

        def warn(msg):
            warnings.append((where, fid, msg))

        if fid in seen_ids:
            err(f"duplicate finding id (also at {seen_ids[fid]})")
        seen_ids[fid] = where

        prefix = fid.split("-")[1]
        if prefix not in LENS_PREFIX:
            err(f"unknown lens prefix {prefix!r}; expected one of {sorted(LENS_PREFIX)}")
        elif fd["lens_file"] in LENS_TO_PREFIX and LENS_PREFIX[prefix] != fd["lens_file"]:
            err(f"id prefix {prefix} does not match the file it lives in ({fd['lens_file']})")

        state = F.get("state", "").strip().upper().replace(" ", "_")
        if state not in STATES:
            err(f"state must be one of {sorted(STATES)}, got {F.get('state')!r}")
        sev = F.get("severity", "").strip().upper()
        if sev not in SEVERITIES:
            err(f"severity must be one of {sorted(SEVERITIES)}, got {F.get('severity')!r}")

        if empty(F.get("fix")):
            err("no fix given; a finding without a concrete remediation is an observation, not a finding")
        if empty(F.get("owner")):
            err("no owner lens declared")
        if not fd["title"].strip():
            err("no title; the dashboard has nothing to name this finding")

        # `impact` is the only field a non-engineer reads. A finding without one
        # reaches the dashboard as a headline nobody can act on.
        impact = F.get("impact", "")
        if empty(impact):
            err("no impact given; state in one or two sentences what a user, the "
                "business, or the data loses - the mechanism belongs in failure-path")
        elif impact.strip() == F.get("failure-path", "").strip():
            err("impact repeats failure-path verbatim; impact is the plain-language "
                "cost, failure-path is the mechanism")
        elif CODE_SHAPED.search(impact):
            warn("impact names a file, path, or code symbol; rewrite it for someone "
                 "who will never open the codebase")

        if state == "CONFIRMED":
            ev = F.get("evidence", "")
            if empty(ev):
                err("CONFIRMED requires evidence")
            elif not EVIDENCE_LOC.search(ev):
                err(f"CONFIRMED evidence must cite file:line, got {ev!r}")

        if state == "NOT_FOUND":
            probe = F.get("probe", "").strip()
            if empty(probe):
                err("NOT_FOUND requires a probe id from the absence ledger; "
                    "an uncited absence is a guess")
            elif probe not in ledger:
                err(f"probe {probe!r} is not in the absence ledger")
            else:
                row = ledger[probe]
                if row["hit_count"] > 0:
                    err(f"probe {probe!r} has {row['hit_count']} hits in the ledger "
                        f"(e.g. {', '.join(h['path'] for h in row['hits'][:2])}); "
                        "this control is present, so NOT_FOUND is wrong")
                elif row["supports_state"] == "none":
                    err(f"probe {probe!r} is a branch selector or a control that does not "
                        f"apply here ({row.get('note','')}); it cannot support a finding")
                elif row["supports_state"] == "UNVERIFIED":
                    err(f"ledger says probe {probe!r} is normally configured outside this "
                        "repo and no IaC was found, so absence here proves nothing; "
                        "restate as UNVERIFIED with a resolve: line")
            blob = f"{fd['title']} {F.get('failure-path','')} {F.get('fix','')}"
            if OVERCLAIM.search(blob):
                err("absence is phrased as established fact; rewrite as "
                    "\"No X found in reviewed scope\"")

        if state == "UNVERIFIED":
            if empty(F.get("resolve")):
                err("UNVERIFIED requires resolve: what specific evidence would settle this "
                    "(CI config, cloud backup policy, IaC repo, runtime dashboards)")
            if sev in ("P0", "P1"):
                warn(f"UNVERIFIED at {sev}: report this as a potential {sev} RISK, "
                     "never as an established defect")
            blob = f"{fd['title']} {F.get('failure-path','')}"
            if OVERCLAIM.search(blob):
                err("UNVERIFIED finding is written in confirmed language; soften to a risk statement")

        if sev == "P0":
            if empty(F.get("failure-path")):
                err("P0 requires failure-path: the specific, articulable path to catastrophic "
                    "loss - if you cannot write it, this is a P1")
            if empty(F.get("compensating")):
                err("P0 requires compensating: name the mitigating control, or state that none "
                    "was found - a plausible compensating control demotes this to P1")

    # cross-lens duplication: same underlying thing reported twice
    def fingerprint(fd):
        F = fd["fields"]
        probe = F.get("probe", "").strip()
        if probe and probe != "-":
            return f"probe:{probe}"
        ev = F.get("evidence", "")
        m = EVIDENCE_LOC.search(ev)
        if m:
            return "loc:" + m.group(0).rsplit(":", 1)[0]
        return None

    buckets = {}
    for fd in all_findings:
        fp = fingerprint(fd)
        if fp:
            buckets.setdefault(fp, []).append(fd)
    for fp, group in buckets.items():
        lenses = {fd["lens_file"] for fd in group}
        if len(group) > 1 and len(lenses) > 1:
            ids = [fd["id"] for fd in group]
            referenced = any(
                any(other in fd["fields"].get("see", "") for other in ids if other != fd["id"])
                for fd in group)
            if not referenced:
                errors.append((", ".join(f"{fd['_file']}:{fd['_line']}" for fd in group),
                               ", ".join(ids),
                               f"same underlying issue ({fp}) reported by {sorted(lenses)}; "
                               "one lens owns it fully, the others add see: <owner-id>"))

    stats = {
        "total": len(all_findings),
        "by_state": {},
        "by_severity": {},
        "by_lens": {},
    }
    for fd in all_findings:
        F = fd["fields"]
        for key, val in (("by_state", F.get("state", "?")),
                         ("by_severity", F.get("severity", "?")),
                         ("by_lens", fd["lens_file"])):
            stats[key][val] = stats[key].get(val, 0) + 1

    return errors, warnings, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project_root")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    errors, warnings, stats = validate(root)

    if args.json:
        print(json.dumps({"errors": errors, "warnings": warnings, "stats": stats}, indent=2))
    else:
        if stats:
            print(f"findings: {stats['total']}  states: {stats['by_state']}  "
                  f"severities: {stats['by_severity']}")
            print()
        if errors:
            print(f"ERRORS ({len(errors)}) - the report is blocked until these are fixed:")
            for where, fid, msg in errors:
                print(f"  [{where}] {fid}: {msg}")
            print()
        if warnings:
            print(f"WARNINGS ({len(warnings)}):")
            for where, fid, msg in warnings:
                print(f"  [{where}] {fid}: {msg}")
            print()
        if not errors and not warnings:
            print("clean - every finding is evidence-backed and correctly scoped.")
        elif not errors:
            print("no blocking errors.")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
