#!/usr/bin/env python3
"""Read-only snapshot server for the local production-readiness dashboard.

Everything the dashboard shows comes from structured data - `findings/*.json`
and `verdict.json`, assembled by `finding_store.py`. Nothing here parses prose.
The markdown trail exists for agents that fix what the audit found; the
dashboard exists for the person deciding whether to ship, and that person
should never have to read a file path to get an answer.
"""

import argparse
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parent))

from finding_store import LENS_LABEL, LENS_ORDER, build_report  # noqa: E402

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Production readiness</title>
    <style>
      :root {
        color-scheme: light;
        --ink:#18222c; --muted:#5c6a76; --paper:#f7f7f5; --line:#dbe0df; --white:#fff;
        --green:#087f5b; --amber:#a35f00; --red:#b4302b; --blue:#2d63c8; --navy:#11263d;
        --p0-bg:#fdeceb; --p0-ink:#8a231b; --p1-bg:#fdf1dd; --p1-ink:#7a4a00;
        --p2-bg:#e8eef7; --p2-ink:#33517d;
      }
      * { box-sizing: border-box; }
      body { margin:0; min-width:320px; color:var(--ink); background:var(--paper);
        font: 15px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      ::selection { background:#c7e7da; color:var(--ink); }
      ::-webkit-scrollbar { width:11px; height:11px; }
      ::-webkit-scrollbar-thumb { background:#c4ccca; border-radius:99px; border:3px solid var(--paper); }
      ::-webkit-scrollbar-thumb:hover { background:#a9b3b1; }
      button { font:inherit; cursor:pointer; color:inherit; }
      :focus-visible { outline:3px solid #8db1f4; outline-offset:3px; border-radius:4px; }
      h1,h2,h3,p { margin:0; }
      h1 { font-size:clamp(1.9rem, 3.6vw, 3.1rem); line-height:1.04; letter-spacing:-.035em; max-width:20ch; }
      h2 { font-size:1.18rem; letter-spacing:-.022em; }
      h3 { font-size:1rem; letter-spacing:-.014em; }
      a { color:var(--blue); text-underline-offset:3px; text-decoration-thickness:1px; }
      .skip { position:absolute; left:-9999px; top:12px; z-index:20; padding:10px 14px;
        background:var(--navy); color:var(--white); border-radius:9px; font-weight:750; text-decoration:none; }
      .skip:focus { left:14px; }
      .shell { max-width:1140px; margin:auto; padding:30px 26px 110px; }
      .muted { color:var(--muted); }
      .lede { max-width:62ch; color:var(--muted); font-size:1.03rem; margin-top:14px; }
      .num { font-variant-numeric:tabular-nums; letter-spacing:-.045em; }
      .mono { font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:.85em; }

      .site-head { display:flex; align-items:center; gap:20px; padding-bottom:20px; border-bottom:1px solid var(--line); }
      .brand { display:flex; align-items:center; gap:10px; font-weight:750; letter-spacing:-.025em; }
      .mark { width:24px; height:24px; border-radius:8px 8px 8px 2px; background:var(--navy); position:relative; flex:none; }
      .mark:after { content:""; position:absolute; right:5px; top:5px; width:7px; height:7px; border-radius:50%; background:#8fe0bf; }
      .nav { display:flex; gap:3px; margin-left:auto; flex-wrap:wrap; }
      .nav button { border:0; border-radius:8px; padding:7px 11px; background:transparent;
        color:var(--muted); font-size:.85rem; font-weight:750; }
      .nav button:hover { background:#edf0ef; color:var(--ink); }
      .nav button[aria-current="page"] { background:var(--navy); color:var(--white); }
      .live { display:flex; align-items:center; gap:7px; font-size:.83rem; font-weight:650; color:var(--green); }
      .live:before { content:""; width:8px; height:8px; border-radius:50%; background:currentColor; box-shadow:0 0 0 4px #d9f3e8; }
      .live.done { color:var(--muted); } .live.done:before { box-shadow:0 0 0 4px #e6e9e8; }

      .hero { display:grid; grid-template-columns:1.4fr .6fr; gap:36px; align-items:end; padding:52px 0 34px; }
      .decision { display:inline-flex; align-items:center; gap:8px; padding:6px 12px; border-radius:999px;
        font-size:.78rem; font-weight:800; letter-spacing:.02em; border:1px solid currentColor; }
      .decision.hold { color:var(--red); background:#fff3f2; }
      .decision.fix_then_ship { color:var(--amber); background:#fff8e9; }
      .decision.ship { color:var(--green); background:#eefbf5; }
      .decision.pending { color:var(--blue); background:#eff5ff; }
      .hero h1 { margin-top:18px; }
      .risk-strip { display:grid; grid-template-columns:repeat(3,1fr); gap:16px; }
      .risk-strip div { padding-top:13px; border-top:1px solid var(--line); }
      .risk-strip strong { display:block; font-size:2rem; line-height:1; }
      .risk-strip span { display:block; margin-top:5px; color:var(--muted); font-size:.79rem; }
      .risk-strip .r0 strong { color:var(--red); } .risk-strip .r1 strong { color:var(--amber); }

      .band { padding-top:30px; margin-top:8px; border-top:1px solid var(--line); }
      .band + .band { margin-top:34px; }
      .band-head { display:flex; align-items:baseline; justify-content:space-between; gap:16px; flex-wrap:wrap; }
      .band-head p { color:var(--muted); font-size:.87rem; }

      .matrix { display:grid; grid-template-columns:repeat(7,1fr); gap:8px; margin-top:16px; }
      .lens { min-height:88px; padding:12px 11px; border-radius:12px; border:1px solid var(--line);
        background:var(--white); display:flex; flex-direction:column; gap:6px; text-align:left;
        transition:transform 160ms ease, border-color 160ms ease, box-shadow 160ms ease; }
      .lens:hover { transform:translateY(-2px); border-color:#5c8ce2; box-shadow:0 8px 18px rgba(0,0,0,.07); }
      .lens.complete { border-color:#9bd8bf; background:#f4fcf8; }
      .lens.running { border-color:#79a2e9; background:#f1f6ff; }
      .lens.waiting, .lens.skipped { opacity:.66; }
      .lens b { font-size:.83rem; letter-spacing:-.01em; }
      .lens em { font-style:normal; font-size:.72rem; color:var(--muted); margin-top:auto; }
      .dot { width:8px; height:8px; border-radius:50%; background:#aeb8bf; flex:none; }
      .dot.complete { background:var(--green); } .dot.running { background:var(--blue); }
      .dot.skipped { background:#c4ccca; }

      .rows { margin-top:6px; }
      .row { display:grid; grid-template-columns:30px minmax(0,1fr) auto; gap:14px; align-items:start;
        width:100%; padding:18px 0; border:0; border-bottom:1px solid var(--line);
        background:transparent; text-align:left; }
      .row:hover .row-title { color:var(--blue); text-decoration:underline; }
      .row > span { min-width:0; overflow-wrap:anywhere; }
      .sev { width:30px; height:30px; display:grid; place-items:center; border-radius:8px;
        font-size:.71rem; font-weight:800; }
      .sev.p0 { background:var(--p0-bg); color:var(--p0-ink); }
      .sev.p1 { background:var(--p1-bg); color:var(--p1-ink); }
      .sev.p2, .sev.p3 { background:var(--p2-bg); color:var(--p2-ink); }
      .row-title { display:block; font-weight:700; letter-spacing:-.016em; }
      .row-impact { display:block; margin-top:5px; color:var(--muted); max-width:70ch; }
      .row-meta { display:block; margin-top:8px; color:var(--muted); font-size:.81rem; }
      .state { font-weight:750; }
      .state.confirmed { color:var(--red); } .state.not_found { color:var(--amber); }
      .state.unverified { color:var(--muted); }
      .row-open { color:var(--muted); font-size:.78rem; font-weight:750; white-space:nowrap; padding-top:4px; }

      .filters { display:flex; gap:7px; flex-wrap:wrap; margin:18px 0 4px; }
      .filter { border:1px solid var(--line); background:var(--white); border-radius:999px;
        padding:6px 11px; font-size:.78rem; font-weight:750; }
      .filter[aria-pressed="true"] { background:var(--navy); color:var(--white); border-color:var(--navy); }

      .empty { padding:40px 0; color:var(--muted); }
      .panel { border:1px solid var(--line); border-radius:14px; background:var(--white); padding:22px; }
      .ledger-item { padding:17px 0; border-bottom:1px solid var(--line); }
      .ledger-item:last-child { border-bottom:0; }
      .ledger-item strong { display:block; margin-top:8px; }
      .ledger-item p { margin-top:5px; color:var(--muted); font-size:.87rem; overflow-wrap:anywhere; }
      .chip { display:inline-flex; padding:4px 9px; border-radius:999px; font-size:.73rem;
        font-weight:750; border:1px solid currentColor; }
      .chip.confirmed { color:var(--green); background:#eefbf5; }
      .chip.not_found { color:var(--amber); background:#fff8e9; }
      .chip.unverified { color:var(--blue); background:#eff5ff; }

      .scrim { position:fixed; inset:0; z-index:9; background:rgba(17,38,61,.24); }
      .drawer { position:fixed; z-index:10; inset:0 0 0 auto; width:min(560px,100%); overflow:auto;
        background:var(--white); padding:28px; box-shadow:-20px 0 45px rgba(0,0,0,.15);
        animation:drawer-in 260ms cubic-bezier(.16,1,.3,1); }
      @keyframes drawer-in { from { transform:translateX(30px); opacity:0; filter:blur(3px); }
        to { transform:none; opacity:1; filter:none; } }
      @media (prefers-reduced-motion: reduce) { .drawer { animation:none; } .lens { transition:none; } }
      .drawer-head { display:flex; align-items:start; justify-content:space-between; gap:14px;
        padding-bottom:18px; border-bottom:1px solid var(--line); }
      .close { border:0; background:#edf0ef; border-radius:9px; width:34px; height:34px; font-size:1.15rem; }
      .close:hover { background:#e0e4e3; }
      .drawer section { padding:20px 0; border-bottom:1px solid var(--line); }
      .drawer section:last-child { border-bottom:0; }
      .drawer h3 { margin-bottom:7px; }
      .drawer p { color:var(--ink); }
      .evidence-list { list-style:none; padding:0; margin:9px 0 0; display:grid; gap:7px; }
      .evidence-list li { padding:9px 11px; background:#f2f5f4; border-radius:8px;
        font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:.83rem; overflow-wrap:anywhere; }

      .report { max-width:76ch; padding-top:26px; }
      .report h1, .report h2, .report h3 { margin:26px 0 9px; letter-spacing:-.02em; }
      .report h1 { font-size:1.7rem; } .report h2 { font-size:1.3rem; } .report h3 { font-size:1.05rem; }
      .report p { margin:11px 0; } .report ul, .report ol { margin:11px 0; padding-left:22px; }
      .report code { background:#edf0ef; border-radius:4px; padding:1px 5px;
        font:.88em ui-monospace, SFMono-Regular, Menlo, monospace; }
      .report pre { background:var(--navy); color:#e5edf9; border-radius:10px; padding:14px; overflow:auto; }
      .report pre code { background:none; padding:0; color:inherit; }
      .report table { border-collapse:collapse; width:100%; margin:14px 0; font-size:.9rem; }
      .report th, .report td { border:1px solid var(--line); padding:7px 9px; text-align:left; vertical-align:top; }
      .report th { background:#f1f3f2; }
      .report blockquote { margin:12px 0; padding:2px 0 2px 15px; border-left:1px solid var(--line); color:var(--muted); }

      .notice { padding:14px 16px; border-radius:10px; background:#fff8e9; color:#6b4300;
        border:1px solid #f0dcb4; margin-top:20px; font-size:.88rem; }

      @media (max-width:860px) {
        .hero { grid-template-columns:1fr; gap:26px; padding:34px 0 26px; }
        .matrix { grid-template-columns:repeat(4,1fr); }
      }
      @media (max-width:620px) {
        .shell { padding:20px 17px 70px; }
        .site-head { flex-wrap:wrap; }
        .nav { order:3; width:100%; margin-left:0; }
        .matrix { grid-template-columns:repeat(2,1fr); }
        .risk-strip { grid-template-columns:1fr; gap:0; }
        .risk-strip div { padding:12px 0; }
        .row { grid-template-columns:30px minmax(0,1fr); }
        .row-open { display:none; }
        .drawer { padding:20px; }
      }
    </style>
  </head>
  <body>
    <a class="skip" href="#app">Skip to content</a>
    <main id="app" aria-live="polite" tabindex="-1"></main>
    <script>
      const app = document.getElementById('app');
      let snapshot = null;

      const ROUTES = ['overview', 'findings', 'evidence', 'report'];
      const SEVERITY_ORDER = ['P0', 'P1', 'P2', 'P3'];
      const STATE_LABEL = { CONFIRMED: 'Confirmed', NOT_FOUND: 'Not found in scope', UNVERIFIED: 'Unverified' };
      const DECISION_LABEL = { HOLD: 'Hold — do not deploy', FIX_THEN_SHIP: 'Fix, then ship', SHIP: 'Ship' };
      // A control's state answers "does this codebase have one", which is a
      // different question from a finding's evidence state. Different words,
      // so the two are never read as the same thing.
      const CONTROL_LABEL = { CONFIRMED: 'Found', NOT_FOUND: 'Missing', UNVERIFIED: 'Not visible from here' };

      function controlNote(control) {
        if (control.state === 'CONFIRMED') {
          const where = control.paths && control.paths.length ? ` — ${control.paths.join(', ')}` : '';
          return `${control.hits} place${control.hits === 1 ? '' : 's'} in this codebase${where}`;
        }
        if (control.state === 'UNVERIFIED') {
          return control.note || 'This normally lives outside the repository, so nothing here proves it either way.';
        }
        return control.note || 'Searched for and not found anywhere in the reviewed code.';
      }

      function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, c => ({
          '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        })[c]);
      }

      function params() { return new URLSearchParams(location.search); }

      function navigate(next) {
        const url = new URL(location);
        Object.entries(next).forEach(([key, value]) =>
          value == null ? url.searchParams.delete(key) : url.searchParams.set(key, value));
        history.pushState({}, '', url);
        render();
      }

      function route() {
        const value = params().get('view') || 'overview';
        return ROUTES.includes(value) ? value : 'overview';
      }

      function lowerKey(value) { return String(value || '').toLowerCase(); }

      function head() {
        const running = snapshot.status === 'running';
        const current = route();
        const tabs = [['overview', 'Overview'], ['findings', 'Findings'], ['evidence', 'Evidence'], ['report', 'Report']];
        return `<header class="site-head">
          <div class="brand"><span class="mark" aria-hidden="true"></span> prod-readiness</div>
          <nav class="nav" aria-label="Dashboard views">${tabs.map(([id, label]) =>
            `<button type="button" data-view="${id}" ${current === id ? 'aria-current="page"' : ''}>${label}</button>`).join('')}</nav>
          <span class="live ${running ? '' : 'done'}">${running ? 'Audit running' : 'Audit complete'}</span>
        </header>`;
      }

      function heroCopy() {
        const { verdict, counts, status } = snapshot;
        if (verdict.decision || verdict.headline) {
          return {
            chip: DECISION_LABEL[verdict.decision] || 'Verdict recorded',
            chipClass: lowerKey(verdict.decision) || 'pending',
            title: verdict.headline || DECISION_LABEL[verdict.decision],
            lede: verdict.summary,
          };
        }
        if (status === 'running') {
          return {
            chip: 'Audit in progress', chipClass: 'pending',
            title: counts.p0 ? `${counts.p0} blocker${counts.p0 === 1 ? '' : 's'} found so far.`
                             : 'The audit is still building its case.',
            lede: 'Counts update as each specialist finishes. Nothing is needed from you yet.',
          };
        }
        return {
          chip: 'No verdict yet', chipClass: 'pending',
          title: 'The audit has not written its verdict.',
          lede: 'Findings below are complete, but the go/no-go call has not been recorded.',
        };
      }

      function hero() {
        const { chip, chipClass, title, lede } = heroCopy();
        const c = snapshot.counts;
        return `<section class="hero">
          <div>
            <span class="decision ${escapeHtml(chipClass)}">${escapeHtml(chip)}</span>
            <h1>${escapeHtml(title)}</h1>
            ${lede ? `<p class="lede">${escapeHtml(lede)}</p>` : ''}
          </div>
          <div class="risk-strip">
            <div class="r0"><strong class="num">${c.p0}</strong><span>block the release</span></div>
            <div class="r1"><strong class="num">${c.p1}</strong><span>serious risks</span></div>
            <div><strong class="num">${c.unverified}</strong><span>could not be checked</span></div>
          </div>
        </section>`;
      }

      function lensMatrix() {
        const cells = snapshot.lenses.map(lens => {
          const worst = lens.counts.p0 ? `${lens.counts.p0} blocking`
            : lens.counts.p1 ? `${lens.counts.p1} to fix`
            : lens.counts.total ? `${lens.counts.total} noted`
            : { complete: 'Nothing found', skipped: 'Not applicable', running: 'Reviewing now' }[lens.status] || 'Waiting';
          return `<button class="lens ${escapeHtml(lens.status)}" type="button" data-lens="${escapeHtml(lens.id)}">
            <span class="dot ${escapeHtml(lens.status)}" aria-hidden="true"></span>
            <b>${escapeHtml(lens.label)}</b>
            <em>${escapeHtml(worst)}</em>
          </button>`;
        }).join('');
        return `<section class="band"><div class="band-head"><h2>What was reviewed</h2>
          <p>Seven specialists, each writing only its own findings.</p></div>
          <div class="matrix">${cells}</div></section>`;
      }

      function findingRow(finding) {
        const meta = [finding.lensLabel, STATE_LABEL[finding.state] || finding.state];
        return `<button class="row" type="button" data-finding="${escapeHtml(finding.id)}">
          <span class="sev ${lowerKey(finding.severity)}">${escapeHtml(finding.severity)}</span>
          <span>
            <span class="row-title">${escapeHtml(finding.title)}</span>
            ${finding.impact ? `<span class="row-impact">${escapeHtml(finding.impact)}</span>` : ''}
            <span class="row-meta">${escapeHtml(meta[0])} · <span class="state ${lowerKey(finding.state)}">${escapeHtml(meta[1])}</span></span>
          </span>
          <span class="row-open">Details →</span>
        </button>`;
      }

      function topFindings() {
        const top = snapshot.findings.filter(f => f.severity === 'P0' || f.severity === 'P1').slice(0, 5);
        if (!top.length) {
          return `<section class="band"><div class="band-head"><h2>What needs attention</h2></div>
            <p class="empty">No blocking or serious findings have been written yet.</p></section>`;
        }
        return `<section class="band"><div class="band-head"><h2>What needs attention first</h2>
          <p>Ordered by severity. Open one to see the cause and the evidence.</p></div>
          <div class="rows">${top.map(findingRow).join('')}</div></section>`;
      }

      function overview() {
        const errors = snapshot.errors || [];
        const notice = errors.length
          ? `<div class="notice">${escapeHtml(errors[0])}${errors.length > 1 ? ` (and ${errors.length - 1} more)` : ''}</div>`
          : '';
        return `${hero()}${notice}${topFindings()}${lensMatrix()}`;
      }

      function findingsView() {
        const filter = params().get('severity') || 'all';
        const lensFilter = params().get('lens');
        let list = snapshot.findings;
        if (filter !== 'all') list = list.filter(f => f.severity === filter);
        if (lensFilter) list = list.filter(f => f.lens === lensFilter);

        const available = ['all', ...SEVERITY_ORDER.filter(s => snapshot.findings.some(f => f.severity === s))];
        const chips = available.map(value => {
          const count = value === 'all' ? snapshot.findings.length : snapshot.findings.filter(f => f.severity === value).length;
          const label = value === 'all' ? 'Everything' : value;
          return `<button class="filter" type="button" data-severity="${value}" aria-pressed="${filter === value}">${label} · ${count}</button>`;
        }).join('');

        const lensChip = lensFilter
          ? `<button class="filter" type="button" data-clear-lens aria-pressed="true">Only ${escapeHtml(
              (snapshot.lenses.find(l => l.id === lensFilter) || {}).label || lensFilter)} ×</button>`
          : '';

        return `<section class="band" style="border-top:0; padding-top:44px">
          <div class="band-head"><h2>Every finding</h2>
          <p>${snapshot.findings.length} recorded across ${snapshot.lenses.filter(l => l.counts.total).length} lenses.</p></div>
          <div class="filters">${chips}${lensChip}</div>
          ${list.length ? `<div class="rows">${list.map(findingRow).join('')}</div>`
            : '<p class="empty">Nothing matches this filter.</p>'}
        </section>`;
      }

      function evidenceView() {
        const controls = snapshot.evidence || [];
        if (!controls.length) {
          return `<section class="band" style="border-top:0; padding-top:44px">
            <div class="band-head"><h2>What was searched for</h2></div>
            <p class="empty">The evidence ledger has not been written yet.</p></section>`;
        }
        const filter = params().get('control') || 'all';
        const states = ['all', 'CONFIRMED', 'NOT_FOUND', 'UNVERIFIED'];
        const shown = filter === 'all' ? controls : controls.filter(c => c.state === filter);
        const chips = states.map(value => {
          const count = value === 'all' ? controls.length : controls.filter(c => c.state === value).length;
          const label = value === 'all' ? 'Everything' : CONTROL_LABEL[value];
          return `<button class="filter" type="button" data-control="${value}" aria-pressed="${filter === value}">${escapeHtml(label)} · ${count}</button>`;
        }).join('');
        return `<section class="band" style="border-top:0; padding-top:44px">
          <div class="band-head"><h2>What the audit looked for</h2>
          <p>Every control it searched for, and whether this codebase has one.</p></div>
          <div class="filters">${chips}</div>
          <div class="panel" style="margin-top:18px">${shown.map(control => `
            <article class="ledger-item">
              <span class="chip ${lowerKey(control.state)}">${escapeHtml(CONTROL_LABEL[control.state])}</span>
              <strong>${escapeHtml(control.label)}</strong>
              <p>${escapeHtml(controlNote(control))}</p>
            </article>`).join('') || '<p class="empty">Nothing matches this filter.</p>'}</div>
        </section>`;
      }

      function reportView() {
        const { verdict, counts, findings, lenses } = snapshot;
        const groups = [
          ['Blocks the release', 'P0', 'Nothing blocks the release.'],
          ['Serious risks', 'P1', 'No serious risks were recorded.'],
          ['Worth cleaning up', 'P2', 'Nothing recorded.'],
          ['Minor', 'P3', 'Nothing recorded.'],
        ].filter(([, severity]) => findings.some(f => f.severity === severity));

        const skipped = lenses.filter(l => l.status === 'skipped');
        const unverified = findings.filter(f => f.state === 'UNVERIFIED');

        return `<article class="report">
          <h1 style="margin-top:0">Production readiness report</h1>
          <p class="muted">${counts.total} findings · ${counts.p0} blocking · ${counts.p1} serious · ${counts.unverified} unverified${
            snapshot.gitRef ? ` · <span class="mono">${escapeHtml(snapshot.gitRef)}</span>` : ''}</p>

          <h2>The call</h2>
          ${verdict.decision || verdict.headline
            ? `<p><span class="decision ${lowerKey(verdict.decision) || 'pending'}">${escapeHtml(
                DECISION_LABEL[verdict.decision] || 'Verdict recorded')}</span></p>
               ${verdict.headline ? `<p>${escapeHtml(verdict.headline)}</p>` : ''}
               ${verdict.summary ? `<p>${escapeHtml(verdict.summary)}</p>` : ''}`
            : '<p class="muted">No verdict has been recorded yet.</p>'}

          ${groups.map(([title, severity, empty]) => {
            const group = findings.filter(f => f.severity === severity);
            return `<h2>${title}</h2>${group.length
              ? `<div class="rows">${group.map(findingRow).join('')}</div>`
              : `<p class="muted">${empty}</p>`}`;
          }).join('')}

          <h2>What could not be checked</h2>
          ${unverified.length
            ? `<p>${unverified.length} finding${unverified.length === 1 ? '' : 's'} could not be settled from the code alone — they depend on a cloud console, a CI pipeline, or infrastructure outside this repository.</p>
               <div class="rows">${unverified.map(findingRow).join('')}</div>`
            : '<p class="muted">Everything the audit raised was settled from evidence in this repository.</p>'}

          ${skipped.length ? `<h2>Not reviewed</h2><ul>${skipped.map(lens =>
            `<li><strong>${escapeHtml(lens.label)}</strong> — ${escapeHtml(lens.skippedReason || 'skipped')}</li>`).join('')}</ul>` : ''}
        </article>`;
      }

      function drawer() {
        const id = params().get('finding');
        const lensId = params().get('open');
        if (id) {
          const finding = snapshot.findings.find(f => f.id === id);
          if (!finding) return '';
          const sections = [
            ['What this costs you', finding.impact],
            ['Why it happens', finding.failure_path],
            ['What already protects you', finding.compensating],
            ['What would settle it', finding.resolve],
            ['How to fix it', finding.fix],
          ].filter(([, value]) => value);
          const evidence = finding.evidence && finding.evidence.length
            ? `<section><h3>Where we saw it</h3><ul class="evidence-list">${
                finding.evidence.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul></section>`
            : '';
          return `<div class="scrim" data-close></div>
            <div class="drawer" role="dialog" aria-modal="true" tabindex="-1" aria-label="${escapeHtml(finding.title)}">
              <header class="drawer-head">
                <div>
                  <span class="sev ${lowerKey(finding.severity)}" style="display:inline-grid">${escapeHtml(finding.severity)}</span>
                  <h2 style="margin-top:11px">${escapeHtml(finding.title)}</h2>
                  <p class="muted" style="margin-top:6px; font-size:.86rem">${escapeHtml(finding.lensLabel)} · <span class="state ${lowerKey(finding.state)}">${escapeHtml(STATE_LABEL[finding.state] || finding.state)}</span> · <span class="mono">${escapeHtml(finding.id)}</span></p>
                </div>
                <button class="close" type="button" data-close aria-label="Close">×</button>
              </header>
              ${sections.map(([title, body]) => `<section><h3>${title}</h3><p>${escapeHtml(body)}</p></section>`).join('')}
              ${evidence}
            </div>`;
        }
        if (lensId) {
          const lens = snapshot.lenses.find(l => l.id === lensId);
          if (!lens) return '';
          const found = snapshot.findings.filter(f => f.lens === lensId);
          return `<div class="scrim" data-close></div>
            <div class="drawer" role="dialog" aria-modal="true" tabindex="-1" aria-label="${escapeHtml(lens.label)}">
              <header class="drawer-head">
                <div><h2>${escapeHtml(lens.label)}</h2>
                <p class="muted" style="margin-top:6px; font-size:.86rem">${escapeHtml(
                  lens.skippedReason || `${found.length} finding${found.length === 1 ? '' : 's'} · ${lens.status}`)}</p></div>
                <button class="close" type="button" data-close aria-label="Close">×</button>
              </header>
              <section>${found.length
                ? `<div class="rows">${found.map(findingRow).join('')}</div>`
                : '<p class="empty">This lens has not written any findings.</p>'}</section>
            </div>`;
        }
        return '';
      }

      let returnFocusTo = null;

      function render() {
        if (!snapshot) return;
        const view = route();
        const body = view === 'findings' ? findingsView()
          : view === 'evidence' ? evidenceView()
          : view === 'report' ? reportView()
          : overview();
        const wasOpen = Boolean(document.querySelector('.drawer'));
        // Read the opener before the DOM is replaced - afterwards the element
        // that had focus no longer exists and activeElement is the body.
        const opener = document.activeElement;
        const openerKey = opener && opener.dataset
          ? (opener.dataset.finding ? `[data-finding="${opener.dataset.finding}"]`
            : opener.dataset.lens ? `[data-lens="${opener.dataset.lens}"]` : null)
          : null;
        app.innerHTML = `<div class="shell">${head()}${body}</div>${drawer()}`;

        // A dialog that never takes focus is a dialog only to sighted mouse
        // users. Move into it on open, and hand focus back on close. Every
        // render replaces the DOM, so the return target is remembered as a
        // selector rather than as the element that opened the dialog.
        const panel = document.querySelector('.drawer');
        if (panel && !wasOpen) {
          returnFocusTo = openerKey;
          panel.focus();
        } else if (!panel && wasOpen) {
          const target = returnFocusTo && document.querySelector(returnFocusTo);
          (target || app).focus();
          returnFocusTo = null;
        }
      }

      app.addEventListener('click', event => {
        const view = event.target.closest('[data-view]');
        if (view) return navigate({ view: view.dataset.view, finding: null, open: null });
        const finding = event.target.closest('[data-finding]');
        if (finding) return navigate({ finding: finding.dataset.finding, open: null });
        const lens = event.target.closest('[data-lens]');
        if (lens) return navigate({ open: lens.dataset.lens, finding: null });
        const severity = event.target.closest('[data-severity]');
        if (severity) return navigate({ severity: severity.dataset.severity });
        const control = event.target.closest('[data-control]');
        if (control) return navigate({ control: control.dataset.control });
        if (event.target.closest('[data-clear-lens]')) return navigate({ lens: null });
      });

      document.addEventListener('click', event => {
        if (event.target.closest('[data-close]')) navigate({ finding: null, open: null });
      });

      window.addEventListener('keydown', event => {
        if (event.key === 'Escape' && (params().get('finding') || params().get('open'))) {
          navigate({ finding: null, open: null });
        }
      });

      window.addEventListener('popstate', render);

      async function refresh() {
        try {
          const response = await fetch('/api/snapshot', { cache: 'no-store' });
          if (!response.ok) throw new Error(`Snapshot request failed (${response.status})`);
          snapshot = await response.json();
          render();
          if (snapshot.status === 'running') setTimeout(refresh, 2000);
        } catch (error) {
          app.innerHTML = `<div class="shell"><section class="band" style="border-top:0">
            <h1>Production readiness</h1>
            <p class="lede">${escapeHtml(error.message || 'The snapshot could not be loaded.')}</p>
          </section></div>`;
        }
      }

      refresh();
    </script>
  </body>
</html>
"""


def read_text_if_present(path: Path) -> str | None:
    """Return a UTF-8 file's text, or ``None`` when it cannot be read."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def load_evidence(audit_root: Path) -> list[dict]:
    """Flatten the absence ledger into the rows the Evidence view shows.

    The ledger is already structured, so this is a projection - a control's
    label, the state its hit count supports, and how many matches it found.
    """
    text = read_text_if_present(audit_root / "evidence" / "absence-ledger.json")
    if text is None:
        return []
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
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
        paths = []
        for hit in (row.get("hits") or [])[:3]:
            if isinstance(hit, dict) and hit.get("path"):
                paths.append(hit["path"])
        rows.append({
            "id": control_id,
            "label": row.get("label") or control_id,
            "lens": row.get("lens"),
            "state": state,
            "hits": hits,
            "paths": paths,
            "note": row.get("note"),
        })
    return rows


def build_snapshot(project_root: Path) -> dict:
    """The dashboard's whole payload, assembled from structured audit data."""
    project_root = Path(project_root)
    audit_root = (project_root / ".readiness-audit").resolve()

    if not audit_root.is_dir():
        return {
            "status": "unavailable",
            "message": "No audit has been run in this project yet.",
            "counts": {"total": 0, "p0": 0, "p1": 0, "p2": 0, "p3": 0,
                       "confirmed": 0, "notFound": 0, "unverified": 0},
            "verdict": {"decision": None, "headline": None, "summary": None},
            "lenses": [{"id": lens, "label": LENS_LABEL[lens], "status": "waiting",
                        "skippedReason": None,
                        "counts": {"total": 0, "p0": 0, "p1": 0, "p2": 0, "p3": 0,
                                   "confirmed": 0, "notFound": 0, "unverified": 0}}
                       for lens in LENS_ORDER],
            "findings": [],
            "evidence": [],
            "gitRef": None,
            "errors": [],
        }

    report = build_report(project_root)
    stage_status = (report.get("stage") or {}).get("status")
    report["status"] = "complete" if stage_status == "complete" else "running"
    report["message"] = ("Audit complete." if report["status"] == "complete"
                         else "Audit is still running.")
    report["evidence"] = load_evidence(audit_root)
    report["auditRoot"] = str(audit_root)
    return report


class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, project_root: Path, port: int):
        self.project_root = project_root
        super().__init__(("127.0.0.1", port), DashboardRequestHandler)


class DashboardRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # The dashboard keeps its view, filters, and open finding in the query
        # string, so every route arrives here as "/?view=..." and must still
        # serve the app shell.
        path = urlsplit(self.path).path
        if path == "/":
            return self.respond(HTTPStatus.OK, "text/html; charset=utf-8", DASHBOARD_HTML.encode())
        if path == "/api/snapshot":
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve a read-only production-readiness dashboard.")
    parser.add_argument("project_root", type=Path, help="Target project root containing .readiness-audit")
    parser.add_argument("--port", type=int, default=0, help="Port to bind on 127.0.0.1 (default: ephemeral)")
    args = parser.parse_args(argv)
    serve(args.project_root, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
