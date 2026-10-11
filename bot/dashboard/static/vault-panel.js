// Vault panel (roadmap P6): the credential vault's entries (name, site, username, which kinds are stored, when added),
// a search box, an add form and delete with a confirmation, plus a plain statement of where the encryption key lives.
// The server never sends a password, a TOTP secret or a note, so this page has none to show. The add form sends the
// secret once and clears its inputs at once; nothing is kept in the page or echoed back. This file is identical in the
// dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/); tests/test_vault_panel.py fails if the two
// differ. It uses the page's own api() and esc(), and draws into #vt-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const errText = (e) => { if (window.ABP && ABP.unitUnavailable && ABP.unitUnavailable(e)) return 'This module is not loaded.'; let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : '-');
  let data = null;
  let q = '';
  let err = '';
  let msg = '';
  let busy = false;
  let draft = {};          // the non-secret fields, kept if a store fails so they need not be retyped

  function css() {
    if ($('vt-css')) return;
    const s = document.createElement('style');
    s.id = 'vt-css';
    s.textContent = `.vt-bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0 10px}
.vt-bar input{max-width:200px}
.vt-note{color:var(--muted);font-size:12px}
.vt-key{background:var(--surface-2);border-radius:8px;padding:8px 12px;margin:8px 0;font-size:13px}
.vt-table{width:100%;border-collapse:collapse;font-size:12.5px}
.vt-table th{text-align:left;color:var(--muted);font-weight:600;padding:6px;border-bottom:1px solid var(--line)}
.vt-table td{padding:6px;border-bottom:1px solid var(--line);vertical-align:top}
.vt-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft);margin-right:4px}
.vt-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin:8px 0}
.vt-form input{width:100%}
.vt-empty{color:var(--muted);text-align:center;padding:18px}`;
    document.head.appendChild(s);
  }

  function rowsHtml() {
    const d = data || { entries: [] };
    const needle = q.trim().toLowerCase();
    const rows = d.entries.filter((e) => !needle || [e.name, e.origin, e.username].some((v) => String(v || '').toLowerCase().includes(needle)));
    return rows.length
      ? rows.map((e) => `<tr><td><b>${E(e.name)}</b></td><td>${E(e.origin)}</td><td>${E(e.username || '-')}</td>
        <td>${e.has_password ? '<span class="vt-chip">password</span>' : ''}${e.has_totp ? '<span class="vt-chip">TOTP</span>' : ''}${!e.has_password && !e.has_totp ? '<span class="vt-chip">username only</span>' : ''}</td>
        <td>${E(when(e.added))}</td><td><button class="btn" data-vt="del" data-name="${E(e.name)}">Delete</button></td></tr>`).join('')
      : `<tr><td colspan="6" class="vt-empty">${!data ? 'Loading…' : d.entries.length ? 'No entry matches the search.' : 'The vault is empty. Add a login below; the agent can then fill it into its own site without ever seeing it.'}</td></tr>`;
  }

  function render() {
    const root = $('vt-root');
    if (!root) return;
    const typing = document.activeElement && document.activeElement.id;
    const d = data || { entries: [], readable: true, key_source: '', key_note: '' };
    const body = rowsHtml();
    root.innerHTML = (err ? `<p class="cardnote">Vault unavailable: ${E(err)}</p>` : '') +
      (d.readable === false ? `<p class="cardnote">The vault cannot be read: ${E(d.error)}</p>` : '') +
      (msg ? `<p class="vt-note" role="status">${E(msg)}</p>` : '') +
      `<div class="vt-key"><b>Key protection: ${E(d.key_source === 'env' ? 'ABP_VAULT_KEY is set' : d.key_source === 'file' ? 'key file beside the vault' : d.key_source === 'none' ? 'no key yet' : '-')}</b><br>${E(d.key_note)}</div>
      <div class="vt-bar"><input id="vt-q" type="search" placeholder="Search name, site or username" aria-label="Search" value="${E(q)}"><button class="btn" data-vt="refresh">Refresh</button>
        <span class="vt-note">The agent sees names, sites and usernames only. Passwords and codes are never shown here.</span></div>
      <table class="vt-table"><thead><tr><th>Name</th><th>Site</th><th>Username</th><th>Stored</th><th>Added</th><th></th></tr></thead><tbody id="vt-body">${body}</tbody></table>
      <h3>Add a login</h3>
      <form id="vt-form" autocomplete="off"><div class="vt-form">
        <input id="vt-name" placeholder="Name (letters, digits, - _ .)" aria-label="Name" maxlength="60" value="${E(draft.name)}">
        <input id="vt-origin" placeholder="Site, e.g. https://github.com" aria-label="Site" value="${E(draft.origin)}">
        <input id="vt-user" placeholder="Username (optional)" aria-label="Username" autocomplete="off" value="${E(draft.username)}">
        <input id="vt-pass" type="password" placeholder="Password" aria-label="Password" autocomplete="new-password">
        <input id="vt-totp" type="password" placeholder="TOTP secret (optional)" aria-label="TOTP secret" autocomplete="off">
      </div><button class="btn" type="submit"${busy ? ' disabled' : ''}>Store</button>
      <span class="vt-note">Sent once over this connection, then forgotten by this page. To change an entry, delete it and add it again.</span></form>`;
    if (typing && $(typing) && typing !== 'vt-pass' && typing !== 'vt-totp') { try { $(typing).focus(); } catch (_) {} }
  }

  async function load() {
    if (!$('vt-root')) return;
    try { data = await api('/api/vault'); err = ''; } catch (e) { err = errText(e); }
    render();
  }

  async function add() {
    const g = (id) => ($(id) || {}).value || '';
    const body = { name: g('vt-name'), origin: g('vt-origin'), username: g('vt-user'), password: g('vt-pass'), totp_secret: g('vt-totp') };
    ['vt-pass', 'vt-totp'].forEach((id) => { if ($(id)) $(id).value = ''; });     // clear the secrets before anything else happens
    busy = true;
    try {
      await api('/api/vault', { method: 'POST', body: JSON.stringify(body) });
      msg = 'Stored ' + body.name.trim() + '.';
      draft = {};
    } catch (e) {
      msg = 'Not stored: ' + errText(e);
      draft = { name: body.name, origin: body.origin, username: body.username };
      body.password = body.totp_secret = '';
    }
    body.password = body.totp_secret = '';
    busy = false;
    await load();
  }

  async function del(name) {
    if (!window.confirm('Delete the stored login "' + name + '"? This cannot be undone.')) return;
    try { await api(`/api/vault/${encodeURIComponent(name)}`, { method: 'DELETE' }); msg = 'Deleted ' + name + '.'; } catch (e) { msg = 'Not deleted: ' + errText(e); }
    await load();
  }

  function init() {
    const root = $('vt-root');
    if (!root) return;
    css();
    render();
    root.addEventListener('click', (ev) => {
      const el = ev.target.closest('[data-vt]');
      if (!el) return;
      if (el.dataset.vt === 'del') del(el.dataset.name); else { msg = ''; load(); }
    });
    root.addEventListener('input', (ev) => { if (ev.target.id === 'vt-q') { q = ev.target.value; if ($('vt-body')) $('vt-body').innerHTML = rowsHtml(); } });
    root.addEventListener('submit', (ev) => { ev.preventDefault(); if (!busy) add(); });
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'vault') load(); };
    window.addEventListener('hashchange', onHash);
    onHash();
    if ('IntersectionObserver' in window) {
      new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting && !data) load(); })).observe(root);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
