# Production Readiness Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a dependency-free, localhost-only dashboard that automatically accompanies a Claude Code production-readiness audit and can be started manually by any compatible agent.

**Architecture:** A new Python standard-library server reads a target project's `.readiness-audit/` directory and provides a normalized `/api/snapshot` endpoint plus a self-contained single-page dashboard at `/`. The existing audit skill starts the server as a non-blocking managed background task after initialization; a separate dashboard skill exposes the same server for manual use.

**Tech Stack:** Python 3 standard library (`argparse`, `html`, `http.server`, `json`, `pathlib`, `socketserver`, `threading`, `unittest`), static HTML/CSS/JavaScript, Markdown skill documentation.

**Spec:** `docs/superpowers/specs/2026-08-30-readiness-dashboard-design.md`

## Global Constraints

- Bind only to `127.0.0.1`; the server must not accept or document a public bind address.
- Use Python 3 standard library only; do not add Node, packages, a file watcher, WebSockets, or SSE.
- Read only `<target>/.readiness-audit/`; never modify the target project or the audit trail.
- The audit stays parallel by default. Dashboard startup is non-blocking and dashboard failure never stops the audit.
- Poll `/api/snapshot` every 2 seconds only while snapshot status is `running`.
- Display artifact text as escaped text, never as parsed HTML.
- Preserve the prototype's locked information architecture: Overview, Findings, Evidence, Report, plus lens-run drill-downs.
- Keep the user's existing untracked `.gitignore` untouched. `docs/` is ignored, so force-add only the plan/spec paths that are intentionally tracked.

---

### Task 1: Read-only snapshot builder

**Files:**

- Create: `scripts/readiness_dashboard.py`
- Create: `tests/test_readiness_dashboard.py`

**Interfaces:**

- Produces `build_snapshot(project_root: pathlib.Path) -> dict`.
- Produces `read_text_if_present(path: pathlib.Path) -> str | None`.
- Produces `LENS_ORDER: tuple[str, ...]` in canonical seven-lens order.
- Consumes `<project_root>/.readiness-audit/state.json`, `findings/*.md`, `report.md`, and `evidence/absence-ledger.md`.
- Returns exactly the top-level snapshot keys `status`, `auditRoot`, `updatedAt`, `stage`, `executionMode`, `lenses`, `summary`, `artifacts`, and `message`.

- [ ] **Step 1: Write the failing snapshot tests**

```python
import json
import tempfile
import unittest
from pathlib import Path

from scripts.readiness_dashboard import build_snapshot


class SnapshotTests(unittest.TestCase):
    def test_missing_audit_returns_unavailable_snapshot(self):
        root = Path(tempfile.mkdtemp())

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["status"], "unavailable")
        self.assertEqual(snapshot["stage"], {"name": None, "status": None, "note": None})
        self.assertIn("No .readiness-audit directory", snapshot["message"])

    def test_running_audit_includes_stage_mode_and_lens_progress(self):
        root = Path(tempfile.mkdtemp())
        audit = root / ".readiness-audit"
        (audit / "findings").mkdir(parents=True)
        (audit / "state.json").write_text(json.dumps({
            "stage": "3-lenses",
            "stage_status": "in_progress",
            "execution_mode": "parallel",
            "notes": [{"stage": "3-lenses", "note": "Wave one complete"}],
            "lenses_to_run": ["security", "backend"],
            "lenses_skipped": {"ai-security": "No model calls"},
            "updated_at": "2026-08-30T10:00:00+00:00",
        }))
        (audit / "findings" / "security.md").write_text("# Security\n\n## P0\n")

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(snapshot["stage"], {"name": "3-lenses", "status": "in_progress", "note": "Wave one complete"})
        self.assertEqual(snapshot["executionMode"], "parallel")
        self.assertEqual(snapshot["lenses"][0]["status"], "complete")
        self.assertEqual(snapshot["lenses"][-1], {
            "id": "ai-security", "label": "AI security", "status": "skipped",
            "findingPath": "findings/ai-security.md",
        })

    def test_complete_audit_uses_report_and_counts_severity_labels(self):
        root = Path(tempfile.mkdtemp())
        audit = root / ".readiness-audit"
        (audit / "findings").mkdir(parents=True)
        (audit / "state.json").write_text(json.dumps({
            "stage": "5-report", "stage_status": "complete", "lenses_to_run": ["security"],
            "lenses_skipped": {}, "notes": []
        }))
        (audit / "findings" / "security.md").write_text("## P0\n## P1\nUNVERIFIED\n")
        (audit / "report.md").write_text("# Report\n\n## Verdict\nHOLD - DO NOT DEPLOY\n")

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["status"], "complete")
        self.assertEqual(snapshot["summary"], {"p0": 1, "p1": 1, "p2": 0, "unverified": 1, "verdict": "HOLD - DO NOT DEPLOY"})
        self.assertEqual(snapshot["artifacts"]["report"], {"available": True, "content": "# Report\n\n## Verdict\nHOLD - DO NOT DEPLOY\n"})
```

