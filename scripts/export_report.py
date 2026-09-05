#!/usr/bin/env python3
"""
export_report.py - build a shareable LaTeX report set from an audit snapshot.

Everything this writes comes from `finding_store.build_report()` - the same
function `readiness_dashboard.py` renders in the browser - so an export can
never say something the dashboard does not also say. The evidence ledger
summary is the one exception: it is read straight from
`.readiness-audit/evidence/absence-ledger.json`, the same file the dashboard's
Evidence tab reads, because that data does not live in `build_report()`.

Each call writes a fresh, timestamped directory under
`.readiness-audit/export/` and never touches an existing one. Nothing here
mutates the audit trail; this is a read-only projection of it, same as the
dashboard.

A finding's file paths and code excerpts are attacker-relevant text. LaTeX
treats a dozen ASCII characters as syntax, so every interpolated string goes
through `latex_escape()` first - never stripped, always escaped, so a mangled
document is never mistaken for a missing finding.

Usage:
    python3 export_report.py <project_root>
"""
import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from finding_store import LENS_LABEL, LENS_ORDER, build_report  # noqa: E402

# --------------------------------------------------------------------------
# LaTeX escaping - a correctness requirement, applied to every interpolated
# string. Each character is looked up once against the original text, so the
# backslashes this introduces are never re-scanned and re-escaped.
# --------------------------------------------------------------------------

_LATEX_SPECIAL = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "%": r"\%",
    "&": r"\&",
    "#": r"\#",
    "_": r"\_",
    "^": r"\textasciicircum{}",
    "~": r"\textasciitilde{}",
}


def latex_escape(value) -> str:
    """Escape one piece of interpolated text for safe use in a .tex document.

    None and "" both become "". Every other value is stringified first, so
    callers never need to coerce ids, counts, or enum values by hand.
    """
    if value is None:
        return ""
    return "".join(_LATEX_SPECIAL.get(ch, ch) for ch in str(value))


def _verbatim_block(text: str) -> str:
    """Wrap raw text (a code excerpt, a file:line reference) in a verbatim
    environment instead of escaping it, per the requirement that code
    snippets render as code. `verbatim` does its own catcode handling, so its
    body must not be escaped - but its body must also never contain the
    literal string that would close the environment early, so that one
    sequence is detected and handled by falling back to an escaped form."""
    if not text:
        return ""
    if r"\end{verbatim}" in text:
        return r"\texttt{" + latex_escape(text) + "}"
    return "\\begin{verbatim}\n" + text + "\n\\end{verbatim}"


def _tex_paragraphs(text: str) -> str:
    """Turn plain prose (context.md, scope.md, a verdict summary) into
    escaped LaTeX paragraphs. Blank-line-separated blocks become paragraphs;
    single newlines inside a block collapse to spaces."""
    if not text or not text.strip():
        return ""
    blocks = [b.strip() for b in text.replace("\r\n", "\n").split("\n\n")]
    paragraphs = []
    for block in blocks:
        if not block:
            continue
        collapsed = " ".join(line.strip() for line in block.splitlines() if line.strip())
        paragraphs.append(latex_escape(collapsed))
    return "\n\n".join(paragraphs)


# --------------------------------------------------------------------------
# Data gathering beyond build_report(): the two free-text files and the
# evidence ledger, none of which are part of the structured report.
# --------------------------------------------------------------------------

def _read_optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return ""


def _load_evidence_summary(audit: Path) -> list[dict]:
    """A projection of the absence ledger's controls, same shape the
    dashboard's Evidence tab reads: id, label, owning lens, evidence state."""
    path = audit / "evidence" / "absence-ledger.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return []
    controls = raw.get("controls") if isinstance(raw, dict) else None
    if not isinstance(controls, dict):
        return []

    rows = []
    for control_id, row in sorted(controls.items()):
        if not isinstance(row, dict) or row.get("polarity") != "control":
            continue
        hits = row.get("hit_count") or 0
        supports = row.get("supports_state")
        if hits > 0:
            state = "CONFIRMED"
        elif supports in ("NOT_FOUND", "UNVERIFIED"):
            state = supports
        else:
            continue
        rows.append({
            "id": control_id,
            "label": row.get("label") or control_id,
            "lens": row.get("lens") or "-",
            "state": state,
        })
    return rows


