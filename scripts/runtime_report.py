#!/usr/bin/env python3
"""
runtime_report.py - the SQA style report and bug list for the runtime lens.

The runtime lens observes the live target. This module turns those
observations into two documents a release reader can use directly.

1. `.readiness-audit/runtime-sqa.md`. Executive summary, scope, screens
   tested, findings grouped by type, bug summary, limitations, risk
   assessment, and a release call. The call is plain arithmetic over
   severities. Any P0 gives NO-GO. P1s with no P0 give GO WITH KNOWN
   RISKS. Anything else gives GO.
2. `.readiness-audit/runtime-bugs.csv`. One row per validated runtime
   finding. The header comes from BUG_CSV_COLUMNS, and each row is built
   in that same order, so the column order holds by construction. A run
   with no findings still writes the header.

Every interpolated string passes through the redact helpers from
runtime_context. Credential looking values leave the trail as
[REDACTED]. Reports carry the credential reference only, never a value.

The walk stays read only. This module reads findings, coverage, context,
scope, and state. It writes two derived files. It changes no audit
state.

Usage:
    python3 runtime_report.py <project_root>

All helpers are importable. Tests call generate directly.
"""
import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from finding_store import load_lens  # noqa: E402
from runtime_context import load_runtime_context, redact, redact_text  # noqa: E402

AUDIT_DIRNAME = ".readiness-audit"
FINDINGS_REL = Path("findings/runtime.json")
COVERAGE_FILENAME = "runtime-coverage.json"
SQA_FILENAME = "runtime-sqa.md"
BUGS_FILENAME = "runtime-bugs.csv"

# The single template for the bug CSV. The writer uses this tuple for the
# header and for each row, so header and rows always agree in name and
# order. Add a column here once and every row follows. Never build a
# header by hand elsewhere.
BUG_CSV_COLUMNS = (
    "id", "title", "severity", "state", "page", "viewport",
    "observed_behavior", "endpoint", "status", "expected", "actual",
    "evidence", "fix", "resolve",
)

CALL_GO = "GO"
CALL_KNOWN_RISKS = "GO WITH KNOWN RISKS"
CALL_NO_GO = "NO-GO"
VALID_CALLS = (CALL_GO, CALL_KNOWN_RISKS, CALL_NO_GO)

# Finding groups in report order. UI covers layout and navigation, the two
# ways a page can look wrong or lead nowhere. API covers data checks that
# cite a read endpoint and a status. Data covers the remaining number
# mismatches. Accessibility holds findings that name an access barrier.
GROUPS = (
    ("responsive", "Responsive Findings"),
    ("data", "Data Findings"),
    ("api", "API Findings"),
    ("console", "Console Findings"),
    ("ui", "UI Findings"),
    ("accessibility", "Accessibility Findings"),
)

_ACCESSIBILITY_HINTS = (
    "accessib", "aria", "contrast", "wcag", "screen reader",
    "keyboard trap", "focus order", "focus visible",
)

_URL_RE = re.compile(r"https?://[^\s,)\]]+")
_ROUTE_RE = re.compile(
    r"(?<![A-Za-z0-9_/:.~+-])(/(?:[A-Za-z0-9_\-~]+(?:/[A-Za-z0-9_\-~]*)*)?)"
    r"(?![A-Za-z0-9_\-~/:.])"
)
_VIEWPORT_RE = re.compile(
    r"\b\d{3,4}\s?[xX]\s?\d{3,4}\b|\b(?:mobile|desktop|tablet)\b"
)
_ENDPOINT_RE = re.compile(r"\bGET\s+(\S+)", re.IGNORECASE)
_API_TOKEN_RE = re.compile(r"(/[A-Za-z0-9_\-~./?=&%#]+)")
_STATUS_RE = re.compile(r"\bstatus\s+(\d{3}|\(unknown[^)]*\))",
                         re.IGNORECASE)
_RETURNS_RE = re.compile(r"\breturns?\s+([^\s,;]+)", re.IGNORECASE)
_SHOWS_RE = re.compile(r"\bshows?\s+([^\s,;]+)", re.IGNORECASE)
_SAW_RE = re.compile(r"\bsaw\s+([^\s,;]+)", re.IGNORECASE)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _audit_dir(root: Path) -> Path:
    return Path(root).expanduser().resolve() / AUDIT_DIRNAME


def _clean_token(text: str) -> str:
    return str(text or "").strip().strip("([<\"'").rstrip(".,;:!?)]}\"'").strip()


