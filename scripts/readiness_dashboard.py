#!/usr/bin/env python3
"""Read-only snapshot helpers for the local production-readiness dashboard."""

import json
import re
from pathlib import Path


LENS_ORDER = (
    "security",
    "backend",
    "frontend",
    "devops",
    "qa",
    "database",
    "ai-security",
)

LENS_LABELS = {
    "security": "Security",
    "backend": "Backend",
    "frontend": "Frontend",
    "devops": "DevOps",
    "qa": "QA",
    "database": "Database",
    "ai-security": "AI security",
}


def read_text_if_present(path: Path) -> str | None:
    """Return a UTF-8 file's text, or ``None`` when it cannot be read."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def load_state(path: Path):
    text = read_text_if_present(path)
    if text is None:
        return None
    try:
        state = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return state if isinstance(state, dict) else None


def _empty_artifact():
    return {"available": False, "content": None}


def unavailable_snapshot(project_root: Path, message: str) -> dict:
    audit_root = (project_root / ".readiness-audit").resolve()
    return {
        "status": "unavailable",
        "auditRoot": str(audit_root),
        "updatedAt": None,
        "stage": {"name": None, "status": None, "note": None},
        "executionMode": None,
        "lenses": [],
        "summary": {"p0": 0, "p1": 0, "p2": 0, "unverified": 0, "verdict": None},
        "artifacts": {
            "report": _empty_artifact(),
            "findings": [],
            "evidenceLedger": _empty_artifact(),
        },
        "message": message,
    }


def _latest_stage_note(state: dict) -> str | None:
    notes = state.get("notes")
    if not isinstance(notes, list):
        return None
    stage = state.get("stage")
    for note in reversed(notes):
        if isinstance(note, dict) and note.get("stage") == stage and note.get("note"):
            return note["note"]
    return None


def _finding_files(audit_root: Path):
    directory = audit_root / "findings"
    try:
        return sorted(directory.glob("*.md"))
    except OSError:
        return []


def _validate_state(state: dict) -> str | None:
    if state.get("stage_status") not in {"in_progress", "complete"}:
        return "Audit state has an unavailable stage status; wait for the audit to write a recognized stage status."
    if "lenses_to_run" not in state or "lenses_skipped" not in state:
        return "Audit state has an invalid lens configuration; both lens configuration keys are required."
    selected = state["lenses_to_run"]
    skipped = state["lenses_skipped"]
    if not isinstance(selected, list) or any(lens not in LENS_ORDER for lens in selected):
        return "Audit state has an invalid lens configuration; lenses_to_run must contain known lens IDs."
    if not isinstance(skipped, dict) or any(lens not in LENS_ORDER for lens in skipped):
        return "Audit state has an invalid lens configuration; lenses_skipped must contain known lens IDs."
    return None


def _lens_status(lens: str, state: dict, finding_text: dict[str, str], unavailable_findings: set[str]) -> str:
    skipped = state.get("lenses_skipped") or {}
    if f"findings/{lens}.md" in unavailable_findings:
        return "unavailable"
    if lens in skipped:
        return "skipped"
    if f"findings/{lens}.md" in finding_text:
        return "complete"
    selected = state.get("lenses_to_run") or []
    if lens in selected and state.get("stage") == "3-lenses" and state.get("stage_status") == "in_progress":
        return "running"
    return "waiting"


def _verdict(report: str | None) -> str | None:
    if not report:
        return None
    match = re.search(r"(?im)^\s*#{1,6}\s*Verdict\s*$\n+\s*([^\n]+)", report)
    return match.group(1).strip() if match else None


def snapshot_from_state(project_root: Path, audit_root: Path, state: dict) -> dict:
    finding_text: dict[str, str] = {}
    findings = []
    unavailable_findings = set()
    unreadable_artifacts = False
    counts = {"p0": 0, "p1": 0, "p2": 0, "unverified": 0}
    for path in _finding_files(audit_root):
        text = read_text_if_present(path)
        if text is None:
            relative = f"findings/{path.name}"
            unavailable_findings.add(relative)
            findings.append({"path": relative, "available": False, "content": None})
            unreadable_artifacts = True
            continue
        relative = f"findings/{path.name}"
        finding_text[relative] = text
        findings.append({"path": relative, "available": True, "content": text})
        for severity in ("p0", "p1", "p2"):
            counts[severity] += len(re.findall(
                rf"(?im)(?:^\s*#+\s*|^\s*severity\s*:\s*){severity}\b", text
            ))
        counts["unverified"] += len(re.findall(r"(?i)\bUNVERIFIED\b", text))

    report_text = read_text_if_present(audit_root / "report.md")
    ledger_text = read_text_if_present(audit_root / "evidence" / "absence-ledger.md")
    report_path = audit_root / "report.md"
    ledger_path = audit_root / "evidence" / "absence-ledger.md"
    if report_path.exists() and report_text is None:
        unreadable_artifacts = True
    if ledger_path.exists() and ledger_text is None:
        unreadable_artifacts = True
    stage_status = state.get("stage_status")
    status = "complete" if stage_status == "complete" else "running"
    artifacts = {
        "report": {"available": report_text is not None, "content": report_text},
        "findings": findings,
        "evidenceLedger": {"available": ledger_text is not None, "content": ledger_text},
    }
    lenses = [
        {
            "id": lens,
            "label": LENS_LABELS[lens],
            "status": _lens_status(lens, state, finding_text, unavailable_findings),
            "findingPath": f"findings/{lens}.md",
        }
        for lens in LENS_ORDER
    ]
    return {
        "status": status,
        "auditRoot": str(audit_root.resolve()),
        "updatedAt": state.get("updated_at"),
        "stage": {
            "name": state.get("stage"),
            "status": stage_status,
            "note": _latest_stage_note(state),
        },
        "executionMode": state.get("execution_mode"),
        "lenses": lenses,
        "summary": {**counts, "verdict": _verdict(report_text)},
        "artifacts": artifacts,
        "message": (
            "Audit artifacts are partially unavailable; wait for readable files before relying on this snapshot."
            if unreadable_artifacts
            else "Audit complete." if status == "complete"
            else "Audit is still running; wait for the remaining stages to finish."
        ),
    }


def build_snapshot(project_root: Path) -> dict:
    audit_root = project_root / ".readiness-audit"
    if not audit_root.is_dir():
        return unavailable_snapshot(project_root, "No .readiness-audit directory exists for this project yet.")
    state_path = audit_root / "state.json"
    state = load_state(state_path)
    if state is None:
        if state_path.exists():
            message = "Audit state is not readable yet; wait for preflight to finish."
        else:
            message = "Audit state is not available yet; wait for preflight to finish."
        return unavailable_snapshot(project_root, message)
    invalid_state = _validate_state(state)
    if invalid_state:
        return unavailable_snapshot(project_root, invalid_state)
    return snapshot_from_state(project_root, audit_root, state)