def _git_short_ref(root: Path) -> str:
    """The short ref of HEAD in `root`, or 'nogit' if git is unavailable,
    the directory is not a repo, or the lookup fails for any reason."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root, capture_output=True, encoding="utf-8", timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "nogit"
    ref = (result.stdout or "").strip()
    if result.returncode != 0 or not ref:
        return "nogit"
    return ref


def _unique_export_dir(audit: Path, ref: str, timestamp: str) -> Path:
    """`.readiness-audit/export/<ref>-<timestamp>/`, never an existing path.
    A numeric suffix disambiguates the rare case of two exports landing in
    the same UTC second."""
    base_name = f"{ref}-{timestamp}"
    candidate = audit / "export" / base_name
    suffix = 1
    while candidate.exists():
        candidate = audit / "export" / f"{base_name}-{suffix}"
        suffix += 1
    return candidate


# --------------------------------------------------------------------------
# Document rendering
# --------------------------------------------------------------------------

_PREAMBLE = (
    "\\documentclass[11pt]{article}\n"
    "\\usepackage[margin=1in]{geometry}\n"
    "\\usepackage{parskip}\n"
    "\\begin{document}\n"
)
_POSTAMBLE = "\n\\end{document}\n"


def _front_page(report: dict, ref: str, timestamp: str) -> str:
    counts = report["counts"]
    verdict = report["verdict"] or {}
    decision = verdict.get("decision") or "NO VERDICT YET"
    headline = verdict.get("headline") or ""
    stage = report.get("stage") or {}
    complete = stage.get("status") == "complete"

    lines = []
    A = lines.append
    A(r"\begin{center}")
    A(r"{\fontsize{44}{50}\selectfont\bfseries %s}\par" % latex_escape(decision))
    A(r"\vspace{0.5cm}")
    if headline:
        A(r"{\Large %s}\par" % latex_escape(headline))
        A(r"\vspace{0.5cm}")
    A(r"{\large P0: %d \quad P1: %d \quad P2: %d \quad P3: %d}\par"
      % (counts["p0"], counts["p1"], counts["p2"], counts["p3"]))
    A(r"{\large CONFIRMED: %d \quad NOT FOUND: %d \quad UNVERIFIED: %d}\par"
      % (counts["confirmed"], counts["notFound"], counts["unverified"]))
    A(r"\vspace{0.5cm}")
    A(r"{\normalsize Git ref: \texttt{%s} \quad Exported: \texttt{%s}}\par"
      % (latex_escape(ref), latex_escape(timestamp)))
    A(r"\end{center}")

    if not complete:
        stage_name = stage.get("name") or "not started"
        not_reported = [lens["label"] for lens in report["lenses"] if lens["status"] != "complete"]
        A(r"\vspace{0.6cm}")
        A(r"\begin{center}")
        A(r"\fbox{\parbox{0.85\textwidth}{\centering")
        A(r"\textbf{AUDIT INCOMPLETE --- stage reached: %s}\\[4pt]" % latex_escape(stage_name))
        if not_reported:
            A(r"Lenses not yet reported: %s" % latex_escape(", ".join(not_reported)))
        else:
            A("Every lens has reported, but the audit has not been marked complete.")
        A(r"}}")
        A(r"\end{center}")
    return "\n".join(lines)


def _context_scope_verdict(report: dict, context_text: str, scope_text: str) -> str:
    lines = []
    A = lines.append
    A(r"\section*{Context}")
    A(_tex_paragraphs(context_text) or "Not recorded.")
    A(r"\section*{Scope}")
    A(_tex_paragraphs(scope_text) or "Not recorded.")
    A(r"\section*{Verdict}")
    verdict = report["verdict"] or {}
    if verdict.get("decision") or verdict.get("headline"):
        A(r"\textbf{%s}\\" % latex_escape(verdict.get("decision") or "NO VERDICT YET"))
        if verdict.get("headline"):
            A(latex_escape(verdict["headline"]) + r"\\")
        if verdict.get("summary"):
            A(_tex_paragraphs(verdict["summary"]))
    else:
        A("No verdict has been recorded yet.")
    return "\n".join(lines)


def _evidence_summary(rows: list[dict]) -> str:
    lines = []
    A = lines.append
    A(r"\section*{Evidence Ledger Summary}")
    if not rows:
        A("No evidence ledger was found for this audit.")
        return "\n".join(lines)
    confirmed = sum(1 for r in rows if r["state"] == "CONFIRMED")
    not_found = sum(1 for r in rows if r["state"] == "NOT_FOUND")
    unverified = sum(1 for r in rows if r["state"] == "UNVERIFIED")
    A(r"%d controls checked: %d confirmed present, %d not found, %d unverified.\par"
      % (len(rows), confirmed, not_found, unverified))
    A(r"\vspace{0.2cm}")
    A(r"\begin{tabular}{lll}")
    A(r"\textbf{Control} & \textbf{Lens} & \textbf{State} \\")
    A(r"\hline")
    for row in rows:
        A(r"%s & %s & %s \\" % (
            latex_escape(row["label"]), latex_escape(row["lens"]), latex_escape(row["state"])))
    A(r"\end{tabular}")
    return "\n".join(lines)


def _render_finding(f: dict) -> str:
    lines = []
    A = lines.append
    A(r"\subsection*{%s -- %s}" % (latex_escape(f["id"]), latex_escape(f.get("title") or "")))
    A(r"\textbf{State:} %s \quad \textbf{Severity:} %s \quad \textbf{Owner:} %s\\"
      % (latex_escape(f["state"]), latex_escape(f["severity"]), latex_escape(f.get("owner") or "")))
    if f.get("cross_lens"):
        A(r"\textbf{Cross-lens:} %s\\" % latex_escape(", ".join(f["cross_lens"])))
    if f.get("impact"):
        A(r"\textbf{Impact:} %s\\" % latex_escape(f["impact"]))
    if f.get("failure_path"):
        A(r"\textbf{Why this severity:} %s\\" % latex_escape(f["failure_path"]))
    if f.get("compensating"):
        A(r"\textbf{Compensating control:} %s\\" % latex_escape(f["compensating"]))
    if f.get("evidence"):
        A(r"\textbf{Evidence:}\\")
        A(_verbatim_block("\n".join(f["evidence"])))
    if f.get("probe"):
        A(r"\textbf{Ledger probe:} \texttt{%s}\\" % latex_escape(f["probe"]))
    A(r"\textbf{Fix:} %s\\" % latex_escape(f.get("fix") or ""))
    if f.get("resolve"):
        A(r"\textbf{Evidence that would resolve this:} %s\\" % latex_escape(f["resolve"]))
    if f.get("see"):
        A(r"\textbf{Owned by:} %s\\" % latex_escape(f["see"]))
    return "\n".join(lines)


def _lens_section(report: dict, lens_id: str) -> str:
    lens_meta = next(lens for lens in report["lenses"] if lens["id"] == lens_id)
    findings = [f for f in report["findings"] if f["lens"] == lens_id]
    lines = []
    A = lines.append
    A(r"\section*{%s}" % latex_escape(LENS_LABEL[lens_id]))
    A(r"Status: %s\\" % latex_escape(lens_meta["status"]))
    if lens_meta.get("skippedReason"):
        A(r"Skipped reason: %s\\" % latex_escape(lens_meta["skippedReason"]))
    if not findings:
        A("No findings recorded for this lens.")
    else:
        for f in findings:
            A(_render_finding(f))
    return "\n".join(lines)


def _unverified_appendix(findings: list[dict]) -> str:
    unverified = [f for f in findings if f["state"] == "UNVERIFIED"]
    lines = []
    A = lines.append
    A(r"\appendix")
    A(r"\section*{Appendix: Unverified Findings}")
    if not unverified:
        A("No unverified findings.")
        return "\n".join(lines)
    for f in unverified:
        A(r"\subsection*{%s -- %s}" % (latex_escape(f["id"]), latex_escape(f.get("title") or "")))
        A(r"Lens: %s\\" % latex_escape(f.get("lensLabel") or f.get("lens") or ""))
        if f.get("resolve"):
            A(r"Evidence that would resolve this: %s\\" % latex_escape(f["resolve"]))
    return "\n".join(lines)


def _document(report: dict, ref: str, timestamp: str, context_text: str, scope_text: str,
              evidence_rows: list[dict], lens_id: str | None) -> str:
    """Build one .tex document. `lens_id=None` builds the combined document
    with every lens and the full unverified appendix; a lens id builds the
    single-lens document that carries no other lens's noise."""
    parts = [
        _PREAMBLE,
        _front_page(report, ref, timestamp),
        r"\clearpage",
        _context_scope_verdict(report, context_text, scope_text),
    ]
    if lens_id is None:
        parts.append(_evidence_summary(evidence_rows))
        for lens in LENS_ORDER:
            parts.append(_lens_section(report, lens))
        parts.append(_unverified_appendix(report["findings"]))
    else:
        parts.append(_lens_section(report, lens_id))
        lens_findings = [f for f in report["findings"] if f["lens"] == lens_id]
        parts.append(_unverified_appendix(lens_findings))
    parts.append(_POSTAMBLE)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# PDF compilation - best effort, never an error when unavailable
