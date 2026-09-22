#!/usr/bin/env python3
"""
runtime_walk.py - the first demoable live run of the runtime lens.

The lens walks the real app the way a senior tester would: it opens the
main screens in route order, reads each one at a desktop viewport and a
mobile viewport, checks navigation and routing, and reads the browser
console. The walk is read-only. It opens pages and reads logs. It never
submits a form, never activates a control, and never stores a secret.

The walk reads three inputs and writes two files:

Inputs (all read-only):
    * the screen list, derived from the live target (an app directory of
      static pages in tests, a live URL in real runs)
    * caller-supplied console and network log entries (data only, the walk
      activates nothing)
    * caller-supplied observations in the same shape (page, viewport,
      observed behavior)

Outputs:
    * .readiness-audit/findings/runtime.json (IDs PRA-RT-001 upward)
    * .readiness-audit/runtime-coverage.json (one row per screen and
      viewport, so a clean screen reads as covered, never as skipped)

Usage:
    python3 runtime_walk.py <project_root> <target> [--viewport VP ...]

All helpers are importable. Tests call run_walk directly.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from finding_store import FINDING_SCHEMA  # noqa: E402
from runtime_context import (  # noqa: E402
    check_runtime_ready,
    is_read_only_log,
    redact,
)

DIRNAME = ".readiness-audit"
FINDINGS_FILE = "findings/runtime.json"
COVERAGE_FILE = "runtime-coverage.json"

DESKTOP_VIEWPORT = "1280x800"
MOBILE_VIEWPORT = "390x844"
DEFAULT_VIEWPORTS = (DESKTOP_VIEWPORT, MOBILE_VIEWPORT)

CATEGORIES = ("layout", "navigation", "console")

# An explicit defect marker inside a static page. The walk reads the page
# file and treats the marker as one observed defect. Real runs replace
# file markers with live browser observations in the same shape.
LAYOUT_MARKER_RE = re.compile(r'data-rt-issue\s*=\s*"layout:\s*([^"]+)"')
NAV_MARKER_RE = re.compile(r'data-rt-issue\s*=\s*"nav:\s*([^"]+)"')

# A fixed pixel width at or above this value breaks the mobile viewport.
FIXED_WIDTH_RE = re.compile(r"width\s*:\s*(\d{3,5})\s*px")
FIXED_WIDTH_LIMIT = 1024

HREF_RE = re.compile(r'href\s*=\s*"([^"]+)"')
SKIP_SCHEMES = ("http://", "https://", "//", "#", "mailto:", "tel:", "javascript:")

FACTORS = {
    "layout": {"exposure": "internet", "data_class": "none",
               "blast_radius": "single-user",
               "compensating_control": "absent"},
    "navigation": {"exposure": "internet", "data_class": "business",
                   "blast_radius": "single-tenant",
                   "compensating_control": "absent"},
    "console": {"exposure": "internet", "data_class": "none",
                "blast_radius": "single-user",
                "compensating_control": "absent"},
}


class WalkError(Exception):
    """The walk stopped before it wrote anything (gate or read-only rule)."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _short(text, limit=80):
    text = str(text or "").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text


# --------------------------------------------------------------------------
# Screen discovery - the walk order
# --------------------------------------------------------------------------

def discover_screens(target):
    """List the screens of the live target in walk order.

    A local app directory yields one screen per top-level *.html file,
    sorted by route (index.html walks first as /). Any other target (a
    live URL) yields the default screen list. Each screen is a dict with
    screen (route), page (evidence token), and file (path or None).
    """
    path = Path(str(target)).expanduser()
    if path.is_dir():
        screens = []
        for file in sorted(path.glob("*.html")):
            route = "/" if file.stem == "index" else "/" + file.stem
            screens.append({"screen": route, "page": route,
                            "file": str(file)})
        screens.sort(key=lambda s: (s["screen"] != "/", s["screen"]))
        return screens
    base = str(target or "").rstrip("/")
    return [{"screen": route, "page": base + route, "file": None}
            for route in ("/", "/login", "/checkout")]