def _blob(finding: dict) -> str:
    parts = [finding.get("title") or ""]
    if finding.get("failure_path"):
        parts.append(str(finding["failure_path"]))
    for item in finding.get("evidence") or []:
        parts.append(str(item))
    return "\n".join(part for part in parts if part)


def load_runtime_findings(root: Path) -> list[dict]:
    """Read validated runtime findings. Empty when the lens has not run."""
    path = _audit_dir(root) / FINDINGS_REL
    if not path.exists():
        return []
    return redact(load_lens(path))


def load_coverage(root: Path) -> dict:
    """Read the walk coverage file. Empty defaults when it is missing."""
    path = _audit_dir(root) / COVERAGE_FILENAME
    empty = {"target": "", "viewports": [], "coverage": [],
             "updated_at": None}
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return empty
    if not isinstance(raw, dict):
        return empty
    rows = raw.get("coverage")
    if not isinstance(rows, list):
        rows = []
    viewports = raw.get("viewports")
    if not isinstance(viewports, list):
        viewports = []
    return redact({
        "target": str(raw.get("target") or ""),
        "viewports": [str(v) for v in viewports],
        "coverage": [r for r in rows if isinstance(r, dict)],
        "updated_at": raw.get("updated_at"),
    })


def load_skip_reason(root: Path):
    """The recorded runtime skip reason, or None when it runs."""
    path = _audit_dir(root) / "state.json"
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None
    skipped = state.get("lenses_skipped") or {}
    if not isinstance(skipped, dict):
        return None
    reason = skipped.get("runtime")
    return redact_text(str(reason)) if reason else None


def load_scope_text(root: Path) -> str:
    """The audit scope file, redacted. Empty when it was not written."""
    path = _audit_dir(root) / "scope.md"
    try:
        return redact_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return ""


def load_context(root: Path) -> dict:
    """The runtime target record, redacted. Empty when intake is missing."""
    data = load_runtime_context(root)
    if not isinstance(data, dict):
        return {}
    return redact(data)


def derive_call(findings: list[dict]) -> str:
    """The release call as arithmetic over severities.

    Any P0 gives NO-GO. P1s with no P0 give GO WITH KNOWN RISKS. A clean
    sheet, including a skipped lens with no findings, gives GO. The
    limitations section still names what the lens did not see.
    """
    severities = {str(f.get("severity") or "").upper() for f in findings}
    if "P0" in severities:
        return CALL_NO_GO
    if "P1" in severities:
        return CALL_KNOWN_RISKS
    return CALL_GO


def classify_finding(finding: dict) -> str:
    """Place one finding in its SQA group.

    Titles from the walk carry the category prefix, so the title decides
    first. Free text decides only for accessibility barriers and for the
    split between plain data mismatches and API checks that cite an
    endpoint. Anything unknown reads as UI, so no finding goes missing.
    """
    title = str(finding.get("title") or "")
    lowered_title = title.lower()
    blob = _blob(finding).lower()
    if any(hint in blob for hint in _ACCESSIBILITY_HINTS):
        return "accessibility"
    if lowered_title.startswith("console report"):
        return "console"
    if lowered_title.startswith("responsive defect"):
        return "responsive"
    if lowered_title.startswith("data mismatch"):
        if "get " in blob and ("status" in blob or "/api" in blob
                               or "endpoint" in blob):
            return "api"
        return "data"
    if lowered_title.startswith("layout defect"):
        return "ui"
    if lowered_title.startswith("broken route"):
        return "ui"
    if "console " in blob:
        return "console"
    if "responsive" in lowered_title:
        return "responsive"
    if "data" in lowered_title:
        return "data"
    return "ui"


def extract_page(finding: dict) -> str:
    """The live page token for one finding. Falls back to /."""
    text = " ".join(str(e) for e in finding.get("evidence") or [])
    extra = str(finding.get("failure_path") or "")
    match = _URL_RE.search(text) or _URL_RE.search(extra)
    if match:
        return _clean_token(match.group(0))
    match = _ROUTE_RE.search(text) or _ROUTE_RE.search(extra)
    if match:
        token = _clean_token(match.group(1))
        return token or "/"
    return "/"


def extract_viewport(finding: dict) -> str:
    """The viewport token for one finding. Empty when none is cited."""
    text = " ".join(str(e) for e in finding.get("evidence") or [])
    extra = str(finding.get("failure_path") or "")
    match = _VIEWPORT_RE.search(text) or _VIEWPORT_RE.search(extra)
    return match.group(0).strip() if match else ""


