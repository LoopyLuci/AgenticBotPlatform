// Octopus panel: the Octopus estate (Octopus-Security) from ABP. Four tabs:
//   Estate       every service, where it answers and whether it is up (its /api/build), grouped by kind
//   Router       octopus-router: status, its own UI in a pane, chat through its routing, models, usage,
//                conversations and missions
//   Integration  keys that let other programs (the Router's Bot Platform view) drive ABP with only the scopes
//                they need, and which sites may show ABP in a frame
//   Sign-in      SSO through octopus-auth (username, password, authenticator code); only the session is kept
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_octopus.py fails if the two differ. It uses the page's own api(), esc() and showToast(), and draws
// into #ocp-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { const d = JSON.parse(m).detail; m = d || m; } catch (_) {} return typeof m === 'string' ? m : JSON.stringify(m); };
  const send = (path, body, method) => api(path, { method: method || 'POST', body: JSON.stringify(body || {}) });
  const KIND_LABEL = { platform: 'Control plane', app: 'Apps', bot: 'Bots', infra: 'Data, security and infrastructure', tool: 'Tools', library: 'Libraries', security: 'Security training' };
  const st = { tab: 'estate', estate: null, router: null, chat: [], timer: null, rv1: 'all', rv2: '' };
  try { st.tab = localStorage.getItem('ocp.tab') || 'estate'; st.rv1 = localStorage.getItem('ocp.rv1') || 'all'; st.rv2 = localStorage.getItem('ocp.rv2') || ''; } catch (_) {}
  const keep = (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} };

  function css() {
    if ($('ocp-css')) return;
    const s = document.createElement('style');
    s.id = 'ocp-css';
    s.textContent = `.ocp-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}.ocp-tabs button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.ocp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(250px,100%),1fr));gap:10px}
.ocp-card{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:var(--surface);min-width:0;display:flex;flex-direction:column;gap:5px}
.ocp-card h3{margin:0;font-size:14px}.ocp-h{margin:14px 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.ocp-muted{color:var(--muted);font-size:12px}.ocp-mono{font-family:var(--font-mono,monospace);font-size:12px;word-break:break-all}
.ocp-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.ocp-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.ocp-chip.on{background:var(--good-soft);color:var(--good,#1f9d55)}.ocp-chip.warn{background:var(--warning-soft);color:#8a5c00}.ocp-chip.bad{background:var(--danger-soft,#fde8e8);color:var(--danger,#c0392b)}
.ocp-form{display:grid;gap:8px;max-width:520px}.ocp-form label{display:grid;gap:3px;font-size:12px;color:var(--ink-soft)}
#ocp-root input,#ocp-root select,#ocp-root textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%}
.ocp-frame{width:100%;height:70vh;border:1px solid var(--line);border-radius:8px;background:#fff}
.ocp-split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}@media (max-width:1100px){.ocp-split{grid-template-columns:1fr}}
.ocp-chat{display:grid;gap:6px;max-height:50vh;overflow:auto}.ocp-msg{padding:6px 9px;border-radius:8px;background:var(--surface-2);white-space:pre-wrap;font-size:13px}.ocp-msg.me{background:var(--accent-soft)}
.ocp-table{width:100%;border-collapse:collapse;font-size:12.5px}.ocp-table td,.ocp-table th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left}
.ocp-secret{padding:8px;border:1px dashed var(--accent);border-radius:8px;background:var(--accent-soft)}`;
    document.head.appendChild(s);
  }
  const chip = (t, k, title) => `<span class="ocp-chip ${k || ''}"${title ? ` title="${E(title)}"` : ''}>${E(t)}</span>`;
  const stateChip = (s) => {
    if (!s || !s.state) return chip('…');
    if (s.state === 'up') return chip(`up · ${s.ms} ms`, 'on', s.http ? `HTTP ${s.http}` : '');
    if (s.state === 'auth') return chip('up · sign-in', 'on', `HTTP ${s.http}`);
    if (s.state === 'no-probe') return chip('no web probe');
    return chip('down', 'bad', s.error || (s.http ? `HTTP ${s.http}` : ''));
  };

  // ---- Estate --------------------------------------------------------------------------------------------------
  async function estate(refresh) {
    st.estate = await api('/api/octopus/estate' + (refresh ? '?refresh=true' : ''));
    const byKind = {};
    for (const s of st.estate.services) (byKind[s.kind] = byKind[s.kind] || []).push(s);
    const up = st.estate.services.filter((s) => ['up', 'auth'].includes(s.status.state)).length;
    const probed = st.estate.services.filter((s) => s.status.state && s.status.state !== 'no-probe').length;
    return `<div class="ocp-row" style="margin-bottom:6px"><button class="btn ghost" id="ocp-est-refresh">Check again</button>
<span class="ocp-muted">${up} of ${probed} probed services answering on ${E(st.estate.domain)} · ${st.estate.services.length} repos in the estate</span></div>` +
      Object.keys(KIND_LABEL).filter((k) => byKind[k]).map((k) => `<div class="ocp-h">${E(KIND_LABEL[k])}</div><div class="ocp-grid">${byKind[k].map((s) => `
<div class="ocp-card"><div class="ocp-row" style="justify-content:space-between"><h3>${E(s.name)}</h3>${stateChip(s.status)}</div>
<div class="ocp-muted">${E(s.purpose)}</div>
<div class="ocp-row">${s.url ? `<a class="btn ghost" href="${E(s.url)}" target="_blank" rel="noopener noreferrer">Open ↗</a>` : ''}<a class="btn ghost" href="${E(s.repo)}" target="_blank" rel="noopener noreferrer">Repo ↗</a>
${s.status.build && (s.status.build.commit || s.status.build.sha || s.status.build.version) ? `<span class="ocp-mono ocp-muted">${E(s.status.build.version || '')} ${E(String(s.status.build.commit || s.status.build.sha || '').slice(0, 7))}</span>` : ''}</div></div>`).join('')}</div>`).join('');
  }

  // ---- Router --------------------------------------------------------------------------------------------------
  async function routerAppCard() {
    const a = await api('/api/octopus/router-app').catch(() => null);
    if (!a) return '';
    const job = a.job || {};
    const chipState = a.running ? chip(`running on :${a.port}`, 'on') : a.installed ? chip('installed, stopped', 'warn') : chip('not installed');
    return `<div class="ocp-card" style="margin-bottom:10px"><div class="ocp-row" style="justify-content:space-between"><h3>Run the Router on this machine</h3>${chipState}</div>
<div class="ocp-muted">ABP clones the latest octopus-router (its private repo; this machine's git sign-in must reach it), builds it, gives it fresh secrets and a scoped ABP key for its Bot Platform view, and points ABP's Router pane and provider at it. It stays its own program; an update never changes its secrets.${a.node ? ` Node ${E(a.node)}.` : ' Needs Node.js 22+.'}${a.commit ? ` <span class="ocp-mono">${E(a.commit)}</span>` : ''}</div>
<div class="ocp-row">${a.installed ? `<button class="btn ghost" data-rapp="update">Update</button>${a.running ? '<button class="btn ghost" data-rapp="stop">Stop</button>' : '<button class="btn" data-rapp="start">Start</button>'}` : '<button class="btn" data-rapp="install">Install the Router here</button>'}</div>
${job.state && job.state !== 'idle' ? `<div class="ocp-muted">${E(job.state)}${job.error ? ': ' + E(job.error) : ''}</div><pre class="ocp-mono" style="max-height:24vh;overflow:auto;white-space:pre-wrap">${E((job.log || []).join('\n'))}</pre>` : ''}</div>`;
  }

  async function routerTab() {
    const appCard = await routerAppCard();
    const r = st.router = await api('/api/octopus/router');
    const conn = !r.reachable ? chip('not reachable', 'bad', r.error) : !r.configured ? chip('no token yet', 'warn') : r.authorized ? chip(`signed in as ${r.owner}`, 'on') : chip('token refused', 'bad', r.error);
    let body = appCard + `<div class="ocp-card"><div class="ocp-row" style="justify-content:space-between"><h3>octopus-router at <span class="ocp-mono">${E(r.url)}</span></h3>${conn}</div>
<div class="ocp-muted">ABP talks to the Router with its owner token (kept in ABP's .env, never shown again). While it is set, the Router is also the model provider <b>octopus-router</b>: its routing aliases (auto, free, cheap, …) work anywhere ABP takes a model.</div>
<form class="ocp-form" id="ocp-rt-form"><div class="ocp-row"><label style="flex:1">Router URL<input id="ocp-rt-url" value="${E(r.url)}"></label>
<label style="flex:1">Owner token (ROUTER_OWNER_TOKEN)<input id="ocp-rt-token" type="password" autocomplete="off" placeholder="${r.configured ? 'set; paste to replace' : 'paste it here'}"></label></div>
<div class="ocp-row"><button class="btn" type="submit">Save</button>${r.configured ? '<button class="btn ghost" type="button" id="ocp-rt-clear">Forget token</button>' : ''}</div></form></div>`;
    if (r.reachable) {
      // The Router's workbench is a tree of panes, one view each. Show the whole of it, or single views as panes of
      // their own (a Router with `?view=` support opens just that view in a window of its own; an older one shows
      // its whole UI instead).
      const views = [['all', 'Whole Router'], ['chat', 'Chat'], ['missions', 'Missions'], ['files', 'Files'], ['terminal', 'Terminal'],
        ['git', 'Source Control'], ['tasks', 'Run & Packages'], ['services', 'Services'], ['bots', 'Bot Platform'], ['browser', 'Browser'],
        ['repos', 'Repos'], ['workspaces', 'Workspaces'], ['keys', 'Keys'], ['usage', 'Usage'], ['settings', 'Settings']];
      const src = (v) => v === 'all' ? `${r.url}/` : `${r.url}/?w=abp${v.replace(/[^a-z]/g, '')}&view=${v}`;
      const pick = (id, cur) => `<select id="${id}">${views.map(([v, l]) => `<option value="${v}"${v === cur ? ' selected' : ''}>${l}</option>`).join('')}</select>`;
      const frame = (v) => `<iframe class="ocp-frame" src="${E(src(v))}" title="octopus-router: ${E(v)}" referrerpolicy="no-referrer"></iframe>`;
      body += `<div class="ocp-row" style="margin:14px 0 6px"><span class="ocp-h" style="margin:0">The Router's own UI</span>${pick('ocp-rv1', st.rv1)}
<label class="ocp-muted"><input type="checkbox" id="ocp-rsplit"${st.rv2 ? ' checked' : ''}> side by side with</label>${st.rv2 ? pick('ocp-rv2', st.rv2) : ''}</div>
<div class="${st.rv2 ? 'ocp-split' : ''}">${frame(st.rv1)}${st.rv2 ? frame(st.rv2) : ''}</div>
<div class="ocp-muted">If a pane stays blank, allow the Router's address on the Integration tab (ABP's frame-src), then reload.</div>`;
    }
    if (r.authorized) {
      const [models, usage, convs, missions] = await Promise.all(['models', 'usage', 'conversations', 'missions'].map((w) => api('/api/octopus/router/' + w).catch((e) => ({ error: errText(e) }))));
      const aliases = (models.models || []).map((m) => `<option value="${E(m.alias)}">${E(m.alias)}${m.cost ? ` (${m.cost})` : ''}</option>`).join('');
      body += `<div class="ocp-split" style="margin-top:12px"><div class="ocp-card"><h3>Chat through the Router</h3>
<div class="ocp-chat" id="ocp-chat">${st.chat.map((m) => `<div class="ocp-msg ${m.role === 'user' ? 'me' : ''}">${E(m.content)}${m.meta ? `<div class="ocp-muted">${E(m.meta)}</div>` : ''}</div>`).join('') || '<div class="ocp-muted">Messages go through the Router\'s routing and your keys there. Free and cheap aliases never spend without asking.</div>'}</div>
<form class="ocp-form" id="ocp-chat-form" style="max-width:none"><div class="ocp-row"><select id="ocp-chat-model">${aliases}</select><button class="btn ghost" type="button" id="ocp-chat-new">New</button></div>
<textarea id="ocp-chat-text" rows="3" placeholder="Ask anything"></textarea><button class="btn" type="submit">Send</button></form></div>
<div style="display:grid;gap:12px"><div class="ocp-card"><h3>Usage</h3>${usage.error ? `<div class="ocp-muted">${E(usage.error)}</div>` : `<table class="ocp-table"><tr><th>Model</th><th>Calls</th><th>Tokens in/out</th><th>Cost</th></tr>${(usage.usage || []).slice(0, 12).map((u) => `<tr><td class="ocp-mono">${E(u.provider)}/${E(u.model)}</td><td>${u.calls}</td><td>${u.inTok || 0} / ${u.outTok || 0}</td><td>$${((u.cents || 0) / 100).toFixed(2)}</td></tr>`).join('')}</table>`}</div>
<div class="ocp-card"><h3>Conversations</h3>${(convs.conversations || []).slice(0, 10).map((c) => `<div class="ocp-row"><span>${E(c.title || c.id)}</span><span class="ocp-muted">${E(c.updatedAt || c.updated_at || '')}</span></div>`).join('') || '<div class="ocp-muted">None yet.</div>'}</div>
<div class="ocp-card"><h3>Missions</h3>${(missions.missions || []).slice(0, 10).map((m) => `<div class="ocp-row"><span>${E(m.title || m.name || m.id)}</span>${m.status ? chip(m.status) : ''}</div>`).join('') || `<div class="ocp-muted">${E(missions.error || 'None yet.')}</div>`}</div></div></div>`;
    }
    return body;
  }

  // ---- Integration ---------------------------------------------------------------------------------------------
  async function integrationTab() {
    const d = await api('/api/integrations');
    const preset = d.presets['octopus-router'];
    return `<div class="ocp-card"><h3>Let octopus-router drive ABP</h3>
<div class="ocp-muted">The Router's Bot Platform view needs an ABP credential. Give it an <b>integration key</b>, not the dashboard token: the key reaches only ${preset.scopes.map((s) => `<span class="ocp-mono">${E(s)}</span>`).join(', ')}. ${E(preset.note)}</div>
<form class="ocp-form" id="ocp-key-form"><label>The Router's address (so it may show ABP in a pane, and ABP may show it)<input id="ocp-key-origin" value="${E((st.router && st.router.url) || 'http://127.0.0.1:3030')}"></label>
<button class="btn" type="submit">Make a Router key</button></form><div id="ocp-key-out"></div></div>
<div class="ocp-h">Integration keys</div><table class="ocp-table"><tr><th>Label</th><th>Scopes</th><th>Last used</th><th></th></tr>
${d.keys.map((k) => `<tr><td>${E(k.label)}${k.revoked ? ' ' + chip('revoked', 'bad') : ''}</td><td class="ocp-mono">${E(k.scopes.join(' '))}</td><td>${E(k.last_used_at || 'never')}</td><td>${k.revoked ? '' : `<button class="btn ghost" data-revoke="${k.id}">Revoke</button>`}</td></tr>`).join('') || '<tr><td colspan="4" class="ocp-muted">None.</td></tr>'}</table>
<div class="ocp-h">Framing</div><form class="ocp-form" id="ocp-frame-form">
<label>Sites that may show ABP in a frame (frame-ancestors), one per line<textarea id="ocp-fa" rows="3">${E(d.frame_ancestors.join('\n'))}</textarea></label>
<label>Sites ABP's panes may show (frame-src), one per line<textarea id="ocp-fs" rows="3">${E(d.frame_src.join('\n'))}</textarea></label>
<button class="btn ghost" type="submit">Save framing</button></form>`;
  }

  // ---- Sign-in -------------------------------------------------------------------------------------------------
  async function ssoTab() {
    const s = await api('/api/octopus/sso');
    const exp = s.expires_at ? new Date(s.expires_at * 1000).toLocaleString() : '';
    return `<div class="ocp-card"><div class="ocp-row" style="justify-content:space-between"><h3>Octopus sign-in (${E(s.auth_url)})</h3>${s.signed_in ? chip(`signed in as ${s.username || '?'}`, 'on') : chip('signed out', 'warn')}</div>
<div class="ocp-muted">ABP signs in once and keeps only the session (7 days, renewed before it runs out); your password and code are sent to octopus-auth and never stored. The session lets ABP's Octopus connectors use the apps as you.</div>
${s.signed_in ? `<div class="ocp-muted">Session ends ${E(exp)}${s.role ? ` · role ${E(s.role)}` : ''}</div><div class="ocp-row"><button class="btn ghost" id="ocp-sso-verify">Check with octopus-auth</button><button class="btn ghost" id="ocp-sso-out">Sign out</button></div>` :
    `<form class="ocp-form" id="ocp-sso-form" autocomplete="off"><label>Username<input id="ocp-sso-user" autocomplete="username"></label><label>Password<input id="ocp-sso-pass" type="password" autocomplete="current-password"></label>
<label>Authenticator or recovery code<input id="ocp-sso-code" inputmode="numeric" autocomplete="one-time-code"></label><button class="btn" type="submit">Sign in</button></form>`}</div>`;
  }

  function wire() {
    document.querySelectorAll('#ocp-root [data-tab]').forEach((b) => b.addEventListener('click', () => { st.tab = b.dataset.tab; try { localStorage.setItem('ocp.tab', st.tab); } catch (_) {} render(); }));
    const on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };
    on('ocp-est-refresh', 'click', () => render(true));
    on('ocp-rt-form', 'submit', async (e) => {
      e.preventDefault();
      try {
        await send('/api/octopus/router/url', { url: $('ocp-rt-url').value }, 'PUT');
        if ($('ocp-rt-token').value) await send('/api/octopus/router/token', { token: $('ocp-rt-token').value }, 'PUT');
        toast('Saved'); render();
      } catch (err) { toast(errText(err), 'error'); }
    });
    document.querySelectorAll('#ocp-root [data-rapp]').forEach((b) => b.addEventListener('click', async () => {
      b.disabled = true;
      try {
        await send('/api/octopus/router-app/' + b.dataset.rapp, {});
        const poll = async () => { const s = await api('/api/octopus/router-app'); if (s.job && s.job.state === 'running') { setTimeout(poll, 3000); } render(); };
        poll();
      } catch (err) { toast(errText(err), 'error'); b.disabled = false; }
    }));
    on('ocp-rv1', 'change', (e) => { st.rv1 = e.target.value; keep('ocp.rv1', st.rv1); render(); });
    on('ocp-rv2', 'change', (e) => { st.rv2 = e.target.value; keep('ocp.rv2', st.rv2); render(); });
    on('ocp-rsplit', 'change', (e) => { st.rv2 = e.target.checked ? (st.rv1 === 'chat' ? 'missions' : 'chat') : ''; keep('ocp.rv2', st.rv2); render(); });
    on('ocp-rt-clear', 'click', async () => { await send('/api/octopus/router/token', { token: '' }, 'PUT'); render(); });
    on('ocp-chat-new', 'click', () => { st.chat = []; render(); });
    on('ocp-chat-form', 'submit', async (e) => {
      e.preventDefault();
      const text = $('ocp-chat-text').value.trim();
      if (!text) return;
      const model = $('ocp-chat-model').value;
      st.chat.push({ role: 'user', content: text });
      $('ocp-chat-text').value = '';
      render();
      try {
        const r = await send('/api/octopus/router/chat', { model, messages: st.chat.map(({ role, content }) => ({ role, content })) });
        st.chat.push({ role: 'assistant', content: r.reply || '', meta: `${r.model || model} · ${r.provider || ''}${r.truncated ? ' · cut off' : ''}` });
      } catch (err) { st.chat.push({ role: 'assistant', content: errText(err), meta: 'error' }); }
      render();
    });
    on('ocp-key-form', 'submit', async (e) => {
      e.preventDefault();
      try {
        const r = await send('/api/integrations/keys', { preset: 'octopus-router', origin: $('ocp-key-origin').value });
        $('ocp-key-out').innerHTML = `<div class="ocp-secret"><b>Copy it now; it is not shown again.</b><div class="ocp-mono" style="margin:6px 0">${E(r.key)}</div><div class="ocp-muted">${E(r.note)}</div><button class="btn ghost" id="ocp-key-copy">Copy</button></div>`;
        $('ocp-key-copy').addEventListener('click', () => navigator.clipboard && navigator.clipboard.writeText(r.key).then(() => toast('Copied')));
      } catch (err) { toast(errText(err), 'error'); }
    });
    document.querySelectorAll('#ocp-root [data-revoke]').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm('Revoke this key? The program using it stops working until it gets a new one.')) return;
      await api('/api/integrations/keys/' + b.dataset.revoke, { method: 'DELETE' }); render();
    }));
    on('ocp-frame-form', 'submit', async (e) => {
      e.preventDefault();
      const lines = (id) => $(id).value.split(/\s+/).map((x) => x.trim()).filter(Boolean);
      try { await send('/api/integrations/framing', { frame_ancestors: lines('ocp-fa'), frame_src: lines('ocp-fs') }, 'PUT'); toast('Saved; reload the page for it to apply here'); render(); } catch (err) { toast(errText(err), 'error'); }
    });
    on('ocp-sso-form', 'submit', async (e) => {
      e.preventDefault();
      try {
        await send('/api/octopus/sso/login', { username: $('ocp-sso-user').value, password: $('ocp-sso-pass').value, code: $('ocp-sso-code').value });
        toast('Signed in'); render();
      } catch (err) { $('ocp-sso-pass').value = ''; toast(errText(err), 'error'); }
    });
    on('ocp-sso-out', 'click', async () => { await send('/api/octopus/sso/logout', {}); render(); });
    on('ocp-sso-verify', 'click', async () => { try { const v = await api('/api/octopus/sso/verify'); toast(v.valid ? `Valid: ${v.user && v.user.username}` : 'Not valid any more: sign in again', v.valid ? '' : 'error'); } catch (err) { toast(errText(err), 'error'); } });
  }

  async function render(refresh) {
    css();
    const root = $('ocp-root');
    if (!root) return;
    const tabs = [['estate', 'Estate'], ['router', 'Router'], ['integration', 'Integration'], ['sso', 'Sign-in']];
    const head = `<div class="ocp-tabs">${tabs.map(([k, l]) => `<button class="btn ghost ${st.tab === k ? 'on' : ''}" data-tab="${k}">${l}</button>`).join('')}</div>`;
    try {
      const html = st.tab === 'router' ? await routerTab() : st.tab === 'integration' ? await integrationTab() : st.tab === 'sso' ? await ssoTab() : await estate(refresh);
      root.innerHTML = head + html;
      const chat = $('ocp-chat'); if (chat) chat.scrollTop = chat.scrollHeight;
    } catch (e) { root.innerHTML = head + `<p class="cardnote">${E(errText(e))}</p>`; }
    wire();
  }

  function init() {
    if (!$('ocp-root')) return;
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'octopus') render(); };
    window.addEventListener('hashchange', onHash);
    onHash();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
