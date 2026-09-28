// Hermes Manager panel: Hermes Manager from ABP. Install and update it from its repo, run its bridge, see Hermes's
// health and gateway, drive its window remotely (sections, every element on screen, clicks, fields, screenshots),
// and run any of its operations from forms built from their own schemas.
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_hermes_manager.py fails if the two differ. It uses the page's own api(), esc() and showToast(),
// and draws into #hmp-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { const d = JSON.parse(m).detail; m = (d && d.error) || d || m; } catch (_) {} return m; };
  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });
  const call = (operation, args) => post('/api/hermes-manager/call', { operation, args: args || {} }).then((r) => r.result);
  const TABS = [['overview', 'Overview'], ['gateway', 'Gateway'], ['window', 'Window'], ['all', 'All features']];
  const st = { tab: 'overview', status: null, ops: null, op: null, group: '', live: null, elements: null };
  try { st.tab = localStorage.getItem('hmp-tab') || 'overview'; } catch (_) {}

  function css() {
    if ($('hmp-css')) return;
    const s = document.createElement('style');
    s.id = 'hmp-css';
    s.textContent = `.hmp-tabs{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0 12px;border-bottom:1px solid var(--line)}
.hmp-tabs button{background:none;border:0;border-bottom:2px solid transparent;color:var(--ink-soft);padding:8px 12px;font:inherit;font-weight:600;cursor:pointer}
.hmp-tabs button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--accent)}
.hmp-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:12px}
.hmp-card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--surface);min-width:0}
.hmp-card h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.hmp-muted{color:var(--muted);font-size:12px}.hmp-mono{font-family:var(--font-mono,monospace);font-size:12px;word-break:break-all}
.hmp-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.hmp-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.hmp-chip.on{background:var(--good-soft);color:var(--good,#1f9d55)}.hmp-chip.warn{background:var(--warning-soft);color:#8a5c00}.hmp-chip.bad{background:var(--danger-soft,#fde8e8);color:var(--danger,#c0392b)}
.hmp-pre{white-space:pre-wrap;max-height:50vh;overflow:auto;background:var(--surface-2);border-radius:6px;padding:8px;font-family:var(--font-mono,monospace);font-size:11.5px}
.hmp-form{display:grid;gap:8px}.hmp-form label{display:grid;gap:3px;font-size:12px;color:var(--ink-soft)}.hmp-form label.chk{display:flex;gap:6px;align-items:center}
#hmp-root input,#hmp-root select,#hmp-root textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%}
.hmp-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:8px}
.hmp-ops{margin:0;padding:0;max-height:60vh;overflow:auto}.hmp-ops li{padding:4px 0;border-bottom:1px solid var(--line);cursor:pointer;list-style:none}.hmp-ops li.sel{background:var(--accent-soft)}
.hmp-split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.4fr);gap:12px}@media (max-width:1000px){.hmp-split{grid-template-columns:1fr}}
.hmp-shot{width:100%;height:auto;border:1px solid var(--line);border-radius:8px;background:#0b0e14}
.hmp-el{display:grid;grid-template-columns:auto 1fr auto;gap:6px;align-items:center;padding:4px 0;border-bottom:1px solid var(--line);font-size:12px}
.hmp-scroll{overflow:auto;max-height:60vh}`;
    document.head.appendChild(s);
  }

  function field(name, s, required) {
    s = s || {};
    const id = 'hmp-f-' + name;
    const label = E(name) + (required ? ' *' : '') + (s.description ? ` <span class="hmp-muted">${E(s.description)}</span>` : '');
    const def = s.default;
    if (s.enum) return `<label>${label}<select id="${id}" data-name="${E(name)}" data-kind="enum">${s.enum.map((v) => `<option ${v === def ? 'selected' : ''}>${E(v)}</option>`).join('')}</select></label>`;
    if (s.type === 'boolean') return `<label class="chk"><input type="checkbox" id="${id}" data-name="${E(name)}" data-kind="bool" ${def ? 'checked' : ''}>${label}</label>`;
    if (s.type === 'integer' || s.type === 'number') return `<label>${label}<input type="number" id="${id}" data-name="${E(name)}" data-kind="${s.type}" value="${def == null ? '' : E(def)}"></label>`;
    if (s.type === 'object' || s.type === 'array' || !s.type) return `<label>${label}<textarea rows="3" id="${id}" data-name="${E(name)}" data-kind="json" placeholder="JSON">${def == null ? '' : E(JSON.stringify(def, null, 1))}</textarea></label>`;
    return `<label>${label}<input id="${id}" data-name="${E(name)}" data-kind="string" value="${def == null ? '' : E(def)}"></label>`;
  }

  function readForm(root) {
    const args = {};
    root.querySelectorAll('[data-name]').forEach((el) => {
      const k = el.dataset.name, kind = el.dataset.kind;
      if (kind === 'bool') { args[k] = el.checked; return; }
      const v = el.value.trim();
      if (v === '') return;
      args[k] = kind === 'integer' ? parseInt(v, 10) : kind === 'number' ? parseFloat(v) : kind === 'json' ? JSON.parse(v) : v;
    });
    return args;
  }

  function shell() {
    const root = $('hmp-root');
    if (!root) return null;
    if (!$('hmp-body')) {
      root.innerHTML = `<div class="hmp-tabs" role="tablist">${TABS.map(([k, t]) => `<button role="tab" data-tab="${k}">${t}</button>`).join('')}</div><div id="hmp-body"></div>`;
      root.querySelectorAll('.hmp-tabs button').forEach((b) => b.addEventListener('click', () => { st.tab = b.dataset.tab; try { localStorage.setItem('hmp-tab', st.tab); } catch (_) {} stopLive(); render(); }));
    }
    root.querySelectorAll('.hmp-tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === st.tab)));
    return $('hmp-body');
  }

  async function render() {
    css();
    const body = shell();
    if (!body) return;
    try {
      if (st.tab === 'overview') await overview(body);
      else if (!st.status || !st.status.bridge.running) await overview(body, 'Start the bridge first (Overview).');
      else if (st.tab === 'gateway') await gateway(body);
      else if (st.tab === 'window') await windowTab(body);
      else await allFeatures(body);
    } catch (e) { body.innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; }
  }

  async function overview(body, note) {
    st.status = await api('/api/hermes-manager/status');
    const s = st.status, i = s.install, b = s.bridge, h = s.health || {};
    const chip = (ok, on, off) => `<span class="hmp-chip ${ok ? 'on' : 'warn'}">${ok ? on : off}</span>`;
    const jobs = (s.jobs || []).map((j) => `<div class="hmp-muted">${E(j.kind)}: ${E((j.log || []).slice(-1)[0] || '…')}</div>`).join('');
    body.innerHTML = `${note ? `<p class="cardnote">${E(note)}</p>` : ''}<div class="hmp-grid">
<div class="hmp-card"><h4>Program</h4>
 <div>${chip(i.installed, 'installed', 'not installed')} ${i.installed ? chip(i.built, 'built', 'not built') : ''} ${i.developer_checkout ? '<span class="hmp-chip">your working copy</span>' : ''}</div>
 <div class="hmp-mono" style="margin:6px 0">${E(i.path)}</div>
 ${i.commit ? `<div class="hmp-muted">${E(i.branch)} @ ${E(i.commit)}: ${E(i.subject)}</div>` : ''}
 ${i.behind ? `<div><span class="hmp-chip warn">${i.behind} update(s) waiting</span></div>` : ''}
 ${i.changed_files ? `<div class="hmp-muted">${i.changed_files} uncommitted change(s)</div>` : ''}
 <div class="hmp-muted">Hermes: ${E(i.hermes_home || 'not found')}</div>
 <div class="hmp-row" style="margin-top:8px"><button class="btn" data-a="setup">${i.installed ? 'Rebuild' : 'Install'}</button>
 <button class="btn" data-a="update" ${i.installed ? '' : 'disabled'}>Update from repo</button>
 <button class="btn" data-a="mcp" ${i.hermes_python ? '' : 'disabled'}>Add its MCP server to ABP</button></div>${jobs}</div>
<div class="hmp-card"><h4>Bridge &amp; window</h4>
 <div>${chip(b.running, 'bridge running', 'bridge stopped')} ${b.running ? chip((s.window || {}).attached, 'window open', 'window closed') : ''}</div>
 ${b.running ? `<div class="hmp-mono" style="margin:6px 0">${E(b.url)} · pid ${E(b.pid)} · started by ${E(b.owner || '?')}</div>` : ''}
 <div class="hmp-row" style="margin-top:8px">${b.running ? `<button class="btn" data-a="stop" ${['abp', 'mcp', 'bridge'].includes(b.owner) ? '' : 'disabled title="the window owns it"'}>Stop bridge</button>` : `<button class="btn" data-a="start" ${i.installed && i.hermes_python ? '' : 'disabled'}>Start bridge</button>`}
 <button class="btn" data-a="window" ${b.running && i.built ? '' : 'disabled'}>Open window</button></div></div>
<div class="hmp-card"><h4>Hermes</h4>${b.running ? `<div class="hmp-pre">${E(JSON.stringify(h, null, 1)).slice(0, 3000)}</div>` : '<p class="hmp-muted">Start the bridge to see Hermes.</p>'}</div></div>`;
    body.querySelectorAll('[data-a]').forEach((btn) => btn.addEventListener('click', async () => {
      const a = btn.dataset.a;
      btn.disabled = true;
      const paths = { setup: '/api/hermes-manager/setup', update: '/api/hermes-manager/update', start: '/api/hermes-manager/bridge/start', stop: '/api/hermes-manager/bridge/stop', window: '/api/hermes-manager/window', mcp: '/api/hermes-manager/mcp' };
      try { await post(paths[a], {}); toast({ setup: 'Building Hermes Manager…', update: 'Updating…', start: 'Bridge started', stop: 'Bridge stopped', window: 'Window open', mcp: 'MCP server added' }[a], 'success'); } catch (e) { toast(errText(e), 'error'); }
      render();
    }));
  }

  async function gateway(body) {
    const g = await call('gateway.status');
    body.innerHTML = `<div class="hmp-grid"><div class="hmp-card"><h4>Gateway</h4>
<div>${g.running ? '<span class="hmp-chip on">running</span>' : '<span class="hmp-chip warn">stopped</span>'} ${g.drain_requested ? '<span class="hmp-chip warn">draining</span>' : ''}</div>
<div class="hmp-row" style="margin-top:8px">${['start', 'stop', 'restart'].map((a) => `<button class="btn" data-g="${a}">${a}</button>`).join('')}<button class="btn" data-g="drain">drain</button></div></div>
<div class="hmp-card"><h4>Details</h4><div class="hmp-pre">${E(JSON.stringify(g, null, 1))}</div></div></div>`;
    body.querySelectorAll('[data-g]').forEach((btn) => btn.addEventListener('click', async () => {
      const a = btn.dataset.g;
      if (!confirm(`${a} the Hermes gateway?`)) return;
      try { await (a === 'drain' ? call('gateway.drain', {}) : call('gateway.lifecycle', { action: a })); toast(`gateway: ${a}`, 'success'); } catch (e) { toast(errText(e), 'error'); }
      render();
    }));
  }

  function stopLive() { clearInterval(st.live); st.live = null; }

  async function windowTab(body) {
    const w = await api('/api/hermes-manager/status');
    if (!(w.window || {}).attached) {
      body.innerHTML = `<div class="hmp-card"><h4>The Hermes Manager window</h4><p class="hmp-muted">The window is not open.</p><button class="btn" id="hmp-open">Open the window</button></div>`;
      $('hmp-open').addEventListener('click', async () => { try { await post('/api/hermes-manager/window', {}); render(); } catch (e) { toast(errText(e), 'error'); } });
      return;
    }
    const sections = await call('gui.sections');
    body.innerHTML = `<div class="hmp-split"><div class="hmp-card"><div class="hmp-row" style="justify-content:space-between"><h4>Screen</h4>
<div class="hmp-row"><select id="hmp-section">${sections.map((s) => `<option value="${E(s.section)}" ${s.current ? 'selected' : ''}>${E(s.label)}</option>`).join('')}</select>
<label class="hmp-muted"><input type="checkbox" id="hmp-live"> live</label><button class="btn btn-sm" id="hmp-refresh">Refresh</button></div></div>
<img id="hmp-shot" class="hmp-shot" alt="the Hermes Manager window"></div>
<div class="hmp-card"><h4>On screen</h4><input id="hmp-filter" placeholder="filter" style="width:100%;margin-bottom:6px"><div id="hmp-els" class="hmp-scroll"></div></div></div>`;
    const shot = async () => { try { const s = await call('gui.screenshot', { max_width: 1400 }); $('hmp-shot').src = 'data:image/png;base64,' + s.base64; } catch (e) { stopLive(); toast(errText(e), 'error'); } };
    const load = async () => { st.elements = (await call('gui.inspect', { max: 300 })).elements; draw(); };
    const refresh = async () => { await shot(); await load(); };
    $('hmp-section').addEventListener('change', async (ev) => { await call('gui.open', { section: ev.target.value }); setTimeout(refresh, 300); });
    $('hmp-refresh').addEventListener('click', refresh);
    $('hmp-live').addEventListener('change', (ev) => { stopLive(); if (ev.target.checked) st.live = setInterval(shot, 1500); });
    $('hmp-filter').addEventListener('input', draw);
    await refresh();

    function draw() {
      const f = ($('hmp-filter').value || '').toLowerCase();
      const shown = (st.elements || []).filter((e) => !f || JSON.stringify(e).toLowerCase().includes(f));
      $('hmp-els').innerHTML = shown.map((e, n) => {
        const name = e.label || e.text || e.id;
        let ctl = '';
        if (['button', 'link', 'tab', 'checkbox', 'radio', 'menuitem', 'switch'].includes(e.kind)) ctl = `<button class="btn btn-sm" data-e="${n}" data-do="click" ${e.enabled === false ? 'disabled' : ''}>click</button>`;
        else if (['text', 'textarea'].includes(e.kind)) ctl = `<button class="btn btn-sm" data-e="${n}" data-do="fill">fill…</button>`;
        else if (e.kind === 'select') ctl = `<button class="btn btn-sm" data-e="${n}" data-do="select">choose…</button>`;
        else if (e.kind === 'table') ctl = `<button class="btn btn-sm" data-e="${n}" data-do="read">read</button>`;
        return `<div class="hmp-el"><span class="hmp-chip">${E(e.kind)}</span><span>${E(String(name).slice(0, 90))}${e.value != null && e.value !== '' ? ` = <span class="hmp-mono">${E(e.value)}</span>` : ''}</span>${ctl}</div>`;
      }).join('') || '<p class="hmp-muted">Nothing on screen.</p>';
      $('hmp-els').querySelectorAll('[data-do]').forEach((btn) => btn.addEventListener('click', async () => {
        const e = shown[+btn.dataset.e], target = { id: e.id };
        try {
          if (btn.dataset.do === 'click') await call('gui.click', { target });
          else if (btn.dataset.do === 'fill') { const v = prompt(`Value for ${e.label || e.id}`, e.value || ''); if (v === null) return; await call('gui.fill', { target, value: v }); }
          else if (btn.dataset.do === 'select') { const v = prompt(`Option for ${e.label || e.id}: ${(e.options || []).map((o) => o.text).join(', ')}`, e.value || ''); if (v === null) return; await call('gui.select', { target, option: v }); }
          else { const r = await call('gui.read', { target }); alert(JSON.stringify(r.rows || r.text, null, 1).slice(0, 4000)); return; }
          setTimeout(refresh, 250);
        } catch (err) { toast(errText(err), 'error'); }
      }));
    }
  }

  async function allFeatures(body) {
    if (!st.ops) st.ops = await api('/api/hermes-manager/operations');
    const groups = [...new Set(st.ops.map((o) => o.group))].sort();
    const shown = st.ops.filter((o) => !st.group || o.group === st.group);
    body.innerHTML = `<div class="hmp-split"><div class="hmp-card"><div class="hmp-row"><select id="hmp-group"><option value="">all (${st.ops.length})</option>${groups.map((g) => `<option ${g === st.group ? 'selected' : ''}>${E(g)}</option>`).join('')}</select><input id="hmp-q" placeholder="search" style="flex:1"></div>
<ul class="hmp-ops">${shown.map((o) => `<li data-id="${E(o.id)}" class="${st.op && st.op.id === o.id ? 'sel' : ''}"><b class="hmp-mono">${E(o.id)}</b> ${o.mutating ? '<span class="hmp-chip warn">changes</span>' : ''}<div class="hmp-muted">${E(o.summary)}</div></li>`).join('')}</ul></div>
<div class="hmp-card" id="hmp-op">${st.op ? '' : '<p class="hmp-muted">Pick an operation.</p>'}</div></div>`;
    $('hmp-group').addEventListener('change', (ev) => { st.group = ev.target.value; render(); });
    $('hmp-q').addEventListener('input', (ev) => { const q = ev.target.value.toLowerCase(); body.querySelectorAll('.hmp-ops li').forEach((li) => { li.style.display = li.textContent.toLowerCase().includes(q) ? '' : 'none'; }); });
    body.querySelectorAll('.hmp-ops li').forEach((li) => li.addEventListener('click', () => { st.op = st.ops.find((o) => o.id === li.dataset.id); render(); }));
    if (!st.op) return;
    const o = st.op, box = $('hmp-op'), props = (o.params && o.params.properties) || {}, req = (o.params && o.params.required) || [];
    box.innerHTML = `<h4>${E(o.id)}</h4><p>${E(o.summary)}</p><div class="hmp-form"><div class="hmp-fields">${Object.entries(props).map(([k, s]) => field(k, s, req.includes(k))).join('') || '<p class="hmp-muted">No arguments.</p>'}</div>
<div class="hmp-row"><button class="btn" id="hmp-run">Run</button></div></div><div id="hmp-out"></div>`;
    $('hmp-run').addEventListener('click', async () => {
      let args;
      try { args = readForm(box); } catch (_) { toast('An argument is not valid JSON', 'error'); return; }
      if (o.mutating && !confirm(`${o.id} changes Hermes. Run it?`)) return;
      $('hmp-out').innerHTML = '<p class="hmp-muted">Running…</p>';
      try {
        const r = await call(o.id, args);
        $('hmp-out').innerHTML = r && r.format === 'png' ? `<img class="hmp-shot" alt="result" src="data:image/png;base64,${r.base64}">` : `<div class="hmp-pre">${E(JSON.stringify(r, null, 1))}</div>`;
      } catch (e) { $('hmp-out').innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; }
    });
  }

  function init() {
    const root = $('hmp-root');
    if (!root) return;
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'hermes-manager') render(); else stopLive(); };
    window.addEventListener('hashchange', onHash);
    onHash();
    if ('IntersectionObserver' in window) {
      let seen = false;
      new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting && !seen) { seen = true; render(); } })).observe(root);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
