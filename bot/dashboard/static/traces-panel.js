// Traces panel (roadmap P0): the native agent's runs, newest first, with filters and paging, and for one run
// a timeline of its model calls, tool calls, approvals and errors with durations and token totals, linked to
// its parent and sub-agent runs. Read-only. A trace records the shape of a run (tool, status, a short redacted
// target), never prompts, replies or tool output. This file is identical in the dashboard (bot/dashboard/static/)
// and the desktop app (desktop-app/ui/); tests/test_traces_panel.py fails if the two differ. It uses the page's
// own api() and esc(), and draws into #tr-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const errText = (e) => { if (window.ABP && ABP.unitUnavailable && ABP.unitUnavailable(e)) return 'This module is not loaded.'; let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const PAGE = 25;
  const num = (n) => (n == null ? '-' : Number(n).toLocaleString());
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : '-');   // epoch seconds, browser time zone
  const dur = (ms) => (ms == null ? '-' : ms < 1000 ? Math.round(ms) + ' ms' : (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + ' s');
  const RANGES = { '': 0, '1h': 3600, '24h': 86400, '7d': 604800 };
  let f = { q: '', status: '', instance: '', range: '' };
  let offset = 0;
  let list = null;
  let detail = null;      // the open run, or null for the list
  let loading = false;
  let err = '';

  function css() {
    if ($('tr-css')) return;
    const s = document.createElement('style');
    s.id = 'tr-css';
    s.textContent = `.tr-bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0 10px}
.tr-bar input,.tr-bar select{max-width:180px}
.tr-note{color:var(--muted);font-size:12px}
.tr-scroll{overflow-x:auto}
.tr-table{width:100%;border-collapse:collapse;font-size:12.5px}
.tr-table th{text-align:left;color:var(--muted);font-weight:600;padding:6px;border-bottom:1px solid var(--line);white-space:nowrap}
.tr-table td{padding:6px;border-bottom:1px solid var(--line);vertical-align:top}
.tr-table td.n{text-align:right;font-variant-numeric:tabular-nums}
.tr-table tr.go{cursor:pointer}
.tr-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.tr-chip.bad{background:var(--critical-soft);color:var(--critical)}
.tr-chip.run{background:var(--surface-2);color:var(--warning)}
.tr-link{background:none;border:0;padding:0;color:var(--accent,#4a7);cursor:pointer;font:inherit;text-decoration:underline}
.tr-tot{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.tr-tot div{background:var(--surface-2);border-radius:8px;padding:6px 10px;min-width:90px}
.tr-tot b{display:block;font-size:15px}
.tr-ev{display:grid;grid-template-columns:78px 120px 1fr;gap:8px;padding:5px 0;border-bottom:1px solid var(--line);font-size:12.5px}
.tr-ev .off{color:var(--muted);font-variant-numeric:tabular-nums;text-align:right}
.tr-mono{font-family:ui-monospace,monospace;word-break:break-all}
.tr-empty{color:var(--muted);text-align:center;padding:18px}`;
    document.head.appendChild(s);
  }

  const chip = (st) => `<span class="tr-chip ${st === 'ok' ? '' : st === 'running' ? 'run' : 'bad'}">${E(st)}</span>`;
  const short = (id) => String(id || '').slice(0, 8);

  function link(id, label) {
    return `<button class="tr-link" data-tr="open" data-run="${E(id)}">${E(label || short(id))}</button>`;
  }

  function renderList() {
    const d = list || { runs: [], total: 0, statuses: [] };
    const statuses = Array.from(new Set(['ok', 'failed', 'cancelled', 'running'].concat(d.statuses || [])));
    const body = d.runs.length
      ? d.runs.map((r) => `<tr class="go" data-tr="open" data-run="${E(r.run_id)}">
        <td>${E(when(r.started))}</td><td>${chip(r.status)}</td>
        <td><b>${E(r.agent || '-')}</b><br><span class="tr-note">${E(r.model || '')}${r.instance_id != null ? ' · bot ' + E(r.instance_id) : ''}${r.parent_run ? ' · sub-agent' : ''}</span></td>
        <td class="n">${E(dur(r.duration_ms))}</td><td class="n">${num(r.llm_calls)}</td><td class="n">${num(r.tool_calls)}${r.failed_tools ? ` <span class="tr-chip bad">${num(r.failed_tools)} failed</span>` : ''}${r.denied_tools ? ` <span class="tr-chip bad">${num(r.denied_tools)} denied</span>` : ''}</td>
        <td class="n">${num(r.tokens)}</td><td class="tr-mono">${E(short(r.run_id))}</td></tr>`).join('')
      : `<tr><td colspan="8" class="tr-empty">${list ? 'No runs match. Runs appear here as soon as a native agent answers a message or runs a routine.' : 'Loading…'}</td></tr>`;
    const from = d.total ? offset + 1 : 0;
    const to = Math.min(d.total, offset + (d.runs || []).length);
    return `<div class="tr-bar">
        <input id="tr-q" type="search" placeholder="Search runs, tools, targets" aria-label="Search" value="${E(f.q)}">
        <select id="tr-status" aria-label="Status"><option value="">Any status</option>${statuses.map((s) => `<option value="${E(s)}"${f.status === s ? ' selected' : ''}>${E(s)}</option>`).join('')}</select>
        <input id="tr-inst" type="number" min="0" placeholder="Bot instance" aria-label="Bot instance" value="${E(f.instance)}" style="width:110px">
        <select id="tr-range" aria-label="Time range">${[['', 'Any time'], ['1h', 'Last hour'], ['24h', 'Last 24 hours'], ['7d', 'Last 7 days']].map(([v, l]) => `<option value="${v}"${f.range === v ? ' selected' : ''}>${l}</option>`).join('')}</select>
        <button class="btn" data-tr="apply">Apply</button>
        <button class="btn" data-tr="refresh">Refresh</button></div>
      <div class="tr-scroll"><table class="tr-table"><thead><tr><th>Started</th><th>Status</th><th>Agent</th><th>Duration</th><th>Model calls</th><th>Tool calls</th><th>Tokens</th><th>Run</th></tr></thead>
      <tbody>${body}</tbody></table></div>
      <div class="tr-bar"><button class="btn" data-tr="prev"${offset <= 0 ? ' disabled' : ''}>Previous</button>
        <button class="btn" data-tr="next"${offset + PAGE >= d.total ? ' disabled' : ''}>Next</button>
        <span class="tr-note">${from}-${to} of ${num(d.total)} · times in your local time zone${d.capped ? ' · only the newest runs are searched; narrow the time range for older ones' : ''}</span></div>`;
  }

  function evLine(e) {
    const d = e.data || {};
    let kind = e.kind, text = '';
    if (e.kind === 'agent.run.start') { kind = 'Run started'; text = `${E(d.agent || '')} · ${E(d.model || '')}${d.transport ? ' · ' + E(d.transport) : ''}${d.tools != null ? ' · ' + num(d.tools) + ' tools' : ''}`; }
    else if (e.kind === 'agent.run.end') { kind = 'Run ended'; text = `${chip(d.status || 'ok')} ${E(dur(d.duration_ms))} · ${num(d.iterations)} model calls · ${num(d.tokens)} tokens${d.error ? ' · ' + E(d.error) : ''}`; }
    else if (e.kind === 'llm.call') { kind = 'Model call ' + (d.iteration != null ? '#' + d.iteration : ''); text = `${E(d.model || '')} · ${E(dur(d.duration_ms))} · ${num(d.tokens)} tokens${d.tool_calls ? ' · ' + num(d.tool_calls) + ' tool calls requested' : ''}${d.cache_read ? ' · ' + num(d.cache_read) + ' cached' : ''}${d.streamed ? ' · streamed' : ''}`; }
    else if (e.kind === 'tool.call') { kind = 'Tool call'; text = `<b>${E(d.tool || '')}</b> ${chip(d.status || '?')} ${E(dur(d.duration_ms))}${d.approval && d.approval !== 'none' ? ' · approval ' + E(d.approval) : ''}${d.target ? `<br><span class="tr-mono">${E(d.target)}</span>` : ''}<br><span class="tr-note">${num(d.arg_chars)} chars in · ${num(d.output_chars)} chars out${d.read_only === 1 ? ' · read-only' : ''}</span>${d.error ? `<br>${E(d.error)}` : ''}`; }
    else if (e.kind === 'approval') { kind = 'Approval'; text = `<b>${E(d.tool || '')}</b> ${chip(d.decision || '?')}${d.reason ? ' · ' + E(d.reason) : ''}`; }
    else if (e.kind === 'compaction') { kind = 'Context compacted'; text = `${num(d.messages)} messages · ${num(d.before_chars)} characters before`; }
    else if (e.kind === 'note') { kind = 'Note'; text = `${d.level && d.level !== 'info' ? chip(d.level) + ' ' : ''}${E(d.message || '')}`; }
    else text = E(JSON.stringify(d));
    return `<div class="tr-ev"><span class="off">+${E(dur(e.offset_ms))}</span><span>${E(kind)}</span><span>${text}</span></div>`;
  }

  function renderDetail() {
    const d = detail, r = d.run, t = d.totals;
    const tot = [['Status', chip(r.status)], ['Duration', E(dur(r.duration_ms))], ['Tokens', num(t.tokens)], ['Model calls', `${num(t.llm_calls)} · ${E(dur(t.llm_ms))}`],
      ['Tool calls', `${num(t.tool_calls)} · ${E(dur(t.tool_ms))}`], ['Failed / denied', `${num(t.failed_tools)} / ${num(t.denied_tools)}`], ['Approvals', num(t.approvals)]]
      .map(([k, v]) => `<div><span class="tr-note">${k}</span><b>${v}</b></div>`).join('');
    const kids = (d.children || []).length
      ? `<p class="tr-note">Sub-agent runs started by this run:</p><div class="tr-scroll"><table class="tr-table"><tbody>${d.children.map((c) => `<tr><td>${link(c.run_id)}</td><td>${chip(c.status)}</td><td>${E(c.agent || '')} · ${E(c.model || '')}</td><td class="n">${E(dur(c.duration_ms))}</td><td class="n">${num(c.tokens)} tokens</td></tr>`).join('')}</tbody></table></div>` : '';
    return `<div class="tr-bar"><button class="btn" data-tr="back">Back to runs</button><button class="btn" data-tr="refresh">Refresh</button>
        <span class="tr-note">Run <span class="tr-mono">${E(r.run_id)}</span> · started ${E(when(r.started))}${r.instance_id != null ? ' · bot ' + E(r.instance_id) : ''}${r.session ? ' · session ' + E(r.session) : ''}</span></div>
      ${d.parent ? `<p class="tr-note">Started by run ${link(d.parent.run_id)} (${E(d.parent.agent || '')}).</p>` : ''}
      ${r.error ? `<p class="cardnote">Error: ${E(r.error)}</p>` : ''}
      <div class="tr-tot">${tot}</div>${kids}
      <h3>Timeline</h3><div>${d.events.map(evLine).join('') || '<p class="tr-empty">No events.</p>'}</div>`;
  }

  function render() {
    const root = $('tr-root');
    if (!root) return;
    const keep = document.activeElement && document.activeElement.id;
    root.innerHTML = err ? `<p class="cardnote">Traces unavailable: ${E(err)}</p>` + (detail ? renderDetail() : renderList()) : detail ? renderDetail() : renderList();
    if (keep && $(keep)) { try { $(keep).focus(); } catch (_) {} }
  }

  function query() {
    const p = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    if (f.q) p.set('q', f.q);
    if (f.status) p.set('status', f.status);
    if (f.instance !== '') p.set('instance_id', f.instance);
    if (RANGES[f.range]) p.set('since', String(Math.floor(Date.now() / 1000) - RANGES[f.range]));
    return p.toString();
  }

  async function load() {
    if (!$('tr-root') || loading) return;
    loading = true;
    try {
      if (detail) detail = await api('/api/agent/traces/' + encodeURIComponent(detail.run.run_id));
      else list = await api('/api/agent/traces?' + query());
      err = '';
    } catch (e) {
      err = errText(e);
    } finally {
      loading = false;
      render();
    }
  }

  async function open(id) {
    detail = { run: { run_id: id }, totals: {}, events: [] };
    loading = false;
    try {
      detail = await api('/api/agent/traces/' + encodeURIComponent(id));
      err = '';
    } catch (e) {
      detail = null;
      err = errText(e);
    }
    render();
    const root = $('tr-root'); if (root && root.scrollIntoView) root.scrollIntoView();
  }

  function readFilters() {
    f = { q: ($('tr-q') || {}).value || '', status: ($('tr-status') || {}).value || '', instance: ($('tr-inst') || {}).value || '', range: ($('tr-range') || {}).value || '' };
    offset = 0;
  }

  function init() {
    const root = $('tr-root');
    if (!root) return;
    css();
    render();
    root.addEventListener('click', (ev) => {
      const el = ev.target.closest('[data-tr]');
      if (!el) return;
      const a = el.dataset.tr;
      if (a === 'open') open(el.dataset.run);
      else if (a === 'back') { detail = null; render(); load(); }
      else if (a === 'apply') { readFilters(); load(); }
      else if (a === 'prev') { offset = Math.max(0, offset - PAGE); load(); }
      else if (a === 'next') { offset += PAGE; load(); }
      else load();
    });
    root.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' && ev.target.closest('.tr-bar input')) { readFilters(); load(); } });
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'traces') load(); };
    window.addEventListener('hashchange', onHash);
    onHash();
    if ('IntersectionObserver' in window) {
      new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting && !list) load(); })).observe(root);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