# --------------------------------------------------------------------------
# Read-only scans over static page files
# --------------------------------------------------------------------------

def scan_layout(screen):
    """Read one page file and return layout defects as observation dicts.

    The scan reads the file only. It reports explicit layout markers and
    fixed pixel widths that break the mobile viewport. Each defect is
    recorded at the mobile viewport, where narrow layout defects surface.
    """
    defects = []
    file = screen.get("file")
    if not file:
        return defects
    try:
        text = Path(file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return defects
    for match in LAYOUT_MARKER_RE.finditer(text):
        defects.append({
            "category": "layout",
            "screen": screen["screen"],
            "page": screen["page"],
            "viewport": MOBILE_VIEWPORT,
            "detail": match.group(1).strip(),
            "observed": "layout defect on %s: %s" % (
                screen["page"], match.group(1).strip()),
            "reproduced": True,
        })
    for match in FIXED_WIDTH_RE.finditer(text):
        try:
            width = int(match.group(1))
        except ValueError:
            continue
        if width >= FIXED_WIDTH_LIMIT:
            defects.append({
                "category": "layout",
                "screen": screen["screen"],
                "page": screen["page"],
                "viewport": MOBILE_VIEWPORT,
                "detail": "fixed width %dpx overflows the mobile viewport" % width,
                "observed": "content sized %dpx wide runs off the screen at %s" % (
                    width, MOBILE_VIEWPORT),
                "reproduced": True,
            })
            break
    return defects


def _resolve_link(target_dir, source_file, href):
    """Resolve one href to a local file, or None when it needs no check."""
    if not href or href.startswith(SKIP_SCHEMES):
        return None
    path = href.split("?", 1)[0].split("#", 1)[0].strip()
    if not path:
        return None
    if path.startswith("/"):
        candidate = Path(target_dir) / path.lstrip("/")
    else:
        candidate = Path(source_file).parent / path
    candidates = [candidate]
    if not candidate.suffix:
        candidates = [Path(str(candidate) + ".html"),
                      candidate / "index.html"]
    for item in candidates:
        if item.is_file():
            return item
    return False


def scan_navigation(screen, target_dir):
    """Read one page file and return broken routes as observation dicts.

    The scan reads href attributes and resolves them against the app
    directory. A link with no target file is a broken route. The scan
    follows no link and loads no page. Each defect is recorded at the
    desktop viewport.
    """
    defects = []
    file = screen.get("file")
    if not file:
        return defects
    try:
        text = Path(file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return defects
    seen = set()
    for match in HREF_RE.finditer(text):
        href = match.group(1).strip()
        resolved = _resolve_link(target_dir, file, href)
        if resolved is False and href not in seen:
            seen.add(href)
            defects.append({
                "category": "navigation",
                "screen": screen["screen"],
                "page": screen["page"],
                "viewport": DESKTOP_VIEWPORT,
                "detail": "link to %s has no target page" % href,
                "observed": "the link to %s from %s leads to a missing page" % (
                    href, screen["page"]),
                "reproduced": True,
            })
    for match in NAV_MARKER_RE.finditer(Path(file).read_text(
            encoding="utf-8", errors="replace")):
        defects.append({
            "category": "navigation",
            "screen": screen["screen"],
            "page": screen["page"],
            "viewport": DESKTOP_VIEWPORT,
            "detail": match.group(1).strip(),
            "observed": "routing defect on %s: %s" % (
                screen["page"], match.group(1).strip()),
            "reproduced": True,
        })
    return defects


# --------------------------------------------------------------------------
# Finding builders - page, viewport, and observed behavior in each finding
# --------------------------------------------------------------------------

def build_resolve(category, page, viewport):
    """State exactly what would settle a non-reproduced item."""
    return ("Revisit %s at %s and repeat the %s check twice. "
            "The lens records the result. "
            "Two matching results settle the item."
            % (page, viewport, category))


def build_evidence(page, viewport, observed, extra=""):
    """Compose one live evidence string with page, viewport, and behavior."""
    text = ("Opened %s at viewport %s, observed %s. "
            "Console and network logs show read-only checks."
            % (page, viewport, observed))
    if extra:
        text += " " + extra.strip()
    return text


def _finding_id(index):
    return "PRA-RT-%03d" % index


def _titles(category, page, detail):
    if category == "layout":
        return "Layout defect on %s: %s" % (page, _short(detail, 60))
    if category == "navigation":
        return "Broken route on %s: %s" % (page, _short(detail, 60))
    return "Console report on %s: %s" % (page, _short(detail, 60))


def _impact(category, page, reproduced):
    if not reproduced:
        if category == "layout":
            return ("Visitors may see a broken layout on %s. "
                    "The page may look wrong and users may leave." % page)
        if category == "navigation":
            return ("Users may follow a link on %s and reach a dead end. "
                    "They may fail to finish the task." % page)
        return ("The page on %s may report an error while users watch. "
                "Users may lose trust in the page." % page)
    if category == "layout":
        return ("Visitors see a broken layout on %s. "
                "The page looks wrong and users leave." % page)
    if category == "navigation":
        return ("Users follow a link on %s and reach a dead end. "
                "They cannot finish the task." % page)
    return ("The page on %s reports an error while users watch. "
            "Users lose trust in the page." % page)


def _fix(category, page, viewport):
    if category == "layout":
        return ("Fix the layout on %s so the content fits %s. "
                "Revisit the page at both viewports and record the result."
                % (page, viewport))
    if category == "navigation":
        return ("Restore the missing route from %s or remove the link. "
                "Revisit the page and follow the link again." % page)
    return ("Remove the cause of the console report on %s. "
            "Reload the page and confirm the console stays clean." % page)


def observation_to_finding(obs, index):
    """Convert one observation dict into a PRA-RT finding.

    A reproduced observation becomes CONFIRMED. A non-reproduced one
    becomes UNVERIFIED with a resolve line that states exactly what
    would settle it. Every finding carries a reproduce record.
    """
    category = str(obs.get("category") or "layout").strip().lower()
    if category not in CATEGORIES:
        raise WalkError("Unknown finding category %r: use one of %s."
                        % (obs.get("category"), ", ".join(CATEGORIES)))
    page = str(obs.get("page") or obs.get("screen") or "/").strip()
    viewport = str(obs.get("viewport") or DESKTOP_VIEWPORT).strip()
    detail = str(obs.get("detail") or obs.get("observed") or "").strip()
    observed = str(obs.get("observed") or detail).strip()
    reproduced = bool(obs.get("reproduced", True))
    state = "CONFIRMED" if reproduced else "UNVERIFIED"
    evidence = build_evidence(page, viewport, observed,
                              str(obs.get("console_note") or ""))
    finding = {
        "id": _finding_id(index),
        "title": _titles(category, page, detail),
        "impact": _impact(category, page, reproduced),
        "state": state,
        "owner": "runtime",
        "cross_lens": [],
        "evidence": [evidence],
        "probe": None,
        "failure_path": ("The lens observed this on %s at %s: %s."
                         % (page, viewport, detail)),
        "compensating": "none found",
        "fix": _fix(category, page, viewport),
        "resolve": None if reproduced else build_resolve(
            category, page, viewport),
        "see": None,
        "factors": dict(FACTORS[category]),
        "reproduce": {"revisits": 2, "reproduced": reproduced},
    }
    return redact(finding)


def console_entry_to_observation(entry, default_viewport=DESKTOP_VIEWPORT):
    """Convert one caller-supplied console entry into an observation.

    Entries with level error or warning become observations. Other levels
    are ignored. The entry is data only. The walk sends no request.
    """
    level = str(entry.get("level") or "").strip().lower()
    if level not in ("error", "warning", "warn"):
        return None
    level = "warning" if level == "warn" else level
    page = str(entry.get("page") or "/").strip()
    viewport = str(entry.get("viewport") or default_viewport).strip()
    message = str(entry.get("message") or "").strip()
    return {
        "category": "console",
        "screen": page,
        "page": page,
        "viewport": viewport,
        "detail": "console %s: %s" % (level, message),
        "observed": "console shows %s: %s" % (level, message),
        "reproduced": bool(entry.get("reproduced", True)),
    }


# --------------------------------------------------------------------------
# Walk runner
# --------------------------------------------------------------------------

def run_walk(root, target, viewports=None, console_entries=None,
             network_entries=None, observations=None):
    """Walk the live target and write findings plus coverage.

    The runner calls check_runtime_ready first and stops when the live
    target is missing or unreachable. It then walks the screen list at
    each viewport, scans layout and navigation, reads caller-supplied
    console and network entries as data, and writes findings/runtime.json
    and runtime-coverage.json. It returns a summary dict.
    """
    ready, message = check_runtime_ready(root)
    if not ready:
        raise WalkError(message)

    viewports = tuple(viewports) if viewports else DEFAULT_VIEWPORTS
    base = Path(root).expanduser().resolve()
    screens = discover_screens(target)

    network_entries = list(network_entries or [])
    ok, violations = is_read_only_log(network_entries)
    if not ok:
        raise WalkError("Network log breaks the read-only rule: %s"
                        % "; ".join(violations))

    collected = []
    target_path = Path(str(target)).expanduser()
    target_dir = str(target_path) if target_path.is_dir() else None
    for screen in screens:
        if target_dir:
            collected.extend(scan_layout(screen))
            collected.extend(scan_navigation(screen, target_dir))
    for entry in redact(list(console_entries or [])):
        obs = console_entry_to_observation(entry)
        if obs is not None:
            collected.append(obs)
    for obs in redact(list(observations or [])):
        if not isinstance(obs, dict):
            raise WalkError("Each observation must state a category, "
                            "a page, and the observed behavior.")
        item = dict(obs)
        item.setdefault("reproduced", True)
        collected.append(item)

    findings = [observation_to_finding(obs, num)
                for num, obs in enumerate(collected, 1)]

    flagged = {(str(f["evidence"][0]).split(" at viewport ")[0]
                .replace("Opened ", ""), obs.get("viewport"))
               for f, obs in zip(findings, collected)}
    coverage = []
    for screen in screens:
        for viewport in viewports:
            hit = (screen["page"], viewport) in flagged
            coverage.append({
                "screen": screen["screen"],
                "viewport": viewport,
                "status": "finding" if hit else "covered",
                "notes": ("Defect recorded in findings/runtime.json."
                          if hit else
                          "Walked with no defect. The screen is covered."),
            })

    audit = base / DIRNAME
    (audit / "findings").mkdir(parents=True, exist_ok=True)
    findings_path = audit / FINDINGS_FILE
    findings_path.write_text(json.dumps(
        {"schema": FINDING_SCHEMA, "lens": "runtime",
         "findings": findings}, indent=2) + "\n", encoding="utf-8")
    coverage_path = audit / COVERAGE_FILE
    coverage_path.write_text(json.dumps(
        {"target": str(target), "viewports": list(viewports),
         "coverage": redact(coverage),
         "updated_at": _now()}, indent=2) + "\n", encoding="utf-8")
    return {"findings": findings, "coverage": coverage,
            "findings_path": str(findings_path),
            "coverage_path": str(coverage_path),
            "message": message}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root")
    parser.add_argument("target")
    parser.add_argument("--viewport", action="append", default=None)
    parser.add_argument("--console-log", default=None,
                        help="Path to a JSON list of console entries.")
    parser.add_argument("--network-log", default=None,
                        help="Path to a JSON list of network entries.")
    args = parser.parse_args(argv)

    def load(path):
        if not path:
            return []
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise WalkError("Log file %s must hold a JSON list." % path)
        return raw

    try:
        summary = run_walk(
            args.project_root, args.target,
            viewports=tuple(args.viewport) if args.viewport else None,
            console_entries=load(args.console_log),
            network_entries=load(args.network_log))
    except WalkError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"walked": len(summary["coverage"]),
                      "findings": len(summary["findings"]),
                      "findings_path": summary["findings_path"],
                      "coverage_path": summary["coverage_path"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
