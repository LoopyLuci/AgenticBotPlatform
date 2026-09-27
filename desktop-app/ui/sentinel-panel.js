// Sentinel panel (ADR-0011): ABP's self-preservation status inside the Resilience page.
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_sentinel_panel.py fails if the two differ. It uses the page's own api(), esc() and showToast(),
// and draws into #sn-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const dot = (level) => `<span class="dot ${level}" aria-hidden="true"></span>`;
  const when = (iso) => (iso ? new Date(iso).toLocaleString() : 'never');
  const size = (b) => (b > 1 << 20 ? (b / (1 << 20)).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB');
  let busy = false;

  function css() {
    if ($('sn-css')) return;
    const s = document.createElement('style');
    s.id = 'sn-css';
    s.textContent = `.sn-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-top:10px}
.sn-tile{border:1px solid var(--line,#3333);border-radius:8px;padding:10px 12px}.sn-tile b{display:block;font-size:12px;opacity:.75;font-weight:600}
.sn-tile .v{font-size:15px;margin-top:4px;display:flex;gap:8px;align-items:center}
.sn-list{margin:8px 0 0;padding:0;list-style:none;max-height:260px;overflow:auto;font-size:12.5px}
.sn-list li{padding:6px 0;border-bottom:1px solid var(--line,#3333);display:flex;gap:8px;align-items:flex-start}
.sn-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.sn-tile .dot,.sn-list .dot{width:9px;height:9px;min-width:9px;border-radius:50%;display:inline-block;margin-top:4px}`;
    document.head.appendChild(s);
  }

  function tile(label, level, value) {
    return `<div class="sn-tile"><b>${esc(label)}</b><div class="v">${dot(level)}<span>${value}</span></div></div>`;
  }

  function render(st) {
    const boot = st.boot || {};
    const wd = st.watchdog || {};
    const cve = st.cve;
    const lb = st.latest_backup;
    const cveCount = cve ? cve.findings.length : 0;
    const worst = cve && cve.findings.length ? cve.findings[0] : null;
    const pyFixable = cve ? cve.findings.some((f) => f.source === 'python-env' && f.fixed && f.fixed.length) : false;
    const tiles = [
      tile('Mode', boot.safe_mode ? 'critical' : 'good', boot.safe_mode ? 'SAFE MODE: ' + esc(boot.reason || '') : 'normal'),
      tile('Latest verified backup', lb ? 'good' : 'warning', lb ? `${esc(when(lb.created_at))} · ${size(lb.size_bytes)}` : 'none yet'),
      tile('Vulnerabilities', cveCount ? ((worst.score || 0) >= 9 ? 'critical' : 'warning') : (cve ? 'good' : 'neutral'),
        cve ? `${cveCount} in ${cve.packages} packages · scanned ${esc(when(cve.scanned_at))}` : 'not scanned yet'),
      tile('Open error signatures', st.issues_open ? 'warning' : 'good', String(st.issues_open)),
      tile('Event loop', (wd.lag_s || 0) > 1 ? 'warning' : 'good',
        wd.lag_s === undefined ? 'watchdog off' : `lag ${wd.lag_s}s · max ${wd.max_lag_s}s · ${wd.stalls} stall(s)`),
      tile('Memory', 'neutral', wd.rss_mb ? `${wd.rss_mb} MB · ${wd.threads} threads` : '—'),
    ].join('');
    const vulns = cve && cve.findings.length
      ? `<ul class="sn-list" aria-label="Vulnerabilities">${cve.findings.slice(0, 30).map((f) => `<li>${dot((f.score || 0) >= 9 ? 'critical' : 'warning')}
          <div><b>${esc(f.id)}</b> ${esc(f.severity)}${f.score ? ' ' + f.score : ''} · ${esc(f.name)} ${esc(f.version)} <span class="mono">[${esc(f.source)}]</span><br>
          <span>${esc(f.summary || '')}</span>${f.fixed && f.fixed.length ? `<br><span class="mono">fixed in ${esc(f.fixed.join(', '))}</span>` : ''}</div></li>`).join('')}</ul>`
      : '';
    const alerts = (st.alerts || []).length
      ? `<ul class="sn-list" aria-label="Recent alerts">${st.alerts.slice(0, 25).map((a) => `<li>${dot(a.level === 'critical' ? 'critical' : 'warning')}
          <div><span class="mono">${esc(a.ts)}</span><br>${esc(a.message)}</div></li>`).join('')}</ul>`
      : '<p class="cardnote">No alerts. Everything the Sentinel watches is healthy.</p>';
    $('sn-root').innerHTML = `<div class="sn-grid">${tiles}</div>
      <div class="sn-actions" role="group" aria-label="Run a Sentinel duty now">
        <button class="btn" data-sn="run" data-duty="backup">Back up now</button>
        <button class="btn" data-sn="run" data-duty="integrity">Check database</button>
        <button class="btn" data-sn="run" data-duty="cve">Scan for vulnerabilities</button>
        <button class="btn" data-sn="run" data-duty="security">Security check</button>
        ${pyFixable ? '<button class="btn" data-sn="fix">Upgrade vulnerable Python packages</button>' : ''}
      </div>
      ${vulns ? '<h4 style="margin-top:14px">Vulnerabilities</h4>' + vulns : ''}
      <h4 style="margin-top:14px">Recent alerts</h4>${alerts}`;
  }

  async function load() {
    if (!$('sn-root')) return;
    try { render(await api('/api/sentinel/status')); } catch (e) { $('sn-root').innerHTML = `<p class="cardnote">Sentinel status unavailable: ${esc(errText(e))}</p>`; }
  }

  async function act(el) {
    if (busy) return;
    busy = true;
    el.disabled = true;
    const label = el.textContent;
    el.textContent = 'Working…';
    try {
      if (el.dataset.sn === 'run') {
        const r = await api('/api/sentinel/run/' + encodeURIComponent(el.dataset.duty), { method: 'POST', body: '{}' });
        toast(r.result && r.result.ok ? `${label}: done` : `${label}: ${(r.result && r.result.error) || 'failed'}`, r.result && r.result.ok ? 'good' : 'error');
      } else if (el.dataset.sn === 'fix') {
        if (!window.confirm('Upgrade each vulnerable Python package to its fixed version? ABP checks it still starts and rolls back any upgrade that breaks it.')) return;
        const r = await api('/api/sentinel/cve/fix', { method: 'POST', body: '{}' });
        const ok = r.outcomes.filter((o) => o.ok).length;
        toast(`${ok}/${r.outcomes.length} package(s) upgraded; restart ABP to load them`, ok === r.outcomes.length ? 'good' : 'error');
      }
      await load();
    } catch (e) {
      toast(errText(e), 'error');
    } finally {
      busy = false;
      el.disabled = false;
      el.textContent = label;
    }
  }

  function init() {
    const root = $('sn-root');
    if (!root) return;
    css();
    root.addEventListener('click', (ev) => { const el = ev.target.closest('[data-sn]'); if (el) act(el); });
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'resilience') load(); };
    window.addEventListener('hashchange', onHash);
    onHash();
    if ('IntersectionObserver' in window) {
      new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) load(); })).observe(root);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
