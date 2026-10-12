// Sandbox screen: what ABP has running right now, from the Sandbox Nervous System. Each cell and process with the
// call site that asked for it, its preset, pid, age and CPU/memory; the reflex actions taken lately; and what this
// OS can enforce. A process shows its executable name only, never its arguments. A cell that is not a daemon has a
// Stop button (it asks first), which is the same audited kill the Processes panel on the diagnostics page uses.
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_sns_status.py fails if the two differ. It uses the page's own api() and esc(), and draws into #sns-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const errMsg = (e) => { let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const num = (n) => (n == null ? '-' : Number(n).toLocaleString());
  const age = (s) => (s == null ? '-' : s < 90 ? Math.round(s) + ' s' : s < 5400 ? Math.round(s / 60) + ' min' : (s / 3600).toFixed(1) + ' h');
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString() : '-');
  let data = null;
  let err = '';
  let loading = false;
  let timer = null;

  function css() {
    if ($('sns-css')) return;
    const s = document.createElement('style');
    s.id = 'sns-css';
    s.textContent = `.sns-bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0 10px}
.sns-note{color:var(--muted);font-size:12px}
.sns-scroll{overflow-x:auto}
.sns-table{width:100%;border-collapse:collapse;font-size:12.5px}
.sns-table th{text-align:left;color:var(--muted);font-weight:600;padding:6px;border-bottom:1px solid var(--line);white-space:nowrap}
.sns-table td{padding:6px;border-bottom:1px solid var(--line);vertical-align:top}
.sns-table td.n{text-align:right;font-variant-numeric:tabular-nums}
.sns-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.sns-chip.bad{background:var(--critical-soft);color:var(--critical)}
.sns-tot{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.sns-tot div{background:var(--surface-2);border-radius:8px;padding:6px 10px;min-width:110px}
.sns-tot b{display:block;font-size:15px}
.sns-mono{font-family:ui-monospace,monospace;word-break:break-all}
.sns-empty{color:var(--muted);text-align:center;padding:14px}`;
    document.head.appendChild(s);
  }

  function renderCaps(c) {
    const yes = (v) => (v ? 'yes' : 'no');
    const rows = [['System', c.os], ['Containment', c.containment], ['Tree kill', c.tree_kill], ['CPU cap', yes(c.cpu_cap)], ['Memory cap', yes(c.memory_cap)],
      ['Priority', yes(c.priority)], ['Measuring CPU/memory', yes(c.sampling)], ['Processors kept free', (c.avoid_cpus || []).join(', ') || 'none']];
    return `<div class="sns-tot">${rows.map(([k, v]) => `<div><span class="sns-note">${E(k)}</span><b>${E(v == null ? '-' : v)}</b></div>`).join('')}</div>`;
  }

  function renderCells(cells) {
    const body = cells.length ? cells.map((c) => `<tr>
        <td><b>${E(c.call_site || '-')}</b><br><span class="sns-note">${E(c.name || '')}</span></td>
        <td><span class="sns-chip">${E(c.preset || '-')}</span>${c.persistent ? ' <span class="sns-note">outlives ABP</span>' : ''}</td>
        <td class="n">${num(c.processes)}</td><td class="n">${E(age(c.age_s))}</td>
        <td class="n">${E(Number(c.cpu_percent || 0).toFixed(1))}%</td><td class="n">${E(Math.round(c.rss_mb || 0))} MB</td>
        <td>${(c.notes || []).map((n) => `<span class="sns-note">${E(n)}</span>`).join('<br>')}</td>
        <td>${c.killable ? `<button class="btn" data-sns="kill" data-cell="${E(c.id)}" data-name="${E(c.call_site || c.name || c.id)}">Stop</button>` : ''}</td></tr>`).join('')
      : '<tr><td colspan="8" class="sns-empty">Nothing is running in a cell right now.</td></tr>';
    return `<div class="sns-scroll"><table class="sns-table"><thead><tr><th>Started by</th><th>Preset</th><th>Processes</th><th>Age</th><th>CPU</th><th>Memory</th><th>Notes</th><th></th></tr></thead><tbody>${body}</tbody></table></div>`;
  }

  function renderProcs(procs) {
    const body = procs.length ? procs.map((p) => `<tr>
        <td class="n sns-mono">${E(p.pid)}</td><td class="sns-mono">${E(p.exe)}</td><td>${E(p.call_site || '-')}</td>
        <td><span class="sns-chip">${E(p.preset || '-')}</span></td><td class="n">${E(age(p.age_s))}</td>
        <td>${p.role === 'descendant' ? 'started by ' + E(p.parent_pid) : 'started by ABP'}</td></tr>`).join('')
      : '<tr><td colspan="6" class="sns-empty">No live processes.</td></tr>';
    return `<div class="sns-scroll"><table class="sns-table"><thead><tr><th>Pid</th><th>Program</th><th>Started by</th><th>Preset</th><th>Age</th><th>Origin</th></tr></thead><tbody>${body}</tbody></table></div>`;
  }

  function renderReflexes(rows) {
    if (!rows.length) return '<p class="sns-empty">No limit has been hit and nothing has been stopped lately.</p>';
    return rows.slice().reverse().map((e) => `<div class="sns-note" style="padding:3px 0"><span class="sns-chip ${e.kind === 'limit_hit' ? 'bad' : ''}">${E(e.kind)}</span> ${E(when(e.ts))} · ${E(e.cell || '')} ${E(e.detail || '')}</div>`).join('');
  }

  function render() {
    const root = $('sns-root');
    if (!root) return;
    const d = data;
    let html = err ? `<p class="cardnote">Sandbox status unavailable: ${E(err)}</p>` : '';
    if (d) {
      html += `<div class="sns-bar"><button class="btn" data-sns="refresh">Refresh</button>
          <span class="sns-note">${num((d.cells || []).length)} cells · ${num((d.processes || []).length)} processes · windowless guard ${d.guard && d.guard.installed ? 'on' : 'off'}</span></div>
        <h3>What this system can enforce</h3>${renderCaps(d.capabilities || {})}
        <h3>Cells</h3>${renderCells(d.cells || [])}
        <h3>Processes</h3>${renderProcs(d.processes || [])}
        <h3>Recent limit and stop actions</h3>${renderReflexes(d.reflexes || [])}`;
    } else if (!err) {
      html += '<p class="cardnote">Loading…</p>';
    }
    root.innerHTML = html;
  }

  async function load() {
    if (!$('sns-root') || loading) return;
    loading = true;
    try {
      data = await api('/api/sandbox/summary');
      err = '';
    } catch (e) {
      err = errMsg(e);
    } finally {
      loading = false;
      render();
    }
  }

  async function kill(id, name) {
    if (!window.confirm('Stop ' + name + ' and everything it started?')) return;
    try {
      await api('/api/sandbox/cells/' + encodeURIComponent(id) + '/kill', { method: 'POST' });
      err = '';
    } catch (e) {
      err = errMsg(e);
    }
    load();
  }

  function init() {
    const root = $('sns-root');
    if (!root) return;
    css();
    render();
    root.addEventListener('click', (ev) => {
      const el = ev.target.closest('[data-sns]');
      if (!el) return;
      if (el.dataset.sns === 'kill') kill(el.dataset.cell, el.dataset.name);
      else load();
    });
    const onHash = () => {
      const here = (location.hash || '').replace('#', '') === 'sns';
      if (here) { load(); if (!timer) timer = setInterval(load, 5000); }
      else if (timer) { clearInterval(timer); timer = null; }
    };
    window.addEventListener('hashchange', onHash);
    onHash();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
