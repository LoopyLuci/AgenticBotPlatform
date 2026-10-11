// Usage & limits panel (roadmap PM): per model, what the last 1 or 7 days used (calls, tokens, rate-limit
// refusals, errors, last use), how much of each known allowance is spent and when it frees up, and any
// "blocked until" the router is honouring. Read-only; it refreshes itself while the screen is showing.
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_usage_panel.py fails if the two differ. It uses the page's own api(), esc() and showToast(),
// and draws into #ul-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const errText = (e) => { if (window.ABP && ABP.unitUnavailable && ABP.unitUnavailable(e)) return 'This module is not loaded.'; let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const REFRESH_MS = 30000;
  const num = (n) => Number(n || 0).toLocaleString();
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : 'never');   // epoch seconds, shown in the browser's own time zone
  let days = 1;
  let data = null;
  let visible = false;
  let loading = false;

  function css() {
    if ($('ul-css')) return;
    const s = document.createElement('style');
    s.id = 'ul-css';
    s.textContent = `.ul-bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0 10px}
.ul-bar .on{background:var(--accent,#4a7);color:#fff}
.ul-note{color:var(--muted);font-size:12px}
.ul-scroll{overflow-x:auto}
.ul-table{width:100%;border-collapse:collapse;font-size:12.5px}
.ul-table th{text-align:left;color:var(--muted);font-weight:600;padding:6px;border-bottom:1px solid var(--line);white-space:nowrap}
.ul-table td{padding:6px;border-bottom:1px solid var(--line);vertical-align:top}
.ul-table td.n{text-align:right;font-variant-numeric:tabular-nums}
.ul-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.ul-chip.critical{background:var(--critical-soft);color:var(--critical)}
.ul-limit{margin:0 0 6px;min-width:220px}
.ul-limit .t{display:flex;justify-content:space-between;gap:10px;font-size:11.5px}
.ul-track{height:7px;border-radius:4px;background:var(--surface-2);overflow:hidden;margin:2px 0}
.ul-fill{height:100%;background:var(--good)}.ul-fill.warning{background:var(--warning)}.ul-fill.critical{background:var(--critical)}
.ul-empty{color:var(--muted);text-align:center;padding:18px}`;
    document.head.appendChild(s);
  }

  function limitBar(w) {
    const pct = w.limit ? Math.min(100, Math.round((w.used / w.limit) * 100)) : 0;
    const level = pct >= 100 ? 'critical' : pct >= 80 ? 'warning' : '';
    return `<div class="ul-limit"><div class="t"><span>${E(w.name)}</span><span>${num(w.remaining)} left of ${num(w.limit)}</span></div>
      <div class="ul-track" role="progressbar" aria-label="${E(w.name)} used" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><div class="ul-fill ${level}" style="width:${pct}%"></div></div>
      <div class="ul-note">${num(w.used)} used · frees up ${E(when(w.resets_at))}</div></div>`;
  }

  function row(m) {
    const u = m['d' + days] || {};
    const blocked = m.blocked_until ? `<br><span class="ul-chip critical">blocked until ${E(when(m.blocked_until))}</span>` : '';
    const bars = (m.windows || []).length ? m.windows.map(limitBar).join('') : '<span class="ul-note">no known limits</span>';
    return `<tr><td><b>${E(m.model)}</b><br><span class="ul-note">${E(m.provider)}</span>${blocked}</td>
      <td class="n">${num(u.calls)}</td><td class="n">${num(u.tokens)}</td><td class="n">${num(u.rate_limited)}</td><td class="n">${num(u.errors)}</td>
      <td>${E(when(u.last_used))}</td><td>${bars}</td></tr>`;
  }

  function render() {
    const root = $('ul-root');
    if (!root) return;
    const models = ((data && data.models) || []).filter((m) => ((m['d' + days] || {}).calls || 0) > 0 || m.blocked_until);
    const body = models.length
      ? models.map(row).join('')
      : `<tr><td colspan="7" class="ul-empty">No model calls in the last ${days === 1 ? 'day' : days + ' days'} yet. Usage appears here as soon as an agent or bot calls a model.</td></tr>`;
    root.innerHTML = `<div class="ul-bar" role="group" aria-label="Period">
        <button class="btn ${days === 1 ? 'on' : ''}" data-ul="days" data-days="1" aria-pressed="${days === 1}">Last 1 day</button>
        <button class="btn ${days === 7 ? 'on' : ''}" data-ul="days" data-days="7" aria-pressed="${days === 7}">Last 7 days</button>
        <button class="btn" data-ul="refresh">Refresh</button>
        <span class="ul-note">${data ? `Times in your local time zone · updates every ${REFRESH_MS / 1000}s while open` : ''}</span></div>
      <div class="ul-scroll"><table class="ul-table"><thead><tr><th>Model</th><th>Calls</th><th>Tokens</th><th>429s</th><th>Errors</th><th>Last used</th><th>Limits</th></tr></thead>
      <tbody>${body}</tbody></table></div>`;
  }

  async function load() {
    if (!$('ul-root') || loading) return;
    loading = true;
    try {
      data = await api('/api/models/usage-limits');
      render();
    } catch (e) {
      $('ul-root').innerHTML = `<p class="cardnote">Usage unavailable: ${E(errText(e))}</p>`;
    } finally {
      loading = false;
    }
  }

  function init() {
    const root = $('ul-root');
    if (!root) return;
    css();
    render();
    root.addEventListener('click', (ev) => {
      const el = ev.target.closest('[data-ul]');
      if (!el) return;
      if (el.dataset.ul === 'days') { days = el.dataset.days === '7' ? 7 : 1; render(); } else load();
    });
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'usage') load(); };
    window.addEventListener('hashchange', onHash);
    onHash();
    if ('IntersectionObserver' in window) {
      new IntersectionObserver((es) => es.forEach((e) => { visible = e.isIntersecting; if (visible) load(); })).observe(root);
    }
    setInterval(() => { if (visible && !document.hidden) load(); }, REFRESH_MS);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