def extract_endpoint(blob: str) -> str:
    """The read endpoint cited by a data check. Empty for other types."""
    match = _ENDPOINT_RE.search(blob or "")
    if match:
        return _clean_token(match.group(1))
    if "/api" in (blob or ""):
        for token in _API_TOKEN_RE.findall(blob):
            cleaned = _clean_token(token)
            if "api" in cleaned:
                return cleaned
    return ""


def extract_status(blob: str) -> str:
    """The HTTP status cited by a data check. Empty when none is cited."""
    match = _STATUS_RE.search(blob or "")
    return match.group(1).strip() if match else ""


def extract_expected(blob: str) -> str:
    """The API value a data check treats as correct."""
    match = _RETURNS_RE.search(blob or "")
    return _clean_token(match.group(1)) if match else ""


def extract_actual(blob: str) -> str:
    """The displayed value a data check treats as wrong."""
    match = _SHOWS_RE.search(blob or "")
    if not match:
        match = _SAW_RE.search(blob or "")
    return _clean_token(match.group(1)) if match else ""


def finding_to_bug_row(finding: dict) -> dict:
    """Project one validated finding onto the bug CSV columns.

    Every cell passes through redact_text, so credential looking values
    leave the file as [REDACTED]. Missing endpoint data stays empty
    rather than guessed.
    """
    evidence = [str(e) for e in finding.get("evidence") or []]
    blob = "\n".join(evidence + [str(finding.get("failure_path") or "")])
    observed = evidence[0] if evidence else str(
        finding.get("failure_path") or finding.get("title") or "")
    row = {
        "id": str(finding.get("id") or ""),
        "title": str(finding.get("title") or ""),
        "severity": str(finding.get("severity") or ""),
        "state": str(finding.get("state") or ""),
        "page": extract_page(finding),
        "viewport": extract_viewport(finding),
        "observed_behavior": observed,
        "endpoint": extract_endpoint(blob),
        "status": extract_status(blob),
        "expected": extract_expected(blob),
        "actual": extract_actual(blob),
        "evidence": " | ".join(evidence),
        "fix": str(finding.get("fix") or ""),
        "resolve": str(finding.get("resolve") or ""),
    }
    return {key: redact_text(str(value or "")) for key, value in row.items()}


def _counts(findings: list[dict]) -> dict:
    counts = {"total": len(findings), "p0": 0, "p1": 0, "p2": 0,
              "p3": 0, "confirmed": 0, "unverified": 0}
    for item in findings:
        sev = str(item.get("severity") or "").lower()
        if sev in ("p0", "p1", "p2", "p3"):
            counts[sev] += 1
        state = str(item.get("state") or "").upper()
        if state == "CONFIRMED":
            counts["confirmed"] += 1
        elif state == "UNVERIFIED":
            counts["unverified"] += 1
    return counts


def _render_finding_block(finding: dict) -> str:
    page = redact_text(extract_page(finding))
    viewport = redact_text(extract_viewport(finding))
    evidence = [redact_text(str(e)) for e in finding.get("evidence") or []]
    observed = evidence[0] if evidence else redact_text(
        str(finding.get("failure_path") or ""))
    lines = [
        "### %s | %s" % (redact_text(str(finding.get("id") or "")),
                          redact_text(str(finding.get("title") or ""))),
        "",
        "- Severity: %s" % redact_text(str(finding.get("severity") or "")),
        "- State: %s" % redact_text(str(finding.get("state") or "")),
        "- Page: %s" % (page or "/"),
        "- Viewport: %s" % (viewport or "not cited"),
        "- Observed: %s" % observed,
    ]
    if evidence:
        lines.append("- Evidence: %s" % " | ".join(evidence))
    if finding.get("failure_path"):
        lines.append("- Cause: %s" % redact_text(
            str(finding.get("failure_path"))))
    lines.append("- Fix: %s" % redact_text(str(finding.get("fix") or "")))
    if finding.get("resolve"):
        lines.append("- Settle: %s" % redact_text(str(finding.get("resolve"))))
    lines.append("")
    return "\n".join(lines)


