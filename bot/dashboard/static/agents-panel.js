// The ABP Agents page. This file is identical in the dashboard (bot/dashboard/static/) and the desktop app
// (desktop-app/ui/); tests/test_agents_page.py fails if the two ever differ. It uses each page's own api(), esc() and
// showToast(), and the markup in <section id="agents">.
//
// Every setting form is drawn from GET /api/agent/config/schema, which describes each setting once (bot/agent_runtime/
// settings_schema.py), so a setting cannot exist without appearing here. Changes are staged, checked by the server as a
// whole, and written to config/backends.yaml only when all of them are valid.
(function () {
  'use strict';
  const root = document.getElementById('agents-root');
  if (!root) return;

  const FIXED_TABS = { overview: 'Overview', skills: 'Skills' };
  const ORDER = ['overview', 'runtime', 'safety', 'tools', 'subagents', 'skills', 'models'];
  const AGENT_BACKENDS = ['native_agent', 'api', 'custom_model'];
  const MODES = ['default', 'plan', 'accept_edits', 'bypass'];
  const MODE_LABEL = { default: 'Default', plan: 'Plan (read-only)', accept_edits: 'Accept edits', bypass: 'Bypass (no approval)' };

  const S = {
    schema: null, values: {}, configured: new Set(), dirty: {}, errors: {}, tab: 'overview',
    advanced: {}, overview: null, tools: null, toolFilter: '', packs: null, quarantine: null, drafts: null, loaded: false,
  };
  const body = document.getElementById('agents-body');
  const tabsEl = document.getElementById('agents-tabs');
  const saveBar = document.getElementById('agents-savebar');
  const subStatic = document.getElementById('agents-static-subagents');
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  // Only touch an element's contents when they actually change. Pressing the mouse on "Save changes" blurs the field being
  // edited, which fires a change event and a re-render; replacing the button between mouse-down and mouse-up loses the click.
  function setHtml(el, html) { if (el.__ag !== html) { el.innerHTML = html; el.__ag = html; } }

  // ------------------------------------------------------------------ values
  const isList = (f) => f.type === 'list' || f.type === 'rules';
  function norm(f, v) {
    if (isList(f)) return (Array.isArray(v) ? v : String(v || '').split('\n')).map(s => String(s).trim()).filter(Boolean);
    if (f.type === 'text' || f.type === 'textarea') return v == null ? '' : String(v);
    return v;
  }
  const same = (f, a, b) => JSON.stringify(norm(f, a)) === JSON.stringify(norm(f, b));
  const field = (id) => S.schema.fields.find(f => f.id === id);
  const shown = (f) => (id => (id in S.dirty ? S.dirty[id] : S.values[id]))(f.id);
  const defaultText = (f) => {
    const d = f.default;
    if (f.type === 'bool') return d ? 'on' : 'off';
    if (f.type === 'enum') { const c = f.choices.find(x => x[0] === d); return c ? c[1].split(' - ')[0] : String(d); }
    if (isList(f)) return d && d.length ? plural(d.length, 'entry', 'entries') : 'empty';
    if (d === '' || d == null) return 'blank';
    return String(d) + (f.unit ? ' ' + f.unit : '');
  };

  function control(f, v) {
    const id = esc(f.id);
    if (f.type === 'bool') return `<label class="ag-switch"><input type="checkbox" data-f="${id}" ${v ? 'checked' : ''}><span>${v ? 'On' : 'Off'}</span></label>`;
    if (f.type === 'enum') return `<select data-f="${id}">${f.choices.map(([val, label]) => `<option value="${esc(val)}" ${val === v ? 'selected' : ''}>${esc(label)}</option>`).join('')}</select>`;
    if (f.type === 'int' || f.type === 'float') {
      const step = f.type === 'float' ? (f.max != null && f.max <= 1 ? '0.05' : '0.01') : '1';
      return `<span class="ag-num"><input type="number" data-f="${id}" value="${v == null ? '' : esc(v)}" step="${step}" ${f.min != null ? `min="${f.min}"` : ''} ${f.max != null ? `max="${f.max}"` : ''}>${f.unit ? `<small>${esc(f.unit)}</small>` : ''}</span>`;
    }
    if (f.type === 'textarea') return `<textarea rows="4" data-f="${id}">${esc(v == null ? '' : v)}</textarea>`;
    if (isList(f)) return `<textarea rows="4" data-f="${id}" placeholder="one per line" spellcheck="false">${esc((v || []).join('\n'))}</textarea>`;
    return `<input type="text" data-f="${id}" value="${esc(v == null ? '' : v)}" autocomplete="off" spellcheck="false">`;
  }

  function dangerActive(f, v) { return !!f.danger && v === f.danger_value; }

  function row(f) {
    const v = shown(f);
    const isDirty = f.id in S.dirty;
    const canReset = !same(f, v, f.default);
    const err = S.errors[f.id];
    return `<div class="ag-row${isDirty ? ' dirty' : ''}" data-row="${esc(f.id)}">
      <div class="ag-text"><b>${esc(f.label)}</b>${isDirty ? ' <span class="ag-mod">changed</span>' : ''}
        ${f.applies !== 'next turn' ? `<span class="ag-applies">applies to ${esc(f.applies)}</span>` : ''}
        <div class="ag-help">${esc(f.help)}</div></div>
      <div class="ag-ctl">${control(f, v)}
        <div class="ag-meta"><span>Default: ${esc(defaultText(f))}</span>${canReset ? ` · <a href="#" data-reset="${esc(f.id)}">Reset to default</a>` : ''}</div></div>
      <div class="ag-danger${dangerActive(f, v) ? '' : ' hidden'}">⚠ ${esc(f.danger)}</div>
      <div class="ag-err${err ? '' : ' hidden'}">${esc(err || '')}</div>
    </div>`;
  }

  function settingsCards(tabId) {
    const fields = S.schema.fields.filter(f => f.tab === tabId);
    const adv = fields.filter(f => f.advanced);
    const showAdv = !!S.advanced[tabId] || adv.some(f => f.id in S.dirty || S.errors[f.id]);
    const sections = [];
    fields.filter(f => showAdv || !f.advanced).forEach(f => {
      let s = sections.find(x => x.name === f.section);
      if (!s) { s = { name: f.section, fields: [] }; sections.push(s); }
      s.fields.push(f);
    });
    const tab = S.schema.tabs.find(t => t.id === tabId);
    return `<p class="cardnote">${esc(tab ? tab.description : '')}</p>
      ${sections.map(s => `<div class="card ag-card"><h3>${esc(s.name)}</h3>${s.fields.map(row).join('')}</div>`).join('')}
      ${adv.length ? `<label class="ag-adv"><input type="checkbox" data-adv="${tabId}" ${showAdv ? 'checked' : ''}> Show advanced settings (${adv.length})</label>` : ''}`;
  }

  // ------------------------------------------------------------------ overview
  function overviewHtml() {
    const o = S.overview;
    if (!o) return '<p class="cardnote">Loading…</p>';
    const c = o.counts;
    const go = (target, label) => `<button class="btn" data-go="${esc(target)}" style="padding:3px 9px; font-size:11px;">${esc(label)}</button>`;
    const checks = o.checks.map(k => `<div class="ag-check ${k.ok ? 'ok' : 'warn'}"><span class="ag-mark">${k.ok ? '✓' : '!'}</span>
        <div><b>${esc(k.title)}</b><div class="ag-help">${esc(k.detail)}</div></div>
        ${!k.ok || k.info ? go(k.go, k.ok ? 'Review' : 'Fix') : ''}</div>`).join('');
    const bots = o.bots.length ? `<div class="tablewrap"><table><thead><tr><th>Bot</th><th>Backend</th><th>Model</th><th>Permissions</th><th>Sub-agents</th><th></th></tr></thead><tbody>
      ${o.bots.map(b => `<tr><td><b>${esc(b.name)}</b> <span class="chip neutral">${esc(b.platform)}</span>${b.enabled ? '' : ' <span class="chip warning">disabled</span>'}${b.is_admin ? ' <span class="chip good">admin</span>' : ''}</td>
        <td>${esc(b.backend_label)}</td><td class="mono">${esc(b.model || '—')}</td>
        <td>${esc(MODE_LABEL[b.permission_mode] || b.permission_mode)}${b.permission_rules ? ` · ${plural(b.permission_rules, 'rule', 'rules')}` : ''}</td>
        <td>up to ${esc(b.max_concurrent_children)} at once${b.worker_model ? ` · workers: <span class="mono">${esc(b.worker_model)}</span>` : ''}${b.plan_approval ? ' · plan approval' : ''}</td>
        <td><button class="btn" data-open-bot="${b.id}" style="padding:3px 9px; font-size:11px;">Agent settings</button></td></tr>`).join('')}
      </tbody></table></div>`
      : `<p class="cardnote">No bot runs an ABP Agent yet. A bot is one chat connection (Telegram, Discord, Slack…); pick the <b>ABP Agent</b> backend when you add one and it answers with ABP's own agent, with tools, memory, sub-agents and swarms. ${go('bots', 'Go to Bots')}</p>`;
    return `<div class="ag-stats">
        <div><b>${c.providers}</b><span>model providers</span></div><div><b>${o.bots.length}</b><span>agent bots</span></div>
        <div><b>${c.tools}</b><span>tools</span></div><div><b>${c.swarms}</b><span>swarms</span></div>
        <div><b>${c.skill_packs}</b><span>skill packs</span></div></div>
      <div class="card ag-card"><h3>Readiness</h3>${checks}</div>
      <div class="card ag-card"><h3>Bots running ABP Agents</h3>${bots}</div>
      <div class="card ag-card"><h3>Setting up an agent</h3><ol class="ag-steps">
        <li><b>Give it a model.</b> Add a provider (an API key, or a local server such as Ollama) on ${go('models', 'Models')}. Free models are marked, and each model's context window and rate limits are shown.</li>
        <li><b>Create a bot that uses it.</b> On ${go('bots', 'Bots')}, add a bot and choose <b>ABP Agent</b> as its backend, then enter the model as <span class="mono">provider/model</span>.</li>
        <li><b>Set how careful it is.</b> ${go('agents:safety', 'Safety')} controls what it may do without asking, and where commands run.</li>
        <li><b>Choose its tools.</b> ${go('agents:tools', 'Tools')} turns the web, a browser, code intelligence and skills on or off.</li>
        <li><b>Let it work in parallel.</b> ${go('agents:subagents', 'Sub-agents & swarms')} sets how many workers it may run and the spending limits for swarms; define swarms on ${go('swarms', 'Swarms')}.</li>
      </ol></div>`;
  }

  // ------------------------------------------------------------------ extra blocks
  function botPermissionsCard() {
    const o = S.overview;
    if (!o) return '';
    const locked = !!S.values['native_agent.permissions.locked'];
    const def = S.values['native_agent.permissions.mode'] || 'default';
    if (!o.bots.length) return `<div class="card ag-card"><h3>Per-bot permission mode</h3><p class="cardnote">No bot runs an ABP Agent yet.</p></div>`;
    return `<div class="card ag-card"><h3>Per-bot permission mode</h3>
      <p class="cardnote">${locked ? 'Permissions are locked to the settings above, so a bot cannot choose its own mode.' : `A bot follows the default (${esc(MODE_LABEL[def] || def)}) unless you give it a stricter or looser mode here.`}</p>
      <div class="tablewrap"><table><thead><tr><th>Bot</th><th>Mode</th></tr></thead><tbody>
      ${o.bots.map(b => `<tr><td>${esc(b.name)}</td><td><select data-bot-mode="${b.id}" ${locked ? 'disabled' : ''}>
        <option value="">Follow the default (${esc(MODE_LABEL[def] || def)})</option>
        ${MODES.map(m => `<option value="${m}" ${b.permission_mode_own === m ? 'selected' : ''}>${esc(MODE_LABEL[m])}</option>`).join('')}</select></td></tr>`).join('')}
      </tbody></table></div></div>`;
  }

  function toolsCard() {
    if (!S.tools) return `<div class="card ag-card"><h3>Tool inventory</h3><p class="cardnote">Loading…</p></div>`;
    const q = S.toolFilter.trim().toLowerCase();
    const list = S.tools.filter(t => !q || t.name.includes(q) || t.description.toLowerCase().includes(q) || t.permission.includes(q));
    const groups = {};
    list.forEach(t => { (groups[t.permission] = groups[t.permission] || []).push(t); });
    return `<div class="card ag-card"><h3>Tool inventory <span class="ag-count">${S.tools.length} tools</span></h3>
      <p class="cardnote">Everything an agent can call, with whether it asks first. Tools are grouped by permission class, which is what permission rules match on (<span class="mono">class:write</span>, <span class="mono">class:execute</span>…).</p>
      <input type="text" id="ag-tool-filter" placeholder="Filter tools…" value="${esc(S.toolFilter)}" autocomplete="off" style="max-width:280px; margin-bottom:8px;">
      ${Object.keys(groups).map(g => `<div class="ag-toolgroup"><h4>${esc(g)} <small>(${groups[g].length})</small></h4><div class="tablewrap"><table><tbody>
        ${groups[g].map(t => `<tr><td class="mono">${esc(t.name)}</td><td>${esc(t.description)}</td>
          <td>${t.asks_first ? '<span class="chip warning">asks first</span>' : '<span class="chip good">runs freely</span>'}</td>
          <td><span class="chip neutral">${esc(t.origin)}</span></td></tr>`).join('')}</tbody></table></div></div>`).join('') || '<p class="cardnote">No tool matches.</p>'}
    </div>`;
  }

  const detail = (item) => Object.entries(item).filter(([k, v]) => !['name', 'description', 'summary'].includes(k) && ['string', 'number', 'boolean'].includes(typeof v) && String(v).length < 90)
    .slice(0, 5).map(([k, v]) => `${esc(k)}: ${esc(v)}`).join(' · ');
  function reviewList(title, note, items, kind) {
    if (items == null) return `<div class="card ag-card"><h3>${title}</h3><p class="cardnote">Loading…</p></div>`;
    return `<div class="card ag-card"><h3>${title} <span class="ag-count">${items.length}</span></h3><p class="cardnote">${note}</p>
      ${items.length ? items.map(i => `<div class="ag-review"><div><b>${esc(i.name)}</b>${i.blocked ? ' <span class="chip critical">blocked by the scan</span>' : ''}
        <div class="ag-help">${esc(i.description || i.summary || '')}</div><div class="ag-help">${detail(i)}</div></div>
        <div class="ag-actions"><button class="btn primary" data-review="${kind}:approve:${esc(i.name)}" ${i.blocked ? 'disabled' : ''} style="padding:3px 10px; font-size:11px;">Approve</button>
        <button class="btn" data-review="${kind}:reject:${esc(i.name)}" style="padding:3px 10px; font-size:11px;">Reject</button></div></div>`).join('') : '<p class="cardnote">Nothing waiting.</p>'}</div>`;
  }
  function skillsHtml() {
    const packs = S.packs;
    return `<p class="cardnote">Skills are reusable instructions an agent can load on demand. Nothing new is used until you approve it.</p>
      ${reviewList('Skill packs awaiting review', 'Fetched packs are scanned and held here. Approve one to install it, or reject it.', S.quarantine, 'quarantine')}
      ${reviewList('Skills the agent drafted', 'When "Learn skills from long tasks" is on, the agent may draft a skill after a long task. You decide whether to keep it.', S.drafts, 'draft')}
      <div class="card ag-card"><h3>Installed skill packs ${packs ? `<span class="ag-count">${packs.length}</span>` : ''}</h3>
        ${packs == null ? '<p class="cardnote">Loading…</p>' : packs.length ? `<div class="tablewrap"><table><thead><tr><th>Name</th><th>What it does</th><th>Source</th><th></th></tr></thead><tbody>
          ${packs.map(p => `<tr><td class="mono">${esc(p.name)}</td><td>${esc(p.description)}</td><td>${esc(p.source)}</td><td>${p.problems && p.problems.length ? `<span class="chip warning">${esc(p.problems.length)} problem(s)</span>` : ''}</td></tr>`).join('')}</tbody></table></div>` : '<p class="cardnote">No skill packs installed.</p>'}</div>
      <div class="card ag-card"><h3>Install a skill pack from git</h3>
        <p class="cardnote">The host must be listed under <b>Tools → Skills → Hosts skills may be fetched from</b>. The pack goes to the review list above first.</p>
        <div class="row" style="gap:8px; flex-wrap:wrap;"><input type="text" id="ag-skill-url" placeholder="https://github.com/owner/skills.git" style="flex:2; min-width:240px;" autocomplete="off" spellcheck="false">
          <input type="text" id="ag-skill-ref" placeholder="branch or tag (optional)" style="flex:1; min-width:140px;" autocomplete="off">
          <input type="text" id="ag-skill-subdir" placeholder="folder (optional)" style="flex:1; min-width:140px;" autocomplete="off">
          <button class="btn primary" id="ag-skill-fetch">Fetch</button></div></div>`;
  }

  function modelsExtra() {
    return `<div class="card ag-card"><h3>What each model can do</h3><p class="cardnote">Context window, free-tier limits and current usage for every model are on the Models page. Limits are only enforced for models whose limits are known.
      <button class="btn" data-go="models" style="padding:3px 9px; font-size:11px; margin-left:6px;">Open Models</button></p></div>`;
  }
  function yamlOnly() {
    return S.schema.yaml_only.length ? `<div class="card ag-card"><h3>Edited in the config file</h3><p class="cardnote">These settings hold tables or commands that a form cannot show honestly. Edit them in <span class="mono">config/backends.yaml</span>:</p>
      <ul class="ag-yaml">${S.schema.yaml_only.map(y => `<li><span class="mono">${esc(y.key)}</span> — ${esc(y.why)}</li>`).join('')}</ul></div>` : '';
  }

  // ------------------------------------------------------------------ render
  function tabsHtml() {
    const dirtyTabs = new Set(Object.keys(S.dirty).map(id => (field(id) || {}).tab));
    const review = S.overview ? S.overview.counts.skills_to_review : 0;
    return ORDER.map(id => {
      const t = S.schema.tabs.find(x => x.id === id);
      const title = FIXED_TABS[id] || (t && t.title) || id;
      const badge = dirtyTabs.has(id) ? '<span class="ag-dot" title="unsaved changes"></span>' : (id === 'skills' && review ? `<span class="ag-badge">${review}</span>` : '');
      return `<button class="agents-tab${S.tab === id ? ' active' : ''}" role="tab" aria-selected="${S.tab === id}" data-tab="${id}">${esc(title)}${badge}</button>`;
    }).join('');
  }

  function render() {
    if (!S.schema) return;
    setHtml(tabsEl, tabsHtml());
    let html = '';
    if (S.tab === 'overview') html = overviewHtml();
    else if (S.tab === 'skills') html = skillsHtml();
    else {
      html = settingsCards(S.tab);
      if (S.tab === 'safety') html += botPermissionsCard();
      if (S.tab === 'tools') html += toolsCard();
      if (S.tab === 'models') html += modelsExtra() + yamlOnly();
    }
    body.innerHTML = html;
    subStatic.classList.toggle('hidden', S.tab !== 'subagents');
    renderSaveBar();
  }

  function renderSaveBar() {
    const n = Object.keys(S.dirty).length;
    saveBar.classList.toggle('hidden', n === 0);
    setHtml(saveBar, n ? `<span><b>${plural(n, 'unsaved change', 'unsaved changes')}</b>${Object.keys(S.errors).length ? ' — fix the highlighted settings' : ''}</span>
      <span><button class="btn" id="ag-discard">Discard</button> <button class="btn primary" id="ag-save">Save changes</button></span>` : '');
  }

  // ------------------------------------------------------------------ editing
  function readControl(f, elm) {
    if (f.type === 'bool') return elm.checked;
    if (f.type === 'int' || f.type === 'float') return elm.value === '' ? null : Number(elm.value);
    if (isList(f)) return elm.value.split('\n');
    return elm.value;
  }
  function stage(f, value) {
    if (same(f, value, S.values[f.id])) delete S.dirty[f.id]; else S.dirty[f.id] = value;
    delete S.errors[f.id];
  }
  function refreshRow(f) {
    const rowEl = body.querySelector(`[data-row="${CSS.escape(f.id)}"]`);
    if (!rowEl) return;
    const v = shown(f);
    rowEl.classList.toggle('dirty', f.id in S.dirty);
    const head = rowEl.querySelector('.ag-text b');
    const mod = rowEl.querySelector('.ag-mod');
    if (f.id in S.dirty && !mod) head.insertAdjacentHTML('afterend', ' <span class="ag-mod">changed</span>');
    if (!(f.id in S.dirty) && mod) mod.remove();
    const d = rowEl.querySelector('.ag-danger'); if (d) d.classList.toggle('hidden', !dangerActive(f, v));
    const e = rowEl.querySelector('.ag-err'); if (e) { e.classList.add('hidden'); e.textContent = ''; }
    const sw = rowEl.querySelector('.ag-switch span'); if (sw) sw.textContent = v ? 'On' : 'Off';
    const meta = rowEl.querySelector('.ag-meta');
    if (meta) meta.innerHTML = `<span>Default: ${esc(defaultText(f))}</span>${same(f, v, f.default) ? '' : ` · <a href="#" data-reset="${esc(f.id)}">Reset to default</a>`}`;
    setHtml(tabsEl, tabsHtml());
    renderSaveBar();
  }

  async function save() {
    const changes = {};
    Object.keys(S.dirty).forEach(id => { const f = field(id); changes[id] = isList(f) ? norm(f, S.dirty[id]) : S.dirty[id]; });
    const btn = document.getElementById('ag-save');
    if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
    try {
      const res = await api('/api/agent/config', { method: 'POST', body: JSON.stringify({ changes }) });
      const n = Object.keys(changes).length;
      const later = Object.keys(changes).some(id => (field(id) || {}).applies === 'new sessions');
      S.values = res.values; S.configured = new Set(res.configured); S.dirty = {}; S.errors = {};
      showToast(`Saved ${plural(n, 'setting', 'settings')}${later ? ' — some apply to new sessions' : ''}`, 'success');
      refreshOverview();
    } catch (e) {
      let detailMsg = null;
      try { detailMsg = JSON.parse(e.message).detail; } catch (_e) { /* not JSON */ }
      if (detailMsg && detailMsg.errors) {
        S.errors = detailMsg.errors;
        const first = Object.keys(S.errors)[0];
        const f = field(first);
        if (f && f.tab !== S.tab) S.tab = f.tab;
        showToast(`Not saved: ${Object.keys(S.errors).length} setting(s) need fixing`, 'error');
      } else showToast('Could not save: ' + (e.message || e), 'error');
    }
    render();
    Object.entries(S.errors).forEach(([id, msg]) => {
      const el = body.querySelector(`[data-row="${CSS.escape(id)}"] .ag-err`);
      if (el) { el.textContent = msg; el.classList.remove('hidden'); }
    });
  }

  // ------------------------------------------------------------------ loading
  async function refreshOverview() {
    let next;
    try { next = await api('/api/agent/overview'); } catch (_e) { return; }
    // An unchanged overview must not rebuild the tab: a rebuild under the pointer swallows a click.
    const unchanged = JSON.stringify(next) === JSON.stringify(S.overview);
    S.overview = next;
    if (unchanged) return;
    if (S.tab === 'overview' || S.tab === 'safety') { const y = window.scrollY; if (!Object.keys(S.dirty).length || S.tab === 'overview') render(); window.scrollTo(0, y); }
    else setHtml(tabsEl, tabsHtml());
  }
  async function loadTools() { try { S.tools = (await api('/api/agent/tools')).tools; } catch (_e) { S.tools = []; } if (S.tab === 'tools') render(); }
  async function loadSkills() {
    const get = async (p, k) => { try { return (await api(p))[k]; } catch (_e) { return []; } };
    [S.packs, S.quarantine, S.drafts] = await Promise.all([get('/api/skills/packs', 'packs'), get('/api/skills/quarantine', 'packs'), get('/api/skills/drafts', 'drafts')]);
    if (S.tab === 'skills') render();
  }
  function setTab(id) {
    if (!ORDER.includes(id)) return;
    S.tab = id;
    render();
    if (id === 'tools' && !S.tools) loadTools();
    if (id === 'skills') loadSkills();
    if (id === 'overview' || id === 'safety') refreshOverview();
  }
  function goTo(target) {
    if (target.startsWith('agents:')) { setTab(target.slice(7)); document.getElementById('agents').scrollIntoView({ behavior: 'smooth', block: 'start' }); return; }
    const link = document.querySelector(`#sidenav a[href="#${target}"]`);
    if (link) link.click(); else { const el = document.getElementById(target); if (el) el.scrollIntoView({ behavior: 'smooth' }); }
  }
  function openBot(id) {
    setTab('subagents');
    const sel = document.getElementById('agent-settings-instance');
    if (sel) { sel.value = String(id); sel.dispatchEvent(new Event('change')); }
    document.getElementById('agents').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  async function load() {
    try {
      const [schema, cfg] = await Promise.all([api('/api/agent/config/schema'), api('/api/agent/config')]);
      S.schema = schema; S.values = cfg.values; S.configured = new Set(cfg.configured);
    } catch (_e) { body.innerHTML = '<p class="cardnote">Connecting to the ABP server…</p>'; return false; }
    S.loaded = true;
    render();
    refreshOverview();
    return true;
  }

  // ------------------------------------------------------------------ events
  root.addEventListener('click', async (ev) => {
    const t = ev.target.closest('[data-tab],[data-go],[data-open-bot],[data-reset],[data-review],#ag-save,#ag-discard,#ag-skill-fetch');
    if (!t) return;
    if (t.dataset.tab) return setTab(t.dataset.tab);
    if (t.dataset.go) return goTo(t.dataset.go);
    if (t.dataset.openBot) return openBot(t.dataset.openBot);
    if (t.dataset.reset) { ev.preventDefault(); const f = field(t.dataset.reset); stage(f, f.default); render(); return; }
    if (t.id === 'ag-save') return save();
    if (t.id === 'ag-discard') { S.dirty = {}; S.errors = {}; render(); return; }
    if (t.dataset.review) {
      const [kind, action, name] = t.dataset.review.split(':');
      const base = kind === 'quarantine' ? '/api/skills/quarantine' : '/api/skills/drafts';
      try {
        await api(`${base}/${action}`, { method: 'POST', body: JSON.stringify({ name }) });
        showToast(`${action === 'approve' ? 'Approved' : 'Rejected'} ${name}`, action === 'approve' ? 'success' : 'info');
      } catch (e) { showToast(`Could not ${action} ${name}: ${e.message || e}`, 'error'); }
      loadSkills(); refreshOverview();
      return;
    }
    if (t.id === 'ag-skill-fetch') {
      const url = document.getElementById('ag-skill-url').value.trim();
      if (!url) { showToast('Enter a git address first', 'error'); return; }
      t.disabled = true; t.textContent = 'Fetching…';
      try {
        await api('/api/skills/fetch', { method: 'POST', body: JSON.stringify({ url, ref: document.getElementById('ag-skill-ref').value.trim() || null, subdir: document.getElementById('ag-skill-subdir').value.trim() || null }) });
        showToast('Fetched. Review it under "Skill packs awaiting review".', 'success');
      } catch (e) { showToast('Could not fetch: ' + (e.message || e), 'error'); }
      loadSkills(); refreshOverview();
    }
  });

  function onEdit(ev) {
    const t = ev.target;
    if (t.dataset && t.dataset.f) {
      const f = field(t.dataset.f);
      if (!f) return;
      stage(f, readControl(f, t));
      refreshRow(f);
    } else if (t.dataset && t.dataset.adv) {
      S.advanced[t.dataset.adv] = t.checked; render();
    } else if (t.dataset && t.dataset.botMode !== undefined) {
      const id = t.dataset.botMode;
      api(`/api/instances/${id}/permissions`, { method: 'PUT', body: JSON.stringify({ mode: t.value }) })
        .then(() => { showToast('Permission mode saved', 'success'); refreshOverview(); })
        .catch(e => { showToast(String(e.message || e).includes('permissions_locked') ? 'Permissions are locked in the settings above' : 'Could not save: ' + (e.message || e), 'error'); refreshOverview(); });
    } else if (t.id === 'ag-tool-filter') {
      S.toolFilter = t.value;
      const pos = t.selectionStart;
      render(); const box = document.getElementById('ag-tool-filter'); box.focus(); box.setSelectionRange(pos, pos);
    }
  }
  root.addEventListener('input', (ev) => { if (ev.target.id === 'ag-tool-filter' || (ev.target.dataset && ev.target.dataset.f && ev.target.type !== 'checkbox' && ev.target.tagName !== 'SELECT')) onEdit(ev); });
  root.addEventListener('change', onEdit);

  window.addEventListener('beforeunload', (ev) => { if (Object.keys(S.dirty).length) { ev.preventDefault(); ev.returnValue = ''; } });
  window.abpAgents = { refresh: refreshOverview, openBot, setTab, goTo, state: S };

  // Start once the page can sign in (the token may arrive a moment after the page loads), then keep the overview fresh.
  (async function start() {
    for (let i = 0; i < 120 && !S.loaded; i++) {
      if (typeof getToken === 'function' && getToken() && await load()) break;
      await new Promise(r => setTimeout(r, 500));
    }
    if (typeof pollWhenVisible === 'function') pollWhenVisible(() => { if (S.loaded && S.tab === 'overview') refreshOverview(); }, 10000);
  })();
})();