# --------------------------------------------------------------------------

def _find_tex_compiler():
    """(name, absolute_path) for the first of tectonic, pdflatex found on
    PATH, or None. Checked in that order: tectonic needs no prior TeX
    install, so it is preferred when both are present."""
    for name in ("tectonic", "pdflatex"):
        path = shutil.which(name)
        if path:
            return name, path
    return None


def _compile_pdf(compiler: tuple[str, str], out_dir: Path) -> bool:
    name, path = compiler
    if name == "tectonic":
        cmd = [path, "--outdir", str(out_dir), str(out_dir / "report.tex")]
    else:
        cmd = [path, "-interaction=nonstopmode", "-halt-on-error",
               "-output-directory", str(out_dir), str(out_dir / "report.tex")]
    try:
        subprocess.run(cmd, cwd=out_dir, capture_output=True, encoding="utf-8",
                        timeout=180, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return (out_dir / "report.pdf").exists()


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def export(project_root: Path) -> Path:
    """Write the full export set for one audit snapshot and return its
    directory. Safe to call with no audit present, no verdict written, an
    audit still in progress, and no TeX compiler installed."""
    root = Path(project_root).expanduser().resolve()
    audit = root / ".readiness-audit"
    report = build_report(root)

    ref = _git_short_ref(root)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = _unique_export_dir(audit, ref, timestamp)
    out_dir.mkdir(parents=True, exist_ok=True)

    context_text = _read_optional(audit / "context.md")
    scope_text = _read_optional(audit / "scope.md")
    evidence_rows = _load_evidence_summary(audit)

    combined = _document(report, ref, timestamp, context_text, scope_text, evidence_rows, None)
    (out_dir / "report.tex").write_text(combined, encoding="utf-8")

    for lens in LENS_ORDER:
        doc = _document(report, ref, timestamp, context_text, scope_text, evidence_rows, lens)
        (out_dir / f"report-{lens}.tex").write_text(doc, encoding="utf-8")

    md_source = audit / "report.md"
    if md_source.exists():
        try:
            (out_dir / "report.md").write_text(
                md_source.read_text(encoding="utf-8"), encoding="utf-8")
        except (OSError, UnicodeError):
            pass

    compiler = _find_tex_compiler()
    if compiler:
        _compile_pdf(compiler, out_dir)
    else:
        # Written to stderr because export() is also called in-process by the
        # dashboard server, where stdout belongs to the caller.
        print("No TeX compiler found on PATH (checked tectonic, pdflatex); the .tex files "
              f"were written to {out_dir}. Install tectonic (tectonic-typesetting.github.io) "
              "or a TeX distribution, then run `tectonic report.tex` or `pdflatex report.tex` "
              "inside that directory to produce report.pdf.", file=sys.stderr)

    return out_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root", type=Path)
    args = parser.parse_args(argv)
    out_dir = export(args.project_root)
    print(str(out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
