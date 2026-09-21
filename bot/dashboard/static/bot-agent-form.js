// The "ABP Agent settings" part of the Add / Edit a bot form. This file is identical in the dashboard
// (bot/dashboard/static/) and the desktop app (desktop-app/ui/); tests/test_agents_page.py fails if the two differ.
//
// It shows itself when the chosen backend runs ABP's own agent loop (native_agent, api, custom_model), fills itself from
// the bot being edited, and saves through the same routes the ABP Agents page uses:
//   PUT  /api/instances/{id}/permissions   the bot's own permission mode ("" = follow the global default)
//   POST /api/agent-settings               sub-agent limits, worker and fallback models, effort, plan approval
// It reads only what the bot has set itself (GET /api/agent-settings?own=true), so saving never pins an inherited value.
(function () {
  'use strict';
  const AGENT_BACKENDS = ['native_agent', 'api', 'custom_model'];
  const EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']; // mirrors bot/effort.py
  const MODES = [
    ['plan', 'Plan - read-only: it looks but changes nothing'],
    ['default', 'Default - asks before it changes anything'],
    ['accept_edits', 'Accept edits - edits files freely, asks before commands'],
    ['bypass', 'Bypass - never asks (must also be allowed under Safety)'],
  ];
  const MODE_NAME = { plan: 'Plan', default: 'Default', accept_edits: 'Accept edits', bypass: 'Bypass' };
  const $ = (id) => document.getElementById(id);
  const panel = () => $('bot-agent-panel');
  if (!panel()) return;

  let loaded = null;       // what the form was filled from, for the bot being edited (null for a new bot)
  let globalDefaults = null;

  function fillOptions() {
    const perm = $('bot-agent-permission');
    if (perm && !perm.options.length) {
      perm.innerHTML = '<option value="">Follow the global default</option>' + MODES.map(([v, t]) => `<option value="${v}">${esc(t)}</option>`).join('');
    }
    ['bot-agent-worker-effort', 'bot-agent-manager-effort'].forEach(id => {
      const sel = $(id);
      if (sel && !sel.options.length) sel.innerHTML = '<option value="">Default</option>' + EFFORTS.map(l => `<option value="${l}">${l}</option>`).join('');
    });
  }

  const join = (provider, model) => (provider && model ? `${provider}/${model}` : (model || provider || ''));
  function split(text) {
    const t = (text || '').trim();
    if (!t) return [null, null];
    const i = t.indexOf('/');
    return i > 0 ? [t.slice(0, i), t.slice(i + 1) || null] : [null, t];
  }

  function read() {
    const [wp, wm] = split($('bot-agent-worker-model').value);
    const [fp, fm] = split($('bot-agent-fallback').value);
    const max = $('bot-agent-max-children').value.trim();
    return {
      mode: $('bot-agent-permission').value,
      settings: {
        max_concurrent_children: max === '' ? null : Number(max),
        worker_provider: wp, worker_model: wm, fallback_provider: fp, fallback_model: fm,
        worker_effort: $('bot-agent-worker-effort').value || null,
        manager_effort: $('bot-agent-manager-effort').value || null,
        require_plan_approval: $('bot-agent-plan-approval').checked ? true : null,
      },
    };
  }

  function write(mode, own, resolved) {
    $('bot-agent-permission').value = mode || '';
    $('bot-agent-max-children').value = own.max_concurrent_children == null ? '' : own.max_concurrent_children;
    $('bot-agent-max-children').placeholder = resolved ? `${resolved.max_concurrent_children} (default)` : 'default';
    $('bot-agent-worker-model').value = join(own.worker_provider, own.worker_model);
    $('bot-agent-fallback').value = join(own.fallback_provider, own.fallback_model);
    $('bot-agent-worker-effort').value = own.worker_effort || '';
    $('bot-agent-manager-effort').value = own.manager_effort || '';
    $('bot-agent-plan-approval').checked = !!own.require_plan_approval;
  }

  async function showGlobals() {
    const target = $('bot-agent-global');
    if (!target) return;
    try {
      if (!globalDefaults) globalDefaults = (await api('/api/agent/config')).values;
    } catch (_e) { target.textContent = ''; return; }
    const v = globalDefaults;
    const on = (id) => v[id] ? 'on' : 'off';
    target.innerHTML = `<b>Global defaults this bot starts from:</b> approvals ${esc(MODE_NAME[v['native_agent.permissions.mode']] || v['native_agent.permissions.mode'])}
      · commands run ${v['native_agent.sandbox.backend'] === 'docker' ? 'in Docker' : 'locally'} · web ${on('native_agent.web.enabled')} · browser ${on('native_agent.browser.enabled')}
      · up to ${esc(v['native_agent.limits.max_iterations'])} steps per turn.`;
    const perm = $('bot-agent-permission');
    if (perm && perm.options.length) perm.options[0].textContent = `Follow the global default (${MODE_NAME[v['native_agent.permissions.mode']] || v['native_agent.permissions.mode']})`;
  }

  // Show or hide the panel for the backend chosen in the form.
  function sync() {
    const backend = $('bot-new-backend').value;
    const show = AGENT_BACKENDS.includes(backend);
    panel().classList.toggle('hidden', !show);
    if (show) { fillOptions(); showGlobals(); }
  }

  function reset() {
    fillOptions();
    loaded = null;
    write('', {}, null);
    api('/api/agent-settings').then(r => { $('bot-agent-max-children').placeholder = `${r.max_concurrent_children} (default)`; }).catch(() => {});
  }

  // Fill the panel from the bot being edited.
  async function load(botId) {
    fillOptions();
    loaded = null;
    try {
      const [own, resolved, perm] = await Promise.all([
        api(`/api/agent-settings?instance_id=${botId}&own=true`),
        api(`/api/agent-settings?instance_id=${botId}`),
        api(`/api/instances/${botId}/permissions`),
      ]);
      const mode = (perm.instance && perm.instance.mode) || '';
      write(mode, own, resolved);
      loaded = { id: botId, snapshot: JSON.stringify(read()) };
    } catch (_e) { /* the panel stays blank; saving then only writes what is typed */ }
  }

  // Save the panel for a bot that now exists. Only writes what changed, and nothing for a new bot left at its defaults.
  async function save(botId) {
    if (!AGENT_BACKENDS.includes($('bot-new-backend').value)) return;
    const now = read();
    const untouched = JSON.stringify(now) === (loaded && loaded.id === botId ? loaded.snapshot : JSON.stringify({ mode: '', settings: { max_concurrent_children: null, worker_provider: null, worker_model: null, fallback_provider: null, fallback_model: null, worker_effort: null, manager_effort: null, require_plan_approval: null } }));
    if (untouched) return;
    const before = loaded && loaded.id === botId ? JSON.parse(loaded.snapshot) : { mode: '' };
    if (now.mode !== before.mode) {
      await api(`/api/instances/${botId}/permissions`, { method: 'PUT', body: JSON.stringify({ mode: now.mode }) });
    }
    if (JSON.stringify(now.settings) !== JSON.stringify(before.settings || {})) {
      await api('/api/agent-settings', { method: 'POST', body: JSON.stringify({ instance_id: botId, ...now.settings }) });
    }
  }

  panel().addEventListener('click', (ev) => {
    if (ev.target.closest('[data-go-agents]')) { ev.preventDefault(); if (window.abpAgents) window.abpAgents.goTo('agents:overview'); }
  });
  window.abpBotAgentForm = { sync, reset, load, save, backends: AGENT_BACKENDS };
  sync();
})();
