#!/usr/bin/env python3
"""
finding_store.py - the structured layer under the audit trail.

Lenses author findings as JSON (`findings/<lens>.json`). That file is the
source of truth: it is what the dashboard renders and what the report is built
from. The markdown a fix agent reads (`findings/<lens>.md`) is *generated* from
it, so the two can never disagree.

The split matters because a human reviewer and a fix agent want different
things from the same finding. The reviewer wants to know that a problem exists,
what it costs them, and enough evidence to believe it. The agent wants every
field. JSON carries both and lets each surface choose.

Usage:
    python3 finding_store.py render <project_root>   # findings/*.json -> findings/*.md
    python3 finding_store.py report <project_root>   # -> report.json
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from severity import FactorError, compute_severity, validate_factors  # noqa: E402

SCHEMA = 1

# The schema of one findings/<lens>.json file, authored by a lens. Bumped from
# 1 because severity is no longer an authored field: a lens now supplies
# `factors` and this module derives severity from them. There is deliberately
# no shim that reads a schema-1 file - inferring factors from a previously
# authored severity would fabricate evidence-grade data, which is exactly the
# failure derived severity exists to prevent. An old file must be re-run or
# archived, not silently reinterpreted.
FINDING_SCHEMA = 2

STATES = {"CONFIRMED", "NOT_FOUND", "UNVERIFIED"}
SEVERITIES = {"P0", "P1", "P2", "P3"}
DECISIONS = {"SHIP", "FIX_THEN_SHIP", "HOLD"}

LENS_ORDER = ["security", "backend", "frontend", "devops", "qa", "database", "ai-security"]
LENS_LABEL = {
    "security": "Security", "backend": "Backend", "frontend": "Frontend",
    "devops": "DevOps", "qa": "QA", "database": "Database",
    "ai-security": "AI security",
}

# Fields a lens may set. `impact` is the one written for a human who will never
# open the codebase; everything else is the technical record.
TEXT_FIELDS = ("title", "impact", "failure_path", "compensating", "fix", "resolve", "see", "probe")
LIST_FIELDS = ("cross_lens", "evidence")


class FindingError(ValueError):
    """A finding file that cannot be trusted enough to render or report on."""


def _text(value):
    """Normalise an optional string field. Absent, null, and '-' all mean unset."""
    if value is None:
        return None
    value = str(value).strip()
    if not value or value == "-":
        return None
    return value


def _list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    return [str(item).strip() for item in value if _text(item)]


def normalise_finding(raw: dict, lens: str) -> dict:
    """Coerce one authored finding into the canonical shape, or raise."""
    if not isinstance(raw, dict):
        raise FindingError(f"{lens}: a finding must be an object, got {type(raw).__name__}")

    fid = _text(raw.get("id"))
    if not fid:
        raise FindingError(f"{lens}: a finding is missing its id")

    state = (_text(raw.get("state")) or "").upper().replace(" ", "_").replace("-", "_")
    if state not in STATES:
        raise FindingError(f"{fid}: state must be one of {sorted(STATES)}, got {state or 'nothing'}")

    # Severity is derived, never authored. A lens that still sets it is
    # trying to make the highest-stakes judgement in the report by hand.
    if _text(raw.get("severity")) is not None:
        raise FindingError(
            f"{fid}: severity must not be set directly; remove it and supply "
            "factors instead, so severity is derived from the rubric"
        )

    factors = raw.get("factors")
    factor_errors = validate_factors(factors)
    if factor_errors:
        raise FindingError(f"{fid}: " + "; ".join(factor_errors))

    try:
        severity = compute_severity(factors, state)
    except FactorError as exc:
        raise FindingError(f"{fid}: {exc}") from exc

    finding = {"id": fid, "state": state, "severity": severity,
               "factors": dict(factors),
               "owner": _text(raw.get("owner")) or lens, "lens": lens}
    for key in TEXT_FIELDS:
        finding[key] = _text(raw.get(key))
    for key in LIST_FIELDS:
        finding[key] = _list(raw.get(key))

    if not finding["title"]:
        raise FindingError(f"{fid}: title is required")
    if not finding["fix"]:
        raise FindingError(f"{fid}: fix is required")
    return finding


def load_lens(path: Path) -> list[dict]:
    """Read one findings/<lens>.json. Returns [] for a file that is not there."""
    lens = path.stem
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        raise FindingError(f"{lens}: cannot read {path.name} ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise FindingError(f"{lens}: {path.name} is not valid JSON (line {exc.lineno}, column {exc.colno})") from exc

    if isinstance(raw, list):
        raw = {"findings": raw}
    if not isinstance(raw, dict):
        raise FindingError(f"{lens}: {path.name} must contain an object or a list")

    schema = raw.get("schema")
    if schema != FINDING_SCHEMA:
        raise FindingError(
            f"{lens}: {path.name} declares schema {schema!r}, expected "
            f"{FINDING_SCHEMA} (findings now carry derived-severity factors "
            "instead of an authored severity). Re-run this lens to regenerate "
            "the file in the current schema, or archive the old audit before "
            "starting a new one."
        )

    findings = raw.get("findings", [])
    if not isinstance(findings, list):
        raise FindingError(f"{lens}: 'findings' must be a list")
    return [normalise_finding(item, lens) for item in findings]


def findings_dir(root: Path) -> Path:
    return root / ".readiness-audit" / "findings"


def load_all(root: Path) -> tuple[list[dict], list[str]]:
    """Every finding across every lens, plus the errors that stopped a file."""
    directory = findings_dir(root)
    findings, errors = [], []
    if not directory.is_dir():
        return findings, errors
    for path in sorted(directory.glob("*.json")):
        try:
            findings.extend(load_lens(path))
        except FindingError as exc:
            errors.append(str(exc))
    return findings, errors


# --------------------------------------------------------------------------
# JSON -> markdown, so a fix agent still gets the trail it expects
# --------------------------------------------------------------------------

def render_markdown(findings: list[dict]) -> str:
    """Render the canonical markdown block format from structured findings."""
    blocks = []
    for f in findings:
        lines = [f"### {f['id']} | {f['title']}"]
        lines.append(f"state: {f['state']}")
        lines.append(f"severity: {f['severity']}")
        lines.append(f"owner: {f['owner']}")
        lines.append(f"cross-lens: {', '.join(f['cross_lens']) or '-'}")
        lines.append(f"evidence: {', '.join(f['evidence']) or '-'}")
        lines.append(f"probe: {f['probe'] or '-'}")
        lines.append(f"impact: {f['impact'] or '-'}")
        lines.append(f"failure-path: {f['failure_path'] or '-'}")
        lines.append(f"compensating: {f['compensating'] or '-'}")
        lines.append(f"fix: {f['fix']}")
        lines.append(f"resolve: {f['resolve'] or '-'}")
        lines.append(f"see: {f['see'] or '-'}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_all(root: Path) -> tuple[list[str], list[str]]:
    directory = findings_dir(root)
    written, errors = [], []
    if not directory.is_dir():
        return written, errors
    for path in sorted(directory.glob("*.json")):
        try:
            findings = load_lens(path)
        except FindingError as exc:
            errors.append(str(exc))
            continue
        target = path.with_suffix(".md")
        target.write_text(render_markdown(findings), encoding="utf-8")
        written.append(str(target))
    return written, errors


# --------------------------------------------------------------------------
# report.json - what the dashboard reads
# --------------------------------------------------------------------------

def load_verdict(root: Path) -> tuple[dict, list[str]]:
    """Read the orchestrator's authored verdict.

    The verdict is a judgement, not arithmetic, so a human or the orchestrator
    writes it - but it is written as data, in `verdict.json`, never scraped back
    out of prose. Every consumer reads the same fields.
    """
    empty = {"decision": None, "headline": None, "summary": None}
    path = root / ".readiness-audit" / "verdict.json"
    if not path.exists():
        return empty, []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        return empty, [f"verdict.json cannot be read ({exc})"]
    except json.JSONDecodeError as exc:
        return empty, [f"verdict.json is not valid JSON (line {exc.lineno}, column {exc.colno})"]
    if not isinstance(raw, dict):
        return empty, ["verdict.json must contain an object"]

    decision = (_text(raw.get("decision")) or "").upper().replace(" ", "_").replace("-", "_")
    errors = []
    if decision and decision not in DECISIONS:
        errors.append(f"verdict.json decision must be one of {sorted(DECISIONS)}, got {decision}")
        decision = None
    return {
        "decision": decision or None,
        "headline": _text(raw.get("headline")),
        "summary": _text(raw.get("summary")),
    }, errors


def compute_decision(findings: list[dict]) -> str:
    """The go/no-go call, as a pure function of severities: any P0 is HOLD,
    P1s with no P0 are FIX_THEN_SHIP, otherwise SHIP.

    This is the rule SKILL.md always described as mechanical. It used to be
    applied by the model when it authored verdict.json by hand; it is now
    computed here so the same findings always produce the same decision.
    """
    severities = {f.get("severity") for f in findings}
    if "P0" in severities:
        return "HOLD"
    if "P1" in severities:
        return "FIX_THEN_SHIP"
    return "SHIP"


def _counts(findings: list[dict]) -> dict:
    counts = {"total": len(findings), "p0": 0, "p1": 0, "p2": 0, "p3": 0,
              "confirmed": 0, "notFound": 0, "unverified": 0}
    for f in findings:
        counts[f["severity"].lower()] += 1
        counts[{"CONFIRMED": "confirmed", "NOT_FOUND": "notFound",
                "UNVERIFIED": "unverified"}[f["state"]]] += 1
    return counts


def _lens_status(lens: str, state: dict, lenses_with_findings: set[str]) -> str:
    if lens in (state.get("lenses_skipped") or {}):
        return "skipped"
    if lens in lenses_with_findings:
        return "complete"
    if (lens in (state.get("lenses_to_run") or [])
            and state.get("stage") == "3-lenses"
            and state.get("stage_status") == "in_progress"):
        return "running"
    return "waiting"


def build_report(root: Path) -> dict:
    audit = root / ".readiness-audit"
    findings, errors = load_all(root)

    state = {}
    state_path = audit / "state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            errors.append("state.json is not readable; lens status is degraded")
    if not isinstance(state, dict):
        state = {}

    verdict, verdict_errors = load_verdict(root)
    errors.extend(verdict_errors)

    # The decision the dashboard shows is computed from the findings, never the
    # one the model typed. It appears only once the audit has finished and
    # written its verdict, so a half-finished run reads as pending rather than
    # claiming SHIP because nothing has been found yet.
    if (state.get("stage_status") == "complete"
            and (root / ".readiness-audit" / "verdict.json").exists()):
        computed = compute_decision(findings)
        authored = verdict.get("decision")
        if authored and authored != computed:
            errors.append(
                f"verdict.json decision {authored} disagrees with the findings, "
                f"which give {computed}")
        verdict = {**verdict, "decision": computed}

    lenses_with_findings = {f["lens"] for f in findings}
    by_lens = {lens: [f for f in findings if f["lens"] == lens] for lens in LENS_ORDER}

    severity_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    state_rank = {"CONFIRMED": 0, "NOT_FOUND": 1, "UNVERIFIED": 2}
    ordered = sorted(
        findings,
        key=lambda f: (severity_rank[f["severity"]], state_rank[f["state"]], f["id"]),
    )

    return {
        "schema": SCHEMA,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repository": str(root),
        "gitRef": state.get("git_ref"),
        "stage": {"name": state.get("stage"), "status": state.get("stage_status")},
        "executionMode": state.get("execution_mode"),
        "updatedAt": state.get("updated_at"),
        "verdict": verdict,
        "counts": _counts(findings),
        "lenses": [
            {
                "id": lens,
                "label": LENS_LABEL[lens],
                "status": _lens_status(lens, state, lenses_with_findings),
                "skippedReason": (state.get("lenses_skipped") or {}).get(lens),
                "counts": _counts(by_lens[lens]),
            }
            for lens in LENS_ORDER
        ],
        "findings": [{**f, "lensLabel": LENS_LABEL.get(f["lens"], f["lens"])} for f in ordered],
        "errors": errors,
    }


def write_report(root: Path) -> Path:
    audit = root / ".readiness-audit"
    audit.mkdir(parents=True, exist_ok=True)
    target = audit / "report.json"
    target.write_text(json.dumps(build_report(root), indent=2) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("render", "report"))
    parser.add_argument("project_root", type=Path)
    args = parser.parse_args(argv)
    root = args.project_root.expanduser().resolve()

    if args.command == "render":
        written, errors = render_all(root)
        print(json.dumps({"written": written, "errors": errors}, indent=2))
        return 1 if errors else 0

    target = write_report(root)
    report = json.loads(target.read_text(encoding="utf-8"))
    print(json.dumps({
        "written_to": str(target),
        "counts": report["counts"],
        "verdict": report["verdict"]["decision"],
        "errors": report["errors"],
    }, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
