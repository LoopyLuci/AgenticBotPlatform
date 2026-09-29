// TransferDaemon panel: TransferDaemon from ABP. Build and update it from its repo, run its daemon, message and send
// files to contacts, follow transfers, drive its window (live screenshot, widgets) and terminal UI (its screen, keys),
// run local relays, and use every operation from forms built from their schemas.
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_transferdaemon.py fails if the two differ. It uses the page's own api(), esc() and showToast(),
// and draws into #tdp-root.
(function (api0) {
  'use strict';
  if (typeof api0 !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { const d = JSON.parse(m).detail; m = (d && d.error) || d || m; } catch (_) {} return m; };
  // Which machine this page controls: this one, or a linked server (Peers page) whose ABP lets linked servers control
  // transferdaemon (peers.remote_control). Calls go through /api/peers/<name>/proxy then.
  let machine = '';
  try { machine = localStorage.getItem('tdp-machine') || ''; } catch (_) {}
  const api = (path, opts) => {
    if (!machine || !path.startsWith('/api/transferdaemon/')) return api0(path, opts);
    const o = opts || {};
    return api0('/api/peers/' + encodeURIComponent(machine) + '/proxy', { method: 'POST',
      body: JSON.stringify({ method: o.method || 'GET', path, body: o.body ? JSON.parse(o.body) : null }) }).then((r) => r.result);
  };
  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });
  const call = (operation, args) => post('/api/transferdaemon/call', { operation, args: args || {} }).then((r) => r.result);
  const TABS = [['overview', 'Overview'], ['messages', 'Messages'], ['transfers', 'Transfers'], ['window', 'Window'], ['terminal', 'Terminal UI'], ['relays', 'Relays'], ['all', 'All features'], ['audit', 'Audit']];
  const st = { tab: 'overview', status: null, ops: null, op: null, group: '', live: null, contact: '', widgets: null };
  try { st.tab = localStorage.getItem('tdp-tab') || 'overview'; } catch (_) {}

  function css() {
    if ($('tdp-css')) return;
    const s = document.createElement('style');
    s.id = 'tdp-css';
    s.textContent = `.tdp-tabs{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0 12px;border-bottom:1px solid var(--line)}
.tdp-tabs button{background:none;border:0;border-bottom:2px solid transparent;color:var(--ink-soft);padding:8px 12px;font:inherit;font-weight:600;cursor:pointer}
.tdp-tabs button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--accent)}
.tdp-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:12px}
.tdp-card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--surface);min-width:0}
.tdp-card h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.tdp-muted{color:var(--muted);font-size:12px}.tdp-mono{font-family:var(--font-mono,monospace);font-size:12px;word-break:break-all}
.tdp-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.tdp-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.tdp-chip.on{background:var(--good-soft);color:var(--good,#1f9d55)}.tdp-chip.warn{background:var(--warning-soft);color:#8a5c00}.tdp-chip.bad{background:var(--danger-soft,#fde8e8);color:var(--danger,#c0392b)}
.tdp-pre{white-space:pre-wrap;max-height:50vh;overflow:auto;background:var(--surface-2);border-radius:6px;padding:8px;font-family:var(--font-mono,monospace);font-size:11.5px}
.tdp-term{white-space:pre;overflow:auto;background:#0b0e14;color:#d6deeb;border-radius:8px;padding:10px;font-family:var(--font-mono,monospace);font-size:12px;line-height:1.25;max-height:70vh}
.tdp-form{display:grid;gap:8px}.tdp-form label{display:grid;gap:3px;font-size:12px;color:var(--ink-soft)}.tdp-form label.chk{display:flex;gap:6px;align-items:center}
#tdp-root input,#tdp-root select,#tdp-root textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%}
.tdp-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:8px}
.tdp-ops{margin:0;padding:0;max-height:60vh;overflow:auto}.tdp-ops li{padding:4px 0;border-bottom:1px solid var(--line);cursor:pointer;list-style:none}.tdp-ops li.sel{background:var(--accent-soft)}
.tdp-split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.6fr);gap:12px}@media (max-width:1000px){.tdp-split{grid-template-columns:1fr}}
.tdp-shot{width:100%;max-width:520px;height:auto;border:1px solid var(--line);border-radius:8px;background:#000}
.tdp-el{display:grid;grid-template-columns:auto 1fr auto;gap:6px;align-items:center;padding:4px 0;border-bottom:1px solid var(--line);font-size:12px}
.tdp-scroll{overflow:auto;max-height:60vh}
.tdp-msgs{display:flex;flex-direction:column;gap:6px;max-height:55vh;overflow:auto;padding:4px}
.tdp-msg{max-width:78%;padding:6px 10px;border-radius:12px;background:var(--surface-2);font-size:13px;word-break:break-word}
.tdp-msg.out{align-self:flex-end;background:var(--accent);color:#fff}.tdp-msg .tdp-muted{font-size:10.5px}.tdp-msg.out .tdp-muted{color:rgba(255,255,255,.8)}
.tdp-bar{height:6px;border-radius:3px;background:var(--surface-2);overflow:hidden}.tdp-bar>i{display:block;height:100%;background:var(--accent)}`;
    document.head.appendChild(s);
  }

  function field(name, s, required) {
    s = s || {};
    const id = 'tdp-f-' + name;
    const label = E(name) + (required ? ' *' : '') + (s.description ? ` <span class="tdp-muted">${E(s.description)}</span>` : '');
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

  const size = (n) => { n = Number(n) || 0; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return `${n.toFixed(i ? 1 : 0)} ${u[i]}`; };
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : '');

  function stopLive() { clearInterval(st.live); st.live = null; }

  function shell() {
    const root = $('tdp-root');
    if (!root) return null;
    if (!$('tdp-body')) {
      root.innerHTML = `<div class="tdp-row" style="margin-bottom:6px"><label class="tdp-muted">Machine <select id="tdp-machine"><option value="">This machine</option></select></label><span id="tdp-machine-note" class="tdp-muted"></span></div><div class="tdp-tabs" role="tablist">${TABS.map(([k, t]) => `<button role="tab" data-tab="${k}">${t}</button>`).join('')}</div><div id="tdp-body"></div>`;
      root.querySelectorAll('.tdp-tabs button').forEach((b) => b.addEventListener('click', () => { st.tab = b.dataset.tab; try { localStorage.setItem('tdp-tab', st.tab); } catch (_) {} stopLive(); render(); }));
    }
    if (!root.dataset.peers) {
      root.dataset.peers = '1';
      api0('/api/peers').then((rows) => {
        const sel = $('tdp-machine');
        (rows || []).forEach((r) => { const o = document.createElement('option'); o.value = r.name; o.textContent = r.name; sel.appendChild(o); });
        if (machine && !(rows || []).some((r) => r.name === machine)) machine = '';
        sel.value = machine;
        sel.addEventListener('change', () => {
          machine = sel.value;
          try { localStorage.setItem('tdp-machine', machine); } catch (_) {}
          stopLive();
          st.status = null; st.ops = null; st.op = null; st.contact = '';
          render();
        });
      }).catch(() => {});
    }
    const note = $('tdp-machine-note');
    if (note) note.textContent = machine ? ' controlling ' + machine + ' through its ABP' : '';
    root.querySelectorAll('.tdp-tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === st.tab)));
    return $('tdp-body');
  }

  async function render() {
    css();
    const body = shell();
    if (!body) return;
    try {
      if (st.tab === 'overview') await overview(body);
      else if (!st.status || !st.status.daemon.running) await overview(body, 'Start TransferDaemon first (Overview).');
      else if (st.tab === 'messages') await messages(body);
      else if (st.tab === 'transfers') await transfers(body);
      else if (st.tab === 'window') await windowTab(body);
      else if (st.tab === 'terminal') await terminal(body);
      else if (st.tab === 'relays') await relays(body);
      else if (st.tab === 'all') await allFeatures(body);
      else await auditTab(body);
    } catch (e) { body.innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; }
  }

  async function overview(body, note) {
    st.status = await api('/api/transferdaemon/status');
    const s = st.status, i = s.install, d = s.daemon, x = s.status || {};
    const chip = (ok, on, off) => `<span class="tdp-chip ${ok ? 'on' : 'warn'}">${ok ? on : off}</span>`;
    const jobs = (s.jobs || []).map((j) => `<div class="tdp-muted">${E(j.kind)}: ${E((j.log || []).slice(-1)[0] || '…')}</div>`).join('');
    const id = x.identity || {};
    body.innerHTML = `${note ? `<p class="cardnote">${E(note)}</p>` : ''}<div class="tdp-grid">
<div class="tdp-card"><h4>Program</h4>
 <div>${chip(i.installed, 'installed', 'not installed')} ${i.installed ? chip(i.ready, 'built', 'not built') : ''} ${i.developer_checkout ? '<span class="tdp-chip">your working copy</span>' : ''}</div>
 <div class="tdp-mono" style="margin:6px 0">${E(i.path)}</div>
 ${i.commit ? `<div class="tdp-muted">${E(i.branch)} @ ${E(i.commit)}: ${E(i.subject)}</div>` : ''}
 ${i.behind ? `<div><span class="tdp-chip warn">${i.behind} update(s) waiting</span></div>` : ''}
 ${i.changed_files ? `<div class="tdp-muted">${i.changed_files} uncommitted change(s)</div>` : ''}
 ${i.cargo ? '' : '<div class="tdp-muted">Rust (cargo) is needed to build it: rustup.rs</div>'}
 <div class="tdp-row" style="margin-top:8px"><button class="btn" data-a="setup" ${i.cargo ? '' : 'disabled'}>${i.ready ? 'Rebuild' : 'Install &amp; build'}</button>
 <button class="btn" data-a="update" ${i.installed ? '' : 'disabled'}>Update from repo</button>
 <button class="btn" data-a="mcp" ${i.ready ? '' : 'disabled'}>Add its MCP server to ABP</button></div>${jobs}</div>
<div class="tdp-card"><h4>Daemon</h4>
 <div>${chip(d.running, 'running', 'stopped')} ${d.running ? chip(!!x.gui, 'window open', 'window closed') + ' ' + chip(!!x.tui, 'terminal UI open', 'terminal UI closed') : ''}</div>
 ${d.running ? `<div class="tdp-mono" style="margin:6px 0">${E(d.url)} · pid ${E(d.pid)} · v${E(d.version)}</div>` : ''}
 ${id.has_identity ? `<div><b>${E(id.display_name || '(no name)')}</b></div><div class="tdp-mono tdp-muted">${E(id.public_key)}</div>` : (d.running ? '<div class="tdp-muted">No identity yet: create one in its window.</div>' : '')}
 ${d.running ? `<div class="tdp-muted" style="margin-top:4px">${E(x.contacts)} contact(s) · ${E(x.transfers)} transfer(s) · ${E(x.groups)} group(s)</div>` : ''}
 <div class="tdp-row" style="margin-top:8px">${d.running ? '<button class="btn" data-a="stop">Stop</button>' : `<button class="btn" data-a="start" ${i.ready ? '' : 'disabled'}>Start</button>`}
 <button class="btn" data-a="window" ${d.running ? '' : 'disabled'}>Open window</button>
 <button class="btn" data-a="tui" ${d.running ? '' : 'disabled'}>Open terminal UI</button></div></div>
<div class="tdp-card"><h4>Relays &amp; network</h4>${d.running ? `<div class="tdp-pre">${E(JSON.stringify({ relays: x.relays, listener: (x.facts || {}).peer_listener }, null, 1))}</div>` : '<p class="tdp-muted">Start the daemon to see its relays.</p>'}</div></div>`;
    body.querySelectorAll('[data-a]').forEach((btn) => btn.addEventListener('click', async () => {
      const a = btn.dataset.a;
      btn.disabled = true;
      const paths = { setup: '/api/transferdaemon/setup', update: '/api/transferdaemon/update', start: '/api/transferdaemon/daemon/start', stop: '/api/transferdaemon/daemon/stop', window: '/api/transferdaemon/window', tui: '/api/transferdaemon/tui', mcp: '/api/transferdaemon/mcp' };
      try { await post(paths[a], a === 'tui' ? { headless: true } : {}); toast({ setup: 'Building TransferDaemon (a first build takes a few minutes)…', update: 'Updating…', start: 'Daemon started', stop: 'Daemon stopped', window: 'Window open', tui: 'Terminal UI running (Terminal UI tab)', mcp: 'MCP server added' }[a], 'success'); } catch (e) { toast(errText(e), 'error'); }
      render();
    }));
    if ((s.jobs || []).length) setTimeout(() => { if (st.tab === 'overview') render(); }, 3000);
  }

  async function messages(body) {
    const contacts = (await call('contacts.get_contacts')).contacts || [];
    if (!st.contact && contacts.length) st.contact = contacts[0].id;
    body.innerHTML = `<div class="tdp-split"><div class="tdp-card"><h4>Contacts</h4><div class="tdp-scroll">${contacts.map((c) => `<div class="tdp-el" data-c="${E(c.id)}" style="cursor:pointer;${c.id === st.contact ? 'background:var(--accent-soft)' : ''}"><span class="tdp-chip ${c.online ? 'on' : ''}">${c.online ? 'online' : 'offline'}</span><span><b>${E(c.name)}</b>${c.blocked ? ' <span class="tdp-chip bad">blocked</span>' : ''}<div class="tdp-mono tdp-muted">${E(c.id.slice(0, 24))}… ${c.address ? '· ' + E(c.address) : ''}</div></span><span></span></div>`).join('') || '<p class="tdp-muted">No contacts yet.</p>'}</div>
<details style="margin-top:8px"><summary class="tdp-muted">Add a contact</summary><div class="tdp-form" style="margin-top:6px"><label>Public key<input id="tdp-pk" placeholder="64 hex characters"></label><label>Name<input id="tdp-name"></label><label>Address (optional)<input id="tdp-addr" placeholder="host:port of their listener"></label><button class="btn" id="tdp-add">Add</button></div></details></div>
<div class="tdp-card"><h4>Conversation</h4><div id="tdp-msgs" class="tdp-msgs"></div>
<div class="tdp-row" style="margin-top:8px"><input id="tdp-text" placeholder="Message" style="flex:1"><button class="btn" id="tdp-send" ${st.contact ? '' : 'disabled'}>Send</button></div>
<div class="tdp-row" style="margin-top:6px"><input id="tdp-file" placeholder="Send a file: its path on ${machine ? E(machine) : 'this machine'}" style="flex:1"><button class="btn" id="tdp-sendfile" ${st.contact ? '' : 'disabled'}>Send file</button></div></div></div>`;
    body.querySelectorAll('[data-c]').forEach((el) => el.addEventListener('click', () => { st.contact = el.dataset.c; render(); }));
    $('tdp-add').addEventListener('click', async () => {
      try { await call('contacts.add_contact', { public_key: $('tdp-pk').value.trim(), name: $('tdp-name').value.trim(), address: $('tdp-addr').value.trim() }); toast('Contact added', 'success'); render(); } catch (e) { toast(errText(e), 'error'); }
    });
    const load = async () => {
      if (!st.contact) return;
      const list = (await call('messages.get_messages', { contact_id: st.contact })).messages || [];
      const box = $('tdp-msgs');
      if (!box) return;
      box.innerHTML = list.map((m) => `<div class="tdp-msg ${m.outbound ? 'out' : ''}">${m.content_type === 'file' ? `📎 ${E(m.file_name)} <span class="tdp-muted">${size(m.file_size_bytes)}</span>` : E(m.text)}<div class="tdp-muted">${E(when(m.timestamp_ts))}${m.outbound ? ' · ' + E(m.status) : ''}</div></div>`).join('') || '<p class="tdp-muted">No messages yet.</p>';
      box.scrollTop = box.scrollHeight;
    };
    const send = async (payload) => { try { await post('/api/transferdaemon/send', { to: st.contact, ...payload }); await load(); } catch (e) { toast(errText(e), 'error'); } };
    $('tdp-send').addEventListener('click', async () => { const t = $('tdp-text').value; if (!t.trim()) return; $('tdp-text').value = ''; await send({ text: t }); });
    $('tdp-text').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') $('tdp-send').click(); });
    $('tdp-sendfile').addEventListener('click', async () => { const f = $('tdp-file').value.trim(); if (!f) return; $('tdp-file').value = ''; await send({ file: f }); });
    await load();
    stopLive();
    st.live = setInterval(() => { if (document.hidden || st.tab !== 'messages') return; load().catch(() => {}); }, 3000);
  }

  async function transfers(body) {
    const list = (await call('transfers.get_transfers')).transfers || [];
    body.innerHTML = `<div class="tdp-card"><h4>Transfers</h4>${list.length ? list.slice().reverse().map((t) => {
      const pct = t.size_bytes ? Math.round(100 * t.transferred_bytes / t.size_bytes) : 0;
      const done = pct >= 100;
      return `<div class="tdp-el"><span class="tdp-chip ${done ? 'on' : ''}">${t.outbound ? 'to' : 'from'}</span><span><b>${E(t.file_name)}</b> <span class="tdp-muted">${E(t.contact_name)} · ${size(t.transferred_bytes)} / ${size(t.size_bytes)}${!done && t.bps ? ' · ' + size(t.bps / 8) + '/s' : ''}</span><div class="tdp-bar"><i style="width:${pct}%"></i></div></span>
<span class="tdp-row">${done ? '' : `${t.outbound ? `<button class="btn btn-sm" data-t="pause" data-id="${E(t.id)}">pause</button><button class="btn btn-sm" data-t="resume" data-id="${E(t.id)}">resume</button>` : ''}<button class="btn btn-sm" data-t="cancel" data-id="${E(t.id)}">cancel</button>`}</span></div>`;
    }).join('') : '<p class="tdp-muted">No transfers.</p>'}</div>`;
    body.querySelectorAll('[data-t]').forEach((b) => b.addEventListener('click', async () => {
      try { await call(`transfers.${b.dataset.t}_transfer`, { transfer_id: b.dataset.id }); toast(`${b.dataset.t}d`, 'success'); } catch (e) { toast(errText(e), 'error'); }
      render();
    }));
    stopLive();
    if (list.some((t) => t.transferred_bytes < t.size_bytes)) st.live = setInterval(() => { if (st.tab === 'transfers' && !document.hidden) render(); }, 2000);
  }

  async function windowTab(body) {
    const x = st.status.status || {};
    if (!x.gui) {
      body.innerHTML = `<div class="tdp-card"><h4>The TransferDaemon window</h4><p class="tdp-muted">The window is not open${machine ? ' on ' + E(machine) : ''}.</p><button class="btn" id="tdp-open">Open the window</button></div>`;
      $('tdp-open').addEventListener('click', async () => { try { await post('/api/transferdaemon/window', {}); st.status = await api('/api/transferdaemon/status'); render(); } catch (e) { toast(errText(e), 'error'); } });
      return;
    }
    const pages = await call('gui.pages');
    body.innerHTML = `<div class="tdp-split"><div class="tdp-card"><div class="tdp-row" style="justify-content:space-between"><h4>Screen</h4>
<div class="tdp-row"><select id="tdp-page"><option value="">go to…</option>${pages.tabs.map((t) => `<option>${E(t)}</option>`).join('')}</select>
<label class="tdp-muted"><input type="checkbox" id="tdp-live"> live</label><button class="btn btn-sm" id="tdp-refresh">Refresh</button></div></div>
<img id="tdp-shot" class="tdp-shot" alt="the TransferDaemon window"><div class="tdp-row" style="margin-top:6px"><input id="tdp-keys" placeholder="keys: Enter, Escape, ctrl+N…" style="flex:1"><button class="btn btn-sm" id="tdp-press">Press</button></div></div>
<div class="tdp-card"><h4>On screen</h4><input id="tdp-filter" placeholder="filter" style="width:100%;margin-bottom:6px"><div id="tdp-els" class="tdp-scroll"></div></div></div>`;
    const shot = async () => { try { const s = await call('gui.screenshot', { max_width: 520 }); $('tdp-shot').src = 'data:image/png;base64,' + s.base64; } catch (e) { stopLive(); toast(errText(e), 'error'); } };
    const load = async () => { st.widgets = (await call('gui.inspect', { max: 300 })).widgets; draw(); };
    const refresh = async () => { await shot(); await load(); };
    $('tdp-page').addEventListener('change', async (ev) => { if (!ev.target.value) return; await call('gui.navigate', { to: ev.target.value }); setTimeout(refresh, 250); });
    $('tdp-refresh').addEventListener('click', refresh);
    $('tdp-press').addEventListener('click', async () => { try { await call('gui.key', { keys: $('tdp-keys').value }); setTimeout(refresh, 250); } catch (e) { toast(errText(e), 'error'); } });
    $('tdp-live').addEventListener('change', (ev) => { stopLive(); if (ev.target.checked) st.live = setInterval(shot, 1500); });
    $('tdp-filter').addEventListener('input', draw);
    await refresh();

    function draw() {
      const f = ($('tdp-filter').value || '').toLowerCase();
      const shown = (st.widgets || []).filter((w) => w.role !== 'inline_text_box' && (!f || JSON.stringify(w).toLowerCase().includes(f)));
      $('tdp-els').innerHTML = shown.map((w, n) => {
        const name = w.means ? `${w.label} (${w.means})` : (w.label || w.value || w.id);
        let ctl = '';
        if (['button', 'link', 'tab', 'check_box', 'radio_button', 'menu_item', 'switch', 'toggle_button'].includes(w.role)) ctl = `<button class="btn btn-sm" data-e="${n}" data-do="click" ${w.enabled === false ? 'disabled' : ''}>click</button>`;
        else if (w.role.includes('text_input')) ctl = `<button class="btn btn-sm" data-e="${n}" data-do="set">fill…</button>`;
        return `<div class="tdp-el"><span class="tdp-chip">${E(w.role)}</span><span>${E(String(name).slice(0, 90))}${w.value && w.label ? ` = <span class="tdp-mono">${E(w.value)}</span>` : ''}</span>${ctl}</div>`;
      }).join('') || '<p class="tdp-muted">Nothing on screen.</p>';
      $('tdp-els').querySelectorAll('[data-do]').forEach((btn) => btn.addEventListener('click', async () => {
        const w = shown[+btn.dataset.e], target = { id: w.id };
        try {
          if (btn.dataset.do === 'click') await call('gui.click', { target });
          else { const v = prompt(`Value for ${w.label || w.role}`, w.value || ''); if (v === null) return; await call('gui.set', { target, value: v }); }
          setTimeout(refresh, 300);
        } catch (err) { toast(errText(err), 'error'); }
      }));
    }
  }

  async function terminal(body) {
    const x = st.status.status || {};
    if (!x.tui) {
      body.innerHTML = `<div class="tdp-card"><h4>The terminal UI</h4><p class="tdp-muted">It is not running. Headless runs it in memory so it can be used from here; a console window opens it on ${machine ? E(machine) : 'this machine'}'s desktop.</p>
<div class="tdp-row"><button class="btn" id="tdp-tui-h">Start headless</button><button class="btn" id="tdp-tui-c">Open in a console window</button></div></div>`;
      const go = async (headless) => { try { await post('/api/transferdaemon/tui', { headless }); st.status = await api('/api/transferdaemon/status'); render(); } catch (e) { toast(errText(e), 'error'); } };
      $('tdp-tui-h').addEventListener('click', () => go(true));
      $('tdp-tui-c').addEventListener('click', () => go(false));
      return;
    }
    body.innerHTML = `<div class="tdp-card"><div class="tdp-row" style="justify-content:space-between"><h4>Terminal UI</h4><div class="tdp-row">${['F1', 'F2', 'F3', 'F4', 'F5', 'Tab', 'Up', 'Down', 'Enter', 'Esc'].map((k) => `<button class="btn btn-sm" data-k="${k}">${k}</button>`).join('')}<button class="btn btn-sm" id="tdp-tui-quit">Quit</button></div></div>
<pre id="tdp-screen" class="tdp-term"></pre><div class="tdp-row" style="margin-top:6px"><input id="tdp-tui-text" placeholder="type into it (Enter sends the text, then presses Enter)" style="flex:1"><button class="btn btn-sm" id="tdp-tui-type">Type</button></div></div>`;
    const draw = async () => { const s = await call('tui.screen'); const el = $('tdp-screen'); if (el) el.textContent = s.text; };
    body.querySelectorAll('[data-k]').forEach((b) => b.addEventListener('click', async () => { try { await call('tui.key', { keys: b.dataset.k }); await draw(); } catch (e) { toast(errText(e), 'error'); } }));
    $('tdp-tui-type').addEventListener('click', async () => { const t = $('tdp-tui-text').value; if (!t) return; $('tdp-tui-text').value = ''; try { await call('tui.type', { text: t }); await call('tui.key', { keys: 'Enter' }); await draw(); } catch (e) { toast(errText(e), 'error'); } });
    $('tdp-tui-quit').addEventListener('click', async () => { try { await call('tui.quit'); st.status = await api('/api/transferdaemon/status'); render(); } catch (e) { toast(errText(e), 'error'); } });
    await draw();
    stopLive();
    st.live = setInterval(() => { if (st.tab === 'terminal' && !document.hidden) draw().catch(() => stopLive()); }, 1500);
  }

  async function relays(body) {
    const r = await call('relay.status');
    const kinds = { relayd: 'UDP relay (relayd)', 'relayd-ws': 'WebSocket relay (relayd-ws)', dhtd: 'DHT bootstrap (dhtd)' };
    body.innerHTML = `<div class="tdp-grid"><div class="tdp-card"><h4>This daemon uses</h4>${(r.configured || []).length ? r.configured.map((a) => `<div class="tdp-mono">${E(a)}</div>`).join('') : '<p class="tdp-muted">No relay configured (direct and LAN/Tailscale peers only). Set transferdaemon.relays in config/backends.yaml.</p>'}
${r.dht_bootstrap ? `<div class="tdp-muted">DHT bootstrap: ${E(r.dht_bootstrap)}</div>` : ''}</div>
<div class="tdp-card"><h4>Local relays</h4>${Object.keys(kinds).map((k) => { const on = (r.local || {})[k]; const avail = (r.available || []).includes(k);
      return `<div class="tdp-el"><span class="tdp-chip ${on ? 'on' : ''}">${on ? 'running' : 'stopped'}</span><span>${E(kinds[k])}${on ? `<div class="tdp-mono tdp-muted">${E(on.bind)} · pid ${E(on.pid)}</div>` : ''}</span><span class="tdp-row">${on ? `<button class="btn btn-sm" data-stop="${k}">stop</button>` : `<input data-bind="${k}" placeholder="bind (default)" style="width:130px"><button class="btn btn-sm" data-start="${k}" ${avail ? '' : 'disabled title="not built"'}>start</button>`}</span></div>`; }).join('')}</div>
<div class="tdp-card"><h4>Check a relay</h4><div class="tdp-row"><input id="tdp-probe" placeholder="udp://host:7777, ws://host:8081, wss://host" style="flex:1"><button class="btn btn-sm" id="tdp-probe-go">Check</button></div><div id="tdp-probe-out"></div></div></div>`;
    body.querySelectorAll('[data-start]').forEach((b) => b.addEventListener('click', async () => {
      const bind = (body.querySelector(`[data-bind="${b.dataset.start}"]`) || {}).value;
      try { await call('relay.start', { kind: b.dataset.start, ...(bind ? { bind } : {}) }); toast('Relay started', 'success'); } catch (e) { toast(errText(e), 'error'); }
      render();
    }));
    body.querySelectorAll('[data-stop]').forEach((b) => b.addEventListener('click', async () => { try { await call('relay.stop', { kind: b.dataset.stop }); } catch (e) { toast(errText(e), 'error'); } render(); }));
    $('tdp-probe-go').addEventListener('click', async () => { try { const p = await call('relay.probe', { addr: $('tdp-probe').value.trim() }); $('tdp-probe-out').innerHTML = `<div class="tdp-pre">${E(JSON.stringify(p, null, 1))}</div>`; } catch (e) { toast(errText(e), 'error'); } });
  }

  async function allFeatures(body) {
    if (!st.ops) st.ops = await api('/api/transferdaemon/operations');
    const groups = [...new Set(st.ops.map((o) => o.group))].sort();
    const shown = st.ops.filter((o) => !st.group || o.group === st.group);
    body.innerHTML = `<div class="tdp-split"><div class="tdp-card"><div class="tdp-row"><select id="tdp-group"><option value="">all (${st.ops.length})</option>${groups.map((g) => `<option ${g === st.group ? 'selected' : ''}>${E(g)}</option>`).join('')}</select><input id="tdp-q" placeholder="search" style="flex:1"></div>
<ul class="tdp-ops">${shown.map((o) => `<li data-id="${E(o.id)}" class="${st.op && st.op.id === o.id ? 'sel' : ''}"><b class="tdp-mono">${E(o.id)}</b> ${o.destructive ? '<span class="tdp-chip bad">cannot be undone</span>' : o.mutating ? '<span class="tdp-chip warn">changes</span>' : ''}<div class="tdp-muted">${E(o.summary)}</div></li>`).join('')}</ul></div>
<div class="tdp-card" id="tdp-op">${st.op ? '' : '<p class="tdp-muted">Pick an operation.</p>'}</div></div>`;
    $('tdp-group').addEventListener('change', (ev) => { st.group = ev.target.value; render(); });
    $('tdp-q').addEventListener('input', (ev) => { const q = ev.target.value.toLowerCase(); body.querySelectorAll('.tdp-ops li').forEach((li) => { li.style.display = li.textContent.toLowerCase().includes(q) ? '' : 'none'; }); });
    body.querySelectorAll('.tdp-ops li').forEach((li) => li.addEventListener('click', () => { st.op = st.ops.find((o) => o.id === li.dataset.id); render(); }));
    if (!st.op) return;
    const o = st.op, box = $('tdp-op'), props = (o.input && o.input.properties) || {}, req = (o.input && o.input.required) || [];
    box.innerHTML = `<h4>${E(o.id)}</h4><p>${E(o.summary)}</p><div class="tdp-form"><div class="tdp-fields">${Object.entries(props).map(([k, s]) => field(k, s, req.includes(k))).join('') || '<p class="tdp-muted">No arguments.</p>'}</div>
<div class="tdp-row"><button class="btn" id="tdp-run">Run</button></div></div><div id="tdp-out"></div>`;
    $('tdp-run').addEventListener('click', async () => {
      let args;
      try { args = readForm(box); } catch (_) { toast('An argument is not valid JSON', 'error'); return; }
      if (o.mutating && !confirm(`${o.id} ${o.destructive ? 'cannot be undone' : 'changes something'}. Run it?`)) return;
      $('tdp-out').innerHTML = '<p class="tdp-muted">Running…</p>';
      try {
        const r = await call(o.id, args);
        $('tdp-out').innerHTML = r && r.format === 'png' ? `<img class="tdp-shot" alt="result" src="data:image/png;base64,${r.base64}">` : r && typeof r.text === 'string' && o.id === 'tui.screen' ? `<pre class="tdp-term">${E(r.text)}</pre>` : `<div class="tdp-pre">${E(JSON.stringify(r, null, 1))}</div>`;
      } catch (e) { $('tdp-out').innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; }
    });
  }

  async function auditTab(body) {
    const rows = await api('/api/transferdaemon/audit?limit=200');
    body.innerHTML = `<div class="tdp-card"><h4>Changes made through TransferDaemon's control hub</h4><div class="tdp-scroll">${rows.map((r) => `<div class="tdp-el"><span class="tdp-muted">${E(when(r.ts))}</span><span class="tdp-mono">${E(r.operation)} <span class="tdp-muted">${E(JSON.stringify(r.args))}</span></span>${r.ok ? '<span class="tdp-chip on">ok</span>' : `<span class="tdp-chip bad" title="${E(r.error)}">failed</span>`}</div>`).join('') || '<p class="tdp-muted">Nothing yet.</p>'}</div></div>`;
  }

  function init() {
    const root = $('tdp-root');
    if (!root) return;
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'transferdaemon') render(); else stopLive(); };
    window.addEventListener('hashchange', onHash);
    onHash();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
