// Routines panel: the saved, parameterised tasks the agent writes with its routine_save tool, listed
// with their schedules, run by hand from here, re-timed, paused and deleted - the same routines
// /routine run / schedule / pause / resume / delete drive in chat.
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_routines_panel.py fails if the two differ. It uses the page's own api(), esc() and
// showToast(), and draws into #rt-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  // The page's own esc() when there is one, and a local equivalent either way: a routine's
  // description, template, parameter names and run summaries are all text a person typed or a
  // model wrote, and none of it is markup.
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const when = (ts) => {
    if (!ts) return '—';
    const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts);
    return isNaN(d.getTime()) ? String(ts) : d.toLocaleString();
  };
  // The scheduler's own rule (bot/scheduler.py's parse_duration), so the page refuses exactly what
  // the API would: a bare number of seconds, or 30s / 10m / 2h / 7d.
  const DURATION = /^(\d+)\s*([smhd])$/i;
  const UNITS = { s: 1, m: 60, h: 3600, d: 86400 };
  function parseInterval(text) {
    const t = String(text || '').trim();
    if (t === '' || !/^\d+$/.test(t)) {
      const m = DURATION.exec(t);
      if (!m) return { error: 'Use an interval like 30m, 2h or 7d (or a plain number of seconds).' };
      const secs = parseInt(m[1], 10) * UNITS[m[2].toLowerCase()];
      return secs < 5 ? { error: 'The interval must be at least 5 seconds.' } : { seconds: secs };
    }
    const secs = parseInt(t, 10);
    return secs < 5 ? { error: 'The interval must be at least 5 seconds.' } : { seconds: secs };
  }
  const intervalText = (s) => {
    if (!s) return '';
    if (s % 86400 === 0) return `${s / 86400}d`;
    if (s % 3600 === 0) return `${s / 3600}h`;
    if (s % 60 === 0) return `${s / 60}m`;
    return `${s}s`;
  };
  const outcome = (o) => (o === 'ok' || o === 'ran' ? 'good' : o === 'error' ? 'critical' : o === 'started' ? 'warning' : 'neutral');
  let openId = null;
  let busy = false;

  function css() {
    if ($('rt-css')) return;
    const s = document.createElement('style');
    s.id = 'rt-css';
    s.textContent = `.rt-note{color:var(--muted);font-size:12px;margin:2px 0 10px}
.rt-scroll{overflow-x:auto}
.rt-table{width:100%;border-collapse:collapse;font-size:12.5px}
.rt-table th{text-align:left;color:var(--muted);font-weight:600;padding:6px;border-bottom:1px solid var(--line);white-space:nowrap}
.rt-table td{padding:6px;border-bottom:1px solid var(--line);vertical-align:top}
.rt-empty td{color:var(--muted);text-align:center;padding:18px}
.rt-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.rt-chip.good{background:var(--good-soft);color:var(--good)}.rt-chip.warning{background:var(--warning-soft);color:var(--warning)}
.rt-chip.critical{background:var(--critical-soft);color:var(--critical)}
.rt-mono{font-family:var(--font-mono,monospace);font-size:12px}
.rt-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0}
.rt-detail td{background:var(--surface-2)}
.rt-detail h4{margin:10px 0 4px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.rt-detail pre{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:8px 10px;white-space:pre-wrap;word-break:break-word;font-size:12px;margin:4px 0 0;max-height:220px;overflow:auto}
#rt-root input,#rt-root select{background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%}
#rt-root label{display:block;font-size:12px;color:var(--muted);margin:6px 0 2px}
.rt-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.rt-err{color:var(--critical);font-size:12px;margin:4px 0 0}
.rt-ok{color:var(--good);font-size:12px;margin:4px 0 0}`;
    document.head.appendChild(s);
  }

  const action = (id, label, act, cls) =>
    `<button class="btn ${cls || ''}" data-rt="${act}" data-id="${id}" style="padding:3px 9px;font-size:11px;">${label}</button>`;

  function detail(r) {
    const params = r.params || {};
    const names = Object.keys(params);
    const inputs = names.length
      ? names.map((k) => `<label for="rt-run-${E(r.id)}-${E(k)}">${E(k)}${params[k] && params[k].default !== undefined ? ` (default ${E(params[k].default)})` : ''}</label>
          <input id="rt-run-${E(r.id)}-${E(k)}" data-rt-param="${E(k)}" style="width:100%;" placeholder="${E((params[k] && params[k].description) || '')}"${params[k] && params[k].default !== undefined ? ` value="${E(params[k].default)}"` : ''}>`).join('')
      : '<p class="rt-note">This routine takes no parameters.</p>';
    const sched = (r.schedules || []).map((s) => `<li>#${s.id} every ${intervalText(s.interval_s)} — ${s.enabled ? `next ${E(when(s.next_run_at))}` : 'paused'} (${s.run_count} run(s))</li>`).join('');
    const hist = (r.history || []).length
      ? r.history.map((h) => `<tr><td class="rt-mono">${E(when(h.started_at))}</td>
          <td><span class="rt-chip ${outcome(h.outcome)}">${E(h.outcome)}</span></td>
          <td>${E((h.summary || '').slice(0, 240))}</td></tr>`).join('')
      : '<tr class="rt-empty"><td colspan="3">Never run.</td></tr>';
    return `<div class="rt-detail"><div style="padding:10px 12px;">
      <div class="rt-row" style="justify-content:space-between;">
        <div><b class="rt-mono">${E(r.name)}</b> <span class="rt-chip">bot ${E(r.instance_id)}</span> ${r.paused ? '<span class="rt-chip warning">paused</span>' : ''}</div>
        <div class="rt-actions" style="margin:0;">
          ${action(r.id, r.paused ? 'Resume' : 'Pause', r.paused ? 'resume' : 'pause')}
          ${action(r.id, 'Close', 'close')}
          ${action(r.id, 'Delete', 'delete', 'danger')}
        </div>
      </div>
      ${r.description ? `<p class="rt-note">${E(r.description)}</p>` : ''}
      <h4>Template</h4><pre>${E(r.template)}</pre>
      <h4>Run now</h4>
      ${inputs}
      <div class="rt-actions"><button class="btn primary" data-rt="run" data-id="${r.id}" style="padding:5px 12px;font-size:12px;">Run now</button>
        <span class="rt-note" id="rt-run-status-${r.id}"></span></div>
      <h4>Schedule</h4>
      <ul class="rt-note" style="padding-left:18px;margin:2px 0;">${sched || '<li>Not scheduled.</li>'}</ul>
      <label for="rt-int-${r.id}">Run every</label>
      <div class="rt-row">
        <input id="rt-int-${r.id}" data-rt-interval style="width:110px;" value="${E(intervalText(r.interval_s))}" placeholder="7d">
        <input data-rt-chat style="flex:1;min-width:150px;" value="" placeholder="chat id (only needed the first time)">
        <button class="btn" data-rt="schedule" data-id="${r.id}" style="padding:5px 12px;font-size:12px;">Save schedule</button>
      </div>
      <p class="rt-note">Values for the re-timed run come from the fields above.</p>
      <p class="rt-err" id="rt-err-${r.id}"></p>
      <h4>History</h4>
      <div class="rt-scroll"><table class="rt-table"><thead><tr><th>When</th><th>Outcome</th><th>Summary</th></tr></thead><tbody>${hist}</tbody></table></div>
    </div></div>`;
  }

  function table(rows) {
    const body = document.querySelector('#rt-root tbody');
    if (!rows.length) {
      body.innerHTML = '<tr class="rt-empty"><td colspan="7">No routines yet. Do a task with the agent, then ask it to save that as a routine.</td></tr>';
      return;
    }
    body.innerHTML = rows.map((r) => `<tr>
      <td>${action(r.id, r.name, 'open', 'primary')}</td>
      <td class="rt-mono">${E(r.instance_id)}</td>
      <td>${E(Object.keys(r.params || {}).join(', ') || '—')}</td>
      <td>${r.interval_s ? E(intervalText(r.interval_s)) : '<span class="rt-chip">not scheduled</span>'}</td>
      <td>${r.paused ? '<span class="rt-chip warning">paused</span>' : r.interval_s ? '<span class="rt-chip good">on</span>' : '<span class="rt-chip">—</span>'}</td>
      <td>${E(r.last_run ? `${r.last_run.outcome} ${when(r.last_run.started_at)}` : 'never')}</td>
      <td>${E(when(r.next_run_at))}</td>
    </tr>`).join('');
  }

  function values(root, id) {
    const out = {};
    root.querySelectorAll('[data-rt-param]').forEach((i) => { if (i.value !== '') out[i.dataset.rtParam] = i.value; });
    const el = root.querySelector(`#rt-int-${CSS.escape(id)}`);
    return { values: out, interval: el ? el.value : '' };
  }

  async function load() {
    const root = $('rt-root');
    if (!root) return;
    css();
    let rows;
    try {
      rows = (await api('/api/routines')).routines || [];
    } catch (e) {
      root.innerHTML = `<p class="rt-err">Could not load routines: ${E(errText(e))}</p>`;
      return;
    }
    if (!root.querySelector('table')) {
      root.innerHTML = `<p class="rt-note">A routine is a task the agent did once and saved, with <span class="rt-mono">{{placeholders}}</span> for whatever changes between runs. Each run is an ordinary agent turn — the ordinary tools, permissions and approvals — so a routine skips nothing.</p>
        <div class="rt-scroll"><table class="rt-table"><thead><tr><th>Name</th><th>Bot</th><th>Parameters</th><th>Every</th><th>State</th><th>Last run</th><th>Next run</th></tr></thead><tbody></tbody></table></div>
        <div id="rt-detail-host"></div>`;
    }
    table(rows);
    if (openId !== null) {
      if (rows.some((r) => r.id === openId)) await open(openId, true);
      else { openId = null; host().innerHTML = ''; }
    }
  }

  const host = () => $('rt-detail-host');

  async function open(id, quiet) {
    let r;
    try {
      r = await api(`/api/routines/${encodeURIComponent(id)}`);
    } catch (e) {
      if (!quiet) toast(errText(e), 'error');
      return;
    }
    if (openId !== id) { openId = id; }
    host().innerHTML = detail(r);
  }

  async function act(el) {
    const kind = el.dataset.rt;
    const id = el.dataset.id;
    if (kind === 'open') return open(Number(id));
    if (kind === 'close') { openId = null; host().innerHTML = ''; return; }
    if (busy) return;
    const errBox = $(`rt-err-${id}`);
    const status = $(`rt-run-status-${id}`);
    if (kind === 'delete') {
      if (!window.confirm('Delete this routine, its schedules and its history? There is no undo.')) return;
    }
    busy = true;
    el.disabled = true;
    const label = el.textContent;
    el.textContent = 'Working…';
    try {
      if (kind === 'run') {
        const v = values($('rt-root'), id);
        if (status) status.textContent = 'Starting…';
        await api(`/api/routines/${encodeURIComponent(id)}/run`, { method: 'POST', body: JSON.stringify({ values: v.values }) });
        if (status) status.textContent = 'Started — the result appears in the history below.';
        toast('Running now.', 'success');
      } else if (kind === 'pause') {
        const r = await api(`/api/routines/${encodeURIComponent(id)}/pause`, { method: 'POST', body: '{}' });
        toast(`Paused ${r.changed} schedule(s).`, 'success');
      } else if (kind === 'resume') {
        const r = await api(`/api/routines/${encodeURIComponent(id)}/resume`, { method: 'POST', body: '{}' });
        toast(`Resumed ${r.changed} schedule(s).`, 'success');
      } else if (kind === 'schedule') {
        const v = values($('rt-root'), id);
        const parsed = parseInterval(v.interval);
        if (parsed.error) { if (errBox) errBox.textContent = parsed.error; return; }
        const chat = ($('rt-root').querySelector('[data-rt-chat]') || {}).value || '';
        await api(`/api/routines/${encodeURIComponent(id)}/schedule`, {
          method: 'PUT', body: JSON.stringify({ interval: v.interval.trim(), values: v.values, chat_id: chat || null }),
        });
        if (errBox) errBox.textContent = '';
        toast('Schedule saved.', 'success');
      } else if (kind === 'delete') {
        await api(`/api/routines/${encodeURIComponent(id)}`, { method: 'DELETE' });
        openId = null;
        host().innerHTML = '';
        toast('Routine deleted.', 'success');
      }
      await load();
    } catch (e) {
      const m = errText(e);
      if (errBox) errBox.textContent = m;
      if (status) status.textContent = '';
      toast(m, 'error');
    } finally {
      busy = false;
      el.disabled = false;
      el.textContent = label;
    }
  }

  function init() {
    const root = $('rt-root');
    if (!root) return;
    css();
    root.addEventListener('click', (ev) => { const el = ev.target.closest('[data-rt]'); if (el) act(el); });
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'routines') load(); };
    window.addEventListener('hashchange', onHash);
    onHash();
    if ('IntersectionObserver' in window) {
      new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) load(); })).observe(root);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