def build_sqa_markdown(findings: list[dict], coverage: dict,
                       context: dict, scope_text: str,
                       skip_reason, generated_at=None) -> str:
    """Compose the full SQA markdown for the runtime lens.

    All inputs should already be redacted. The builder redacts again at
    each interpolation, so a missed caller still cannot leak a value.
    """
    counts = _counts(findings)
    call = derive_call(findings)
    stamp = generated_at or _now()
    target = redact_text(str((context or {}).get("url") or ""))
    environment = redact_text(str((context or {}).get("environment") or ""))
    role = redact_text(str((context or {}).get("role") or ""))
    credential_ref = redact_text(str((context or {}).get("credential_ref")
                                     or ""))
    scope_notes = redact_text(str((context or {}).get("scope_notes") or ""))
    rows = (coverage or {}).get("coverage") or []
    viewports = (coverage or {}).get("viewports") or []
    clean = sum(1 for r in rows if r.get("status") == "covered")
    flagged = sum(1 for r in rows if r.get("status") == "finding")

    out = []
    A = out.append
    A("# Runtime Lens SQA Report")
    A("")
    A("Generated: %s" % redact_text(str(stamp)))
    A("Target: %s" % (target or "No live target recorded."))
    if environment or role:
        A("Environment: %s  Role: %s" % (environment or "not stated",
                                         role or "not stated"))
    if credential_ref:
        A("Credential reference: %s" % credential_ref)
    A("")
    A("Release call: %s" % call)
    A("")

    A("## Executive Summary")
    A("")
    if skip_reason:
        A("The runtime lens is skipped. %s" % redact_text(str(skip_reason)))
        A("This report records no live observation.")
        A("The release call rests on static lenses only.")
    elif not findings and not rows:
        A("The runtime lens has not walked a target yet.")
        A("This report records no live observation.")
        A("Run the walk before release, or record a skip with a reason.")
    elif not findings:
        A("The runtime lens opens the live target and reads each screen.")
        A("It checks layout, navigation, console output, displayed data, "
          "API totals, and small screen behavior.")
        A("It performs reads only and activates no control.")
        A("The walk covers %d screen and viewport pairs with no defect. "
          "The covered screens show no release risk." % len(rows))
    else:
        A("The runtime lens opens the live target and reads each screen.")
        A("It checks layout, navigation, console output, displayed data, "
          "API totals, and small screen behavior.")
        A("It performs reads only and activates no control.")
        A("The walk records %d findings (%d P0, %d P1, %d P2 or P3). "
          "The release call follows from these counts."
          % (counts["total"], counts["p0"], counts["p1"],
             counts["p2"] + counts["p3"]))
    A("")

    A("## Scope")
    A("")
    if target:
        A("In scope: the live target at %s." % target)
    else:
        A("In scope: the live target, once intake records its URL.")
    if environment:
        A("Environment under test: %s." % environment)
    if scope_notes:
        A("Intake notes: %s" % scope_notes)
    A("The lens reads pages, console logs, and read only network logs.")
    A("One login POST is allowed at most.")
    A("Out of scope: source review, load tests, and any state change.")
    if scope_text and scope_text.strip():
        A("")
        A("Audit scope file states:")
        A("")
        A(scope_text.strip())
    A("")

    A("## Screens Tested")
    A("")
    if not rows:
        A("No coverage file was found.")
        A("The lens records no walked screen.")
    else:
        A("The walk covers %d screen and viewport pairs. "
          "%d are clean. %d hold findings."
          % (len(rows), clean, flagged))
        if viewports:
            A("Viewports: %s." % ", ".join(
                redact_text(str(v)) for v in viewports))
        A("")
        for row in rows:
            screen = redact_text(str(row.get("screen") or ""))
            viewport = redact_text(str(row.get("viewport") or ""))
            status = redact_text(str(row.get("status") or ""))
            notes = redact_text(str(row.get("notes") or ""))
            line = "- %s at %s: %s." % (screen or "/", viewport or "?",
                                        status or "unknown")
            if notes:
                line += " %s" % notes
            A(line)
    A("")

    grouped = {key: [] for key, _ in GROUPS}
    for item in sorted(findings, key=lambda f: str(f.get("id") or "")):
        grouped[classify_finding(item)].append(item)
    for key, heading in GROUPS:
        A("## %s" % heading)
        A("")
        items = grouped[key]
        if not items:
            A("None recorded.")
            A("")
            continue
        for item in items:
            A(_render_finding_block(item))

    A("## Bug Summary")
    A("")
    if not findings:
        A("No runtime bug was recorded.")
    else:
        A("The walk validates %d bugs (%d confirmed, %d unverified)."
          % (counts["total"], counts["confirmed"], counts["unverified"]))
        A("")
        A("| ID | Severity | State | Title |")
        A("| --- | --- | --- | --- |")
        for item in sorted(findings, key=lambda f: str(f.get("id") or "")):
            A("| %s | %s | %s | %s |" % (
                redact_text(str(item.get("id") or "")),
                redact_text(str(item.get("severity") or "")),
                redact_text(str(item.get("state") or "")),
                redact_text(str(item.get("title") or ""))))
    A("")

    A("## Limitations")
    A("")
    if skip_reason:
        A("- The lens did not run. %s" % redact_text(str(skip_reason)))
    if not rows:
        A("- No coverage file exists, so no screen counts as walked.")
    else:
        A("- Clean screens read as covered, not as skipped.")
        A("- The walk covers the listed screens only.")
    if counts["unverified"]:
        A("- %d findings are UNVERIFIED and need a revisit with fresh "
          "evidence." % counts["unverified"])
    else:
        A("- Every recorded finding is reproduced.")
    A("- The lens sees the live target only. It states no source cause.")
    A("- Secrets never appear in this file. Values read as [REDACTED].")
    A("")

    A("## Risk Assessment")
    A("")
    if call == CALL_NO_GO:
        titles = ", ".join(redact_text(str(f.get("id") or ""))
                           for f in findings
                           if str(f.get("severity") or "") == "P0")
        A("P0 defects block release.")
        A("The live target shows %d blockers (%s)."
          % (counts["p0"], titles or "see findings"))
        A("A release exposes users to these defects.")
    elif call == CALL_KNOWN_RISKS:
        titles = ", ".join(redact_text(str(f.get("id") or ""))
                           for f in findings
                           if str(f.get("severity") or "") == "P1")
        A("P1 defects allow release with known risks.")
        A("The team accepts %s before release."
          % (titles or "the listed P1 items"))
        A("Each accepted risk needs an owner and a fix date.")
    else:
        A("No P0 or P1 defect remains on the covered screens.")
        if counts["unverified"]:
            A("UNVERIFIED items still need evidence before they close.")
        else:
            A("The covered screens show no release risk.")
    A("")

    A("## Release Call")
    A("")
    A("Release call: %s" % call)
    A("")
    if call == CALL_NO_GO:
        A("Reason: P0 defects are present, so the target is not shippable.")
    elif call == CALL_KNOWN_RISKS:
        A("Reason: P1 defects are present with no P0, so the team ships "
          "with named risks.")
    else:
        A("Reason: no P0 or P1 defect blocks the covered screens.")
    A("")
    return "\n".join(out)


