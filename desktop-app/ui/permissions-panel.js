// Permissions & approvals panel (roadmap P2 + P6): pending approvals with a preview and Approve / Deny, recent
// decisions, the permission mode and rules (the host's, read-only, and a chosen bot's, editable), MCP tool pins
// and which sessions have read untrusted content. It only calls routes the server already has; the server decides
// who may do what (a paired device may approve one call but not grant a standing approval; changing rules or
// pins needs the dashboard token; a host that locked permissions refuses rule edits). This file is identical in
// the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/); tests/test_permissions_panel.py
// fails if the two differ. It uses the page's own api() and esc(), and draws into #pm-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const errText = (e) => { if (window.ABP && ABP.unitUnavailable && ABP.unitUnavailable(e)) return 'This module is not loaded.'; let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const REFRESH_MS = 8000;
  const MODE_HELP = { default: 'Each tool follows its own approval setting', plan: 'Read-only: anything that changes something is denied', accept_edits: 'File edits are allowed without asking', bypass: 'Everything allowed (only if the host permits bypass)' };
  const DECISIONS = ['allow', 'ask', 'deny'];
  let visible = false;
  let loading = false;
  let live = { approvals: null, pins: null, taint: [], err: '' };
  let host = null;                 // GET /api/agent/permissions
  let bots = [];
  let botId = '';
  let bot = null;                  // GET /api/instances/{id}/permissions
  let editing = null;              // index of the bot rule being edited, -1 for a new one, null for none
  let note = '';                   // result of the last action
  let rulesErr = '';
  let lookup = null;               // result of a taint lookup by hand

  function css() {
    if ($('pm-css')) return;
    const s = document.createElement('style');
    s.id = 'pm-css';
    s.textContent = `.pm-block{margin:0 0 22px}
.pm-bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0 10px}
.pm-bar input,.pm-bar select{max-width:200px}
.pm-note{color:var(--muted);font-size:12px}
.pm-scroll{overflow-x:auto}
.pm-table{width:100%;border-collapse:collapse;font-size:12.5px}
.pm-table th{text-align:left;color:var(--muted);font-weight:600;padding:6px;border-bottom:1px solid var(--line);white-space:nowrap}
.pm-table td{padding:6px;border-bottom:1px solid var(--line);vertical-align:top}
.pm-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.pm-chip.bad{background:var(--critical-soft);color:var(--critical)}
.pm-chip.warn{background:var(--surface-2);color:var(--warning)}
.pm-card{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:8px 0}
.pm-card pre{margin:6px 0;padding:8px;background:var(--surface-2);border-radius:6px;max-height:220px;overflow:auto;white-space:pre-wrap;word-break:break-word;font-size:12px}
.pm-mono{font-family:ui-monospace,monospace;word-break:break-all}
.pm-empty{color:var(--muted);padding:12px 0}
.pm-form{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:8px;background:var(--surface-2);border-radius:8px;margin:6px 0}`;
    document.head.appendChild(s);
  }

  const chip = (t, cls) => `<span class="pm-chip ${cls || ''}">${E(t)}</span>`;
  const ageText = (iso) => {
    const t = Date.parse(iso || '');
    if (isNaN(t)) return '-';
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    return s < 90 ? s + ' s ago' : s < 5400 ? Math.round(s / 60) + ' min ago' : s < 129600 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago';
  };
  const botName = (id) => { const b = bots.find((x) => String(x.id) === String(id)); return b ? (b.name || 'bot ' + id) : 'bot ' + id; };

  // ---- approvals ----
  function approvalCard(a) {
    const bad = a.untrusted_content_in_session ? ' ' + chip('session read untrusted content', 'warn') : '';
    return `<div class="pm-card"><div><b>${E(a.tool)}</b> · ${E(botName(a.instance_id))} · <span class="pm-note">${E(ageText(a.created_at))}, session ${E(a.session)}</span>${bad}</div>
      <div>${E(a.summary)}</div><pre>${E(a.body)}</pre>
      <div class="pm-bar"><button class="btn" data-pm="decide" data-id="${E(a.id)}" data-outcome="once">Approve once</button>
        <button class="btn" data-pm="decide" data-id="${E(a.id)}" data-outcome="session" title="Allow this tool for the rest of the session (needs the dashboard token)">Allow for session</button>
        <button class="btn" data-pm="decide" data-id="${E(a.id)}" data-outcome="always" title="Always allow this tool for this bot (needs the dashboard token)">Always allow</button>
        <button class="btn" data-pm="decide" data-id="${E(a.id)}" data-outcome="deny">Deny</button></div></div>`;
  }

  function renderApprovals() {
    const all = live.approvals;
    if (!all) return '<p class="pm-empty">Loading…</p>';
    const pending = all.filter((a) => a.status === 'pending');
    const done = all.filter((a) => a.status !== 'pending').slice(0, 15);
    const hist = done.length
      ? `<div class="pm-scroll"><table class="pm-table"><thead><tr><th>Decision</th><th>Tool</th><th>What</th><th>Bot</th><th>By</th><th>When</th></tr></thead><tbody>${done.map((a) => `<tr><td>${chip(String(a.status).replace('approved_', 'approved '), a.status === 'denied' || a.status === 'expired' ? 'bad' : '')}</td><td>${E(a.tool)}</td><td>${E(a.summary)}</td><td>${E(botName(a.instance_id))}</td><td>${E(a.resolved_by || '-')}</td><td class="pm-note">${E(ageText(a.resolved_at || a.created_at))}</td></tr>`).join('')}</tbody></table></div>`
      : '<p class="pm-empty">No decisions yet.</p>';
    return `<h3>Waiting for you ${pending.length ? chip(pending.length, 'warn') : ''}</h3>
      ${pending.length ? pending.map(approvalCard).join('') : '<p class="pm-empty">Nothing is waiting. When an agent wants to do something that needs your say-so, it shows up here with what it will do.</p>'}
      <h3>Recent decisions</h3>${hist}`;
  }

  // ---- rules ----
  const ruleRow = (r, i, editable) => `<tr><td>${chip(r.decision, r.decision === 'deny' ? 'bad' : r.decision === 'ask' ? 'warn' : '')}</td><td class="pm-mono">${E(r.tool)}</td><td class="pm-mono">${E(r.match || '(any)')}</td><td class="pm-note">${E(r.note || '')}</td>${editable ? `<td><button class="btn" data-pm="rule-edit" data-i="${i}">Edit</button> <button class="btn" data-pm="rule-del" data-i="${i}">Delete</button></td>` : ''}</tr>`;

  function ruleForm(r) {
    r = r || { decision: 'ask', tool: '', match: '', note: '' };
    return `<div class="pm-form"><select id="pm-r-decision" aria-label="Decision">${DECISIONS.map((d) => `<option value="${d}"${r.decision === d ? ' selected' : ''}>${d}</option>`).join('')}</select>
      <input id="pm-r-tool" placeholder="Tool, class:write or *" aria-label="Tool" value="${E(r.tool)}">
      <input id="pm-r-match" placeholder="Pattern (optional)" aria-label="Pattern" value="${E(r.match || '')}">
      <input id="pm-r-note" placeholder="Note (optional)" aria-label="Note" value="${E(r.note || '')}">
      <button class="btn" data-pm="rule-save">Save rule</button><button class="btn" data-pm="rule-cancel">Cancel</button></div>`;
  }

  function renderRules() {
    const h = host;
    const hostPart = !h ? '<p class="pm-empty">Loading…</p>' : `<div class="pm-bar"><span>Mode: <b>${E(h.mode)}</b></span><span class="pm-note">${E(MODE_HELP[h.mode] || '')}</span>
        ${h.locked ? chip('locked by the host: bots cannot change rules or mode', 'warn') : chip('bots may add their own rules')}
        ${h.allow_bypass ? chip('bypass permitted', 'bad') : chip('bypass off')}</div>
      ${(h.problems || []).length ? `<p class="cardnote">Skipped rules in the host settings: ${E(h.problems.join('; '))}</p>` : ''}
      ${h.rules.length ? `<div class="pm-scroll"><table class="pm-table"><thead><tr><th>Decision</th><th>Tool / class</th><th>Pattern</th><th>Note</th></tr></thead><tbody>${h.rules.map((r, i) => ruleRow(r, i, false)).join('')}</tbody></table></div>`
        : '<p class="pm-empty">The host has no rules of its own.</p>'}
      <p class="pm-note">Host rules live in the settings file (native_agent.permissions) and apply to every bot; this screen shows them but does not edit them. A deny rule always wins.</p>`;
    const opts = bots.map((b) => `<option value="${E(b.id)}"${String(b.id) === String(botId) ? ' selected' : ''}>${E(b.name || 'bot ' + b.id)}</option>`).join('');
    let botPart = '';
    if (!bots.length) botPart = '<p class="pm-empty">No bots yet.</p>';
    else if (bot) {
      const locked = !!bot.locked;
      const rules = bot.instance.rules || [];
      botPart = `<div class="pm-bar"><label>Mode <select id="pm-bot-mode" aria-label="Bot mode"${locked ? ' disabled' : ''}><option value="">Follow the host (${E(host ? host.mode : 'default')})</option>${bot.modes.map((m) => `<option value="${E(m)}"${bot.instance.mode === m ? ' selected' : ''}>${E(m)}</option>`).join('')}</select></label>
          <span class="pm-note">In force: <b>${E(bot.effective.mode)}</b>, ${bot.effective.rules.length} rule(s) in total</span></div>
        ${locked ? '<p class="pm-note">The host locked permissions, so this bot cannot change its mode or rules.</p>' : ''}
        ${rules.length ? `<div class="pm-scroll"><table class="pm-table"><thead><tr><th>Decision</th><th>Tool / class</th><th>Pattern</th><th>Note</th><th></th></tr></thead><tbody>${rules.map((r, i) => ruleRow(r, i, !locked)).join('')}</tbody></table></div>` : '<p class="pm-empty">This bot has no rules of its own.</p>'}
        ${editing !== null && !locked ? ruleForm(editing >= 0 ? rules[editing] : null) : (locked ? '' : '<div class="pm-bar"><button class="btn" data-pm="rule-new">Add rule</button></div>')}`;
    }
    return `<h3>Host rules</h3>${hostPart}
      <h3>Rules for one bot</h3><div class="pm-bar"><select id="pm-bot" aria-label="Bot">${opts}</select></div>${rulesErr ? `<p class="cardnote">${E(rulesErr)}</p>` : ''}${botPart}`;
  }

  // ---- MCP pins and taint ----
  function renderPins() {
    const p = live.pins;
    if (!p) return '<p class="pm-empty">Loading…</p>';
    if (!p.length) return '<p class="pm-empty">No MCP servers are connected, so there is nothing to pin.</p>';
    return `<div class="pm-scroll"><table class="pm-table"><thead><tr><th>Server</th><th>Tool</th><th>Status</th><th>Description</th><th></th></tr></thead><tbody>${p.map((t) => `<tr><td>${E(t.server)}</td><td class="pm-mono">${E(t.tool)}</td><td>${chip(t.status === 'ok' ? 'trusted' : t.status === 'new' ? 'new: not yet approved' : 'changed: needs approval', t.status === 'ok' ? '' : 'warn')}</td><td class="pm-note">${E(t.description)}</td><td>${t.status === 'ok' ? '' : `<button class="btn" data-pm="pin" data-server="${E(t.server)}" data-tool="${E(t.tool)}">Approve</button>`}</td></tr>`).join('')}</tbody></table></div>
      <p class="pm-note">A tool whose description changed after you approved it stays off until you approve the new text.</p>`;
  }

  function renderTaint() {
    const rows = live.taint.concat(lookup && lookup.tainted && !live.taint.some((t) => t.session === lookup.session) ? [lookup] : []);
    const list = rows.length
      ? `<div class="pm-scroll"><table class="pm-table"><thead><tr><th>Session</th><th>Read untrusted content from</th><th></th></tr></thead><tbody>${rows.map((t) => `<tr><td class="pm-mono">${E(t.session)}</td><td>${E((t.sources || []).join(', ') || 'unknown source')}</td><td><button class="btn" data-pm="taint-clear" data-session="${E(t.session)}">Clear mark</button></td></tr>`).join('')}</tbody></table></div>`
      : '<p class="pm-empty">No session with a pending approval has read untrusted content.</p>';
    const res = lookup ? `<span class="pm-note">${E(lookup.session)}: ${lookup.tainted ? 'untrusted (' + E((lookup.sources || []).join(', ')) + ')' : 'clean'}</span>` : '';
    return `<p class="pm-note">After a session reads a web page or an untrusted MCP server, anything it was allowed to do automatically asks first, until you clear the mark. Sessions shown are the ones with approvals on this screen.</p>${list}
      <div class="pm-bar"><input id="pm-session" placeholder="Check a session key" aria-label="Session key"><button class="btn" data-pm="taint-look">Check</button>${res}</div>`;
  }

  function render() {
    const root = $('pm-root');
    if (!root) return;
    const keep = document.activeElement && document.activeElement.id;
    const val = keep && $(keep) && 'value' in $(keep) ? $(keep).value : null;
    root.innerHTML = `<div class="pm-bar"><button class="btn" data-pm="refresh">Refresh</button><span class="pm-note">Updates while this screen is open${note ? ' · ' + E(note) : ''}</span></div>
      ${live.err ? `<p class="cardnote">Some data is unavailable: ${E(live.err)}</p>` : ''}
      <div class="pm-block"><h2>Approvals</h2>${renderApprovals()}</div>
      <div class="pm-block"><h2>Permission rules</h2>${renderRules()}</div>
      <div class="pm-block"><h2>MCP tool pins</h2>${renderPins()}</div>
      <div class="pm-block"><h2>Untrusted sessions</h2>${renderTaint()}</div>`;
    if (keep && $(keep)) { try { if (val != null) $(keep).value = val; $(keep).focus(); } catch (_) {} }
  }

  // ---- loading ----
  async function loadLive() {
    const errs = [];
    const one = async (path) => { try { return await api(path); } catch (e) { errs.push(errText(e)); return null; } };
    const [ap, pins] = await Promise.all([one('/api/approvals?status=all&limit=100'), one('/api/mcp/pins')]);
    live.approvals = ap ? ap.approvals : (live.approvals || []);
    live.pins = pins ? pins.tools : (live.pins || []);
    const sessions = Array.from(new Set(live.approvals.filter((a) => a.untrusted_content_in_session).map((a) => a.session)));
    const taints = [];
    for (const s of sessions) {
      try { const t = await api('/api/agent/taint?session=' + encodeURIComponent(s)); if (t.tainted) taints.push(t); } catch (e) { errs.push(errText(e)); }
    }
    live.taint = taints;
    live.err = Array.from(new Set(errs)).join('; ');
  }

  async function loadRules() {
    try {
      host = await api('/api/agent/permissions');
      bots = await api('/api/bots');
      if (!Array.isArray(bots)) bots = [];
      if (bots.length && !bots.some((b) => String(b.id) === String(botId))) botId = String(bots[0].id);
      bot = botId ? await api(`/api/instances/${encodeURIComponent(botId)}/permissions`) : null;
      rulesErr = '';
    } catch (e) {
      rulesErr = 'Rules unavailable: ' + errText(e);
    }
  }

  async function load(withRules) {
    if (!$('pm-root') || loading) return;
    loading = true;
    try {
      await loadLive();
      if (withRules || !host) await loadRules();
    } finally {
      loading = false;
      render();
    }
  }

  function formOpen() {
    const a = document.activeElement;
    return editing !== null || (a && a.closest && a.closest('#pm-root') && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName));
  }

  // ---- actions ----
  async function act(fn, done) {
    try { await fn(); note = done; rulesErr = ''; } catch (e) { note = ''; rulesErr = errText(e); }
    await loadLive();
    await loadRules();
    render();
  }

  function putBot(body) {
    return api(`/api/instances/${encodeURIComponent(botId)}/permissions`, { method: 'PUT', body: JSON.stringify(body) });
  }

  function readRule() {
    return { decision: ($('pm-r-decision') || {}).value || 'ask', tool: (($('pm-r-tool') || {}).value || '').trim(), match: (($('pm-r-match') || {}).value || '').trim(), note: (($('pm-r-note') || {}).value || '').trim() };
  }

  function init() {
    const root = $('pm-root');
    if (!root) return;
    css();
    render();
    root.addEventListener('click', (ev) => {
      const el = ev.target.closest('[data-pm]');
      if (!el) return;
      const a = el.dataset.pm;
      const rules = bot ? (bot.instance.rules || []).slice() : [];
      if (a === 'refresh') { note = ''; load(true); }
      else if (a === 'decide') act(() => api(`/api/approvals/${encodeURIComponent(el.dataset.id)}/resolve`, { method: 'POST', body: JSON.stringify({ outcome: el.dataset.outcome }) }), el.dataset.outcome === 'deny' ? 'Denied.' : 'Approved.');
      else if (a === 'pin') act(() => api('/api/mcp/pins/approve', { method: 'POST', body: JSON.stringify({ server: el.dataset.server, tool: el.dataset.tool }) }), 'Tool approved.');
      else if (a === 'taint-clear') act(() => api('/api/agent/taint/clear', { method: 'POST', body: JSON.stringify({ session: el.dataset.session }) }), 'Mark cleared.').then(() => { lookup = null; render(); });
      else if (a === 'taint-look') {
        const s = (($('pm-session') || {}).value || '').trim();
        if (s) api('/api/agent/taint?session=' + encodeURIComponent(s)).then((t) => { lookup = t; render(); }).catch((e) => { rulesErr = errText(e); render(); });
      }
      else if (a === 'rule-new') { editing = -1; render(); }
      else if (a === 'rule-edit') { editing = Number(el.dataset.i); render(); }
      else if (a === 'rule-cancel') { editing = null; render(); }
      else if (a === 'rule-save') {
        const r = readRule();
        if (!r.tool) { rulesErr = 'A rule needs a tool (a name, class:<permission> or *).'; render(); return; }
        if (editing >= 0) rules[editing] = r; else rules.push(r);
        editing = null;
        act(() => putBot({ rules }), 'Rule saved.');
      }
      else if (a === 'rule-del') {
        const i = Number(el.dataset.i), r = rules[i];
        if (!r || !window.confirm('Delete this rule?\n\n' + [r.decision, r.tool, r.match].filter(Boolean).join(' '))) return;
        rules.splice(i, 1);
        act(() => putBot({ rules }), 'Rule deleted.');
      }
    });
    root.addEventListener('change', (ev) => {
      if (ev.target.id === 'pm-bot') { botId = ev.target.value; editing = null; loadRules().then(render); }
      else if (ev.target.id === 'pm-bot-mode') act(() => putBot({ mode: ev.target.value }), 'Mode saved.');
    });
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'permissions') load(true); };
    window.addEventListener('hashchange', onHash);
    onHash();
    if ('IntersectionObserver' in window) {
      new IntersectionObserver((es) => es.forEach((e) => { visible = e.isIntersecting; if (visible && live.approvals === null) load(true); })).observe(root);
    }
    setInterval(() => { if (visible && !document.hidden && !formOpen()) load(false); }, REFRESH_MS);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
