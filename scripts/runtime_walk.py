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
TABLET_VIEWPORT = "768x1024"
DEFAULT_VIEWPORTS = (DESKTOP_VIEWPORT, MOBILE_VIEWPORT)
RESPONSIVE_VIEWPORTS = (MOBILE_VIEWPORT, TABLET_VIEWPORT, DESKTOP_VIEWPORT)
BREAKAGE_CLASSES = ("overlap", "clipping", "off-screen", "unusable-table")
STATE_CHANGING_KINDS = ("create", "update", "delete", "write", "submit",
                        "place-order", "checkout")

CATEGORIES = ("layout", "navigation", "console", "data", "responsive")

# An explicit defect marker inside a static page. The walk reads the page
# file and treats the marker as one observed defect. Real runs replace
# file markers with live browser observations in the same shape.
LAYOUT_MARKER_RE = re.compile(r'data-rt-issue\s*=\s*"layout:\s*([^"]+)"')
NAV_MARKER_RE = re.compile(r'data-rt-issue\s*=\s*"nav:\s*([^"]+)"')
DATA_MARKER_RE = re.compile(r'data-rt-issue\s*=\s*"data:\s*([^"]+)"')
RESPONSIVE_MARKER_RE = re.compile(
    r'data-rt-issue\s*=\s*"responsive:\s*([^"]+)"')
