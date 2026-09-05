#!/usr/bin/env python3
"""
progress.py - per-lens heartbeats, so a running audit is not a black box.

Seven lens agents run in parallel by default (see audit_state.py LENSES).
Until a lens writes findings/<lens>.json, the dashboard has no way to tell
"working" from "stuck" from "never started". This gives each lens a place to
say what it is doing right now.

Storage is append-only JSON Lines, one file per lens
(.readiness-audit/progress/<lens>.jsonl). One file per lens is deliberate:
the lenses run concurrently, and a single shared file would need locking to
avoid interleaved writes corrupting each other's lines. A lens only ever
appends to its own file, so no two writers ever touch the same file at once.

An existing progress file is never rewritten or truncated - only appended to.

Usage:
    python3 progress.py note <project_root> <lens> <phase> ["<text>"]
    python3 progress.py read <project_root>
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DIRNAME = ".readiness-audit"

LENSES = ["security", "backend", "frontend", "devops", "qa", "database", "ai-security"]
PHASES = ["started", "evidence-read", "analyzing", "writing-findings", "done"]

# A lens is treated as having gone quiet if its most recent heartbeat is
# older than this many seconds and it has not reported "done".
SILENCE_THRESHOLD_SECONDS = 120


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _progress_dir(root: Path) -> Path:
    return root / DIRNAME / "progress"


def _progress_file(root: Path, lens: str) -> Path:
    return _progress_dir(root) / f"{lens}.jsonl"


def append_note(root: Path, lens: str, phase: str, note: str | None) -> dict:
    """Append one heartbeat to <lens>.jsonl. Raises ValueError for a bad lens/phase."""
    if lens not in LENSES:
        raise ValueError(f"unknown lens {lens!r}; expected one of {LENSES}")
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}; expected one of {PHASES}")

    event = {
        "ts": _now_iso(),
        "lens": lens,
        "phase": phase,
        "note": note or None,
    }

    directory = _progress_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = _progress_file(root, lens)
    # "a" opens for append and never truncates an existing file. A single
    # line write is not guaranteed atomic across processes on every
    # filesystem, but each lens only ever appends to its own file, so there
    # is no concurrent writer to interleave with.
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(event) + "\n")
    return event


def _load_events(path: Path) -> list[dict]:
    """Read every well-formed event from a lens's progress file, in order.

    A malformed line (partial write, corruption) is skipped rather than
    treated as fatal - a dashboard polling every two seconds must be able to
    read a file another process is mid-write on without breaking.
    """
    events = []
    if not path.exists():
        return events
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if not {"ts", "lens", "phase"} <= event.keys():
                continue
            events.append(event)
    return events


def read_progress(root: Path) -> dict:
    """Every lens's heartbeat history, for the dashboard to poll directly.

    Returns a dict keyed by lens id. A lens that has never emitted a
    heartbeat gets an empty events list and no fabricated progress - honest
    silence, not invented activity.
    """
    now = datetime.now(timezone.utc)
    result = {}
    for lens in LENSES:
        events = _load_events(_progress_file(root, lens))
        latest = events[-1] if events else None
        latest_phase = latest["phase"] if latest else None
        latest_ts = latest["ts"] if latest else None

        seconds_since = None
        signal = "no-signal"
        if latest_ts is not None:
            try:
                latest_dt = datetime.fromisoformat(latest_ts)
                if latest_dt.tzinfo is None:
                    latest_dt = latest_dt.replace(tzinfo=timezone.utc)
                seconds_since = (now - latest_dt).total_seconds()
                if latest_phase == "done" or seconds_since <= SILENCE_THRESHOLD_SECONDS:
                    signal = "active"
            except ValueError:
                seconds_since = None
                signal = "no-signal"

        result[lens] = {
            "events": events,
            "latest_phase": latest_phase,
            "latest_ts": latest_ts,
            "seconds_since_latest": seconds_since,
            "signal": signal,
        }
    return result


def cmd_note(root: Path, lens: str, phase: str, note: str | None) -> int:
    try:
        event = append_note(root, lens, phase, note)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(event, indent=2))
    return 0


def cmd_read(root: Path) -> int:
    print(json.dumps(read_progress(root), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("note")
    s.add_argument("project_root")
    s.add_argument("lens")
    s.add_argument("phase")
    s.add_argument("note", nargs="?", default=None)

    s = sub.add_parser("read")
    s.add_argument("project_root")

    args = ap.parse_args(argv)

    root = Path(args.project_root).expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1

    if args.cmd == "note":
        return cmd_note(root, args.lens, args.phase, args.note)
    if args.cmd == "read":
        return cmd_read(root)
    return 1


if __name__ == "__main__":
    sys.exit(main())