- [ ] **Step 2: Run the snapshot tests to verify they fail**

Run: `python3 -m unittest tests.test_readiness_dashboard.SnapshotTests -v`

Expected: FAIL because `scripts.readiness_dashboard` does not exist.

- [ ] **Step 3: Implement the smallest snapshot builder**

```python
LENS_ORDER = ("security", "backend", "frontend", "devops", "qa", "database", "ai-security")


def build_snapshot(project_root: Path) -> dict:
    audit_root = project_root / ".readiness-audit"
    if not audit_root.is_dir():
        return unavailable_snapshot(project_root, "No .readiness-audit directory exists for this project yet.")
    state = load_state(audit_root / "state.json")
    if state is None:
        return unavailable_snapshot(project_root, "Audit state is not available yet; wait for preflight to finish.")
    return snapshot_from_state(project_root, audit_root, state)
```

Implement `load_state` so malformed JSON returns `None`, `read_text_if_present` so missing/unreadable files return `None`, and `snapshot_from_state` so it derives lens status, severity counts, verdict, report, findings, and ledger without writing files.

- [ ] **Step 4: Run the snapshot tests to verify they pass**

Run: `python3 -m unittest tests.test_readiness_dashboard.SnapshotTests -v`

Expected: PASS.

- [ ] **Step 5: Add malformed and partial-artifact regression tests**

```python
    def test_malformed_state_is_unavailable_instead_of_raising(self):
        root = Path(tempfile.mkdtemp())
        audit = root / ".readiness-audit"
        audit.mkdir()
        (audit / "state.json").write_text("{")

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIn("not readable", snapshot["message"])

    def test_missing_report_keeps_running_audit_partial(self):
        root = Path(tempfile.mkdtemp())
        audit = root / ".readiness-audit"
        audit.mkdir()
        (audit / "state.json").write_text(json.dumps({
            "stage": "4-validation", "stage_status": "in_progress", "lenses_to_run": [],
            "lenses_skipped": {}, "notes": []
        }))

        snapshot = build_snapshot(root)

        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(snapshot["artifacts"]["report"], {"available": False, "content": None})
```

- [ ] **Step 6: Run all snapshot tests**

Run: `python3 -m unittest tests.test_readiness_dashboard -v`

Expected: PASS.

- [ ] **Step 7: Commit Task 1**

```bash
git add scripts/readiness_dashboard.py tests/test_readiness_dashboard.py
git commit -m "feat: add readiness dashboard snapshot"
```

### Task 2: Local HTTP server and safe static dashboard shell

**Files:**

- Modify: `scripts/readiness_dashboard.py`
- Modify: `tests/test_readiness_dashboard.py`

**Interfaces:**

