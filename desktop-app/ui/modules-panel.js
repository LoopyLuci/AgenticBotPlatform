// Modules panel: every separate program ABP installs, updates, builds and drives (VM-Harness, Hermes-Manager,
// TransferDaemon, ModelMistress, Continuum, TridentDroid, BrainBuilder, Wrightspace...), on this machine or a linked
// server. Each card shows its checkout, updates waiting and hub; a module's page shows its toolchain, jobs with live
// logs, its operations as forms built from their schemas, and a conformance check against the module contract.
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_modules.py fails if the two differ. It uses the page's own api(), esc() and showToast(), and draws
// into #mdp-root.
(function (api0) {
  'use strict';
  if (typeof api0 !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { const d = JSON.parse(m).detail; m = (d && d.error) || d || m; } catch (_) {} return typeof m === 'string' ? m : JSON.stringify(m); };
  // Which machine this page controls: this one, or a linked server whose ABP allows peers.remote_control: [modules].
  let machine = '';
  try { machine = localStorage.getItem('mdp-machine') || ''; } catch (_) {}
  const api = (path, opts) => {
    if (!machine || !path.startsWith('/api/modules')) return api0(path, opts);
    const o = opts || {};
    return api0('/api/peers/' + encodeURIComponent(machine) + '/proxy', { method: 'POST',
      body: JSON.stringify({ method: o.method || 'GET', path, body: o.body ? JSON.parse(o.body) : null }) }).then((r) => r.result);
  };
  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });
  const st = { open: '', list: null, status: null, ops: null, op: null, poll: null, query: '' };

  function css() {
    if ($('mdp-css')) return;
    const s = document.createElement('style');
    s.id = 'mdp-css';
    s.textContent = `.mdp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr));gap:12px}
.mdp-card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--surface);min-width:0;display:flex;flex-direction:column;gap:8px}
.mdp-card h3{margin:0;font-size:15px}.mdp-card h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.mdp-muted{color:var(--muted);font-size:12px}.mdp-mono{font-family:var(--font-mono,monospace);font-size:12px;word-break:break-all}
.mdp-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.mdp-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.mdp-chip.on{background:var(--good-soft);color:var(--good,#1f9d55)}.mdp-chip.warn{background:var(--warning-soft);color:#8a5c00}.mdp-chip.bad{background:var(--danger-soft,#fde8e8);color:var(--danger,#c0392b)}
.mdp-pre{white-space:pre-wrap;max-height:45vh;overflow:auto;background:var(--surface-2);border-radius:6px;padding:8px;font-family:var(--font-mono,monospace);font-size:11.5px}
.mdp-split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.6fr);gap:12px}@media (max-width:1000px){.mdp-split{grid-template-columns:1fr}}
.mdp-ops{margin:0;padding:0;max-height:55vh;overflow:auto}.mdp-ops li{padding:4px 0;border-bottom:1px solid var(--line);cursor:pointer;list-style:none}.mdp-ops li.sel{background:var(--accent-soft)}
.mdp-form{display:grid;gap:8px}.mdp-form label{display:grid;gap:3px;font-size:12px;color:var(--ink-soft)}.mdp-form label.chk{display:flex;gap:6px;align-items:center}
.mdp-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:8px}
#mdp-root input,#mdp-root select,#mdp-root textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%}
.mdp-kv{display:grid;grid-template-columns:auto 1fr;gap:3px 10px;font-size:12.5px}.mdp-kv dt{color:var(--muted)}.mdp-kv dd{margin:0;min-width:0;word-break:break-all}`;
    document.head.appendChild(s);
  }

  function field(name, s, required) {
    s = s || {};
    const id = 'mdp-f-' + name;
    const label = E(name) + (required ? ' *' : '') + (s.description ? ` <span class="mdp-muted">${E(s.description)}</span>` : '');
    if (s.enum && s.type !== 'integer') return `<label>${label}<select id="${id}" data-name="${E(name)}" data-kind="enum">${s.enum.map((v) => `<option>${E(v)}</option>`).join('')}</select></label>`;
    if (s.type === 'boolean') return `<label class="chk"><input type="checkbox" id="${id}" data-name="${E(name)}" data-kind="bool">${label}</label>`;
    if (s.type === 'integer' || s.type === 'number') return `<label>${label}<input type="number" id="${id}" data-name="${E(name)}" data-kind="${s.type}"></label>`;
    if (s.type === 'object' || s.type === 'array' || !s.type) return `<label>${label}<textarea rows="3" id="${id}" data-name="${E(name)}" data-kind="json" placeholder="JSON"></textarea></label>`;
    return `<label>${label}<input id="${id}" data-name="${E(name)}" data-kind="string"></label>`;
  }

  function readForm(root) {
    const args = {};
    root.querySelectorAll('[data-name]').forEach((el) => {
      const k = el.dataset.name, kind = el.dataset.kind;
      if (kind === 'bool') { if (el.checked) args[k] = true; return; }
      const v = el.value.trim();
      if (v === '') return;
      args[k] = kind === 'integer' ? parseInt(v, 10) : kind === 'number' ? parseFloat(v) : kind === 'json' ? JSON.parse(v) : v;
    });
    return args;
  }

  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : '');
  const chip = (text, kind, title) => `<span class="mdp-chip ${kind || ''}"${title ? ` title="${E(title)}"` : ''}>${E(text)}</span>`;
  function stopPoll() { clearTimeout(st.poll); st.poll = null; }

  function chips(r) {
    const out = [];
    if (!r.installed) out.push(chip('not installed'));
    else {
      out.push(r.ready ? chip('built', 'on') : chip(r.can_build ? 'not built' : 'checked out', r.can_build ? 'warn' : ''));
      if (r.behind) out.push(chip(`${r.behind} update${r.behind > 1 ? 's' : ''}`, 'warn'));
      if (r.ahead) out.push(chip(`${r.ahead} unpushed`, '', 'commits here that are not in its repo yet'));
      if (r.changed_files) out.push(chip(`${r.changed_files} changed`, 'warn', 'uncommitted changes: updates wait until they are committed'));
    }
    if (r.has_hub) out.push(r.hub && r.hub.running ? chip('hub running', 'on') : chip('hub stopped'));
    if (r.host_supported === false) out.push(chip('not for this OS', 'bad'));
    if (r.job) out.push(chip(r.job + '…', 'warn'));
    return out.join(' ');
  }

  function shell() {
    const root = $('mdp-root');
    if (!root) return null;
    if (!$('mdp-body')) {
      root.innerHTML = `<div class="mdp-row" style="margin-bottom:8px"><label class="mdp-muted">Machine <select id="mdp-machine"><option value="">This machine</option></select></label><button class="btn ghost" id="mdp-refresh">Refresh</button><span id="mdp-note" class="mdp-muted"></span></div><div id="mdp-body"></div>`;
      $('mdp-refresh').addEventListener('click', () => render(true));
    }
    if (!root.dataset.peers) {
      root.dataset.peers = '1';
      api0('/api/peers').then((rows) => {
        const sel = $('mdp-machine');
        (rows || []).forEach((r) => { const o = document.createElement('option'); o.value = r.name; o.textContent = r.name; sel.appendChild(o); });
        if (machine && !(rows || []).some((r) => r.name === machine)) machine = '';
        sel.value = machine;
      }).catch(() => {});
      $('mdp-machine').addEventListener('change', (e) => {
        machine = e.target.value;
        try { localStorage.setItem('mdp-machine', machine); } catch (_) {}
        st.open = ''; st.ops = null; stopPoll(); render();
      });
    }
    return $('mdp-body');
  }

  async function action(mid, what, label) {
    try {
      const r = await post(`/api/modules/${encodeURIComponent(mid)}/${what}`);
      if (r && r.id && r.state) toast(`${label}: started`); else toast(`${label}: done`);
      render(true);
    } catch (e) { toast(`${label}: ${errText(e)}`, 'error'); }
  }

  function buttons(r) {
    const b = [];
    const btn = (what, label, ghost) => `<button class="btn${ghost ? ' ghost' : ''}" data-act="${what}" data-mid="${E(r.id)}" data-label="${E(label)}">${E(label)}</button>`;
    if (!r.installed) b.push(btn('setup', 'Install'));
    else {
      if (r.behind || !r.ready) b.push(btn('update', r.behind ? 'Update' : 'Build'));
      if (r.has_hub) b.push(r.hub && r.hub.running ? btn('hub/stop', 'Stop hub', true) : btn('hub/start', 'Start hub', true));
      if (r.has_gui && r.ready) b.push(btn('gui', 'Open window', true));
    }
    b.push(`<button class="btn ghost" data-open="${E(r.id)}">Details</button>`);
    return b.join(' ');
  }

  function wire(body) {
    body.querySelectorAll('[data-act]').forEach((el) => el.addEventListener('click', () => action(el.dataset.mid, el.dataset.act, el.dataset.label)));
    body.querySelectorAll('[data-open]').forEach((el) => el.addEventListener('click', () => { st.open = el.dataset.open; st.ops = null; st.op = null; render(); }));
  }

  async function listView(body, refresh) {
    if (!st.list || refresh) st.list = await api('/api/modules');
    const errs = Object.entries(st.list.manifest_errors || {});
    const areas = {};
    st.list.modules.forEach((r) => { (areas[r.area] = areas[r.area] || []).push(r); });
    body.innerHTML = (errs.length ? `<p class="cardnote">${errs.map(([k, v]) => `<b>${E(k)}</b>: ${E(v)}`).join('<br>')}</p>` : '') +
      `<div class="mdp-grid">${st.list.modules.map((r) => `<div class="mdp-card"><div class="mdp-row" style="justify-content:space-between"><h3>${E(r.name)}</h3><span class="mdp-muted">${E(r.area)}</span></div>
<div class="mdp-muted">${E(r.description)}</div><div class="mdp-row">${chips(r)}</div>
${r.installed ? `<div class="mdp-mono mdp-muted">${E(r.path || '')}${r.commit ? ` · ${E(r.branch || '')} ${E(r.commit)}` : ''}</div>` : ''}
<div class="mdp-row">${buttons(r)}</div></div>`).join('')}</div>`;
    wire(body);
    const running = st.list.modules.some((r) => r.job);
    stopPoll();
    if (running) st.poll = setTimeout(() => render(true), 3000);
  }

  function kv(pairs) { return `<dl class="mdp-kv">${pairs.filter(([, v]) => v !== undefined && v !== null && v !== '').map(([k, v]) => `<dt>${E(k)}</dt><dd>${v}</dd>`).join('')}</dl>`; }

  async function detailView(body) {
    const mid = st.open;
    const s = st.status = await api(`/api/modules/${encodeURIComponent(mid)}`);
    const m = s.module, inst = s.install || {}, hub = s.hub || {};
    const jobs = await api(`/api/modules/${encodeURIComponent(mid)}/jobs`).catch(() => []);
    const tools = (s.toolchain || []).map((t) => `${t.ok ? chip(t.tool, 'on') : chip(t.tool + (t.found ? ' too old' : ' missing'), 'bad', t.url)} <span class="mdp-muted">${E(t.version || '')}${t.min ? ' (needs ' + E(t.min) + '+)' : ''}</span>`).join('<br>');
    const act = (what, label, ghost) => `<button class="btn${ghost ? ' ghost' : ''}" data-act="${what}" data-mid="${E(mid)}" data-label="${E(label)}">${E(label)}</button>`;
    body.innerHTML = `<div class="mdp-row" style="margin-bottom:8px"><button class="btn ghost" id="mdp-back">← All modules</button><h3 style="margin:0">${E(m.name)}</h3><span class="mdp-muted">${E(m.description)}</span></div>
${s.manifest_error ? `<p class="cardnote">${E(s.manifest_error)}</p>` : ''}
<div class="mdp-grid">
<div class="mdp-card"><h4>Checkout</h4>${kv([['path', `<span class="mdp-mono">${E(inst.path)}</span>`], ['repo', `<span class="mdp-mono">${E(m.repo)}</span>`], ['branch', E(inst.branch)], ['commit', E(inst.commit ? inst.commit + ' ' + (inst.subject || '') : '')], ['updates', inst.behind != null ? E(`${inst.behind} behind, ${inst.ahead} ahead of ${inst.upstream}`) : ''], ['changes', inst.changed_files ? E(`${inst.changed_files} uncommitted`) : ''], ['manifest', E(m.source)], ['problem', E(inst.git_error)]])}
<div class="mdp-row">${inst.installed ? act('update', 'Update', false) + (m.can_build ? act('build', 'Build', true) : '') : act('setup', 'Install', false)}<button class="btn ghost" id="mdp-fetch">Check for updates</button>${m.has_pipeline ? act('pipeline', 'Run pipeline', true) : ''}</div></div>
<div class="mdp-card"><h4>Running</h4>${kv([['hub', m.has_hub ? (hub.running ? chip('running', 'on') + ` <span class="mdp-mono">${E(hub.url || '')}</span> pid ${E(hub.pid || '')}` : chip('stopped')) : '<span class="mdp-muted">no hub yet</span>'], ['version', E(hub.version)], ['this OS', s.host ? (s.host.supported ? chip(s.host.os, 'on') : chip(s.host.os + ': not supported', 'bad')) : ''], ['needs', E((s.host && s.host.needs || []).join(', '))]])}
<div class="mdp-row">${m.has_hub ? (hub.running ? act('hub/stop', 'Stop hub', true) : act('hub/start', 'Start hub', false)) : ''}${m.has_gui && inst.ready ? act('gui', 'Open window', true) : ''}${m.has_tui ? act('tui', 'Open terminal UI', true) : ''}${m.has_mcp || m.adapter ? act('mcp', 'Add its MCP server', true) : ''}${m.has_hub ? '<button class="btn ghost" id="mdp-conf">Check conformance</button>' : ''}</div><div id="mdp-conf-out"></div></div>
${tools ? `<div class="mdp-card"><h4>Toolchain</h4>${tools}</div>` : ''}
<div class="mdp-card" style="grid-column:1/-1"><h4>Jobs</h4>${(jobs || []).slice(0, 6).map((j) => `<details${j.state === 'running' ? ' open' : ''}><summary>${E(j.kind)} ${j.state === 'running' ? chip('running', 'warn') : j.state === 'done' ? chip('done', 'on') : chip(j.state, 'bad')} <span class="mdp-muted">${E(when(j.started))}</span></summary><div class="mdp-pre">${E((j.log || []).join('\n'))}${j.error ? '\n' + E(j.error) : ''}</div></details>`).join('') || '<p class="mdp-muted">None yet.</p>'}</div>
</div>
${m.has_hub ? `<div class="mdp-card" style="margin-top:12px"><h4>Operations</h4><div id="mdp-ops-area">${hub.running ? '<p class="mdp-muted">Loading…</p>' : '<p class="mdp-muted">Start the hub to use its operations.</p>'}</div></div>` : ''}`;
    $('mdp-back').addEventListener('click', () => { st.open = ''; stopPoll(); render(true); });
    $('mdp-fetch').addEventListener('click', async () => { try { await api(`/api/modules/${encodeURIComponent(mid)}?fetch=1`); render(); } catch (e) { toast(errText(e), 'error'); } });
    const conf = $('mdp-conf');
    if (conf) conf.addEventListener('click', async () => {
      $('mdp-conf-out').innerHTML = '<p class="mdp-muted">Checking…</p>';
      try {
        const r = await post(`/api/modules/${encodeURIComponent(mid)}/conformance`);
        $('mdp-conf-out').innerHTML = r.checks.map((c) => `<div>${c.ok ? chip('ok', 'on') : chip('fails', 'bad')} ${E(c.check)} <span class="mdp-muted">${E(c.detail)}</span></div>`).join('');
      } catch (e) { $('mdp-conf-out').innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; }
    });
    wire(body);
    if (m.has_hub && hub.running) await opsArea(mid);
    stopPoll();
    if ((jobs || []).some((j) => j.state === 'running')) st.poll = setTimeout(() => render(), 1500);
  }

  async function opsArea(mid) {
    const area = $('mdp-ops-area');
    if (!area) return;
    try { if (!st.ops) st.ops = await api(`/api/modules/${encodeURIComponent(mid)}/operations`); } catch (e) { area.innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; return; }
    area.innerHTML = `<div class="mdp-split"><div><input id="mdp-q" placeholder="Search ${st.ops.length} operations" value="${E(st.query)}" style="width:100%;margin-bottom:6px"><ul class="mdp-ops" id="mdp-ops"></ul></div><div id="mdp-op"><p class="mdp-muted">Pick an operation.</p></div></div>`;
    const draw = () => {
      const q = st.query.toLowerCase().split(/\s+/).filter(Boolean);
      $('mdp-ops').innerHTML = st.ops.filter((o) => q.every((w) => `${o.id} ${o.summary}`.toLowerCase().includes(w))).slice(0, 300).map((o) => `<li data-op="${E(o.id)}" class="${st.op && st.op.id === o.id ? 'sel' : ''}"><span class="mdp-mono">${E(o.id)}</span> ${o.mutating ? chip(o.destructive ? 'destructive' : 'changes', o.destructive ? 'bad' : 'warn') : ''}<div class="mdp-muted">${E(o.summary)}</div></li>`).join('');
      $('mdp-ops').querySelectorAll('li').forEach((li) => li.addEventListener('click', () => { st.op = st.ops.find((o) => o.id === li.dataset.op); draw(); opForm(mid); }));
    };
    $('mdp-q').addEventListener('input', (e) => { st.query = e.target.value; draw(); });
    draw();
    if (st.op) opForm(mid);
  }

  function opForm(mid) {
    const o = st.op, box = $('mdp-op');
    if (!o || !box) return;
    const sch = o.input_schema || {}, props = sch.properties || {}, req = sch.required || [];
    box.innerHTML = `<h4>${E(o.id)}</h4><p>${E(o.summary)}</p><div class="mdp-form"><div class="mdp-fields">${Object.entries(props).map(([k, s]) => field(k, s, req.includes(k))).join('') || '<p class="mdp-muted">No arguments.</p>'}</div>
<div class="mdp-row"><button class="btn" id="mdp-run">Run</button></div></div><div id="mdp-out"></div>`;
    $('mdp-run').addEventListener('click', async () => {
      let args;
      try { args = readForm(box); } catch (_) { toast('An argument is not valid JSON', 'error'); return; }
      if (o.mutating && !confirm(`${o.id} ${o.destructive ? 'cannot be undone' : 'changes something'}. Run it?`)) return;
      $('mdp-out').innerHTML = '<p class="mdp-muted">Running…</p>';
      try {
        const r = (await post(`/api/modules/${encodeURIComponent(mid)}/call`, { operation: o.id, args })).result;
        $('mdp-out').innerHTML = r && r.format === 'png' && r.base64 ? `<img alt="result" style="max-width:100%" src="data:image/png;base64,${r.base64}">` : `<div class="mdp-pre">${E(JSON.stringify(r, null, 1))}</div>`;
      } catch (e) { $('mdp-out').innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; }
    });
  }

  async function render(refresh) {
    css();
    const body = shell();
    if (!body) return;
    $('mdp-note').textContent = machine ? `Controlling ${machine}` : '';
    try {
      if (st.open) await detailView(body); else await listView(body, refresh || !st.list);
    } catch (e) { body.innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; }
  }

  function init() {
    const root = $('mdp-root');
    if (!root) return;
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'modules') render(true); else stopPoll(); };
    window.addEventListener('hashchange', onHash);
    onHash();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
