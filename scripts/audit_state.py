#!/usr/bin/env python3
"""
audit_state.py - single source of truth for where a readiness audit is up to.

The audit is designed to survive /clear, a crash, or a week-long gap, so the
stage pointer lives on disk rather than in conversation memory. Every stage
reads its inputs from .readiness-audit/ and writes its outputs there before
the next stage starts.

Usage:
    python3 audit_state.py init <project_root> [--execution-mode parallel|sequential]
    python3 audit_state.py status <project_root>
    python3 audit_state.py set-stage <project_root> <stage> <status> [--note TEXT]
    python3 audit_state.py set-lenses <project_root> --run a,b --skip c=reason
    python3 audit_state.py archive <project_root>
"""
import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DIRNAME = ".readiness-audit"
STAGES = [
    "0-preflight",
    "1-context",
    "2-evidence",
    "3-lenses",
    "4-validation",
    "5-report",
]
LENSES = ["security", "backend", "frontend", "devops", "qa", "database", "ai-security"]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _dir(root: Path) -> Path:
    return root / DIRNAME


def _file(root: Path) -> Path:
    return _dir(root) / "state.json"


def _git(root: Path, *args):
    try:
        out = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=15
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _load(root: Path):
    p = _file(root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _save(root: Path, state):
    _dir(root).mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now()
    _file(root).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def cmd_init(root: Path, execution_mode: str):
    existing = _load(root)
    if existing:
        print(json.dumps({"already_initialised": True, "state": existing}, indent=2))
        return 0
    head = _git(root, "rev-parse", "HEAD")
    dirty = _git(root, "status", "--porcelain")
    state = {
        "schema": 1,
        "project_root": str(root.resolve()),
        "created_at": _now(),
        "git_ref": head,
        "dirty_at_start": bool(dirty),
        "dirty_files": (dirty.splitlines() if dirty else []),
        "stage": STAGES[0],
        "stage_status": "in_progress",
        "execution_mode": execution_mode,
        "notes": [],
        "lenses_to_run": [],
        "lenses_skipped": {},
    }
    _save(root, state)
    for sub in ("evidence", "findings"):
        (_dir(root) / sub).mkdir(parents=True, exist_ok=True)
    print(json.dumps({"initialised": True, "state": state}, indent=2))
    return 0


def cmd_status(root: Path):
    state = _load(root)
    if not state:
        print(json.dumps({"exists": False, "hint": "run: audit_state.py init"}, indent=2))
        return 0
    d = _dir(root)
    artefacts = {
        "context.md": (d / "context.md").exists(),
        "scope.md": (d / "scope.md").exists(),
        "evidence/inventory.json": (d / "evidence" / "inventory.json").exists(),
        "evidence/absence-ledger.json": (d / "evidence" / "absence-ledger.json").exists(),
        "evidence/map.md": (d / "evidence" / "map.md").exists(),
        "report.md": (d / "report.md").exists(),
    }
    findings = sorted(p.name for p in (d / "findings").glob("*.md")) if (d / "findings").exists() else []
    print(json.dumps({"exists": True, "state": state, "artefacts": artefacts,
                      "finding_files": findings}, indent=2))
    return 0


def cmd_set_stage(root: Path, stage: str, status: str, note: str | None):
    state = _load(root)
    if not state:
        print("no state.json - run init first", file=sys.stderr)
        return 1
    if stage not in STAGES:
        print(f"unknown stage {stage!r}; expected one of {STAGES}", file=sys.stderr)
        return 1
    state["stage"] = stage
    state["stage_status"] = status
    if note:
        state["notes"].append({"at": _now(), "stage": stage, "note": note})
    _save(root, state)
    print(json.dumps({"stage": stage, "stage_status": status}, indent=2))
    return 0


def cmd_set_lenses(root: Path, run: str | None, skip: list[str]):
    state = _load(root)
    if not state:
        print("no state.json - run init first", file=sys.stderr)
        return 1
    if run:
        wanted = [x.strip() for x in run.split(",") if x.strip()]
        bad = [x for x in wanted if x not in LENSES]
        if bad:
            print(f"unknown lens(es) {bad}; expected from {LENSES}", file=sys.stderr)
            return 1
        state["lenses_to_run"] = wanted
    for entry in skip or []:
        lens, _, reason = entry.partition("=")
        lens = lens.strip()
        if lens not in LENSES:
            print(f"unknown lens {lens!r}", file=sys.stderr)
            return 1
        if not reason.strip():
            print(f"skip for {lens!r} needs a reason: --skip {lens}=<why>", file=sys.stderr)
            return 1
        state["lenses_skipped"][lens] = reason.strip()
    _save(root, state)
    print(json.dumps({"lenses_to_run": state["lenses_to_run"],
                      "lenses_skipped": state["lenses_skipped"]}, indent=2))
    return 0


def cmd_archive(root: Path):
    d = _dir(root)
    if not d.exists():
        print("nothing to archive")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = d / "archive" / stamp
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir()
    for item in d.iterdir():
        if item.name == "archive":
            continue
        shutil.move(str(item), str(dest / item.name))
    print(json.dumps({"archived_to": str(dest)}, indent=2))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init")
    s.add_argument("project_root")
    s.add_argument(
        "--execution-mode", choices=("parallel", "sequential"), default="parallel"
    )
    for name in ("status", "archive"):
        s = sub.add_parser(name)
        s.add_argument("project_root")
    s = sub.add_parser("set-stage")
    s.add_argument("project_root")
    s.add_argument("stage")
    s.add_argument("status")
    s.add_argument("--note")
    s = sub.add_parser("set-lenses")
    s.add_argument("project_root")
    s.add_argument("--run")
    s.add_argument("--skip", action="append", default=[])
    args = ap.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1

    if args.cmd == "init":
        return cmd_init(root, args.execution_mode)
    if args.cmd == "status":
        return cmd_status(root)
    if args.cmd == "archive":
        return cmd_archive(root)
    if args.cmd == "set-stage":
        return cmd_set_stage(root, args.stage, args.status, args.note)
    if args.cmd == "set-lenses":
        return cmd_set_lenses(root, args.run, args.skip)
    return 1


if __name__ == "__main__":
    sys.exit(main())