- Produces `create_server(project_root: pathlib.Path, port: int = 0) -> http.server.ThreadingHTTPServer`.
- Produces `serve(project_root: pathlib.Path, port: int = 0) -> None`.
- Serves `GET /` as `text/html; charset=utf-8` and `GET /api/snapshot` as `application/json; charset=utf-8`.
- Returns 404 for every other path.
- Consumes `build_snapshot(project_root)` from Task 1.

- [ ] **Step 1: Write the failing HTTP tests**

```python
import http.client
import threading

from scripts.readiness_dashboard import create_server


class DashboardHttpTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.server = create_server(self.root, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path):
        host, port = self.server.server_address
        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read().decode()
        connection.close()
        return response, body

    def test_server_binds_to_loopback_and_serves_snapshot(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        response, body = self.request("/api/snapshot")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(json.loads(body)["status"], "unavailable")

    def test_server_serves_dashboard_and_unknown_paths_are_404(self):
        root_response, root_body = self.request("/")
        missing_response, _ = self.request("/unexpected")
        self.assertEqual(root_response.status, 200)
        self.assertIn('id="app"', root_body)
        self.assertEqual(missing_response.status, 404)
```

- [ ] **Step 2: Run the HTTP tests to verify they fail**

Run: `python3 -m unittest tests.test_readiness_dashboard.DashboardHttpTests -v`

Expected: FAIL because `create_server` does not exist.

- [ ] **Step 3: Implement loopback-only HTTP routing**

```python
class DashboardServer(ThreadingHTTPServer):
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
```

Set `allow_reuse_address = True` on the server class and silence the default request log so artifacts are never printed.

- [ ] **Step 4: Run the HTTP tests to verify they pass**

Run: `python3 -m unittest tests.test_readiness_dashboard.DashboardHttpTests -v`

Expected: PASS.

- [ ] **Step 5: Write the failing startup-announcement test**

```python
    def test_startup_url_uses_the_actual_ephemeral_port(self):
        server = create_server(Path(tempfile.mkdtemp()), port=0)
        host, port = server.server_address
        try:
            self.assertEqual(host, "127.0.0.1")
            self.assertGreater(port, 0)
            self.assertEqual(startup_url(server), f"http://127.0.0.1:{port}/")
        finally:
            server.server_close()
```

- [ ] **Step 6: Implement and verify `startup_url`**

```python
def startup_url(server: DashboardServer) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}/"
```

Run: `python3 -m unittest tests.test_readiness_dashboard -v`

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

```bash
git add scripts/readiness_dashboard.py tests/test_readiness_dashboard.py
git commit -m "feat: serve local readiness dashboard"
```

### Task 3: Self-contained client with locked navigation and polling

**Files:**

- Modify: `scripts/readiness_dashboard.py`
- Modify: `tests/test_readiness_dashboard.py`

**Interfaces:**

- Produces `DASHBOARD_HTML: str` containing no remote scripts, styles, fonts, or image URLs.
- Client consumes `GET /api/snapshot`.
- Client exposes four buttons marked `data-route="overview|findings|evidence|report"` and a lens detail view.
- Client escapes artifact text using a local `escapeHtml(value)` function before assigning it to rendered HTML.

- [ ] **Step 1: Write the failing client-contract tests**

```python
from scripts.readiness_dashboard import DASHBOARD_HTML


class DashboardClientContractTests(unittest.TestCase):
    def test_client_has_only_the_locked_navigation_destinations(self):
        for route in ("overview", "findings", "evidence", "report"):
            self.assertIn(f'data-route="{route}"', DASHBOARD_HTML)
        self.assertNotIn("switcher", DASHBOARD_HTML)
        self.assertNotIn("Back to dashboard", DASHBOARD_HTML)

    def test_client_polls_snapshot_every_two_seconds_while_running(self):
        self.assertIn("/api/snapshot", DASHBOARD_HTML)
        self.assertIn("setTimeout(refresh, 2000)", DASHBOARD_HTML)
        self.assertIn("snapshot.status === 'running'", DASHBOARD_HTML)

    def test_client_escapes_artifact_content_and_has_no_remote_assets(self):
        self.assertIn("function escapeHtml", DASHBOARD_HTML)
        self.assertNotIn("https://", DASHBOARD_HTML)
        self.assertNotIn("http://", DASHBOARD_HTML)
```

