// VM-Harness panel: VM-Harness from ABP. Install and update it from its repo, run its hub, see and control every VM
// and container it reaches, drive its window remotely (panels, widgets, clicks, screenshots), and run any of its
// operations from forms built from their own schemas.
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_vm_harness.py fails if the two differ. It uses the page's own api(), esc() and showToast(),
// and draws into #vh-root.
(function (api0) {
  'use strict';
  if (typeof api0 !== 'function') return;
  // Which machine this page controls: this one, or a linked server (Peers page) whose ABP lets linked servers control
  // vm-harness (peers.remote_control). Calls to the module's API go through /api/peers/<name>/proxy then.
  let machine = '';
  try { machine = localStorage.getItem('vh-machine') || ''; } catch (_) {}
  const api = (path, opts) => {
    if (!machine || !path.startsWith('/api/vm-harness/')) return api0(path, opts);
    const o = opts || {};
    return api0('/api/peers/' + encodeURIComponent(machine) + '/proxy', { method: 'POST',
      body: JSON.stringify({ method: o.method || 'GET', path, body: o.body ? JSON.parse(o.body) : null }) }).then((r) => r.result);
  };
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { const d = JSON.parse(m).detail; m = (d && d.error) || d || m; } catch (_) {} return m; };
  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });
  const call = (operation, args) => post('/api/vm-harness/call', { operation, args: args || {} }).then((r) => r.result);
  const TABS = [['overview', 'Overview'], ['vms', 'Machines'], ['containers', 'Containers'], ['window', 'Window'], ['all', 'All features'], ['audit', 'Audit']];
  const st = { tab: 'overview', status: null, ops: null, op: null, group: '', panel: '', widgets: null, live: null, jobPoll: null };
  try { st.tab = localStorage.getItem('vh-tab') || 'overview'; } catch (_) {}

  function css() {
    if ($('vh-css')) return;
    const s = document.createElement('style');
    s.id = 'vh-css';
    s.textContent = `.vh-tabs{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0 12px;border-bottom:1px solid var(--line)}
.vh-tabs button{background:none;border:0;border-bottom:2px solid transparent;color:var(--ink-soft);padding:8px 12px;font:inherit;font-weight:600;cursor:pointer}
.vh-tabs button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--accent)}
.vh-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:12px}
.vh-card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--surface);min-width:0}
.vh-card h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.vh-muted{color:var(--muted);font-size:12px}.vh-mono{font-family:var(--font-mono,monospace);font-size:12px;word-break:break-all}
.vh-table{width:100%;border-collapse:collapse;font-size:12.5px}.vh-table th{text-align:left;color:var(--muted);font-weight:600;padding:6px;border-bottom:1px solid var(--line)}
.vh-table td{padding:6px;border-bottom:1px solid var(--line);vertical-align:top}.vh-scroll{overflow:auto;max-width:100%}.vh-scroll.tall{max-height:60vh}
.vh-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.vh-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.vh-chip.on{background:var(--good-soft);color:var(--good,#1f9d55)}.vh-chip.warn{background:var(--warning-soft);color:#8a5c00}.vh-chip.bad{background:var(--danger-soft,#fde8e8);color:var(--danger,#c0392b)}
.vh-pre{white-space:pre-wrap;max-height:50vh;overflow:auto;background:var(--surface-2);border-radius:6px;padding:8px;font-family:var(--font-mono,monospace);font-size:11.5px}
.vh-form{display:grid;gap:8px}.vh-form label{display:grid;gap:3px;font-size:12px;color:var(--ink-soft)}.vh-form label.chk{display:flex;gap:6px;align-items:center}
#vh-root input,#vh-root select,#vh-root textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%}
.vh-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:8px}
.vh-ops{margin:0;padding:0;max-height:60vh;overflow:auto}.vh-ops li{padding:4px 0;border-bottom:1px solid var(--line);cursor:pointer;list-style:none}.vh-ops li.sel{background:var(--accent-soft)}
.vh-split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.4fr);gap:12px}@media (max-width:1000px){.vh-split{grid-template-columns:1fr}}
.vh-shot{width:100%;height:auto;border:1px solid var(--line);border-radius:8px;background:#0f172a}
.vh-w{display:grid;grid-template-columns:auto 1fr auto;gap:6px;align-items:center;padding:4px 0;border-bottom:1px solid var(--line);font-size:12px}`;
    document.head.appendChild(s);
  }

  // ---- forms from JSON Schema ------------------------------------------------------------------------------------------
  function field(name, s, required) {
    s = s || {};
    const id = 'vh-f-' + name;
    const label = E(name) + (required ? ' *' : '') + (s.description ? ` <span class="vh-muted">${E(s.description)}</span>` : '');
    const def = s.default;
    if (s.enum) return `<label>${label}<select id="${id}" data-name="${E(name)}" data-kind="enum">${s.enum.map((v) => `<option ${v === def ? 'selected' : ''}>${E(v)}</option>`).join('')}</select></label>`;
    if (s.type === 'boolean') return `<label class="chk"><input type="checkbox" id="${id}" data-name="${E(name)}" data-kind="bool" ${def ? 'checked' : ''}>${label}</label>`;
    if (s.type === 'integer' || s.type === 'number') return `<label>${label}<input type="number" id="${id}" data-name="${E(name)}" data-kind="${s.type}" value="${def == null ? '' : E(def)}"></label>`;
    if (s.type === 'object' || s.type === 'array' || !s.type)
      return `<label>${label}<textarea rows="3" id="${id}" data-name="${E(name)}" data-kind="json" placeholder="JSON">${def == null ? '' : E(JSON.stringify(def, null, 1))}</textarea></label>`;
    return `<label>${label}<input id="${id}" data-name="${E(name)}" data-kind="string" value="${def == null ? '' : E(def)}"></label>`;
  }

  function readForm(root) {
    const args = {};
    root.querySelectorAll('[data-name]').forEach((el) => {
      const k = el.dataset.name, kind = el.dataset.kind;
      if (kind === 'bool') { args[k] = el.checked; return; }
      const v = el.value.trim();
      if (v === '') return;
      if (kind === 'integer') args[k] = parseInt(v, 10);
      else if (kind === 'number') args[k] = parseFloat(v);
      else if (kind === 'json') args[k] = JSON.parse(v);
      else args[k] = v;
    });
    return args;
  }

  // ---- tabs ------------------------------------------------------------------------------------------------------------
  function shell() {
    const root = $('vh-root');
    if (!root) return null;
    if (!$('vh-body')) {
      root.innerHTML = `<div class="vh-row" style="margin-bottom:6px"><label class="vh-muted">Machine <select id="vh-machine"><option value="">This machine</option></select></label><span id="vh-machine-note" class="vh-muted"></span></div><div class="vh-tabs" role="tablist">${TABS.map(([k, t]) => `<button role="tab" data-tab="${k}">${t}</button>`).join('')}</div><div id="vh-body"></div>`;
      root.querySelectorAll('.vh-tabs button').forEach((b) => b.addEventListener('click', () => { st.tab = b.dataset.tab; try { localStorage.setItem('vh-tab', st.tab); } catch (_) {} stopLive(); render(); }));
    }
    if (!root.dataset.peers) {
      root.dataset.peers = '1';
      api0('/api/peers').then((rows) => {
        const sel = $('vh-machine');
        (rows || []).forEach((r) => { const o = document.createElement('option'); o.value = r.name; o.textContent = r.name; sel.appendChild(o); });
        if (machine && !(rows || []).some((r) => r.name === machine)) machine = '';
        sel.value = machine;
        sel.addEventListener('change', () => {
          machine = sel.value;
          try { localStorage.setItem('vh-machine', machine); } catch (_) {}
          stopLive();
          st.status = null; st.ops = null; st.op = null;
          render();
        });
      }).catch(() => {});
    }
    const note = $('vh-machine-note');
    if (note) note.textContent = machine ? ' controlling ' + machine + ' through its ABP' : '';
    root.querySelectorAll('.vh-tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === st.tab)));
    return $('vh-body');
  }

  async function render() {
    css();
    const body = shell();
    if (!body) return;
    try {
      if (st.tab === 'overview') await overview(body);
      else if (!st.status || !st.status.hub.running) await overview(body, 'Start VM-Harness first (Overview).');
      else if (st.tab === 'vms') await vms(body);
      else if (st.tab === 'containers') await containers(body);
      else if (st.tab === 'window') await windowTab(body);
      else if (st.tab === 'all') await allFeatures(body);
      else if (st.tab === 'audit') await auditTab(body);
    } catch (e) {
      body.innerHTML = `<p class="cardnote">${E(errText(e))}</p>`;
    }
  }

  async function overview(body, note) {
    st.status = await api('/api/vm-harness/status');
    const s = st.status, i = s.install, h = s.hub, b = s.backends || {};
    const chip = (ok, on, off) => `<span class="vh-chip ${ok ? 'on' : 'warn'}">${ok ? on : off}</span>`;
    const hv = Object.entries(b.hypervisors || {}).map(([n, x]) => `<tr><td>${E(n)}</td><td>${chip(x.available, 'works', 'no')}</td><td class="vh-muted">${E(x.version || x.reason || '')}</td></tr>`).join('');
    const ce = Object.entries(b.containers || {}).map(([n, x]) => `<tr><td>${E(n)}</td><td>${chip(x.available, 'works', 'no')}</td><td class="vh-muted">${E(x.reason || '')}</td></tr>`).join('');
    const jobs = (s.jobs || []).map((j) => `<div class="vh-muted">${E(j.kind)}: ${E((j.log || []).slice(-1)[0] || '…')}</div>`).join('');
    body.innerHTML = `${note ? `<p class="cardnote">${E(note)}</p>` : ''}<div class="vh-grid">
<div class="vh-card"><h4>Program</h4>
 <div>${chip(i.installed, 'installed', 'not installed')} ${i.developer_checkout ? '<span class="vh-chip">your working copy</span>' : ''}</div>
 <div class="vh-mono" style="margin:6px 0">${E(i.path)}</div>
 ${i.commit ? `<div class="vh-muted">${E(i.branch)} @ ${E(i.commit)}: ${E(i.subject)}</div>` : ''}
 ${i.behind ? `<div><span class="vh-chip warn">${i.behind} update(s) waiting</span></div>` : ''}
 ${i.changed_files ? `<div class="vh-muted">${i.changed_files} uncommitted change(s) in the checkout</div>` : ''}
 <div class="vh-row" style="margin-top:8px">
  <button class="btn" data-a="setup">${i.installed ? 'Reinstall dependencies' : 'Install'}</button>
  <button class="btn" data-a="update" ${i.installed ? '' : 'disabled'}>Update from repo</button>
  <button class="btn" data-a="mcp" ${i.installed ? '' : 'disabled'}>Add its MCP server to ABP</button></div>${jobs}</div>
<div class="vh-card"><h4>Hub &amp; window</h4>
 <div>${chip(h.running, 'hub running', 'hub stopped')} ${h.running ? chip(h.window, 'window open', 'window closed') : ''}</div>
 ${h.running ? `<div class="vh-mono" style="margin:6px 0">${E(h.url)} · pid ${E(h.pid)} · v${E(h.version)}${h.remote ? ' · remote' : ''}</div>` : ''}
 <div class="vh-row" style="margin-top:8px">
  ${h.running ? '<button class="btn" data-a="stop">Stop hub</button>' : `<button class="btn" data-a="start" ${i.venv ? '' : 'disabled'}>Start hub</button>`}
  <button class="btn" data-a="window" ${h.running ? '' : 'disabled'}>Open window</button></div></div>
<div class="vh-card"><h4>Hypervisors</h4>${hv ? `<table class="vh-table">${hv}</table>` : '<p class="vh-muted">Start the hub to check.</p>'}</div>
<div class="vh-card"><h4>Containers</h4>${ce ? `<table class="vh-table">${ce}</table>` : '<p class="vh-muted">Start the hub to check.</p>'}</div></div>`;
    body.querySelectorAll('[data-a]').forEach((btn) => btn.addEventListener('click', async () => {
      const a = btn.dataset.a;
      btn.disabled = true;
      try {
        const paths = { setup: '/api/vm-harness/setup', update: '/api/vm-harness/update', start: '/api/vm-harness/hub/start', stop: '/api/vm-harness/hub/stop', window: '/api/vm-harness/window', mcp: '/api/vm-harness/mcp' };
        await post(paths[a], {});
        toast({ setup: 'Installing VM-Harness…', update: 'Updating VM-Harness…', start: 'Hub started', stop: 'Hub stopped', window: 'Window open', mcp: 'MCP server added' }[a], 'success');
        if (a === 'setup' || a === 'update') pollJobs();
      } catch (e) { toast(errText(e), 'error'); }
      render();
    }));
  }

  function pollJobs() {
    clearInterval(st.jobPoll);
    st.jobPoll = setInterval(async () => {
      try {
        const jobs = await api('/api/vm-harness/jobs');
        if (!jobs.some((j) => j.state === 'running')) {
          clearInterval(st.jobPoll);
          const last = jobs[0];
          if (last) toast(`${last.kind}: ${last.state}${last.error ? ' — ' + last.error : ''}`, last.state === 'done' ? 'success' : 'error');
        }
        if (st.tab === 'overview') render();
      } catch (_) { clearInterval(st.jobPoll); }
    }, 2500);
  }

  // ---- machines ----------------------------------------------------------------------------------------------------------
  async function vms(body) {
    body.innerHTML = '<p class="vh-muted">Asking every hypervisor…</p>';
    const list = await api('/api/vm-harness/vms');
    const acts = { running: ['pause', 'stop', 'reboot'], paused: ['resume', 'stop'], suspended: ['start'], stopped: ['start'] };
    const rows = list.map((v, n) => {
      if (!v.name) return `<tr><td colspan="4" class="vh-muted">${E(v.backend)}: ${E(v.error)}</td></tr>`;
      const s = v.status || {};
      const btns = (acts[v.state] || []).map((a) => `<button class="btn btn-sm" data-vm="${n}" data-op="${a}">${a}</button>`).join(' ');
      return `<tr><td><b>${E(v.name)}</b><div class="vh-muted">${E(v.backend)}</div></td>
<td><span class="vh-chip ${v.state === 'running' ? 'on' : v.state === 'error' ? 'bad' : ''}">${E(v.state)}</span>${v.error ? `<div class="vh-muted">${E(v.error)}</div>` : ''}</td>
<td class="vh-muted">${s.cpus_allocated ? E(s.cpus_allocated) + ' CPU · ' : ''}${s.ram_allocated_mb ? E(s.ram_allocated_mb) + ' MB' : ''}${s.last_error ? '<br>' + E(s.last_error) : ''}</td>
<td><div class="vh-row">${btns}<button class="btn btn-sm" data-vm="${n}" data-op="snapshots">snapshots</button>${v.state === 'running' ? `<button class="btn btn-sm" data-vm="${n}" data-op="screenshot">screen</button>` : ''}</div></td></tr>`;
    }).join('');
    body.innerHTML = `<div class="vh-card"><div class="vh-row" style="justify-content:space-between"><h4>Virtual machines</h4><button class="btn btn-sm" id="vh-vm-refresh">Refresh</button></div>
<div class="vh-scroll"><table class="vh-table"><tr><th>Machine</th><th>State</th><th>Resources</th><th></th></tr>${rows || '<tr><td colspan="4" class="vh-muted">No VMs found.</td></tr>'}</table></div></div><div id="vh-vm-detail"></div>`;
    $('vh-vm-refresh').addEventListener('click', render);
    body.querySelectorAll('[data-op]').forEach((btn) => btn.addEventListener('click', async () => {
      const v = list[+btn.dataset.vm], op = btn.dataset.op, detail = $('vh-vm-detail');
      const args = { name: v.name, backend: v.backend };
      btn.disabled = true;
      try {
        if (op === 'snapshots') {
          const snaps = await call('vm.snapshot.list', args);
          detail.innerHTML = `<div class="vh-card" style="margin-top:12px"><h4>Snapshots of ${E(v.name)}</h4>${snaps.length ? `<table class="vh-table">${snaps.map((s) => `<tr><td>${E(s.name)}</td><td class="vh-muted">${E(s.created_at)}</td></tr>`).join('')}</table>` : '<p class="vh-muted">None.</p>'}</div>`;
        } else if (op === 'screenshot') {
          const shot = await call('vm.screenshot', args);
          detail.innerHTML = `<div class="vh-card" style="margin-top:12px"><h4>${E(v.name)}'s screen</h4><img class="vh-shot" alt="screen of ${E(v.name)}" src="data:image/${E(shot.format)};base64,${shot.base64}"></div>`;
        } else {
          if (op === 'stop' && !confirm(`Stop ${v.name}? The guest is asked to shut down.`)) return;
          await call('vm.' + op, args);
          toast(`${v.name}: ${op} done`, 'success');
          render();
        }
      } catch (e) { toast(errText(e), 'error'); } finally { btn.disabled = false; }
    }));
  }

  // ---- containers --------------------------------------------------------------------------------------------------------
  async function containers(body) {
    body.innerHTML = '<p class="vh-muted">Asking Docker…</p>';
    let list = [];
    try { list = await call('container.list', { all: true }); } catch (e) { body.innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; return; }
    const rows = list.map((c, n) => {
      const running = /running|up/i.test(c.status || '');
      return `<tr><td><b>${E(c.name)}</b><div class="vh-muted vh-mono">${E(String(c.id || '').slice(0, 12))}</div></td><td class="vh-muted">${E(c.image)}</td>
<td><span class="vh-chip ${running ? 'on' : ''}">${E(c.status)}</span></td>
<td><div class="vh-row">${running ? `<button class="btn btn-sm" data-c="${n}" data-op="stop">stop</button><button class="btn btn-sm" data-c="${n}" data-op="restart">restart</button>` : `<button class="btn btn-sm" data-c="${n}" data-op="start">start</button>`}<button class="btn btn-sm" data-c="${n}" data-op="logs">logs</button></div></td></tr>`;
    }).join('');
    body.innerHTML = `<div class="vh-card"><h4>Docker containers</h4><div class="vh-scroll tall"><table class="vh-table"><tr><th>Container</th><th>Image</th><th>Status</th><th></th></tr>${rows}</table></div></div><div id="vh-c-detail"></div>`;
    body.querySelectorAll('[data-op]').forEach((btn) => btn.addEventListener('click', async () => {
      const c = list[+btn.dataset.c], op = btn.dataset.op;
      try {
        if (op === 'logs') {
          const logs = await call('container.logs', { container_id: c.id || c.name, tail: 200 });
          $('vh-c-detail').innerHTML = `<div class="vh-card" style="margin-top:12px"><h4>${E(c.name)} logs</h4><div class="vh-pre">${E(typeof logs === 'string' ? logs : JSON.stringify(logs, null, 1))}</div></div>`;
        } else {
          await call('container.' + op, { container_id: c.id || c.name });
          toast(`${c.name}: ${op} done`, 'success');
          render();
        }
      } catch (e) { toast(errText(e), 'error'); }
    }));
  }

  // ---- the window ------------------------------------------------------------------------------------------------------
  function stopLive() { clearInterval(st.live); st.live = null; }

  async function windowTab(body) {
    const gs = await call('gui.status');
    if (!gs.attached) {
      body.innerHTML = `<div class="vh-card"><h4>The VM-Harness window</h4><p class="vh-muted">The window is not open. Open it to see and drive it from here.</p><button class="btn" id="vh-open">Open the window</button></div>`;
      $('vh-open').addEventListener('click', async () => { try { await post('/api/vm-harness/window', {}); render(); } catch (e) { toast(errText(e), 'error'); } });
      return;
    }
    const panels = await call('gui.panels');
    const cur = panels.find((p) => p.current);
    st.panel = st.panel || (cur && cur.name) || 'dashboard';
    body.innerHTML = `<div class="vh-split"><div class="vh-card"><div class="vh-row" style="justify-content:space-between"><h4>Screen</h4>
<div class="vh-row"><select id="vh-panel">${panels.map((p) => `<option value="${E(p.name)}" ${p.name === st.panel ? 'selected' : ''}>${E(p.label)}</option>`).join('')}</select>
<label class="vh-muted"><input type="checkbox" id="vh-live"> live</label><button class="btn btn-sm" id="vh-shot-btn">Refresh</button></div></div>
<img id="vh-shot" class="vh-shot" alt="the VM-Harness window"></div>
<div class="vh-card"><h4>Widgets on this panel</h4><input id="vh-wfilter" placeholder="filter" style="width:100%;margin-bottom:6px"><div id="vh-widgets" class="vh-scroll tall"></div></div></div>`;
    const shot = async () => { try { const s = await call('gui.screenshot', { max_width: 1400 }); $('vh-shot').src = 'data:image/png;base64,' + s.base64; } catch (e) { toast(errText(e), 'error'); stopLive(); } };
    const loadWidgets = async () => {
      const r = await call('gui.inspect', { panel: st.panel });
      st.widgets = r.widgets;
      drawWidgets();
    };
    $('vh-panel').addEventListener('change', async (ev) => { st.panel = ev.target.value; await call('gui.open', { panel: st.panel }); await shot(); await loadWidgets(); });
    $('vh-shot-btn').addEventListener('click', async () => { await shot(); await loadWidgets(); });
    $('vh-live').addEventListener('change', (ev) => { stopLive(); if (ev.target.checked) st.live = setInterval(shot, 1500); });
    $('vh-wfilter').addEventListener('input', drawWidgets);
    await call('gui.open', { panel: st.panel });
    await shot();
    await loadWidgets();
  }

  function drawWidgets() {
    const box = $('vh-widgets');
    if (!box || !st.widgets) return;
    const f = ($('vh-wfilter').value || '').toLowerCase();
    const shown = st.widgets.filter((w) => !f || JSON.stringify(w).toLowerCase().includes(f));
    box.innerHTML = shown.map((w, n) => {
      const name = w.label || w.text || w.id;
      const val = w.value != null && w.value !== '' ? ` = <span class="vh-mono">${E(typeof w.value === 'object' ? JSON.stringify(w.value) : w.value)}</span>` : '';
      let ctl = '';
      if (['button', 'checkbox', 'radio'].includes(w.kind)) ctl = `<button class="btn btn-sm" data-w="${n}" data-do="click" ${w.enabled ? '' : 'disabled'}>click</button>`;
      else if (['text', 'textarea', 'number', 'combo', 'datetime', 'slider'].includes(w.kind)) ctl = `<button class="btn btn-sm" data-w="${n}" data-do="set" ${w.enabled ? '' : 'disabled'}>set…</button>`;
      else if (['tabs', 'list', 'table', 'tree'].includes(w.kind)) ctl = `<button class="btn btn-sm" data-w="${n}" data-do="read">read</button>`;
      return `<div class="vh-w"><span class="vh-chip">${E(w.kind)}</span><span>${E(String(name).slice(0, 80))}${val}</span>${ctl}</div>`;
    }).join('') || '<p class="vh-muted">No widgets.</p>';
    const ws = shown;
    box.querySelectorAll('[data-do]').forEach((btn) => btn.addEventListener('click', async () => {
      const w = ws[+btn.dataset.w], target = { panel: st.panel, id: w.id };
      try {
        if (btn.dataset.do === 'click') await call('gui.click', { target });
        else if (btn.dataset.do === 'set') {
          const v = prompt(`New value for ${w.label || w.text || w.id}` + (w.items ? `\n(one of: ${w.items.join(', ')})` : ''), w.value == null ? '' : String(w.value));
          if (v === null) return;
          await call(w.kind === 'combo' && w.items ? 'gui.select' : 'gui.set', w.kind === 'combo' && w.items ? { target, item: v } : { target, value: w.kind === 'number' || w.kind === 'slider' ? Number(v) : v });
        } else {
          const r = await call('gui.read', { target, max_rows: 200 });
          alert(JSON.stringify(r.table || r.items || r.value || r, null, 1).slice(0, 4000));
          return;
        }
        const s = await call('gui.screenshot', { max_width: 1400 });
        $('vh-shot').src = 'data:image/png;base64,' + s.base64;
        const r = await call('gui.inspect', { panel: st.panel });
        st.widgets = r.widgets;
        drawWidgets();
      } catch (e) { toast(errText(e), 'error'); }
    }));
  }

  // ---- all features ----------------------------------------------------------------------------------------------------
  async function allFeatures(body) {
    if (!st.ops) st.ops = await api('/api/vm-harness/operations');
    const groups = [...new Set(st.ops.map((o) => o.group))].sort();
    const shown = st.ops.filter((o) => !st.group || o.group === st.group);
    body.innerHTML = `<div class="vh-split"><div class="vh-card"><div class="vh-row"><select id="vh-group"><option value="">all groups (${st.ops.length})</option>${groups.map((g) => `<option ${g === st.group ? 'selected' : ''}>${E(g)}</option>`).join('')}</select><input id="vh-q" placeholder="search" style="flex:1"></div>
<ul class="vh-ops" id="vh-ops">${shown.map((o) => `<li data-id="${E(o.id)}" class="${st.op && st.op.id === o.id ? 'sel' : ''}"><b class="vh-mono">${E(o.id)}</b> ${o.destructive ? '<span class="vh-chip bad">destructive</span>' : o.mutating ? '<span class="vh-chip warn">changes</span>' : ''}<div class="vh-muted">${E(o.summary)}</div></li>`).join('')}</ul></div>
<div class="vh-card" id="vh-op">${st.op ? '' : '<p class="vh-muted">Pick an operation.</p>'}</div></div>`;
    $('vh-group').addEventListener('change', (ev) => { st.group = ev.target.value; render(); });
    $('vh-q').addEventListener('input', (ev) => { const q = ev.target.value.toLowerCase(); body.querySelectorAll('#vh-ops li').forEach((li) => { li.style.display = li.textContent.toLowerCase().includes(q) ? '' : 'none'; }); });
    body.querySelectorAll('#vh-ops li').forEach((li) => li.addEventListener('click', () => { st.op = st.ops.find((o) => o.id === li.dataset.id); render(); }));
    if (st.op) drawOp();
  }

  function drawOp() {
    const o = st.op, box = $('vh-op');
    const props = (o.params && o.params.properties) || {}, req = (o.params && o.params.required) || [];
    box.innerHTML = `<h4>${E(o.id)}</h4><p>${E(o.summary)}</p>${o.needs === 'gui' ? '<p class="vh-muted">Needs the window open.</p>' : ''}
<div class="vh-form"><div class="vh-fields">${Object.entries(props).map(([k, s]) => field(k, s, req.includes(k))).join('') || '<p class="vh-muted">No arguments.</p>'}</div>
<div class="vh-row"><button class="btn" id="vh-run">Run</button>${o.destructive ? '<span class="vh-chip bad">cannot be undone</span>' : ''}</div></div><div id="vh-out"></div>`;
    $('vh-run').addEventListener('click', async () => {
      let args;
      try { args = readForm(box); } catch (e) { toast('An argument is not valid JSON', 'error'); return; }
      if (o.destructive && !confirm(`${o.id} deletes or overwrites something that cannot be recovered. Run it?`)) return;
      const out = $('vh-out');
      out.innerHTML = '<p class="vh-muted">Running…</p>';
      try {
        const r = await call(o.id, args);
        out.innerHTML = r && r.format === 'png' && r.base64 ? `<img class="vh-shot" alt="result" src="data:image/png;base64,${r.base64}">` : `<div class="vh-pre">${E(JSON.stringify(r, null, 1))}</div>`;
      } catch (e) { out.innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; }
    });
  }

  // ---- audit -------------------------------------------------------------------------------------------------------------
  async function auditTab(body) {
    const rows = await api('/api/vm-harness/audit?limit=200');
    body.innerHTML = `<div class="vh-card"><h4>Everything that changed something (VM-Harness's own log)</h4><div class="vh-scroll tall"><table class="vh-table"><tr><th>When</th><th>Operation</th><th>By</th><th></th></tr>${rows.map((r) => `<tr><td class="vh-muted">${E(new Date(r.ts * 1000).toLocaleString())}</td><td class="vh-mono">${E(r.operation)}<div class="vh-muted">${E(JSON.stringify(r.args))}</div></td><td>${E(r.client)}</td><td>${r.ok ? '<span class="vh-chip on">ok</span>' : `<span class="vh-chip bad">failed</span><div class="vh-muted">${E(r.error)}</div>`}</td></tr>`).join('')}</table></div></div>`;
  }

  function init() {
    const root = $('vh-root');
    if (!root) return;
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'vm-harness') render(); else stopLive(); };
    window.addEventListener('hashchange', onHash);
    onHash();
    if ('IntersectionObserver' in window) {
      let seen = false;
      new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting && !seen) { seen = true; render(); } })).observe(root);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
