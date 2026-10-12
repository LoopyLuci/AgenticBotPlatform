// Coordination panel: the work board that keeps ABP's agents from racing, duplicating work or sitting idle. Shows which
// board is in use (AgentSentinel's or ABP's own), per board the task counts, who holds which files and resources,
// leases about to run out, idle agents and tasks that are blocked with the reason, plus a filterable task list.
// Read-only. This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_coordination_panel.py fails if the two differ. It uses the page's own api() and esc(), and draws into #co-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const errText = (e) => { if (window.ABP && ABP.unitUnavailable && ABP.unitUnavailable(e)) return 'This module is not loaded.'; let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const STATES = ['open', 'claimed', 'running', 'review', 'done', 'failed', 'cancelled'];
  const when = (ms) => (ms ? new Date(ms).toLocaleTimeString() : '-');
  const left = (ms) => (ms == null ? '-' : (ms < 0 ? 'expired' : Math.round(ms / 1000) + ' s'));
  let f = { board: '', state: '' };
  let data = null;      // {backend, enabled, summary}
  let tasks = null;     // {tasks}
  let loading = false;
  let err = '';
  let timer = null;

  function css() {
    if ($('co-css')) return;
    const s = document.createElement('style');
    s.id = 'co-css';
    s.textContent = `.co-bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0 10px}
.co-note{color:var(--muted);font-size:12px}
.co-scroll{overflow-x:auto}
.co-table{width:100%;border-collapse:collapse;font-size:12.5px}
.co-table th{text-align:left;color:var(--muted);font-weight:600;padding:6px;border-bottom:1px solid var(--line);white-space:nowrap}
.co-table td{padding:6px;border-bottom:1px solid var(--line);vertical-align:top}
.co-chip{display:inline-block;padding:1px 8px;margin:1px 2px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.co-chip.bad{background:var(--critical-soft);color:var(--critical)}
.co-chip.run{background:var(--surface-2);color:var(--warning)}
.co-card{background:var(--surface-2);border-radius:8px;padding:10px 12px;margin:8px 0}
.co-mono{font-family:ui-monospace,monospace;word-break:break-all}
.co-empty{color:var(--muted);text-align:center;padding:18px}`;
    document.head.appendChild(s);
  }

  const chip = (st, n) => `<span class="co-chip ${st === 'failed' ? 'bad' : (st === 'running' || st === 'claimed') ? 'run' : ''}">${E(st)}${n != null ? ' ' + E(n) : ''}</span>`;

  function boardCard(name, b) {
    const counts = STATES.filter((s) => (b.counts || {})[s]).map((s) => chip(s, b.counts[s])).join('') || '<span class="co-note">no tasks</span>';
    const holders = (b.holders || []).length
      ? `<div class="co-scroll"><table class="co-table"><thead><tr><th>Resource</th><th>Held by</th><th>Task</th><th>Lease ends</th></tr></thead><tbody>${b.holders.map((h) =>
        `<tr><td class="co-mono">${E(h.resource)}${h.mode === 'shared' ? ' (shared)' : ''}</td><td>${E(h.owner || h.agent || '')}</td><td>${E(h.title || h.task || '')}</td><td>${E(when(h.lease_until))}</td></tr>`).join('')}</tbody></table></div>`
      : '<p class="co-note">Nothing locked.</p>';
    const blocked = (b.blocked || []).length
      ? `<p class="co-note">Waiting:</p><ul>${b.blocked.map((t) => `<li><b>${E(t.title)}</b> - ${E(blockWhy(t))}</li>`).join('')}</ul>` : '';
    const expiring = (b.expiring || []).length
      ? `<p class="co-note">Leases running out:</p><ul>${b.expiring.map((t) => `<li>${E(t.title)} (${E(t.owner || '')}) in ${E(left(t.in_ms))}</li>`).join('')}</ul>` : '';
    const idle = (b.idle_agents || []).length ? `<p class="co-note">Idle agents: ${b.idle_agents.map((a) => E(a)).join(', ')}</p>` : '';
    return `<div class="co-card"><h3>${E(name)}</h3><div>${counts}</div>${holders}${blocked}${expiring}${idle}</div>`;
  }

  function blockWhy(t) {
    if (t.reason === 'deps') return 'waiting for ' + (t.waiting_for || []).length + ' task(s) to finish';
    if (t.reason === 'locks') return 'locked by ' + (t.holders || []).map((h) => (h.agent || h.owner || '?') + (h.title ? ' (' + h.title + ')' : '')).join(', ');
    if (t.reason === 'needs') return 'no live agent has ' + (t.needs || []).join(', ');
    return t.reason || '';
  }

  function render() {
    const root = $('co-root');
    if (!root) return;
    const keep = document.activeElement && document.activeElement.id;
    const d = data;
    const boards = d && d.summary && d.summary.boards ? Object.keys(d.summary.boards).sort() : [];
    const ag = d && d.summary && d.summary.agents ? d.summary.agents : {};
    const head = d
      ? `<p><span class="co-chip">${E(d.backend === 'sentinel' ? 'AgentSentinel board' : 'ABP local board')}</span>${d.enabled ? '' : ' <span class="co-chip bad">sub-agent coordination off</span>'}
         <span class="co-note"> ${E((ag.idle || []).length)} idle agent(s), ${E((ag.gone || []).length)} gone</span></p>` : '';
    const cards = boards.length ? boards.map((n) => boardCard(n, d.summary.boards[n])).join('') : `<p class="co-empty">${d ? 'No work on the board yet. Sub-agents and swarm runs appear here as they start.' : 'Loading…'}</p>`;
    const rows = ((tasks && tasks.tasks) || []).map((t) => `<tr><td>${chip(t.state)}</td><td>${E(t.title)}<br><span class="co-note co-mono">${E(t.board)}</span></td><td>${E(t.owner || '-')}</td><td>${E(t.attempts)}/${E(t.max_attempts)}</td><td>${E(t.note || '')}</td></tr>`).join('');
    root.innerHTML = (err ? `<p class="cardnote">Coordination unavailable: ${E(err)}</p>` : '') + head +
      `<div class="co-bar"><select id="co-board" aria-label="Board"><option value="">All boards</option>${boards.map((b) => `<option value="${E(b)}"${f.board === b ? ' selected' : ''}>${E(b)}</option>`).join('')}</select>
        <select id="co-state" aria-label="State"><option value="">Any state</option>${STATES.map((s) => `<option value="${s}"${f.state === s ? ' selected' : ''}>${s}</option>`).join('')}</select>
        <button class="btn" data-co="apply">Apply</button><button class="btn" data-co="refresh">Refresh</button></div>` +
      cards + `<h3>Tasks</h3><div class="co-scroll"><table class="co-table"><thead><tr><th>State</th><th>Task</th><th>Owner</th><th>Attempts</th><th>Note</th></tr></thead><tbody>${rows || '<tr><td colspan="5" class="co-empty">No tasks match.</td></tr>'}</tbody></table></div>`;
    if (keep && $(keep)) { try { $(keep).focus(); } catch (_) {} }
  }

  async function load() {
    if (!$('co-root') || loading) return;
    loading = true;
    try {
      const p = new URLSearchParams({ limit: '200' });
      if (f.board) p.set('board', f.board);
      if (f.state) p.set('state', f.state);
      const q = f.board ? '?board=' + encodeURIComponent(f.board) : '';
      const [s, t] = await Promise.all([api('/api/coordination/summary' + q), api('/api/coordination/tasks?' + p.toString())]);
      data = s; tasks = t; err = '';
    } catch (e) {
      err = errText(e);
    } finally {
      loading = false;
      render();
    }
  }

  function init() {
    const root = $('co-root');
    if (!root) return;
    css();
    render();
    root.addEventListener('click', (ev) => {
      const el = ev.target.closest('[data-co]');
      if (!el) return;
      if (el.dataset.co === 'apply') f = { board: ($('co-board') || {}).value || '', state: ($('co-state') || {}).value || '' };
      load();
    });
    const onHash = () => {
      const on = (location.hash || '').replace('#', '') === 'coordination';
      if (on) { load(); if (!timer) timer = setInterval(load, 5000); }
      else if (timer) { clearInterval(timer); timer = null; }
    };
    window.addEventListener('hashchange', onHash);
    onHash();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