- [ ] **Step 2: Run the client-contract tests to verify they fail**

Run: `python3 -m unittest tests.test_readiness_dashboard.DashboardClientContractTests -v`

Expected: FAIL because the static client is not defined.

- [ ] **Step 3: Implement the minimum client**

Embed one static page in `DASHBOARD_HTML` with:

```javascript
async function refresh() {
  const response = await fetch('/api/snapshot', { cache: 'no-store' });
  const snapshot = await response.json();
  render(snapshot);
  if (snapshot.status === 'running') setTimeout(refresh, 2000);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[character]);
}
```

Implement Overview as the aligned timeline with current state and lens cards; Findings as available raw finding summaries; Evidence as an in-page right drawer; Report as escaped `<pre>` text; and lens cards as in-page details. Use buttons with `data-route` attributes and history hash state, not a second page or comparison switcher.

- [ ] **Step 4: Run the client-contract tests to verify they pass**

Run: `python3 -m unittest tests.test_readiness_dashboard.DashboardClientContractTests -v`

Expected: PASS.

- [ ] **Step 5: Run all dashboard tests and inspect source safety**

Run:

```bash
python3 -m unittest tests.test_readiness_dashboard -v
rg -n '0\.0\.0\.0|https://|http://' scripts/readiness_dashboard.py
```

Expected: tests PASS; grep shows only the loopback URL construction in `startup_url`.

- [ ] **Step 6: Commit Task 3**

```bash
git add scripts/readiness_dashboard.py tests/test_readiness_dashboard.py
git commit -m "feat: add readiness audit dashboard UI"
```

### Task 4: Add the manual dashboard skill and automatic Claude launch

**Files:**

- Create: `skills/production-readiness-dashboard/SKILL.md`
- Modify: `skills/production-readiness-audit/SKILL.md`
- Modify: `README.md`
- Create: `tests/test_dashboard_skill_docs.py`

**Interfaces:**

- Manual command: `/prod-readiness:production-readiness-dashboard`.
- Server command: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/readiness_dashboard.py" <target-project-root>`.
- Audit Stage 0 starts the same command as a managed background Bash task only after successful `audit_state.py init` or successful state-resume confirmation.
- Dashboard launch failure is caught and reported as non-fatal before the audit continues.

- [ ] **Step 1: Write the failing skill/documentation contract test**

```python
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class DashboardSkillDocumentationTests(unittest.TestCase):
    def test_manual_dashboard_skill_uses_python_loopback_server(self):
        skill = (ROOT / "skills/production-readiness-dashboard/SKILL.md").read_text()
        self.assertIn("/prod-readiness:production-readiness-dashboard", skill)
        self.assertIn("readiness_dashboard.py", skill)
        self.assertIn("127.0.0.1", skill)
        self.assertIn("read-only", skill.lower())

    def test_audit_starts_dashboard_without_changing_parallel_default(self):
        audit_skill = (ROOT / "skills/production-readiness-audit/SKILL.md").read_text()
        self.assertIn("readiness_dashboard.py", audit_skill)
        self.assertIn("managed background Bash task", audit_skill)
        self.assertIn("Parallel is the default", audit_skill)
        self.assertIn("non-fatal", audit_skill)

    def test_readme_explains_automatic_and_manual_dashboard_use(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("production-readiness-dashboard", readme)
        self.assertIn("automatically", readme.lower())
        self.assertIn("127.0.0.1", readme)
```

- [ ] **Step 2: Run the documentation test to verify it fails**