def write_sqa(root: Path, markdown: str) -> Path:
    """Write the SQA markdown under .readiness-audit and return its path."""
    audit = _audit_dir(root)
    audit.mkdir(parents=True, exist_ok=True)
    target = audit / SQA_FILENAME
    target.write_text(markdown, encoding="utf-8")
    return target


def write_bugs_csv(root: Path, findings: list[dict]) -> Path:
    """Write one CSV row per finding plus the header. Return its path.

    The header and each row follow BUG_CSV_COLUMNS in order. A run with
    no findings still writes the header, so consumers always see the
    template.
    """
    audit = _audit_dir(root)
    audit.mkdir(parents=True, exist_ok=True)
    target = audit / BUGS_FILENAME
    ordered = sorted(findings, key=lambda f: str(f.get("id") or ""))
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(list(BUG_CSV_COLUMNS))
        for item in ordered:
            row = finding_to_bug_row(item)
            writer.writerow([row[col] for col in BUG_CSV_COLUMNS])
    return target


def generate(root) -> dict:
    """Build both runtime documents for one project root.

    Reads findings, coverage, context, scope, and the skip reason. Writes
    the SQA markdown and the bug CSV. Returns the paths, the finding
    count, and the release call.
    """
    base = Path(root).expanduser().resolve()
    findings = load_runtime_findings(base)
    coverage = load_coverage(base)
    context = load_context(base)
    scope_text = load_scope_text(base)
    skip_reason = load_skip_reason(base)
    markdown = build_sqa_markdown(findings, coverage, context, scope_text,
                                  skip_reason)
    sqa_path = write_sqa(base, markdown)
    bugs_path = write_bugs_csv(base, findings)
    return {"sqa_path": str(sqa_path), "bugs_path": str(bugs_path),
            "findings": len(findings), "call": derive_call(findings)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root", type=Path)
    args = parser.parse_args(argv)
    try:
        summary = generate(args.project_root)
    except Exception as error:  # noqa: BLE001 - reported as JSON
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps({"ok": True, **summary}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
