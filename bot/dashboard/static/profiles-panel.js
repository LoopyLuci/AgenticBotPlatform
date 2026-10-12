// User profiles panel (roadmap P3): the optional user model. For each person a bot talks to, the notes the agent has
// proposed (preferences, expertise, standing instructions, open goals) with their evidence and confidence. A proposal is
// never used until it is approved here (or by the person with /profile); approved notes go into the prompt as the
// person's preferences, never as permissions. A profile can be edited, exported, and forgotten item by item or entirely.
// Everything is shown as text, never as HTML. This file is identical in the dashboard (bot/dashboard/static/) and the
// desktop app (desktop-app/ui/); tests/test_user_model.py fails if the two differ. It uses the page's own api() and
// esc(), and draws into #up-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const errText = (e) => { if (window.ABP && ABP.unitUnavailable && ABP.unitUnavailable(e)) return 'This module is not loaded.'; let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : '-');
  const KINDS = { preference: 'Preferences', expertise: 'Expertise', instruction: 'Standing instructions', goal: 'Open goals' };
  let overview = null, picked = null, items = null;
  let msg = '', err = '';

  function css() {
    if ($('up-css')) return;
    const s = document.createElement('style');
    s.id = 'up-css';
    s.textContent = `.up-card{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:8px 0}
.up-note{color:var(--muted);font-size:12px}
.up-bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0}
.up-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.up-chip.warn{color:var(--warning)}
.up-chip.bad{background:var(--critical-soft);color:var(--critical)}
.up-ev{margin:4px 0 0 18px;padding:0;font-size:12px;color:var(--muted)}
.up-pre{white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,monospace;font-size:12px;background:var(--surface-2);border-radius:8px;padding:8px;max-height:320px;overflow:auto}
.up-empty{color:var(--muted);padding:8px 0}`;
    document.head.appendChild(s);
  }

  const chip = (st) => `<span class="up-chip ${st === 'approved' ? '' : st === 'rejected' ? 'bad' : 'warn'}">${E(st)}</span>`;
  const q = (u) => `instance_id=${encodeURIComponent(u.instance_id)}&user_id=${encodeURIComponent(u.user_id)}`;

  function renderUsers() {
    if (!overview) return '<p class="up-empty">Loading…</p>';
    if (!overview.users.length) return `<p class="up-empty">No profiles yet. ${overview.enabled ? 'They appear once the agent has proposed something about a person.' : 'The user model is off.'}</p>`;
    return overview.users.map((u, i) => `<div class="up-card"><b>Bot #${E(u.instance_id)}</b> · user ${E(u.user_id)}
      <span class="up-note">${E(u.approved)} in use · ${E(u.pending)} waiting for approval</span>
      <div class="up-bar"><button class="btn" data-up="open" data-i="${i}">${picked === i ? 'Refresh' : 'Review'}</button></div></div>`).join('');
  }

  function renderItems() {
    if (picked == null || !overview) return '';
    const u = overview.users[picked];
    if (!u) return '';
    if (!items) return '<p class="up-empty">Loading…</p>';
    const shown = items.filter((i) => i.status !== 'rejected');
    const body = Object.keys(KINDS).map((k) => {
      const rows = shown.filter((i) => i.kind === k);
      if (!rows.length) return '';
      return `<h4>${E(KINDS[k])}</h4>` + rows.map((i) => `<div class="up-card"><div>${E(i.text)} ${chip(i.status)}
        <span class="up-note">confidence ${E(Math.round(i.confidence * 100))}% · ${E(i.source)} · updated ${E(when(i.updated))}</span></div>
        <ul class="up-ev">${(i.evidence || []).map((e) => `<li>${e.turn ? 'turn ' + E(e.turn) : ''}${e.session ? ' of ' + E(e.session) : ''}${e.quote ? ': “' + E(e.quote) + '”' : ''} <span class="up-note">${E(when(e.at))}</span></li>`).join('')}</ul>
        <div class="up-bar">${i.status === 'pending' ? `<button class="btn" data-up="approve" data-id="${E(i.id)}">Approve…</button><button class="btn" data-up="reject" data-id="${E(i.id)}">Reject</button>` : ''}
          <button class="btn" data-up="edit" data-id="${E(i.id)}">Edit…</button><button class="btn" data-up="forget" data-id="${E(i.id)}">Forget…</button></div></div>`).join('');
    }).join('');
    return `<h3>Bot #${E(u.instance_id)} · user ${E(u.user_id)}</h3>
      <div class="up-bar"><button class="btn" data-up="export">Export</button><button class="btn" data-up="forget-all">Forget everything…</button></div>
      ${body || '<p class="up-empty">Nothing is kept for this person.</p>'}`;
  }

  function render() {
    const root = $('up-root');
    if (!root) return;
    root.innerHTML = `${err ? `<p class="cardnote">User profiles unavailable: ${E(err)}</p>` : ''}${msg ? `<p class="cardnote">${E(msg)}</p>` : ''}${exported ? `<div class="up-pre">${E(exported)}</div>` : ''}
      <p class="up-note">${overview ? (overview.enabled ? 'The user model is on' + (overview.auto_approve ? ', and proposals are used without review.' : '; proposals wait for approval.') : 'The user model is off (Agents, Tools, User model). Nothing is learned or used.') : ''}
      Approved notes are the person's own preferences in the prompt, never permissions. A person can also review theirs in chat with /profile.</p>
      <div class="up-bar"><button class="btn" data-up="refresh">Refresh</button></div>
      <h3>People with a profile</h3>${renderUsers()}${renderItems()}`;
  }

  async function loadItems() {
    const u = picked != null && overview ? overview.users[picked] : null;
    if (!u) { items = null; return render(); }
    try { items = (await api('/api/user-model/profile?' + q(u))).items || []; err = ''; } catch (e) { err = errText(e); }
    render();
  }

  async function load() {
    if (!$('up-root')) return;
    try { overview = await api('/api/user-model'); err = ''; } catch (e) { err = errText(e); }
    if (overview && picked != null && !overview.users[picked]) picked = null;
    render();
    loadItems();
  }

  async function call(path, method, body, done) {
    try { const r = await api(path, { method, body: body ? JSON.stringify(body) : undefined }); msg = done(r); err = ''; }
    catch (e) { msg = ''; err = errText(e); }
    await load();
  }

  let exported = '';
  function act(a, el) {
    const u = picked != null && overview ? overview.users[picked] : null;
    const id = el.dataset.id;
    exported = '';
    if (a === 'refresh') { msg = ''; return load(); }
    if (a === 'open') { picked = Number(el.dataset.i); items = null; return loadItems(); }
    if (!u) return;
    const who = { instance_id: u.instance_id, user_id: u.user_id };
    if (a === 'approve') {
      if (!window.confirm('Approve this note? The agent will read it as this person\'s preference in every conversation with them. It is never a permission.')) return;
      return call(`/api/user-model/items/${encodeURIComponent(id)}/approve`, 'POST', who, () => 'Approved.');
    }
    if (a === 'reject') return call(`/api/user-model/items/${encodeURIComponent(id)}/reject`, 'POST', who, () => 'Rejected.');
    if (a === 'edit') {
      const cur = (items || []).find((i) => String(i.id) === String(id));
      const text = window.prompt('Reword this note. Saving approves it.', cur ? cur.text : '');
      if (text == null) return;
      return call(`/api/user-model/items/${encodeURIComponent(id)}`, 'PUT', { ...who, text }, () => 'Saved and approved.');
    }
    if (a === 'forget') {
      if (!window.confirm('Forget this note? It is deleted.')) return;
      return call(`/api/user-model/items/${encodeURIComponent(id)}?${q(u)}`, 'DELETE', null, () => 'Forgotten.');
    }
    if (a === 'forget-all') {
      if (!window.confirm('Forget everything about this person in this bot? Every note, approved or not, is deleted.')) return;
      picked = null;
      return call(`/api/user-model/profile?${q(u)}`, 'DELETE', null, (r) => `Forgot ${r.forgotten} note(s).`);
    }
    if (a === 'export') {
      return api('/api/user-model/export?' + q(u)).then((r) => { msg = 'Everything held about this person:'; exported = JSON.stringify(r, null, 1); err = ''; render(); }, (e) => { err = errText(e); render(); });
    }
  }

  function init() {
    const root = $('up-root');
    if (!root) return;
    css();
    render();
    root.addEventListener('click', (ev) => {
      const el = ev.target.closest('[data-up]');
      if (el && !el.disabled) act(el.dataset.up, el);
    });
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'profiles') load(); };
    window.addEventListener('hashchange', onHash);
    onHash();
    if ('IntersectionObserver' in window) {
      new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting && !overview) load(); })).observe(root);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
