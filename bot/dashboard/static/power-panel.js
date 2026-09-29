// Power panel: keep this machine (or a linked server) awake while it is in use, and wake sleeping machines with
// Wake-on-LAN. Also where the owner chooses what linked servers may control here (peers.remote_control).
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_power.py fails if the two differ. It uses the page's own api(), esc() and showToast(), and draws into
// #pw-root.
(function (api0) {
  'use strict';
  if (typeof api0 !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { const d = JSON.parse(m).detail; m = (d && d.error) || d || m; } catch (_) {} return m; };
  let machine = '';
  try { machine = localStorage.getItem('pw-machine') || ''; } catch (_) {}
  // Calls to /api/power/ go to the chosen linked server through /api/peers/<name>/proxy.
  const api = (path, opts) => {
    if (!machine || !path.startsWith('/api/power/')) return api0(path, opts);
    const o = opts || {};
    return api0('/api/peers/' + encodeURIComponent(machine) + '/proxy', { method: 'POST',
      body: JSON.stringify({ method: o.method || 'GET', path, body: o.body ? JSON.parse(o.body) : null }) }).then((r) => r.result);
  };
  const send = (method, path, body) => api(path, { method, body: JSON.stringify(body || {}) });
  const AREAS = { 'vm-harness': 'VM-Harness (virtual machines, containers, its window)', 'hermes-manager': 'Hermes Manager', transferdaemon: 'TransferDaemon (messages, files, its window, relays)', power: 'Power (keep awake, wake others)', modules: 'Modules (install, update, build and drive every module)' };
  let peers = [];
  let timer = null;

  function css() {
    if ($('pw-css')) return;
    const s = document.createElement('style');
    s.id = 'pw-css';
    s.textContent = `.pw-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(320px,100%),1fr));gap:12px}
.pw-card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--surface);min-width:0}
.pw-card h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.pw-muted{color:var(--muted);font-size:12px}.pw-mono{font-family:var(--font-mono,monospace);font-size:12px;word-break:break-all}
.pw-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:4px 0}
.pw-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.pw-chip.on{background:var(--good-soft);color:var(--good,#1f9d55)}.pw-chip.warn{background:var(--warning-soft);color:#8a5c00}
.pw-table{width:100%;border-collapse:collapse;font-size:12.5px}.pw-table th{text-align:left;color:var(--muted);font-weight:600;padding:5px;border-bottom:1px solid var(--line)}
.pw-table td{padding:5px;border-bottom:1px solid var(--line);vertical-align:top}.pw-scroll{overflow:auto;max-width:100%}
#pw-root input,#pw-root select{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%}
#pw-root ul{margin:4px 0;padding-left:18px}`;
    document.head.appendChild(s);
  }

  const peerOptions = (sel) => peers.map((p) => `<option value="${E(p.name)}" ${p.name === sel ? 'selected' : ''}>${E(p.name)}</option>`).join('');

  async function render() {
    css();
    const root = $('pw-root');
    if (!root) return;
    try { peers = await api0('/api/peers'); } catch (_) { peers = []; }
    if (machine && !peers.some((p) => p.name === machine)) machine = '';
    let st, info, control;
    try {
      [st, info] = await Promise.all([api('/api/power/status'), api('/api/power/info').catch(() => null)]);
      control = machine ? null : await api0('/api/peers/control').catch(() => null);
    } catch (e) {
      root.innerHTML = head() + `<p class="cardnote">${E(errText(e))}</p>`;
      wireHead(root);
      return;
    }
    const until = (h) => (h.until ? 'until ' + new Date(h.until * 1000).toLocaleTimeString() : 'until released');
    const where = machine ? E(machine) : 'this machine';
    const targets = Object.entries(st.wake || {});
    root.innerHTML = head() + `<div class="pw-grid">
<div class="pw-card"><h4>Staying awake: ${where}</h4>
  <div class="pw-row">${st.awake_requested ? '<span class="pw-chip on">kept awake</span>' : '<span class="pw-chip">may sleep</span>'}${st.display_on ? ' <span class="pw-chip">display on</span>' : ''}</div>
  ${st.reasons.length ? `<ul>${st.reasons.map((r) => `<li>${E(r)}</li>`).join('')}</ul>` : '<p class="pw-muted">Nothing needs it right now, so it can sleep normally.</p>'}
  ${st.holds.length ? `<table class="pw-table"><tr><th>Hold</th><th>Asked by</th><th></th><th></th></tr>${st.holds.map((h) => `<tr><td>${E(h.reason)}</td><td>${E(h.by || '')}</td><td class="pw-muted">${until(h)}</td><td><button class="ghost" data-release="${E(h.key)}">Release</button></td></tr>`).join('')}</table>` : ''}
  <div class="pw-row"><label class="pw-muted">Keep awake for <input id="pw-min" type="number" min="0" value="60" style="width:70px"> min (0 = until released)</label></div>
  <div class="pw-row"><input id="pw-reason" placeholder="why (shown to anyone looking)" style="flex:1"><button class="primary" id="pw-hold">Keep awake</button></div>
</div>
<div class="pw-card"><h4>Settings: ${where}</h4>
  <div class="pw-row"><label class="pw-muted">Mode <select id="pw-mode">${[['while_busy', 'While in use (agent turns, jobs, a linked server using it)'], ['always', 'Always stay awake'], ['off', 'Never (the OS decides)']].map(([v, t]) => `<option value="${v}" ${st.keep_awake === v ? 'selected' : ''}>${t}</option>`).join('')}</select></label></div>
  <div class="pw-row"><label class="pw-muted">Stay awake <input id="pw-idle" type="number" min="0" step="1" value="${E(st.idle_minutes)}" style="width:70px"> min after the last use</label></div>
  <div class="pw-row"><label><input type="checkbox" id="pw-display" ${st.keep_display_on ? 'checked' : ''}> Keep the display on too</label></div>
  <div class="pw-row"><label><input type="checkbox" id="pw-auto" ${st.auto_wake ? 'checked' : ''}> Wake a linked server automatically when it does not answer</label></div>
  <div class="pw-row"><button class="primary" id="pw-save">Save</button></div>
</div>
<div class="pw-card"><h4>Wake a machine</h4>
  ${targets.length ? `<table class="pw-table"><tr><th>Machine</th><th>Card</th><th></th></tr>${targets.map(([n, t]) => `<tr><td>${E(n)}<div class="pw-muted">${E(t.hostname || '')} ${E(t.ip || '')}</div></td><td class="pw-mono">${E(t.mac)}<div class="pw-muted">${E(t.broadcast || '')}</div></td><td><div class="pw-row"><button class="primary" data-wake="${E(n)}">Wake</button><select data-via="${E(n)}" title="send the packet from a machine on its network"><option value="">from here</option>${peerOptions('')}</select></div></td></tr>`).join('')}</table>` : '<p class="pw-muted">No machines learned yet.</p>'}
  ${peers.length ? `<div class="pw-row"><span class="pw-muted">Learn a linked server's network card:</span>${peers.map((p) => `<button class="ghost" data-learn="${E(p.name)}">${E(p.name)}</button>`).join('')}</div>` : ''}
  <div class="pw-row"><input id="pw-mac" placeholder="MAC (AA:BB:CC:DD:EE:FF)" style="width:170px"><input id="pw-bcast" placeholder="broadcast (optional)" style="width:140px"><button class="ghost" id="pw-wake-mac">Wake</button></div>
  <p class="pw-muted">A sleeping machine wakes only if its network card and firmware allow Wake-on-LAN, and the packet must be sent on its own network: for a machine on another subnet, send it from a linked server on that network.</p>
</div>
<div class="pw-card"><h4>Network cards: ${where}</h4>${info ? `<div class="pw-scroll"><table class="pw-table"><tr><th>Card</th><th>MAC</th><th>Address</th><th>Wake</th></tr>${info.cards.map((c) => `<tr><td>${E(c.name)}${c.physical ? '' : ' <span class="pw-chip">virtual</span>'}</td><td class="pw-mono">${E(c.mac)}</td><td class="pw-mono">${E(c.ip)}</td><td>${c.wake_on_magic_packet == null ? '<span class="pw-muted">?</span>' : /enabled/i.test(c.wake_on_magic_packet) ? '<span class="pw-chip on">enabled</span>' : `<span class="pw-chip warn">${E(c.wake_on_magic_packet)}</span>`}</td></tr>`).join('')}</table></div><p class="pw-muted">To be woken, a card's Wake on Magic Packet must be enabled (Device Manager → the card → Power Management and Advanced), and Wake-on-LAN on in the firmware.</p>` : '<p class="pw-muted">Not available.</p>'}</div>
${control ? `<div class="pw-card"><h4>What linked servers may control here</h4>
  ${control.areas.map((a) => `<div class="pw-row"><label><input type="checkbox" data-area="${E(a)}" ${control.allowed.includes(a) ? 'checked' : ''}> ${E(AREAS[a] || a)}</label></div>`).join('')}
  <p class="pw-muted">A linked server can always see this machine's overview and start or stop its bots. These let it do more, through its own ABP. Only this machine can change this.</p>
  <div class="pw-row"><button class="primary" id="pw-control">Save</button></div></div>` : ''}
</div>`;
    wireHead(root);
    const act = async (fn, ok) => { try { await fn(); if (ok) toast(ok, 'success'); render(); } catch (e) { toast(errText(e), 'error'); } };
    root.querySelectorAll('[data-release]').forEach((b) => b.addEventListener('click', () => act(() => send('POST', '/api/power/release', { key: b.dataset.release }), 'Released')));
    $('pw-hold').addEventListener('click', () => act(() => send('POST', '/api/power/hold', { minutes: parseFloat($('pw-min').value || '60'), reason: $('pw-reason').value || 'kept awake from the Power page', by: machine ? 'a linked server' : '' }), 'Kept awake'));
    $('pw-save').addEventListener('click', () => act(() => send('PUT', '/api/power/settings', { keep_awake: $('pw-mode').value, idle_minutes: parseFloat($('pw-idle').value || '10'), keep_display_on: $('pw-display').checked, auto_wake: $('pw-auto').checked }), 'Saved'));
    root.querySelectorAll('[data-wake]').forEach((b) => b.addEventListener('click', () => {
      const via = root.querySelector(`[data-via="${CSS.escape(b.dataset.wake)}"]`).value;
      act(() => send('POST', '/api/power/wake', { target: b.dataset.wake, via: via || undefined }), 'Wake packet sent to ' + b.dataset.wake);
    }));
    root.querySelectorAll('[data-learn]').forEach((b) => b.addEventListener('click', () => act(() => send('POST', '/api/power/learn/' + encodeURIComponent(b.dataset.learn)), 'Learned ' + b.dataset.learn)));
    $('pw-wake-mac').addEventListener('click', () => act(() => send('POST', '/api/power/wake', { mac: $('pw-mac').value.trim(), broadcast: $('pw-bcast').value.trim() || undefined }), 'Wake packet sent'));
    if ($('pw-control')) $('pw-control').addEventListener('click', () => act(() => api0('/api/peers/control', { method: 'PUT', body: JSON.stringify({ allowed: [...root.querySelectorAll('[data-area]')].filter((c) => c.checked).map((c) => c.dataset.area) }) }), 'Saved'));
  }

  function head() {
    return `<div class="pw-row" style="margin-bottom:8px"><label class="pw-muted">Machine <select id="pw-machine"><option value="">This machine</option>${peerOptions(machine)}</select></label>${machine ? `<span class="pw-muted">through ${E(machine)}'s ABP (it must allow power)</span>` : ''}</div>`;
  }

  function wireHead(root) {
    const sel = root.querySelector('#pw-machine');
    if (sel) sel.addEventListener('change', () => { machine = sel.value; try { localStorage.setItem('pw-machine', machine); } catch (_) {} render(); });
  }

  function init() {
    const root = $('pw-root');
    if (!root) return;
    const onHash = () => {
      clearInterval(timer); timer = null;
      if ((location.hash || '').replace('#', '') === 'power') { render(); timer = setInterval(() => { if (!document.hidden && !root.contains(document.activeElement)) render(); }, 30000); }
    };
    window.addEventListener('hashchange', onHash);
    onHash();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
