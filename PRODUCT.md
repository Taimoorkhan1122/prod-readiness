# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Solo developers and vibe coders running the production-readiness audit against their own repo. They kick off `/production-readiness-audit`, then watch the dashboard on `127.0.0.1` while the seven-lens audit runs in the background, and read the go/no-go verdict when it finishes.

## Product Purpose

The dashboard exists so the audit's progress and results are legible without reading raw terminal output or JSON. It shows live stage progress while the audit runs, then a skimmable verdict and findings (CONFIRMED / NOT FOUND / UNVERIFIED per lens) once complete.

## Positioning

Live progress plus a skimmable, markdown-rendered verdict — the thing a plain CLI/terminal log cannot give: at-a-glance state of a multi-minute, multi-lens background process, and findings you can scan instead of grep.

## Operating Context

- Auto-launched in the background by the `production-readiness-audit` skill; can also be run manually via `scripts/readiness_dashboard.py`.
- Reads its snapshot from `.readiness-audit/state.json` and per-lens finding/report files under `.readiness-audit/`, polling every 2 seconds for live updates.
- Runs for the duration of the audit (stage progress across 7 lenses), then continues serving the completed report.

## Capabilities and Constraints

- Local-only: server binds to `127.0.0.1`; never exposed beyond localhost.
- Zero external dependencies: implemented with Python's stdlib `http.server` (`ThreadingHTTPServer`/`BaseHTTPRequestHandler`) only — no Flask/FastAPI/etc.
- Single-file delivery: the HTML/CSS/JS is an embedded string (`DASHBOARD_HTML`) inside `scripts/readiness_dashboard.py`, served as one response — no separate static asset pipeline.
- No authentication (matches local-only, single-user trust model).
- Read-only with respect to the audit: the dashboard displays state, it does not mutate audit data.
- Views/nav: Overview and Findings tabs (per prior work — verify current tab set against code before assuming more exist).
- Markdown rendering is used for findings, report, and evidence text.

## Product Principles

1. Legible over exhaustive — surface stage/verdict at a glance before requiring drill-down.
2. Zero-friction to run — no install step, no auth, no external service; must work the instant the audit starts.
3. Local-only trust boundary is non-negotiable — never widen beyond `127.0.0.1` or add network egress.
4. Read-only observer — the dashboard never becomes a control plane for the audit.
5. Built for a solo dev's terminal-adjacent workflow, not a hosted multi-user product.
