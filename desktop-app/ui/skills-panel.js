// Skills panel (roadmap P4): installed skill packs, the quarantine (scan findings and signature status, approve or
// reject), skill drafts the agent wrote (the text, an automatic advisory check, approve or reject), and a form that
// fetches a pack from a git URL into quarantine. Approving is always a person's click on a write-auth route, after a
// confirmation; the advisory check never approves anything. Skill text is shown as text, never as HTML. This file is
// identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/); tests/test_skills_panel.py
// fails if the two differ. It uses the page's own api() and esc(), and draws into #sk-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const errText = (e) => { if (window.ABP && ABP.unitUnavailable && ABP.unitUnavailable(e)) return 'This module is not loaded.'; let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : '-');
  let packs = null, held = null, drafts = null;
  let evals = {};          // draft name -> evaluation (or { error })
  let open = {};           // draft name -> text expanded
  let msg = '', err = '', busy = false;

  function css() {
    if ($('sk-css')) return;
    const s = document.createElement('style');
    s.id = 'sk-css';
    s.textContent = `.sk-card{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:8px 0}
.sk-note{color:var(--muted);font-size:12px}
.sk-bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0}
.sk-bar input{min-width:240px}
.sk-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.sk-chip.bad{background:var(--critical-soft);color:var(--critical)}
.sk-chip.warn{color:var(--warning)}
.sk-pre{white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,monospace;font-size:12px;background:var(--surface-2);border-radius:8px;padding:8px;max-height:320px;overflow:auto}
.sk-list{margin:4px 0 4px 18px;padding:0;font-size:12.5px}
.sk-empty{color:var(--muted);padding:8px 0}`;
    document.head.appendChild(s);
  }

  const chip = (st) => `<span class="sk-chip ${st === 'pass' || st === 'ok' || st === 'trusted' ? '' : st === 'fail' || st === 'invalid' || st === 'BLOCKED' ? 'bad' : 'warn'}">${E(st)}</span>`;

  function renderPacks() {
    if (!packs) return '<p class="sk-empty">Loading…</p>';
    if (!packs.length) return '<p class="sk-empty">No skill packs are installed.</p>';
    return packs.map((p) => `<div class="sk-card"><b>${E(p.name)}</b> <span class="sk-note">${E(p.source)} · ${E(p.files)} bundled file(s)</span>
      <div>${E(p.description)}</div>${(p.problems || []).length ? `<div class="sk-note">${p.problems.map(E).join('; ')}</div>` : ''}${p.source === 'user' ? `<div class="sk-bar"><button class="btn" data-sk="remove-pack" data-name="${E(p.name)}">Remove…</button></div>` : ''}</div>`).join('');
  }

  function renderHeld() {
    if (!held) return '<p class="sk-empty">Loading…</p>';
    if (!held.length) return '<p class="sk-empty">Nothing is in quarantine.</p>';
    return held.map((h) => {
      const name = h.quarantine_name || h.name;
      const fs = h.findings || [];
      return `<div class="sk-card"><b>${E(name)}</b> ${h.ok ? chip('ok') : chip('BLOCKED')} <span class="sk-note">signature: ${E(h.signature || 'none')} · ${E(h.files)} file(s) · ${E(h.bytes)} bytes · ${E(h.source || '')} · ${E(when(h.quarantined_at))}</span>
        <div>${E(h.description || '')}</div>
        ${fs.length ? `<ul class="sk-list">${fs.map((f) => `<li>${chip(f.severity === 'block' ? 'fail' : 'warn')} ${E(f.file)}: ${E(f.message)}</li>`).join('')}</ul>` : '<div class="sk-note">The scan found nothing. The scan is a filter, not a guarantee.</div>'}
        <div class="sk-bar"><button class="btn" data-sk="approve" data-name="${E(name)}"${h.ok ? '' : ' disabled title="A pack with blocking findings cannot be approved"'}>Approve…</button>
        <button class="btn" data-sk="reject" data-name="${E(name)}">Reject…</button></div></div>`;
    }).join('');
  }

  function renderEval(name) {
    const ev = evals[name];
    if (!ev) return '<div class="sk-note">Checking…</div>';
    if (ev.error) return `<div class="sk-note">The automatic check is unavailable: ${E(ev.error)}</div>`;
    return `<div><b>Automatic check:</b> ${chip(ev.verdict === 'review' ? 'warn' : ev.verdict)} <span class="sk-note">${E(ev.verdict)} · advice only, it approves nothing</span>
      <ul class="sk-list">${(ev.checks || []).map((c) => `<li>${chip(c.status)} <b>${E(c.id)}</b> ${E(c.detail)}</li>`).join('')}</ul></div>`;
  }

  function renderDrafts() {
    if (!drafts) return '<p class="sk-empty">Loading…</p>';
    if (!drafts.length) return '<p class="sk-empty">No drafts. The agent writes them only when skill learning is switched on.</p>';
    return drafts.map((d) => `<div class="sk-card"><b>${E(d.name)}</b> <span class="sk-note">${E(when(d.created))}${(d.similar_to || []).length ? ' · similar to ' + d.similar_to.map(E).join(', ') : ''}</span>
      <div>${E(d.description)}</div>${renderEval(d.name)}
      <div class="sk-bar"><button class="btn" data-sk="toggle" data-name="${E(d.name)}">${open[d.name] ? 'Hide text' : 'Read the draft'}</button>
        <button class="btn" data-sk="approve-draft" data-name="${E(d.name)}">Approve…</button>
        <button class="btn" data-sk="reject-draft" data-name="${E(d.name)}">Reject…</button></div>
      ${open[d.name] ? `<div class="sk-pre">${E(d.text)}</div>` : ''}</div>`).join('');
  }

  function render() {
    const root = $('sk-root');
    if (!root) return;
    const keep = document.activeElement && document.activeElement.id;
    root.innerHTML = `${err ? `<p class="cardnote">Skills unavailable: ${E(err)}</p>` : ''}${msg ? `<p class="cardnote">${E(msg)}</p>` : ''}
      <div class="sk-bar"><button class="btn" data-sk="refresh">Refresh</button></div>
      <h3>Fetch from a git URL</h3>
      <p class="sk-note">The pack is cloned and scanned, then held in quarantine. Nothing is installed until you approve it below.</p>
      <div class="sk-bar"><input id="sk-url" type="url" placeholder="https://github.com/owner/repo" aria-label="Git URL">
        <input id="sk-ref" type="text" placeholder="Branch or tag (optional)" aria-label="Ref" style="min-width:160px">
        <input id="sk-sub" type="text" placeholder="Folder in the repo (optional)" aria-label="Subfolder" style="min-width:160px">
        <button class="btn" data-sk="fetch"${busy ? ' disabled' : ''}>Fetch into quarantine</button></div>
      <h3>Quarantine</h3>${renderHeld()}
      <h3>Drafts written by the agent</h3>${renderDrafts()}
      <h3>Installed skills</h3>${renderPacks()}`;
    if (keep && $(keep)) { try { $(keep).focus(); } catch (_) {} }
  }

  async function loadEvals() {
    for (const d of drafts || []) {
      if (evals[d.name]) continue;
      try { evals[d.name] = await api('/api/skills/drafts/' + encodeURIComponent(d.name) + '/evaluation'); }
      catch (e) { evals[d.name] = { error: errText(e) }; }
      render();
    }
  }

  async function load() {
    if (!$('sk-root')) return;
    try {
      [packs, held, drafts] = (await Promise.all([api('/api/skills/packs'), api('/api/skills/quarantine'), api('/api/skills/drafts')])).map((r, i) => r[['packs', 'packs', 'drafts'][i]] || []);
      err = '';
    } catch (e) {
      err = errText(e);
    }
    evals = {};
    render();
    loadEvals();
  }

  async function post(path, body, done, method) {
    try {
      const r = await api(path, { method: method || 'POST', body: body ? JSON.stringify(body) : undefined });
      msg = done(r);
      err = '';
    } catch (e) {
      msg = '';
      err = errText(e);
    }
    await load();
  }

  function act(a, name) {
    if (a === 'refresh') { msg = ''; return load(); }
    if (a === 'toggle') { open[name] = !open[name]; return render(); }
    if (a === 'fetch') {
      const url = ($('sk-url') || {}).value || '';
      if (!url.trim()) { err = 'Enter a git URL first.'; return render(); }
      busy = true; render();
      const body = { url: url.trim(), ref: (($('sk-ref') || {}).value || '').trim() || null, subdir: (($('sk-sub') || {}).value || '').trim() };
      return post('/api/skills/fetch', body, (r) => `Fetched ${r.name || 'the pack'} into quarantine (${r.ok ? 'no blocking findings' : 'blocked by the scan'}). Review it below.`).finally(() => { busy = false; render(); });
    }
    if (a === 'remove-pack') {
      if (!window.confirm(`Remove the installed skill ${name}? Its folder is deleted and the agent can no longer use it.`)) return;
      return post('/api/skills/packs/' + encodeURIComponent(name), null, () => `Removed ${name}.`, 'DELETE');
    }
    const ask = {
      approve: ['/api/skills/quarantine/approve', `Approve ${name}? It becomes an installed skill that the agent can read and follow. Only approve a pack you have read.`, () => `Approved ${name}.`],
      reject: ['/api/skills/quarantine/reject', `Reject ${name}? It is deleted from quarantine.`, () => `Rejected ${name}.`],
      'approve-draft': ['/api/skills/drafts/approve', `Approve the draft ${name}? It becomes an installed skill that the agent can read and follow. The automatic check is advice only.`, () => `Approved draft ${name}.`],
      'reject-draft': ['/api/skills/drafts/reject', `Reject the draft ${name}? It is deleted.`, () => `Rejected draft ${name}.`],
    }[a];
    if (!ask || !window.confirm(ask[1])) return;
    return post(ask[0], { name }, ask[2]);
  }

  function init() {
    const root = $('sk-root');
    if (!root) return;
    css();
    render();
    root.addEventListener('click', (ev) => {
      const el = ev.target.closest('[data-sk]');
      if (el && !el.disabled) act(el.dataset.sk, el.dataset.name);
    });
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'skills') load(); };
    window.addEventListener('hashchange', onHash);
    onHash();
    if ('IntersectionObserver' in window) {
      new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting && !packs) load(); })).observe(root);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