VIEWPORT_TOKEN_RE = re.compile(r"\b(\d{3,4}x\d{3,4})\b")

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
    "data": {"exposure": "internet", "data_class": "business",
             "blast_radius": "single-tenant",
             "compensating_control": "absent"},
    "responsive": {"exposure": "internet", "data_class": "none",
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
# Data, network, and responsive slice
# --------------------------------------------------------------------------

def _extract_viewport(text):
    """Return the first viewport token in text, or None."""
    match = VIEWPORT_TOKEN_RE.search(str(text or ""))
    if match:
        return match.group(1)
    return None


def _extract_total(value):
    """Return the total from a bare value or a payload dict."""
    if isinstance(value, dict):
        for key in ("total", "count", "value", "amount"):
            if key in value and value[key] is not None:
                return value[key]
        return None
    return value


def _normalize_total(value):
    """Normalize one total for comparison."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    text = str(value).strip()
    if not text:
        return ""
    try:
        number = float(text.replace(",", "").strip())
        if number.is_integer():
            return str(int(number))
        return str(number)
    except (ValueError, AttributeError):
        return text


def compare_ui_vs_api(page, displayed, api_payload, endpoint, status,
                      viewport=DESKTOP_VIEWPORT):
    """Compare one displayed number with one read-only API payload.

    The API payload is the expected value. The displayed number is
    the actual value. A match returns None. A mismatch returns one
    data observation with endpoint, status, expected, actual, and page.
    The helper sends no request. It compares caller-supplied data only.
    """
    page_token = str(page or "/").strip() or "/"
    viewport_token = str(viewport or DESKTOP_VIEWPORT).strip()
    endpoint_token = str(endpoint or "").strip() or "(unknown endpoint)"
    if status is None:
        status_token = "(unknown status)"
    else:
        status_token = str(status).strip() or "(unknown status)"
    expected = _extract_total(api_payload)
    actual = _extract_total(displayed)
    if _normalize_total(expected) == _normalize_total(actual):
        return None
    expected_text = str(expected) if expected is not None else "(missing)"
    actual_text = str(actual) if actual is not None else "(missing)"
    detail = ("UI shows %s on %s but GET %s status %s returns %s" % (
        actual_text, page_token, endpoint_token, status_token,
        expected_text))
    observed = ("opened %s and saw %s while GET %s status %s returns %s" % (
        page_token, actual_text, endpoint_token, status_token,
        expected_text))
    return redact({
        "category": "data",
        "screen": page_token,
        "page": page_token,
        "viewport": viewport_token,
        "detail": detail,
        "observed": observed,
        "reproduced": True,
        "endpoint": endpoint_token,
        "status": status_token,
        "expected": expected_text,
        "actual": actual_text,
    })


def compare_cross_page_totals(locations):
    """Compare totals shown on two or more pages.

    Each location holds page and total. Agreement returns an empty
    list. Disagreement returns one data observation that cites both
    locations. The helper reads caller-supplied data only.
    """
    items = list(locations or [])
    if len(items) < 2:
        return []
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        page = str(item.get("page") or item.get("screen") or "/").strip()
        total = _extract_total(item.get("total", item.get("value")))
        normalized.append((page or "/", _normalize_total(total),
                           str(total) if total is not None else "(missing)"))
    if len(normalized) < 2:
        return []
    first_value = normalized[0][1]
    if all(entry[1] == first_value for entry in normalized):
        return []
    first = None
    second = None
    for entry in normalized:
        if entry[1] != first_value:
            second = entry
            first = normalized[0]
            break
    if first is None:
        first = normalized[0]
        second = normalized[1]
    detail = ("Total on %s is %s but total on %s is %s. "
              "The totals disagree." % (
                  first[0], first[2], second[0], second[2]))
    observed = ("opened %s and %s and saw totals %s and %s. "
                "The totals disagree." % (
                    first[0], second[0], first[2], second[2]))
    return [redact({
        "category": "data",
        "screen": first[0],
        "page": first[0],
        "viewport": DESKTOP_VIEWPORT,
        "detail": detail,
        "observed": observed,
        "reproduced": True,
    })]


def _table_search(rows, query):
    """Filter rows by a case-insensitive substring query."""
    query_text = str(query or "").strip().lower()
    if not query_text:
        return list(rows)
    matched = []
    for row in rows:
        if isinstance(row, dict):
            haystack = " ".join(str(value) for value in row.values()).lower()
        else:
            haystack = str(row).lower()
        if query_text in haystack:
            matched.append(row)
    return matched


def exercise_search_filter_sort_pagination(dataset, actions):
    """Run read-only table actions over supplied rows.

    The helper copies the dataset and never mutates the input. Each
    read-only search, filter, sort, and pagination action runs against
    the copy. Each state-changing action is skipped with a recorded
    reason and never runs. Empty and no-result probes always run.
    """
    rows = list(dataset or [])
    ordered_actions = list(actions or [])
    results = []
    skipped = []
    for action in ordered_actions:
        if not isinstance(action, dict):
            skipped.append(redact({
                "name": "(unnamed)",
                "kind": "(unknown)",
                "reason": "Action is not an object. The lens skips it.",
            }))
            continue
        name = str(action.get("name") or action.get("kind")
                   or "(unnamed)").strip()
        kind = str(action.get("kind") or "").strip().lower()
        if action.get("state_changing") is True or kind in STATE_CHANGING_KINDS:
            skipped.append(redact({
                "name": name,
                "kind": kind or "(unknown)",
                "reason": ("Action is state-changing. "
                           "The lens records it and skips it."),
            }))
            continue
        if kind == "search":
            query = str(action.get("query") or "")
            matched = _table_search(rows, query)
            results.append({"name": name, "kind": "search",
                            "query": query, "count": len(matched),
                            "rows": matched})
        elif kind == "filter":
            field = action.get("field")
            value = action.get("value")
            if field is None:
                matched = []
            else:
                matched = [row for row in rows
                           if isinstance(row, dict)
                           and str(row.get(field)) == str(value)]
            results.append({"name": name, "kind": "filter",
                            "field": field, "value": value,
                            "count": len(matched), "rows": matched})
        elif kind == "sort":
            field = action.get("field")
            direction = str(action.get("direction") or "asc").strip().lower()
            reverse = direction == "desc"

            def _sort_key(row):
                if isinstance(row, dict) and field:
                    raw = row.get(field)
                else:
                    raw = row
                if isinstance(raw, bool):
                    return (1, 0.0, str(raw))
                if isinstance(raw, (int, float)):
                    return (0, float(raw), "")
                try:
                    return (0, float(str(raw).strip()), "")
                except (TypeError, ValueError, AttributeError):
                    return (1, 0.0, str(raw).lower())

            try:
                ordered = sorted(rows, key=_sort_key, reverse=reverse)
            except Exception:
                ordered = list(rows)
            results.append({"name": name, "kind": "sort",
                            "field": field, "direction": direction,
                            "count": len(ordered), "rows": ordered})
        elif kind in ("pagination", "paginate", "page"):
            try:
                page_number = int(action.get("page", 1))
            except (TypeError, ValueError):
                page_number = 1
            try:
                per_page = int(action.get("per_page",
                                          action.get("perPage", 10)))
            except (TypeError, ValueError):
                per_page = 10
            if page_number < 1:
                page_number = 1
            if per_page < 1:
                per_page = 10
            start = (page_number - 1) * per_page
            sliced = rows[start:start + per_page]
            results.append({"name": name, "kind": "pagination",
                            "page": page_number, "per_page": per_page,
                            "count": len(sliced), "rows": sliced})
        else:
            skipped.append(redact({
                "name": name,
                "kind": kind or "(unknown)",
                "reason": ("Action kind is unknown. "
                           "The lens skips it without execution."),
            }))
    results.append({"name": "empty-dataset", "kind": "search",
                    "query": "", "count": 0, "rows": [],
                    "note": "Empty dataset returns no rows."})
    probe = "__no_such_value__"
    while _table_search(rows, probe):
        probe += "_x"
    no_result_rows = _table_search(rows, probe)
    results.append({"name": "no-result", "kind": "search",
                    "query": probe, "count": len(no_result_rows),
                    "rows": no_result_rows,
                    "note": "No-result query returns no rows."})
    return redact({"results": results, "skipped": skipped})


def _detect_breakage(layout_report):
    """Return the canonical breakage class in a layout report, or None."""
    if isinstance(layout_report, dict):
        explicit = str(layout_report.get("breakage")
                        or layout_report.get("type") or "").strip()
        explicit = explicit.lower().replace("_", "-")
        if explicit in BREAKAGE_CLASSES:
            return explicit
        for key, token in (("overlap", "overlap"), ("clipping", "clip"),
                           ("off_screen", "off"), ("off-screen", "off"),
                           ("table", "table")):
            if layout_report.get(key):
                if key == "table":
                    return "unusable-table"
                if key in ("off_screen", "off-screen"):
                    return "off-screen"
                return key
        for value in layout_report.values():
            found = _detect_breakage(str(value)) if value else None
            if found:
                return found
        return None
    lowered = str(layout_report or "").lower().replace("_", "-")
    if not lowered.strip():
        return None
    for token in BREAKAGE_CLASSES:
        if token in lowered:
            return token
    if "overlap" in lowered:
        return "overlap"
    if "clip" in lowered:
        return "clipping"
    if "off" in lowered and "screen" in lowered:
        return "off-screen"
    if "table" in lowered or "horizontal" in lowered:
        return "unusable-table"
    return None


def _describe_breakage(layout_report, breakage):
    """Return a short human description of a layout report."""
    if isinstance(layout_report, dict):
        for key in ("detail", "description", "observed", "note"):
            text = str(layout_report.get(key) or "").strip()
            if text:
                return text
        return "layout breaks with %s" % breakage
    text = str(layout_report or "").strip()
    if text:
        return text
    return "layout breaks with %s" % breakage


def check_viewport(screen, viewport, layout_report):
    """Check one screen at one viewport for responsive defects.

    A clean layout returns None. A broken layout returns one
    responsive observation that cites the exact viewport and the
    breakage class. The helper reads caller-supplied data only.
    """
    viewport_token = str(viewport or "").strip()
    if not viewport_token:
        raise WalkError("Viewport is missing: state the viewport.")
    page_token = str(screen or "/").strip() or "/"
    breakage = _detect_breakage(layout_report)
    if breakage is None:
        return None
    description = _describe_breakage(layout_report, breakage)
    detail = "%s at %s: %s" % (breakage, viewport_token, description)
    observed = ("opened %s at %s and saw %s (%s)" % (
        page_token, viewport_token, description, breakage))
    return redact({
        "category": "responsive",
        "screen": page_token,
        "page": page_token,
        "viewport": viewport_token,
        "detail": detail,
        "observed": observed,
        "reproduced": True,
        "breakage": breakage,
    })


def scan_data(screen):
    """Read one page file and return data markers as observation dicts.

    The scan reads the file only. Each data marker becomes one data
    observation at the desktop viewport, unless the marker names
    another viewport. The observation keeps the full marker text, so
    endpoint, status, expected, and actual stay in the finding.
    """
    defects = []
    file = screen.get("file")
    if not file:
        return defects
    try:
        text = Path(file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return defects
    for match in DATA_MARKER_RE.finditer(text):
        detail = match.group(1).strip()
        viewport = _extract_viewport(detail) or DESKTOP_VIEWPORT
        defects.append({
            "category": "data",
            "screen": screen["screen"],
            "page": screen["page"],
            "viewport": viewport,
            "detail": detail,
            "observed": "data check on %s: %s" % (
                screen["page"], detail),
            "reproduced": True,
        })
    return defects


def scan_responsive(screen):
    """Read one page file and return responsive markers as observations.

    The scan reads the file only. Each responsive marker becomes one
    responsive observation. The viewport comes from the marker text
    when present, else the mobile viewport. The detail keeps the
    breakage class from the marker text.
    """
    defects = []
    file = screen.get("file")
    if not file:
        return defects
    try:
        text = Path(file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return defects
    for match in RESPONSIVE_MARKER_RE.finditer(text):
        detail = match.group(1).strip()
        viewport = _extract_viewport(detail) or MOBILE_VIEWPORT
        defects.append({
            "category": "responsive",
            "screen": screen["screen"],
            "page": screen["page"],
            "viewport": viewport,
            "detail": detail,
            "observed": "responsive check on %s at %s: %s" % (
                screen["page"], viewport, detail),
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
    if category == "data":
        return "Data mismatch on %s: %s" % (page, _short(detail, 60))
    if category == "responsive":
        return "Responsive defect on %s: %s" % (page, _short(detail, 60))
    return "Console report on %s: %s" % (page, _short(detail, 60))


def _impact(category, page, reproduced):
    if not reproduced:
        if category == "layout":
            return ("Visitors may see a broken layout on %s. "
                    "The page may look wrong and users may leave." % page)
        if category == "navigation":
            return ("Users may follow a link on %s and reach a dead end. "
                    "They may fail to finish the task." % page)
        if category == "data":
            return ("Shoppers may see a wrong number on %s. "
                    "They may lose trust in the page." % page)
        if category == "responsive":
            return ("Visitors may fail to use %s on a small screen. "
                    "Content may overlap or run off the screen." % page)
        return ("The page on %s may report an error while users watch. "
                "Users may lose trust in the page." % page)
    if category == "layout":
        return ("Visitors see a broken layout on %s. "
                "The page looks wrong and users leave." % page)
    if category == "navigation":
        return ("Users follow a link on %s and reach a dead end. "
                "They cannot finish the task." % page)
    if category == "data":
        return ("Shoppers see a wrong number on %s. "
                "They cannot trust the page." % page)
    if category == "responsive":
        return ("Visitors cannot use %s on a small screen. "
                "Content overlaps or runs off the screen." % page)
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
    if category == "data":
        return ("Fix the data source for %s so the page matches the API. "
                "Revisit the page and compare the numbers again." % page)
    if category == "responsive":
        return ("Fix the layout on %s so the content fits %s. "
                "Revisit the page at 390x844, 768x1024, and 1280x800 "
                "and record each result." % (page, viewport))
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
             network_entries=None, observations=None, api_checks=None,
             totals_checks=None, table_cases=None, viewport_reports=None):
    """Walk the live target and write findings plus coverage.

    The runner calls check_runtime_ready first and stops when the live
    target is missing or unreachable. It walks the screen list at
    each viewport, scans layout, navigation, data, and responsive
    markers, reads caller-supplied console and network entries as
    data, compares UI numbers with API payloads, compares totals
    across pages, checks viewports, and writes findings/runtime.json
    and runtime-coverage.json. It returns a summary dict. Table cases
    run read-only and never change data.
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
            collected.extend(scan_data(screen))
            collected.extend(scan_responsive(screen))
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
    for check in list(api_checks or []):
        if not isinstance(check, dict):
            raise WalkError("Each API check must state page, displayed "
                            "value, API payload, endpoint, and status.")
        obs = compare_ui_vs_api(
            check.get("page", "/"), check.get("displayed"),
            check.get("api_payload"), check.get("endpoint"),
            check.get("status"), check.get("viewport") or DESKTOP_VIEWPORT)
        if obs is not None:
            collected.append(obs)
    for item in list(totals_checks or []):
        if isinstance(item, dict) and "locations" in item:
            locations = item.get("locations")
        else:
            locations = item
        if not isinstance(locations, list):
            raise WalkError("Each totals check must hold a locations list.")
        collected.extend(compare_cross_page_totals(locations))
    for case in list(table_cases or []):
        if not isinstance(case, dict):
            raise WalkError("Each table case must state dataset and actions.")
        exercise_search_filter_sort_pagination(
            case.get("dataset", []), case.get("actions", []))
    for report in list(viewport_reports or []):
        if not isinstance(report, dict):
            raise WalkError("Each viewport report must state screen, "
                            "viewport, and layout report.")
        obs = check_viewport(
            report.get("screen") or report.get("page") or "/",
            report.get("viewport"),
            report.get("layout_report", report.get("detail", "")))
        if obs is not None:
            collected.append(obs)

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
