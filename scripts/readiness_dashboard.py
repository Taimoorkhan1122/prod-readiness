#!/usr/bin/env python3
"""Read-only snapshot helpers for the local production-readiness dashboard."""

import argparse
import json
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Production Readiness</title>
    <style>
      :root { color-scheme: dark; font-family: ui-sans-serif, system-ui, sans-serif; background: #0b1220; color: #e5edf9; }
      * { box-sizing: border-box; } body { margin: 0; } button { font: inherit; }
      .shell { max-width: 1180px; margin: auto; padding: 24px; } .topbar { display: flex; justify-content: space-between; gap: 20px; align-items: start; border-bottom: 1px solid #23324a; padding-bottom: 18px; }
      h1, h2, h3, p { margin-top: 0; } h1 { font-size: 1.6rem; margin-bottom: 5px; } h2 { font-size: 1.1rem; } .muted { color: #a9b8cc; }
      .nav { display: flex; flex-wrap: wrap; gap: 8px; } .nav button, .button { cursor: pointer; border: 1px solid #465b7d; border-radius: 7px; background: #17243a; color: inherit; padding: 8px 12px; }
      .nav button[aria-current="page"] { background: #2559a7; border-color: #6fa4ff; } .summary { display: grid; grid-template-columns: repeat(5, minmax(110px, 1fr)); gap: 12px; margin: 22px 0; }
      .metric, .panel, .lens { border: 1px solid #253751; border-radius: 9px; background: #101b2c; padding: 15px; } .metric strong { display: block; font-size: 1.4rem; }
      .timeline { display: grid; gap: 10px; } .timeline-item { display: grid; grid-template-columns: 130px 1fr; gap: 12px; padding: 13px; border-left: 3px solid #41658f; background: #101b2c; }
      .lens-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 12px; margin-top: 14px; } .lens { text-align: left; cursor: pointer; color: inherit; }
      .lens .status { display: block; margin-top: 8px; } .status { color: #9fb5d8; font-size: .88rem; text-transform: capitalize; } .status.complete { color: #62d89b; } .status.running { color: #ffd36d; } .status.unavailable { color: #ff9c9c; }
      .columns { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; } pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; font: .85rem/1.5 ui-monospace, monospace; color: #d3e1f5; }
      details { border-top: 1px solid #253751; padding: 12px 0; } details:first-of-type { border-top: 0; } summary { cursor: pointer; color: #c9dcf9; } .empty { padding: 22px; text-align: center; }
      .drawer-backdrop { position: fixed; inset: 0; background: rgba(0, 0, 0, .48); } .drawer { position: fixed; inset: 0 0 0 auto; width: min(560px, 94vw); background: #0d1728; border-left: 1px solid #415977; box-shadow: -12px 0 32px rgba(0, 0, 0, .35); padding: 24px; overflow: auto; }
      .drawer-header { display: flex; align-items: start; justify-content: space-between; gap: 12px; } .drawer-header button { background: transparent; color: inherit; border: 0; font-size: 1.4rem; cursor: pointer; }
      @media (max-width: 720px) { .shell { padding: 16px; } .topbar, .columns { display: block; } .nav { margin-top: 16px; } .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); } .columns > * { margin-bottom: 16px; } .timeline-item { grid-template-columns: 1fr; gap: 5px; } }
    </style>
  </head>
  <body>
    <main id="app" aria-live="polite"></main>
    <script>
      const app = document.getElementById('app');
      let currentSnapshot = null;
      let activeLens = null;

      function escapeHtml(value) {
        return String(value).replace(/[&<>"']/g, character => ({
          '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        })[character]);
      }

      function routeFromHash() {
        const route = window.location.hash.slice(1);
        return ['overview', 'findings', 'evidence', 'report'].includes(route) ? route : 'overview';
      }

      function findingFor(snapshot, path) {
        return (snapshot.artifacts.findings || []).find(finding => finding.path === path);
      }

      function statusClass(status) {
        return ['complete', 'running', 'unavailable'].includes(status) ? status : 'waiting';
      }

      function lensCards(snapshot) {
        return (snapshot.lenses || []).map(lens => `
          <button class="lens" type="button" data-lens="${escapeHtml(lens.id)}">
            <strong>${escapeHtml(lens.label)}</strong>
            <span class="status ${statusClass(lens.status)}">${escapeHtml(lens.status)}</span>
          </button>`).join('') || '<p class="empty">No lens status is available yet.</p>';
      }

      function lensDetail(snapshot) {
        if (!activeLens) return '';
        const lens = (snapshot.lenses || []).find(item => item.id === activeLens);
        if (!lens) return '';
        const finding = findingFor(snapshot, lens.findingPath);
        const content = finding && finding.available ? finding.content : 'No readable finding artifact is available for this lens.';
        return `<section class="panel" id="lens-detail"><div class="drawer-header"><div><h2>${escapeHtml(lens.label)} lens</h2><p class="status ${statusClass(lens.status)}">${escapeHtml(lens.status)}</p></div><button type="button" data-close-lens aria-label="Close lens detail">×</button></div><pre>${escapeHtml(content)}</pre></section>`;
      }

      function overview(snapshot) {
        return `<section><div class="summary">
          <div class="metric"><strong>${escapeHtml(snapshot.summary.p0)}</strong><span>P0 findings</span></div>
          <div class="metric"><strong>${escapeHtml(snapshot.summary.p1)}</strong><span>P1 findings</span></div>
          <div class="metric"><strong>${escapeHtml(snapshot.summary.p2)}</strong><span>P2 findings</span></div>
          <div class="metric"><strong>${escapeHtml(snapshot.summary.unverified)}</strong><span>Unverified</span></div>
          <div class="metric"><strong>${escapeHtml(snapshot.summary.verdict || 'Pending')}</strong><span>Verdict</span></div>
        </div><div class="columns"><section class="panel"><h2>Current state</h2><div class="timeline">
          <div class="timeline-item"><strong>Audit</strong><span class="status ${statusClass(snapshot.status)}">${escapeHtml(snapshot.status)}</span></div>
          <div class="timeline-item"><strong>${escapeHtml(snapshot.stage.name || 'Waiting')}</strong><span>${escapeHtml(snapshot.stage.note || snapshot.message || 'No status note yet.')}</span></div>
          <div class="timeline-item"><strong>Mode</strong><span>${escapeHtml(snapshot.executionMode || 'Not available')}</span></div>
          <div class="timeline-item"><strong>Updated</strong><span>${escapeHtml(snapshot.updatedAt || 'Not available')}</span></div>
        </div></section><section class="panel"><h2>Lens progress</h2><div class="lens-grid">${lensCards(snapshot)}</div></section></div>${lensDetail(snapshot)}</section>`;
      }

      function findings(snapshot) {
        const findings = snapshot.artifacts.findings || [];
        const list = findings.length ? findings.map(finding => {
          const text = finding.available ? finding.content : 'Artifact is unavailable.';
          const summary = String(text || '').split('\\n').find(line => line.trim()) || finding.path;
          return `<details><summary>${escapeHtml(finding.path)} — ${escapeHtml(summary)}</summary><pre>${escapeHtml(text)}</pre></details>`;
        }).join('') : '<p class="empty">No finding artifacts are available yet.</p>';
        return `<section class="panel"><h2>Findings</h2><p class="muted">Raw finding summaries from the current audit snapshot.</p>${list}</section>`;
      }

      function report(snapshot) {
        const report = snapshot.artifacts.report || {};
        const text = report.available ? report.content : 'The report artifact is not available yet.';
        return `<section class="panel"><h2>Report</h2><pre>${escapeHtml(text)}</pre></section>`;
      }

      function evidenceDrawer(snapshot, visible) {
        if (!visible) return '';
        const artifact = snapshot.artifacts.evidenceLedger || {};
        const text = artifact.available ? artifact.content : 'The evidence ledger is not available yet.';
        return `<div class="drawer-backdrop" data-evidence-backdrop><aside class="drawer" role="dialog" aria-modal="true" aria-label="Evidence ledger"><div class="drawer-header"><div><h2>Evidence</h2><p class="muted">Read-only absence ledger</p></div><button type="button" data-close-evidence aria-label="Close evidence">×</button></div><pre>${escapeHtml(text)}</pre></aside></div>`;
      }

      function render(snapshot) {
        currentSnapshot = snapshot;
        const route = routeFromHash();
        const content = route === 'findings' ? findings(snapshot) : route === 'report' ? report(snapshot) : overview(snapshot);
        app.innerHTML = `<div class="shell"><header class="topbar"><div><h1>Production readiness</h1><p class="muted">${escapeHtml(snapshot.message || 'Loading snapshot…')}</p></div><nav class="nav" aria-label="Dashboard views">
          <button type="button" data-route="overview" ${route === 'overview' ? 'aria-current="page"' : ''}>Overview</button>
          <button type="button" data-route="findings" ${route === 'findings' ? 'aria-current="page"' : ''}>Findings</button>
          <button type="button" data-route="evidence" ${route === 'evidence' ? 'aria-current="page"' : ''}>Evidence</button>
          <button type="button" data-route="report" ${route === 'report' ? 'aria-current="page"' : ''}>Report</button>
        </nav></header>${content}</div>${evidenceDrawer(snapshot, route === 'evidence')}`;
      }

      app.addEventListener('click', event => {
        const routeButton = event.target.closest('[data-route]');
        if (routeButton) { window.location.hash = routeButton.dataset.route; return; }
        const lensButton = event.target.closest('[data-lens]');
        if (lensButton && currentSnapshot) { activeLens = lensButton.dataset.lens; window.location.hash = 'overview'; render(currentSnapshot); return; }
        if (event.target.closest('[data-close-lens]')) { activeLens = null; render(currentSnapshot); return; }
        if (event.target.closest('[data-close-evidence]') || event.target.matches('[data-evidence-backdrop]')) { window.location.hash = 'overview'; }
      });

      window.addEventListener('hashchange', () => { if (currentSnapshot) render(currentSnapshot); });

      async function refresh() {
        try {
          const response = await fetch('/api/snapshot', { cache: 'no-store' });
          if (!response.ok) throw new Error(`Snapshot request failed (${response.status})`);
          const snapshot = await response.json();
          render(snapshot);
          if (snapshot.status === 'running') setTimeout(refresh, 2000);
        } catch (error) {
          app.innerHTML = `<div class="shell"><section class="panel"><h1>Production readiness</h1><p>${escapeHtml(error.message || 'Unable to load the snapshot.')}</p></section></div>`;
        }
      }

      refresh();
    </script>
  </body>
</html>
"""


class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, project_root: Path, port: int):
        self.project_root = project_root
        super().__init__(("127.0.0.1", port), DashboardRequestHandler)


class DashboardRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            return self.respond(HTTPStatus.OK, "text/html; charset=utf-8", DASHBOARD_HTML.encode())
        if self.path == "/api/snapshot":
            payload = json.dumps(build_snapshot(self.server.project_root)).encode()
            return self.respond(HTTPStatus.OK, "application/json; charset=utf-8", payload)
        self.send_error(HTTPStatus.NOT_FOUND)

    def respond(self, status: HTTPStatus, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def create_server(project_root: Path, port: int = 0) -> ThreadingHTTPServer:
    return DashboardServer(project_root, port)


def startup_url(server: DashboardServer) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}/"


def serve(project_root: Path, port: int = 0) -> None:
    server = create_server(project_root, port)
    print(startup_url(server), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve a read-only production-readiness dashboard.")
    parser.add_argument("project_root", type=Path, help="Target project root containing .readiness-audit")
    parser.add_argument("--port", type=int, default=0, help="Port to bind on 127.0.0.1 (default: ephemeral)")
    args = parser.parse_args(argv)
    serve(args.project_root, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
