// Settings: one organized place for every setting ABP has (PLAN-local-chat.md P3). Nothing moves out of its page and
// nothing is duplicated: each section explains what it covers and links straight to the page or card where that setting
// lives today, the backend setup wizard is "Run setup again" under Backends, and the Local Chat defaults (new with it) are
// edited here. A search box filters everything. Old links keep working: #setup, #wizard, #backends, #preferences and
// #config land on the matching section, and #settings/<section> opens one directly.
//
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_settings_hub.py fails if the two differ, if a link points at a page neither app has, or if a page that
// existed before Settings is missing from both the navigation and this index. It uses the page's own api() and draws
// into #settings-root. ABP.settingsIndex is the same table as data (used by the capability index and Local Chat's palette).
// A search also shows matches from ABP's capability index (/api/capabilities) under "Elsewhere in ABP".
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) { /* plain */ } return String(m); };
  const toast = (m, t) => { if (typeof window.showToast === 'function') window.showToast(m, t); };

  // Each item: [label, what it covers, target] - target is a section id, an element id inside a page ("card:<id>"), or an
  // action ("do:<name>").
  const SECTIONS = [
    { id: 'backends', title: 'Backends', desc: 'The cloud and local backends ABP routes work through: Claude API, Claude Code CLI, Claude Desktop, Hermes. Local Chat needs none of them.',
      items: [['Run setup again', 'The backend setup wizard: API keys, CLI paths, the Hermes gateway', 'do:wizard'],
        ['Control Center', 'Default backend per action, backend models, security and process controls', 'control'],
        ['Models & providers', 'Providers, API keys and per-model toggles', 'models'],
        ['Model Router', 'Automatic routing between models, its learning and policy', 'router'],
        ['Hermes Manager', 'The Hermes agent: gateway, sessions, config, backups', 'hermes-manager']] },
    { id: 'local-models', title: 'Local models', desc: 'ModelMistress (the local model hub), Local Chat\'s defaults and the other local runtimes.',
      items: [['Local Chat defaults', 'Default model, agent type, autonomy, cloud assist and worktrees for new sessions (below)', 'card:sh-localchat'],
        ['Modules (ModelMistress)', 'The hub: install, update, start, its models and settings', 'modules'],
        ['Local AI', 'Local models, server and fine-tuning', 'localai'],
        ['Ollama', 'Pull, load and create Ollama models (opt-in)', 'ollama'],
        ['Unsloth', 'Local models, fine-tuning and export (opt-in)', 'unsloth'],
        ['Neural Lab', 'Design and train models', 'lab']] },
    { id: 'cloud', title: 'Cloud & capacity', desc: 'When cloud models help: the capacity governor (cloud-led / hybrid / local-only), budgets and usage.',
      items: [['Capacity governor', 'The current mode, pinning it by hand, probes and handoff packs', 'coordination'],
        ['Usage & limits', 'Model use, allowances, blocks and resets', 'usage']] },
    { id: 'agent', title: 'Agent', desc: 'How ABP\'s own agent behaves: permissions, approvals, tools, skills, MCP servers, hooks.',
      items: [['ABP Agents', 'Agent settings, safety, tools, skills and sub-agents', 'agents'],
        ['Permissions & approvals', 'Approval rules, modes, MCP pins, taint', 'permissions'],
        ['Skills', 'Skill packs, quarantine and drafts', 'skills'],
        ['MCP servers', 'The MCP servers ABP runs and controlling ABP over MCP', 'control'],
        ['Automation', 'Hooks, agent settings and auto-manage', 'automation'],
        ['Routines', 'Saved tasks you can run and re-time', 'routines'],
        ['User profiles', 'What the agent learned about people', 'profiles']] },
    { id: 'factory', title: 'Factory', desc: 'The conductor (planner, confined workers, reviewer, push queue), swarms and evaluation.',
      items: [['Conductor & push queue', 'Integration branches waiting for approval; nothing pushes without it', 'coordination'],
        ['Swarms', 'Multi-bot orchestration', 'swarms'],
        ['Kanban', 'Per-bot task boards', 'kanban'],
        ['Evals', 'Agent eval results', 'evals'],
        ['Traces', 'What each agent run did', 'traces']] },
    { id: 'safety', title: 'Safety', desc: 'Confinement, what is running, the emergency stops, credentials.',
      items: [['Sandbox', 'What ABP has running, cells and limits', 'sns'],
        ['Emergency stop', 'Engage or release the agent emergency stop', 'do:estop'],
        ['Diagnostics', 'Crash reports, processes, support bundle', 'diagnostics'],
        ['Vault', 'Logins the browser agent may use', 'vault'],
        ['Allowlisted users', 'Who may talk to the bots', 'control']] },
    { id: 'platforms', title: 'Platforms & bots', desc: 'Chat platforms and the bots on them.',
      items: [['Bots', 'Bot instances, their platforms and agents', 'bots'],
        ['Platforms', 'Legacy single-bot config', 'platforms'],
        ['Chat', 'Talk to any connected bot (Chat with Bot)', 'chat'],
        ['Support Bot', 'The local assistant for server management', 'support-bot', 'support'],
        ['Training', 'Support Bot phrases and bot instructions', 'training']] },
    { id: 'devices', title: 'Devices & linked servers', desc: 'Phones, other ABP installs, remote machines and networks.',
      items: [['Mobile', 'Pair and manage devices', 'mobile'],
        ['Linked Servers', 'Other AgenticBotPlatform installs', 'servers'],
        ['Server Chat', 'Messages and files between devices', 'server-chat'],
        ['Tailscale', 'Tailnet, Serve and Funnel', 'tailscale'],
        ['SSH Toolkit', 'SSH connections', 'ssh-toolkit'],
        ['Browser', 'Link your real browser', 'browser'],
        ['Power', 'Keep awake and wake machines', 'power'],
        ['TransferDaemon', 'Encrypted messages and files', 'transferdaemon']] },
    { id: 'storage', title: 'Storage', desc: 'ABP\'s database, retention and the file server.',
      items: [['Database', 'Storage and maintenance', 'database'],
        ['Retention', 'How long records are kept, auto-vacuum', 'card:retention-vacuum-days'],
        ['Storage', 'File server, parity, backups', 'storage'],
        ['Files', 'Browse the server\'s files', 'files']] },
    { id: 'appearance', title: 'Appearance & shortcuts', desc: 'How ABP looks on this device and its keyboard shortcuts.',
      items: [['Appearance', 'Theme, accent, scale, density, icon (this device only)', 'card:appearance-theme-seg'],
        ['Keyboard shortcuts', 'Every shortcut, and changing them', 'do:shortcuts'],
        ['Customize UI', 'Describe a change, preview, apply', 'customize'],
        ['Studio', 'Change the UI, see it live', 'studio']] },
    { id: 'updates', title: 'Updates', desc: 'New versions and keeping ABP healthy.',
      items: [['Updates', 'Check for and install the latest version', 'updates'],
        ['Resilience', 'Hot reload and health checks', 'resilience']] },
    { id: 'advanced', title: 'Advanced', desc: 'Raw configuration, its history and backups, the environment, logs.',
      items: [['Environment', 'The .env file ABP reads', 'card:env-editor'],
        ['Snapshots', 'Save and restore the whole configuration', 'card:snapshots-card'],
        ['Config history', 'Every config change and a way back', 'control'],
        ['Live Logs', 'Streaming tail', 'logs'],
        ['Connections & Telemetry', 'Health, latency and errors', 'telemetry'],
        ['Jobs', 'Queue, history and backends', 'jobs'],
        ['Sessions', 'Past conversations', 'sessions']] },
  ];
  // Old links that now land in Settings (nothing they pointed at was removed).
  const REDIRECTS = { setup: 'backends', wizard: 'backends', 'setup-wizard': 'backends', backends: 'backends', preferences: '', config: 'advanced' };

  const MODES = ['chat', 'build', 'plan', 'review'];
  const AUTONOMY = ['0 Locked', '1 Cautious', '2 Supervised', '3 Trusted', '4 Autonomous', '5 Sovereign'];
  let query = '';
  let lc = null;          // /api/local-chat/status (settings + hub)
  let models = [];        // chat models from the catalog
  let setup = null;       // /api/setup/status
  let elsewhere = [];     // capability-index matches for the current search
  let capTimer = null;

  window.ABP = window.ABP || {};
  window.ABP.settingsIndex = SECTIONS.map((s) => ({ id: s.id, title: s.title, desc: s.desc,
    items: s.items.map(([label, desc, target, alt]) => ({ label, desc, target, alt: alt || null })) }));

  function css() {
    if ($('sh-css')) return;
    const st = document.createElement('style');
    st.id = 'sh-css';
    st.textContent = `nav.sidenav a.nav-settings{position:sticky;bottom:0;background:var(--surface);border-top:1px solid var(--line);z-index:1}
.sh-bar{display:flex;gap:8px;align-items:center;margin:4px 0 12px}
.sh-bar input{flex:1;max-width:520px;padding:7px 10px;border-radius:8px;border:1px solid var(--line);background:var(--surface-2);color:var(--ink);font:inherit}
.sh-toc{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px}
.sh-toc a{font-size:12px;padding:3px 10px;border-radius:999px;background:var(--surface-2);color:var(--ink-soft);text-decoration:none}
.sh-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px}
.sh-card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.sh-card.sh-hit{border-color:var(--accent)}
.sh-card h3{margin:0 0 4px}
.sh-card p.sh-desc{margin:0 0 8px;color:var(--muted);font-size:12.5px}
.sh-item{display:flex;gap:8px;align-items:baseline;padding:5px 0;border-top:1px solid var(--line)}
.sh-item a,.sh-item button.sh-link{font-weight:600;color:var(--accent);background:none;border:0;padding:0;font:inherit;cursor:pointer;text-decoration:none;white-space:nowrap}
.sh-item span{color:var(--ink-soft);font-size:12.5px}
.sh-missing{opacity:.55}
.sh-form{display:grid;grid-template-columns:auto 1fr;gap:6px 10px;align-items:center;margin:8px 0;font-size:12.5px}
.sh-form select,.sh-form input{padding:4px 6px;border-radius:6px;border:1px solid var(--line);background:var(--surface-2);color:var(--ink);font:inherit;font-size:12.5px;min-width:0}
.sh-note{color:var(--muted);font-size:12px}
.sh-flash{animation:sh-flash 1.6s ease-out}
@keyframes sh-flash{0%{box-shadow:0 0 0 3px var(--accent)}100%{box-shadow:0 0 0 0 transparent}}`;
    document.head.appendChild(st);
  }

  const exists = (target) => {
    if (target.startsWith('do:')) return true;
    const id = target.startsWith('card:') ? target.slice(5) : target;
    return !!$(id);
  };
  function itemHtml([label, desc, target, alt]) {
    const t = exists(target) ? target : alt && exists(alt) ? alt : target;
    const ok = exists(t);
    const link = t.startsWith('do:') || t.startsWith('card:')
      ? `<button type="button" class="sh-link" data-sh-go="${E(t)}">${E(label)}</button>`
      : `<a href="#${E(t)}" data-sh-go="${E(t)}">${E(label)}</a>`;
    return `<div class="sh-item${ok ? '' : ' sh-missing'}">${link}<span>${E(desc)}${ok ? '' : ' (not in this app)'}</span></div>`;
  }
  function matches(sec, q) {
    if (!q) return true;
    const hay = (sec.title + ' ' + sec.desc + ' ' + sec.items.map((i) => i[0] + ' ' + i[1]).join(' ')).toLowerCase();
    return q.split(/\s+/).every((w) => hay.includes(w));
  }
  function itemMatches(item, q) { return !q || q.split(/\s+/).every((w) => (item[0] + ' ' + item[1]).toLowerCase().includes(w)); }

  function backendsExtra() {
    if (!setup) return '';
    const missing = (setup.missing || setup.required || []).length;
    return `<p class="sh-note">${setup.ready ? 'A backend is set up.' : 'No cloud backend is set up (optional: Local Chat works without one).'}${missing && !setup.ready ? ' ' + E(missing) + ' required field(s) open.' : ''}</p>
<button type="button" class="btn primary" data-sh-go="do:wizard">Run setup again</button>`;
  }
  function localChatForm() {
    const st = (lc && lc.settings) || {};
    const hub = (lc && lc.hub) || {};
    const opt = (vals, cur, fmt) => vals.map((v, n) => `<option value="${E(v)}"${String(cur) === String(v) ? ' selected' : ''}>${E(fmt ? fmt(v, n) : v)}</option>`).join('');
    return `<div id="sh-localchat"><p class="sh-note">ModelMistress: ${lc ? (hub.up ? 'running ' + E(hub.version || '') + (hub.degraded ? ' (degraded: ' + E(hub.degraded) + ')' : '') : 'not running') : 'checking…'}</p>
<div class="sh-form">
<label for="sh-lc-model">Default model</label><select id="sh-lc-model" data-sh-key="default_model">${opt(['', ...models.map((m) => m.id)], st.default_model || '', (v) => (v ? (models.find((m) => m.id === v) || {}).name || v : 'the loaded model, else the best local coder'))}</select>
<label for="sh-lc-mode">Agent type</label><select id="sh-lc-mode" data-sh-key="default_mode">${opt(MODES, st.default_mode || 'build')}</select>
<label for="sh-lc-autonomy">Autonomy</label><select id="sh-lc-autonomy" data-sh-key="default_autonomy" data-sh-int="1">${opt([0, 1, 2, 3, 4, 5], st.default_autonomy == null ? 2 : st.default_autonomy, (v, n) => AUTONOMY[n])}</select>
<label for="sh-lc-cloud">Cloud assist</label><select id="sh-lc-cloud" data-sh-key="cloud_assist">${opt(['off', 'ask', 'auto'], st.cloud_assist || 'ask')}</select>
<label for="sh-lc-wt">Worktrees</label><select id="sh-lc-wt" data-sh-key="worktrees">${opt(['auto', 'off'], st.worktrees || 'auto', (v) => (v === 'auto' ? 'a worktree per code session' : 'off'))}</select>
<label for="sh-lc-prefix">Branch prefix</label><input id="sh-lc-prefix" data-sh-key="worktree_prefix" value="${E(st.worktree_prefix || 'chat')}">
<label for="sh-lc-root">Worktree folder</label><input id="sh-lc-root" data-sh-key="worktree_root" value="${E(st.worktree_root || '')}" placeholder="default: a short folder in ABP's agent data">
<label for="sh-lc-compact">Compact at</label><select id="sh-lc-compact" data-sh-key="compact_at" data-sh-num="1">${opt([0.5, 0.6, 0.7, 0.8, 0.9, 0], st.compact_at == null ? 0.7 : st.compact_at, (v) => (Number(v) ? Math.round(v * 100) + '% of the context' : 'never'))}</select>
<label for="sh-lc-archive">Auto-archive</label><select id="sh-lc-archive" data-sh-key="auto_archive_days" data-sh-int="1">${opt([0, 7, 14, 30, 90], st.auto_archive_days || 0, (v) => (Number(v) ? 'after ' + v + ' quiet days' : 'off'))}</select>
<label for="sh-lc-next">Next-step box</label><select id="sh-lc-next" data-sh-key="next_step_box" data-sh-bool="1">${opt(['true', 'false'], String(st.next_step_box !== false), (v) => (v === 'true' ? 'on: task list and a question' : 'off'))}</select>
</div><p class="sh-note">Saved to the config (local_chat.*) as you change them; new sessions use them.</p></div>`;
  }

  function render() {
    const root = $('settings-root');
    if (!root) return;
    const q = query.trim().toLowerCase();
    const shown = SECTIONS.filter((s) => matches(s, q));
    const focusId = document.activeElement && document.activeElement.id;
    root.innerHTML = `<div class="sh-bar"><input type="search" id="sh-search" aria-label="Search settings" placeholder="Search every setting (e.g. theme, API key, worktree, approvals)" value="${E(query)}"></div>
<nav class="sh-toc" aria-label="Settings sections">${SECTIONS.map((s) => `<a href="#settings/${E(s.id)}">${E(s.title)}</a>`).join('')}</nav>
<div class="sh-grid">${shown.map((s) => `<div class="sh-card${q ? ' sh-hit' : ''}" id="settings-${E(s.id)}"><h3>${E(s.title)}</h3><p class="sh-desc">${E(s.desc)}</p>
${s.id === 'backends' ? backendsExtra() : ''}${s.items.filter((i) => !q || matches({ title: s.title, desc: s.desc, items: [] }, q) || itemMatches(i, q)).map(itemHtml).join('')}
${s.id === 'local-models' ? localChatForm() : ''}</div>`).join('') || '<p class="sh-note">No setting matches. Try fewer words.</p>'}</div>
${q && elsewhere.length ? `<div class="sh-card sh-else"><h3>Elsewhere in ABP</h3>${elsewhere.map((c) => {
    const go = c.where.startsWith('#') ? c.where.slice(1) : c.where.startsWith('action:') ? 'do:action:' + c.where.slice(7) : c.where.startsWith('settings/') ? 'do:section:' + c.where.slice(9) : '';
    return `<div class="sh-item">${go ? `<button type="button" class="sh-link" data-sh-go="${E(go)}">${E(c.label)}</button>` : `<b>${E(c.label)}</b>`}<span>${E(c.kind)} · ${E(c.desc)}</span></div>`;
  }).join('')}</div>` : ''}`;
    if (focusId === 'sh-search') { const box = $('sh-search'); box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
  }

  function scrollTo(el) {
    if (!el) return;
    // Every section is content-visibility:auto, so scrollIntoView lands short of a far target: lay them all out first and
    // scroll the page's scroller to a fixed offset (what the navigation links do too).
    const sections = document.querySelectorAll('section');
    sections.forEach((sec) => { sec.style.contentVisibility = 'visible'; });
    void document.body.offsetHeight;
    const scroller = document.querySelector('main');
    if (scroller && scroller.contains(el)) {
      scroller.scrollTo({ top: el.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop - 70 });   // instant: a smooth jump this far outlasts the layout
    } else el.scrollIntoView({ block: 'start' });
    setTimeout(() => sections.forEach((sec) => { sec.style.contentVisibility = ''; }), 1500);
    el.classList.remove('sh-flash');
    void el.offsetWidth;
    el.classList.add('sh-flash');
  }
  function go(target) {
    if (target === 'do:wizard') { const b = $('btn-open-wizard'); if (b) b.click(); else toast('The setup wizard is not in this app.', 'error'); return; }
    if (target === 'do:shortcuts') { if (window.abpShortcuts) window.abpShortcuts.openHelp(); return; }
    if (target === 'do:estop') { if (window.abpActions) window.abpActions.run('estop.agent'); return; }
    if (target.startsWith('do:action:')) { if (window.abpActions) window.abpActions.run(target.slice(10)); return; }
    if (target.startsWith('do:section:')) { openSection(target.slice(11)); return; }
    if (target.startsWith('card:')) { const el = $(target.slice(5)); scrollTo(el && (el.closest('.card') || el.closest('[id^="sh-"]') || el)); return; }
    const link = document.querySelector(`nav.sidenav a[href="#${target}"]`);
    if (link) link.click(); else scrollTo($(target));
  }
  function openSection(id) {
    scrollTo($('settings-' + id) || $('settings'));
  }
  function onHash() {
    const h = (location.hash || '').replace(/^#/, '');
    if (Object.prototype.hasOwnProperty.call(REDIRECTS, h)) {
      history.replaceState(null, '', '#settings/' + (REDIRECTS[h] || 'backends'));
      openSection(REDIRECTS[h] || 'backends');
      return;
    }
    if (h.startsWith('settings/')) openSection(h.slice(9));
  }

  async function load() {
    const [a, b, c] = await Promise.all([api('/api/local-chat/status').catch(() => null), api('/api/setup/status').catch(() => null),
      api('/api/local-chat/models').catch(() => null)]);
    lc = a; setup = b;
    models = ((c && c.models) || []).filter((m) => m.kind === 'chat');
    render();
  }

  function init() {
    const root = $('settings-root');
    if (!root) return;
    css();
    render();
    root.addEventListener('input', (ev) => {
      if (ev.target.id !== 'sh-search') return;
      query = ev.target.value;
      render();
      clearTimeout(capTimer);
      const q = query.trim();
      if (q.length < 2) { elsewhere = []; return; }
      capTimer = setTimeout(() => {
        api('/api/capabilities?limit=8&kind=page&kind=action&kind=command&kind=tool&kind=module&q=' + encodeURIComponent(q))
          .then((d) => { if (query.trim() === q) { elsewhere = d.results || []; render(); } }, () => {});
      }, 250);
    });
    root.addEventListener('click', (ev) => {
      const t = ev.target.closest('[data-sh-go]');
      if (t) { ev.preventDefault(); go(t.dataset.shGo); return; }
      const toc = ev.target.closest('.sh-toc a');
      if (toc) { ev.preventDefault(); const id = toc.getAttribute('href').split('/')[1]; history.replaceState(null, '', toc.getAttribute('href')); scrollTo($('settings-' + id)); }
    });
    root.addEventListener('change', async (ev) => {
      const t = ev.target;
      if (!t.dataset || !t.dataset.shKey) return;
      const value = t.dataset.shInt || t.dataset.shNum ? Number(t.value) : t.dataset.shBool ? t.value === 'true' : t.value.trim();
      try {
        await api('/api/config/set', { method: 'POST', body: JSON.stringify({ path: ['local_chat', t.dataset.shKey], value }) });
        toast('Saved.');
        load();
      } catch (e) { toast(errText(e), 'error'); }
    });
    window.addEventListener('hashchange', onHash);
    // A deep link at boot: rewrite the old hash now, scroll once the page has finished laying out (panels keep growing
    // until then, which would leave the scroll short).
    const later = () => setTimeout(onHash, 700);
    if (document.readyState === 'complete') later(); else window.addEventListener('load', later, { once: true });
    const h0 = (location.hash || '').replace(/^#/, '');
    if (Object.prototype.hasOwnProperty.call(REDIRECTS, h0)) history.replaceState(null, '', '#settings/' + (REDIRECTS[h0] || 'backends'));
    const sec = root.closest('section') || root;
    if ('IntersectionObserver' in window) new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) load(); }).observe(sec);   // fresh each time it comes into view
    else load();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
