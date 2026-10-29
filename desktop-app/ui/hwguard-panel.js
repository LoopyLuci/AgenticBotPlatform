// Hardware panel (bot/hwguard, docs/hwguard.md): live readings of this machine (CPU, GPU, memory, disks), the load
// governor's verdict and worker caps, the heavy-work slot, the WHEA hardware-error history, and every setting with its
// own switch. Software can lower load and react early; it cannot stop a faulty CPU from raising machine checks.
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_hwguard.py fails if the two differ. It uses the page's own api(), esc() and showToast(), and draws into
// #hw-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { if (window.ABP && ABP.unitUnavailable && ABP.unitUnavailable(e)) return 'This module is not loaded.'; let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const REFRESH_MS = 15000;
  const KINDS = { pytest: 'Test workers (pytest -n)', factory: 'Factory / conductor workers', render: 'Video renders', model_load: 'Model loads', build: 'Builds' };
  const SWITCHES = { enabled: 'Governor on (caps heavy work by the machine\'s health)', monitor: 'Monitor on (a reading every interval)',
    whea_watch: 'Watch the WHEA hardware-error log (a new event pauses heavy work)', alerts: 'Alerts (Sentinel journal and push)',
    exclusive: 'One heavy job at a time (render, full test run, model load, build never stack)' };
  const NUMBERS = { interval_s: 'Reading every (s)', cpu_temp_caution_c: 'CPU caution (C)', cpu_temp_pause_c: 'CPU pause (C)',
    gpu_temp_caution_c: 'GPU caution (C)', gpu_temp_pause_c: 'GPU pause (C)', cpu_load_caution_pct: 'CPU load caution (%)',
    vram_caution_pct: 'VRAM caution (%)', ram_caution_gb: 'Free RAM caution (GB)', ram_pause_gb: 'Free RAM pause (GB)',
    commit_caution_pct: 'Commit caution (%)', commit_pause_pct: 'Commit pause (%)', disk_pause_gb: 'Disk free pause (GB)',
    whea_caution_days: 'Fatal WHEA keeps caution (days)' };
  const v = (x, unit) => (x == null ? '<span class="hw-muted">not readable</span>' : E(x) + (unit || ''));
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : 'never');
  let data = null;
  let visible = false;
  let loading = false;

  function css() {
    if ($('hw-css')) return;
    const s = document.createElement('style');
    s.id = 'hw-css';
    s.textContent = `.hw-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr));gap:12px}
.hw-card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--surface);min-width:0}
.hw-card h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.hw-muted{color:var(--muted);font-size:12px}.hw-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:4px 0}
.hw-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.hw-chip.ok{background:var(--good-soft);color:var(--good,#1f9d55)}.hw-chip.caution{background:var(--warning-soft);color:#8a5c00}
.hw-chip.pause{background:var(--danger-soft,#fde8e8);color:var(--danger,#c0392b)}
.hw-table{width:100%;border-collapse:collapse;font-size:12.5px}.hw-table th{text-align:left;color:var(--muted);font-weight:600;padding:5px;border-bottom:1px solid var(--line)}
.hw-table td{padding:5px;border-bottom:1px solid var(--line);vertical-align:top}.hw-scroll{overflow:auto;max-width:100%}
#hw-root input[type=number]{width:80px;background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:4px 6px;font:inherit;font-size:12.5px}
#hw-root ul{margin:4px 0;padding-left:18px}`;
    document.head.appendChild(s);
  }

  function readings(s) {
    const c = s.cpu || {}, g = s.gpu || {}, m = s.memory || {}, cm = m.commit || {}, pf = m.pagefile || {};
    const disks = (s.disks || []).map((d) => `<tr><td>${E(d.mount)}</td><td>${E(d.free_gb)} GB free</td><td>${E(d.pct)}% used</td></tr>`).join('');
    return `<div class="hw-grid">
<div class="hw-card"><h4>CPU</h4><div>Load ${v(c.load_pct, '%')} (busiest core ${v(c.max_core_pct, '%')}), ${E(c.cores)} threads</div><div>Temperature ${v(c.temp_c, ' C')}</div></div>
<div class="hw-card"><h4>GPU</h4><div>VRAM ${v(g.vram_used_gb, ' GB')} of ${v(g.vram_total_gb, ' GB')} (${v(g.vram_pct, '%')})</div><div>Load ${v(g.load_pct, '%')} · Temperature ${v(g.temp_c, ' C')}</div><div class="hw-muted">VRAM from the OS GPU counters (every process on the card), not a per-process budget.</div></div>
<div class="hw-card"><h4>Memory</h4><div>Free RAM ${v(m.available_gb, ' GB')} of ${v(m.total_gb, ' GB')}</div><div>Commit ${v(cm.used_gb, ' GB')} of ${v(cm.limit_gb, ' GB')} (${v(cm.pct, '%')})</div><div>Pagefile ${v(pf.used_gb, ' GB')} of ${v(pf.total_gb, ' GB')}</div></div>
<div class="hw-card"><h4>Disks</h4><div class="hw-scroll"><table class="hw-table">${disks || '<tr><td class="hw-muted">none read</td></tr>'}</table></div></div>
</div>${(s.unavailable || []).length ? `<p class="hw-muted">Not readable on this machine:</p><ul class="hw-muted">${s.unavailable.map((u) => `<li>${E(u)}</li>`).join('')}</ul>` : ''}`;
  }

  function wheaTable(events, ack) {
    if (!events.length) return '<p class="hw-muted">No WHEA hardware errors in the System log.</p>';
    const rows = events.slice(0, 25).map((e) => `<tr><td>${E(new Date(e.ts * 1000).toLocaleString())}</td><td>${E(e.event_id)}</td>
<td><span class="hw-chip ${e.severity === 'fatal' ? 'pause' : 'caution'}">${E(e.severity)}</span></td><td>${E(e.kind)}</td><td>${E(e.error_type || '')}</td>
<td>${E(e.source || '')}</td><td>${E(e.apic_id == null ? '' : e.apic_id)}</td><td>${e.ts > (ack || 0) ? '<b>new</b>' : ''}</td></tr>`).join('');
    return `<div class="hw-scroll"><table class="hw-table"><tr><th>When</th><th>ID</th><th>Severity</th><th>What</th><th>Error type</th><th>Source</th><th>APIC</th><th></th></tr>${rows}</table></div>`;
  }

  function settingsForm(cfg) {
    const sw = Object.entries(SWITCHES).map(([k, label]) => `<label class="hw-row"><input type="checkbox" data-hw-set="${k}" ${cfg[k] ? 'checked' : ''}> ${E(label)}</label>`).join('');
    const kinds = Object.entries(KINDS).map(([k, label]) => `<tr><td>${E(label)}</td><td><input type="checkbox" data-hw-gov="${k}" ${(cfg.govern || {})[k] ? 'checked' : ''}></td>
<td><input type="number" min="0" step="1" data-hw-cap="${k}" value="${E((cfg.caps || {})[k])}"></td></tr>`).join('');
    const nums = Object.entries(NUMBERS).map(([k, label]) => `<label class="hw-row" style="justify-content:space-between">${E(label)} <input type="number" min="0" step="any" data-hw-num="${k}" value="${E(cfg[k])}"></label>`).join('');
    return `<div class="hw-grid"><div class="hw-card"><h4>Switches</h4>${sw}</div>
<div class="hw-card"><h4>Governed work</h4><table class="hw-table"><tr><th>Kind</th><th>Governed</th><th>Cap</th></tr>${kinds}</table></div>
<div class="hw-card"><h4>Limits</h4>${nums}</div></div><div class="hw-row"><button class="btn" data-hw="save">Save settings</button></div>`;
  }

  function render() {
    const root = $('hw-root');
    if (!root || !data) return;
    const st = data.latest || {};
    const verdict = st.verdict || {};
    const s = st.snapshot || {};
    const ack = (st.whea || {}).ack_ts;
    const caps = Object.entries(verdict.caps || {}).map(([k, n]) => `<tr><td>${E(KINDS[k] || k)}</td><td>${n === 0 ? '<b>wait</b>' : E(n)}</td></tr>`).join('');
    const slot = data.slot;
    root.innerHTML = `<div class="hw-card" style="margin-bottom:12px"><div class="hw-row"><span class="hw-chip ${E(verdict.level || '')}">${E(verdict.level || 'unknown')}</span>
<span class="hw-muted">reading ${E(when(st.ts))} · monitor ${data.running ? 'running' : 'stopped'}</span><button class="btn" data-hw="sample">Read now</button></div>
${(verdict.reasons || []).length ? `<ul>${verdict.reasons.map((r) => `<li>${E(r)}</li>`).join('')}</ul>` : '<p class="hw-muted">Every reading is inside its limits.</p>'}
<p class="hw-muted">What this does: caps parallel workers, keeps heavy jobs from stacking, pauses guarded jobs and alerts on the first hardware error. What it cannot do: stop a faulty CPU from raising a machine check - a fatal one still crashes the machine; the BIOS/hardware check is the owner's.</p></div>
${readings(s)}
<div class="hw-grid" style="margin-top:12px"><div class="hw-card"><h4>Caps now</h4><table class="hw-table">${caps}</table></div>
<div class="hw-card"><h4>Heavy-work slot</h4>${slot ? `<div>${E(slot.kind)}: ${E(slot.label || '')} (pid ${E(slot.pid)}, since ${E(when(slot.since))})</div><div class="hw-row"><button class="btn" data-hw="release">Free the slot</button></div>` : '<p class="hw-muted">Free.</p>'}</div></div>
<div class="hw-card" style="margin-top:12px"><h4>WHEA hardware errors</h4><div class="hw-row"><span class="hw-muted">Acknowledged up to ${E(when(ack))}. A new event pauses heavy work until it is acknowledged here.</span>
<button class="btn" data-hw="ack">Acknowledge</button></div>${wheaTable(st.events || [], ack)}</div>
<div class="hw-card" style="margin-top:12px"><h4>Recent readings</h4><div class="hw-scroll"><table class="hw-table"><tr><th>When</th><th>Level</th><th>CPU %</th><th>CPU C</th><th>GPU %</th><th>GPU C</th><th>VRAM GB</th><th>Free RAM GB</th><th>Commit %</th></tr>
${(data.history || []).slice(-12).reverse().map((h) => `<tr><td>${E(new Date(h.ts * 1000).toLocaleTimeString())}</td><td>${E(h.level)}</td><td>${E(h.cpu_pct ?? '')}</td><td>${E(h.cpu_c ?? '-')}</td><td>${E(h.gpu_pct ?? '')}</td><td>${E(h.gpu_c ?? '-')}</td><td>${E(h.vram_gb ?? '')}</td><td>${E(h.ram_free_gb ?? '')}</td><td>${E(h.commit_pct ?? '')}</td></tr>`).join('')}</table></div></div>
<h3 style="margin-top:16px">Settings</h3>${settingsForm(data.settings || {})}`;
  }

  async function load() {
    if (!$('hw-root') || loading) return;
    const focused = document.activeElement;
    if (focused && focused.tagName === 'INPUT' && $('hw-root').contains(focused)) return;   // keep unsaved edits
    loading = true;
    try {
      data = await api('/api/hwguard/state');
      render();
    } catch (e) {
      $('hw-root').innerHTML = `<p class="cardnote">Hardware guard unavailable: ${E(errText(e))}</p>`;
    } finally {
      loading = false;
    }
  }

  async function act(what) {
    const root = $('hw-root');
    try {
      if (what === 'sample') data = await api('/api/hwguard/sample', { method: 'POST', body: '{}' });
      if (what === 'ack') {
        const note = prompt('Acknowledge the WHEA events so far (heavy work may resume). A note for the record:', '');
        if (note === null) return;
        data = await api('/api/hwguard/ack', { method: 'POST', body: JSON.stringify({ note }) });
        toast('WHEA events acknowledged');
      }
      if (what === 'release') {
        if (!confirm('Free the heavy-work slot? Only do this when the job holding it is stuck.')) return;
        await api('/api/hwguard/slot/release', { method: 'POST', body: '{}' });
        toast('Slot freed');
        return load();
      }
      if (what === 'save') {
        const body = { govern: {}, caps: {} };
        root.querySelectorAll('[data-hw-set]').forEach((el) => { body[el.dataset.hwSet] = el.checked; });
        root.querySelectorAll('[data-hw-gov]').forEach((el) => { body.govern[el.dataset.hwGov] = el.checked; });
        root.querySelectorAll('[data-hw-cap]').forEach((el) => { body.caps[el.dataset.hwCap] = Number(el.value); });
        root.querySelectorAll('[data-hw-num]').forEach((el) => { body[el.dataset.hwNum] = Number(el.value); });
        await api('/api/hwguard/settings', { method: 'PUT', body: JSON.stringify(body) });
        toast('Hardware guard settings saved');
        return load();
      }
      render();
    } catch (e) {
      toast(errText(e), 'error');
    }
  }

  function init() {
    const root = $('hw-root');
    if (!root) return;
    css();
    root.addEventListener('click', (ev) => {
      const el = ev.target.closest('[data-hw]');
      if (el) act(el.dataset.hw);
    });
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'hardware') load(); };
    window.addEventListener('hashchange', onHash);
    onHash();
    if ('IntersectionObserver' in window) {
      new IntersectionObserver((es) => es.forEach((e) => { visible = e.isIntersecting; if (visible) load(); })).observe(root);
    }
    setInterval(() => { if (visible && !document.hidden) load(); }, REFRESH_MS);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