Run: `python3 -m unittest tests.test_dashboard_skill_docs -v`

Expected: FAIL because the dashboard skill and launch documentation do not exist.

- [ ] **Step 3: Write the manual dashboard skill**

Include frontmatter `name: production-readiness-dashboard`; state that it is optional, read-only, local-only, and does not interrupt or rerun an audit. Provide exactly this command, with `<target-project-root>` replaced by the open project path:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/readiness_dashboard.py" <target-project-root>
```

Tell the user the printed `http://127.0.0.1:<port>/` URL is the fallback if their browser does not open automatically, and Ctrl-C stops a manually launched server.

- [ ] **Step 4: Update audit Stage 0 with non-blocking automatic launch**

Add a short Stage 0 subsection directly after initialization/resume handling. It instructs Claude to start the command in a managed background Bash task, wait only for the URL line or an immediate error, best-effort open that URL, report it, and continue immediately to Stage 1. It must also say:

```text
If dashboard launch fails, tell the user it is unavailable and continue the audit.
Do not wait for the dashboard process, use it as an audit agent, or change the selected parallel/sequential execution mode.
```

- [ ] **Step 5: Add concise README instructions**

Add a `### Watch an audit in your browser (optional)` section immediately after execution-mode instructions. Document automatic Claude launch, manual command invocation, local `127.0.0.1` scope, browser URL fallback, Ctrl-C behavior for manual use, and that the audit proceeds if the dashboard cannot start.

- [ ] **Step 6: Run documentation tests to verify they pass**

Run: `python3 -m unittest tests.test_dashboard_skill_docs -v`

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add skills/production-readiness-dashboard/SKILL.md \
  skills/production-readiness-audit/SKILL.md README.md tests/test_dashboard_skill_docs.py
git commit -m "feat: launch readiness dashboard with audits"
```

### Task 5: Full verification and release handoff

**Files:**

- Modify only if verification reveals a defect in a task above.

**Interfaces:**

- Confirms the server, client contract, skill docs, and existing routing/state tests remain compatible.

- [ ] **Step 1: Run the full repository test suite**

Run: `python3 -m unittest discover -s tests -v`

Expected: PASS, including existing `test_audit_state.py` and `test_route_readiness_prompt.py`.

- [ ] **Step 2: Verify a real ephemeral server session**

Run:

```bash
tmp_root=$(mktemp -d)
python3 scripts/readiness_dashboard.py "$tmp_root" > "$tmp_root/dashboard.log" 2>&1 &
dashboard_pid=$!
sleep 1
url=$(head -n 1 "$tmp_root/dashboard.log")
curl --fail --silent "$url/api/snapshot"
kill "$dashboard_pid"
wait "$dashboard_pid" 2>/dev/null || true
```

Expected: the snapshot JSON has `"status": "unavailable"`; the printed URL begins with `http://127.0.0.1:`; server exits after the explicit stop.

- [ ] **Step 3: Verify scope and working tree**

Run:

```bash
git diff --check HEAD~1..HEAD
git status --short
rg -n '0\.0\.0\.0|WebSocket|EventSource|https?://' scripts/readiness_dashboard.py skills/production-readiness-dashboard README.md
```

Expected: no whitespace errors; no unrelated tracked changes; no public bind or remote dashboard dependency. The pre-existing untracked `.gitignore` may remain.

- [ ] **Step 4: Commit any verification-only corrections**

```bash
git add scripts/readiness_dashboard.py \
  tests/test_readiness_dashboard.py \
  skills/production-readiness-dashboard/SKILL.md \
  skills/production-readiness-audit/SKILL.md \
  README.md \
  tests/test_dashboard_skill_docs.py
git commit -m "fix: harden readiness dashboard"
```

- [ ] **Step 5: Hand off**

Report the localhost-only safety boundary, exact tests run, the automatic Claude behavior, the manual command, and the implementation commits. Do not claim browser auto-open was verified unless the environment actually opened it.
