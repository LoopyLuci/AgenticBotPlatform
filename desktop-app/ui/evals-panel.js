// Evals panel (roadmap: Evals): the agent eval harness's committed and local JSON reports - one row per run with
// model, date, pass rate, model calls, tokens and wall time, the failing tasks of a run, and prompt-variant
// comparisons. Read-only. This file is identical in the dashboard (bot/dashboard/static/) and the desktop app
// (desktop-app/ui/); tests/test_eval_reports_page.py fails if the two differ. It uses the page's own api() and
// esc(), and draws into #ev-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const errText = (e) => { if (window.ABP && ABP.unitUnavailable && ABP.unitUnavailable(e)) return 'This module is not loaded.'; let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const num = (n) => (n == null ? '-' : Number(n).toLocaleString());
  const day = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString() : '-');
  const wall = (ms) => { const s = (ms || 0) / 1000; return s >= 60 ? Math.floor(s / 60) + ' m ' + String(Math.floor(s % 60)).padStart(2, '0') + ' s' : s.toFixed(1) + ' s'; };
  let list = null;
  let detail = null;
  let err = '';

  function css() {
    if ($('ev-css')) return;
    const s = document.createElement('style');
    s.id = 'ev-css';
    s.textContent = `.ev-scroll{overflow-x:auto}
.ev-table{width:100%;border-collapse:collapse;font-size:12.5px}
.ev-table th{text-align:left;color:var(--muted);font-weight:600;padding:6px;border-bottom:1px solid var(--line);white-space:nowrap}
.ev-table td{padding:6px;border-bottom:1px solid var(--line);vertical-align:top}
.ev-table td.n{text-align:right;font-variant-numeric:tabular-nums}
.ev-table tr.go{cursor:pointer}
.ev-note{color:var(--muted);font-size:12px}
.ev-bad{color:var(--critical)}
.ev-empty{color:var(--muted);text-align:center;padding:18px}`;
    document.head.appendChild(s);
  }

  function renderList() {
    const rs = (list && list.reports) || [];
    const runs = rs.filter((r) => r.kind === 'run');
    const comps = rs.filter((r) => r.kind === 'compare');
    const rows = runs.length
      ? runs.map((r) => `<tr class="go" data-ev="open" data-name="${E(r.name)}"><td>${E(day(r.when))}</td><td><b>${E(r.model)}</b><br><span class="ev-note">${E(r.mode)} · ${E(r.name)}</span></td>
        <td class="n">${E(r.score)}%</td><td class="n">${num(r.passed)}/${num(r.measured)}</td><td class="n">${num(r.tasks_failed)}</td><td class="n">${num(r.model_calls)}</td><td class="n">${num(r.tokens)}</td><td class="n">${E(wall(r.duration_ms))}</td></tr>`).join('')
      : `<tr><td colspan="8" class="ev-empty">${list ? 'No run reports found.' : 'Loading…'}</td></tr>`;
    const cmp = comps.map((r) => `<tr class="go" data-ev="open" data-name="${E(r.name)}"><td><b>${E(r.model)}</b><br><span class="ev-note">${E(r.name)}</span></td><td>${(r.variants || []).map((v) => `${E(v.name)}: ${num(v.passed)}/${num(v.total)} (${E(v.verdict)})`).join('<br>')}</td></tr>`).join('');
    return `<p><button data-ev="refresh">Refresh</button></p>${err ? `<p class="ev-bad">${E(err)}</p>` : ''}
      <div class="ev-scroll"><table class="ev-table"><tr><th>Date</th><th>Model</th><th>Pass rate</th><th>Passed</th><th>Failed</th><th>Model calls</th><th>Tokens</th><th>Wall time</th></tr>${rows}</table></div>
      ${comps.length ? `<h3>Prompt comparisons</h3><div class="ev-scroll"><table class="ev-table"><tr><th>Report</th><th>Variants</th></tr>${cmp}</table></div>` : ''}`;
  }

  function renderDetail() {
    const d = detail;
    if (d.kind === 'compare') {
      return `<p><button data-ev="back">Back</button></p><h3>${E(d.model)} <span class="ev-note">${E(d.name)}</span></h3>${d.note ? `<p>${E(d.note)}</p>` : ''}
        <table class="ev-table"><tr><th>Variant</th><th>Passed</th><th>Score</th><th>Tokens</th><th>Gained</th><th>Lost</th><th>Verdict</th></tr>${(d.variants || []).map((v) => `<tr><td>${E(v.name)}</td><td>${num(v.passed)}/${num(v.total)}</td><td>${E(v.score)}%</td><td>${num(v.tokens)}</td><td>${E((v.gained || []).join(', '))}</td><td>${E((v.lost || []).join(', '))}</td><td>${E(v.verdict)}</td></tr>`).join('')}</table>`;
    }
    const cats = Object.keys(d.categories || {}).map((k) => `<tr><td>${E(k)}</td><td>${num(d.categories[k].passed)}/${num(d.categories[k].total)}</td></tr>`).join('');
    const failed = (d.failed || []).map((x) => `<tr><td>${E(x.id)}<br><span class="ev-note">${E(x.title)}</span></td><td>${E(x.category)}</td><td>${E(x.why || '')}</td></tr>`).join('');
    return `<p><button data-ev="back">Back</button></p><h3>${E(d.model)} <span class="ev-note">${E(day(d.when))} · ${E(d.name)}</span></h3>
      <p>${E(d.mode)} run: <b>${num(d.passed)}/${num(d.measured)}</b> tasks passed (${E(d.score)}%), ${num(d.model_calls)} model calls, ${num(d.tokens)} tokens, ${E(wall(d.duration_ms))}.</p>
      <table class="ev-table"><tr><th>Category</th><th>Passed</th></tr>${cats}</table>
      ${failed ? `<h3>Failed tasks</h3><div class="ev-scroll"><table class="ev-table"><tr><th>Task</th><th>Category</th><th>Why</th></tr>${failed}</table></div>` : '<p>No failed tasks.</p>'}`;
  }

  function render() {
    const root = $('ev-root');
    if (root) root.innerHTML = detail ? renderDetail() : renderList();
  }

  async function load() {
    try { list = await api('/api/agent/evals/reports'); err = ''; } catch (e) { err = errText(e); }
    render();
  }

  async function open(name) {
    try { detail = await api('/api/agent/evals/reports/' + encodeURIComponent(name)); err = ''; } catch (e) { detail = null; err = errText(e); }
    render();
  }

  function init() {
    const root = $('ev-root');
    if (!root) return;
    css();
    render();
    root.addEventListener('click', (ev) => {
      const el = ev.target.closest('[data-ev]');
      if (!el) return;
      const a = el.dataset.ev;
      if (a === 'open') open(el.dataset.name);
      else if (a === 'back') { detail = null; render(); } else load();
    });
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'evals') load(); };
    window.addEventListener('hashchange', onHash);
    onHash();
    if ('IntersectionObserver' in window) {
      new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting && !list) load(); })).observe(root);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
