// This page runs two ways: inside the Tauri desktop shell (window.__TAURI__
// present — the dashboard's own fetch() calls must go cross-origin to the
// Python server on 127.0.0.1) or served directly by FastAPI in a plain
// browser (relative fetch() calls, no boot overlay needed).
const IS_TAURI = !!window.__TAURI__;
const API_BASE = IS_TAURI ? 'http://127.0.0.1:8787' : '';

const state = { jobFilter: 'all', logLevel: 'all', configCache: null };

function getToken() { return localStorage.getItem('dashboard_token') || ''; }
function setToken(t) { localStorage.setItem('dashboard_token', t); }

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  const token = getToken();
  if (token) headers['X-Dashboard-Token'] = token;
  if (opts.method && opts.method !== 'GET') {
    headers['Content-Type'] = 'application/json';
  }
  const res = await fetch(API_BASE + path, Object.assign({}, opts, { headers }));
  if (res.status === 401 || res.status === 503) {
    showTokenModal();
    throw new Error('unauthorized');
  }
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function showTokenModal() { document.getElementById('tokenModal').classList.remove('hidden'); }
function hideTokenModal() { document.getElementById('tokenModal').classList.add('hidden'); }
document.getElementById('btn-token').onclick = showTokenModal;
document.getElementById('tokenCancel').onclick = hideTokenModal;
document.getElementById('tokenSave').onclick = () => {
  setToken(document.getElementById('tokenInput').value.trim());
  hideTokenModal();
  refreshEnvEditor(true);
  refreshEnvBackups();
  connectDevicesSocket();
};

// ---------------------------------------------------- live device presence
// A WebSocket to /api/ws carries device-presence deltas (pairing, going
// online/offline) so the Mobile tab updates in real time instead of on the
// next 15s poll. refreshMobileKeys()'s own poll stays running regardless —
// this is a live upgrade over that fallback, not a replacement for it, so a
// dropped/never-established socket degrades to "as live as before" rather
// than "broken".
function wsBase() {
  const httpBase = API_BASE || location.origin;
  return httpBase.replace(/^http/, 'ws');
}

let devicesSocket = null;
let devicesSocketRetryTimer = null;

function connectDevicesSocket() {
  const token = getToken();
  if (!token) return;
  if (devicesSocket && (devicesSocket.readyState === WebSocket.OPEN || devicesSocket.readyState === WebSocket.CONNECTING)) return;
  clearTimeout(devicesSocketRetryTimer);
  try {
    devicesSocket = new WebSocket(`${wsBase()}/api/ws?token=${encodeURIComponent(token)}`);
  } catch (_e) {
    devicesSocketRetryTimer = setTimeout(connectDevicesSocket, 4000);
    return;
  }
  devicesSocket.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      if (msg.type === 'device_list') {
        mobileKeysState.devices = msg.devices;
        renderMobileKeysTable();
      } else if ((msg.type === 'job_tool_event' || msg.type === 'job_children_update') && msg.job_id === openDelegationDetailJobId) {
        renderDelegationDetail(msg.job_id);
      }
    } catch (_e) { /* malformed frame, ignore */ }
  };
  devicesSocket.onclose = devicesSocket.onerror = () => {
    devicesSocket = null;
    devicesSocketRetryTimer = setTimeout(connectDevicesSocket, 4000);
  };
}

function esc(s) { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; }

// Custom dropdown standing in for native <select> where it matters: Chrome
// force-closes an open native select popup the instant the underlying page
// scrolls, even for a scroll the user meant to apply to the page behind it,
// not the menu. This one is just an absolutely-positioned div anchored to
// its trigger, so it scrolls along with the page instead of vanishing, and
// only closes on an explicit outside click, Escape, or picking an option.
function wireCustomSelects(root, onChange) {
  root.querySelectorAll('.custom-select').forEach(box => {
    const btn = box.querySelector('.custom-select-btn');
    btn.onclick = (e) => {
      e.stopPropagation();
      const wasOpen = box.classList.contains('open');
      document.querySelectorAll('.custom-select.open').forEach(o => o.classList.remove('open'));
      if (!wasOpen) box.classList.add('open');
    };
    box.querySelectorAll('.custom-select-opt').forEach(opt => {
      opt.onclick = (e) => {
        e.stopPropagation();
        box.classList.remove('open');
        box.querySelectorAll('.custom-select-opt').forEach(o => o.classList.remove('sel'));
        opt.classList.add('sel');
        btn.textContent = opt.dataset.label ?? opt.textContent;
        onChange(box, opt.dataset.value);
      };
    });
  });
}
document.addEventListener('click', () => {
  document.querySelectorAll('.custom-select.open').forEach(o => o.classList.remove('open'));
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') document.querySelectorAll('.custom-select.open').forEach(o => o.classList.remove('open'));
});
function fmtBytes(n) {
  if (!n) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}
function fmtMs(ms) {
  if (ms == null) return '—';
  if (ms < 1000) return Math.round(ms) + 'ms';
  if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
  return Math.round(ms / 60000) + 'm ' + Math.round((ms % 60000) / 1000) + 's';
}
function fmtTime(iso) { if (!iso) return '—'; try { return iso.split('T')[1].split('.')[0] + (iso.includes('Z') ? '' : ''); } catch { return iso; } }
function fmtBytes(b) { return (b / (1024 * 1024)).toFixed(1) + ' MB'; }
function statusChip(status) {
  const map = { running: 'good', success: 'good', queued: 'neutral', retrying: 'warning', failed: 'critical' };
  const cls = map[status] || 'neutral';
  const colorVar = { good: '--good', warning: '--warning', critical: '--critical', neutral: '--muted' }[cls];
  return `<span class="chip ${cls}"><span class="dot" style="background:var(${colorVar})"></span>${esc(status)}</span>`;
}

// ---------------------------------------------------------------- overview
async function refreshOverview() {
  const ov = await api('/api/overview');
  document.getElementById('k-running').textContent = ov.jobs_running;
  document.getElementById('k-queued-note').textContent = ov.jobs_queued + ' queued';
  document.getElementById('k-completed').textContent = ov.completed_today;
  document.getElementById('k-success-rate').textContent = ov.success_rate_7d + '% success (7d)';
  document.getElementById('k-failed').textContent = ov.failed_today;
  document.getElementById('k-duration').textContent = fmtMs(ov.avg_duration_ms);
  document.getElementById('k-dbsize').textContent = ov.db_size_mb + ' MB';
  document.getElementById('k-tokens').textContent = (ov.tokens_today || 0).toLocaleString();
  document.getElementById('s-desktop').textContent = ov.desktop_running ? `running (pid ${ov.desktop_pid})` : 'stopped';
  document.getElementById('s-version').textContent = 'v' + ov.config_version;
  document.getElementById('s-default-backend').textContent = ov.default_backend;
  document.getElementById('reload-version').textContent = 'v' + ov.config_version;
  document.getElementById('s-refreshed').textContent = new Date().toLocaleTimeString();

  const pillBot = document.getElementById('pill-bot');
  pillBot.innerHTML = `<span class="dot good"></span>Bot online`;
  const pillReload = document.getElementById('pill-reload');
  pillReload.innerHTML = `<span class="dot good"></span>Hot-reload armed`;
  document.getElementById('inflight-chip').textContent = ov.jobs_running + ' in-flight' + (ov.jobs_running === 0 ? ' — safe to reload' : '');
  document.getElementById('inflight-chip').className = 'chip ' + (ov.jobs_running === 0 ? 'good' : 'warning');
}

// -------------------------------------------------------------------- jobs
async function refreshJobsTable() {
  const status = state.jobFilter === 'all' ? null : state.jobFilter;
  const jobs = await api('/api/jobs' + (status ? `?status=${status}&limit=50` : '?limit=50'));
  const tbody = document.getElementById('jobs-tbody');
  if (!jobs.length) { tbody.innerHTML = '<tr class="emptyrow"><td colspan="8">No jobs yet — send the bot a message.</td></tr>'; return; }
  tbody.innerHTML = jobs.map(j => `
    <tr>
      <td class="mono">#${j.id}</td>
      <td>${esc(j.action_type)} — ${esc((j.prompt || '').slice(0, 48))}</td>
      <td class="mono">${esc(j.backend)}</td>
      <td>${statusChip(j.status)}</td>
      <td>${esc(j.user_id)}</td>
      <td class="mono">${fmtTime(j.started_at)}</td>
      <td class="num mono">${fmtMs(j.duration_ms)}</td>
      <td class="num mono">${j.tokens != null ? j.tokens.toLocaleString() : '—'}</td>
    </tr>`).join('');
}

async function refreshJobsCharts() {
  const ts = await api('/api/jobs/timeseries');
  const svg = document.getElementById('chart-24h');
  const w = 640, h = 170, base = 140, top = 10;
  const max = Math.max(1, ...ts.map(b => b.completed + b.failed));
  const barW = ts.length ? Math.min(24, (w - 20) / ts.length - 3) : 0;
  let bars = '<g stroke="var(--line)" stroke-width="1"><line x1="0" y1="20" x2="640" y2="20"/><line x1="0" y1="60" x2="640" y2="60"/><line x1="0" y1="100" x2="640" y2="100"/><line x1="0" y1="140" x2="640" y2="140"/></g>';
  ts.forEach((b, i) => {
    const x = 10 + i * ((w - 20) / Math.max(1, ts.length));
    const compH = (b.completed / max) * (base - top);
    const failH = (b.failed / max) * (base - top);
    bars += `<rect x="${x}" y="${base - compH}" width="${barW}" height="${compH}" fill="var(--s1)" rx="2"/>`;
    bars += `<rect x="${x}" y="${base - compH - failH - 2}" width="${barW}" height="${failH}" fill="var(--s2)" rx="2"/>`;
  });
  svg.innerHTML = bars;

  const byBackend = await api('/api/jobs/by-backend');
  const total = Object.values(byBackend).reduce((a, b) => a + b, 0) || 1;
  const colors = { api: 'var(--s1)', cli: 'var(--s2)', ui: 'var(--s3)' };
  const bar = document.getElementById('backend-split-bar');
  const legend = document.getElementById('backend-split-legend');
  bar.innerHTML = ''; legend.innerHTML = '';
  Object.entries(byBackend).forEach(([name, count]) => {
    const pct = Math.round(100 * count / total);
    bar.innerHTML += `<div style="width:${pct}%; background:${colors[name] || 'var(--muted)'}"></div>`;
    legend.innerHTML += `<span class="li"><span class="sw" style="background:${colors[name] || 'var(--muted)'}"></span>${esc(name)} · ${pct}%</span>`;
  });
  if (!Object.keys(byBackend).length) legend.innerHTML = '<span class="li">No jobs today yet.</span>';

  const recent12 = ts.slice(-12);
  const perHour = recent12.map(b => b.completed + b.failed);
  const maxPh = Math.max(1, ...perHour);
  const pts = perHour.map((v, i) => `${(i / Math.max(1, perHour.length - 1)) * 220},${36 - (v / maxPh) * 32}`).join(' ');
  document.getElementById('spark-jobs').setAttribute('points', pts);
  document.getElementById('k-jobsph').textContent = perHour.length ? perHour[perHour.length - 1] : 0;
}

document.querySelectorAll('#job-filter button').forEach(btn => btn.onclick = () => {
  document.querySelectorAll('#job-filter button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  state.jobFilter = btn.dataset.status;
  refreshJobsTable();
});

// -------------------------------------------------------------- telemetry
async function refreshTelemetry() {
  const t = await api('/api/telemetry');
  const grid = document.getElementById('health-grid');
  const items = [];
  items.push({ name: 'Claude Desktop', ok: t.desktop.running, detail: t.desktop.running ? `pid ${t.desktop.pid}` : 'not running' });
  t.mcp_servers.forEach(s => items.push({ name: 'MCP · ' + s.name, ok: s.enabled, detail: s.enabled ? 'enabled' : 'disabled' }));
  Object.entries(t.latency_by_backend).forEach(([name, l]) => {
    const errs = t.recent_errors[name] || 0;
    items.push({ name: 'backend · ' + name, ok: errs === 0, detail: errs ? `${errs} errors (15m)` : `p50 ${Math.round(l.p50_ms)}ms` });
  });
  grid.innerHTML = items.map(i => `<div class="healthitem"><span class="dot ${i.ok ? 'good' : 'critical'}"></span><div class="txt"><b>${esc(i.name)}</b><span>${esc(i.detail)}</span></div></div>`).join('');
  document.getElementById('resilience-health').innerHTML = items.map(i => `<div class="healthitem"><span class="dot ${i.ok ? 'good' : 'warning'}"></span><div class="txt"><b>${esc(i.name)}</b><span>${i.ok ? 'ok' : 'degraded'}</span></div></div>`).join('');

  const degraded = t.mcp_servers.filter(s => !s.enabled);
  const warnPill = document.getElementById('pill-mcp-warn');
  if (degraded.length) {
    warnPill.classList.remove('hidden');
    document.getElementById('pill-mcp-warn-text').textContent = `${degraded.length} MCP disabled`;
  } else warnPill.classList.add('hidden');

  const bars = document.getElementById('latency-bars');
  const maxLatency = Math.max(1, ...Object.values(t.latency_by_backend).map(l => l.p95_ms));
  bars.innerHTML = Object.entries(t.latency_by_backend).map(([name, l]) => {
    const color = { api: 'var(--s1)', cli: 'var(--s2)', ui: 'var(--s3)' }[name] || 'var(--accent)';
    const pct = Math.round(100 * l.p95_ms / maxLatency);
    return `<div><div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:4px;"><span class="mono">${esc(name)}</span><span class="mono">${fmtMs(l.p50_ms)} / ${fmtMs(l.p95_ms)}</span></div><div class="barcap" style="background:var(--surface-2);"><i style="width:${pct}%; background:${color};"></i></div></div>`;
  }).join('') || '<p class="cardnote">No requests recorded yet.</p>';

  const ts = await api('/api/jobs/timeseries');
  const last6 = ts.slice(-6);
  const errorPct = last6.map(b => { const tot = b.completed + b.failed; return tot ? (100 * b.failed / tot) : 0; });
  const maxErr = Math.max(5, ...errorPct);
  const pts = errorPct.map((v, i) => `${(i / Math.max(1, errorPct.length - 1)) * 260},${80 - (v / maxErr) * 60}`).join(' ');
  document.getElementById('chart-errorrate').innerHTML = `<line x1="0" y1="70" x2="260" y2="70" stroke="var(--line)"/><polyline fill="none" stroke="var(--critical)" stroke-width="2" points="${pts}"/>`;
  document.getElementById('errorrate-note').textContent = last6.length ? `Latest hour: ${errorPct[errorPct.length - 1].toFixed(1)}% failed.` : 'No data yet.';
}

// --------------------------------------------------------------- database
async function refreshDatabase() {
  const dbInfo = await api('/api/database');
  document.getElementById('db-size').textContent = fmtBytes(dbInfo.size_bytes);
  document.getElementById('db-path').textContent = dbInfo.path;
  document.getElementById('db-counts').innerHTML = Object.entries(dbInfo.table_counts)
    .map(([t, c]) => `<tr><td>${esc(t)}</td><td class="num mono">${c.toLocaleString()} rows</td></tr>`).join('');

  const exportSel = document.getElementById('export-table');
  if (!exportSel.options.length) {
    exportSel.innerHTML = Object.keys(dbInfo.table_counts)
      .map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join('');
  }

  const t = await api('/api/telemetry');
  const events = t.connection_events.slice(0, 8);
  document.getElementById('db-recent').innerHTML = events.length
    ? events.map(e => `<div><span class="ts">${fmtTime(e.ts)}</span> ${esc(e.component)}.${esc(e.event)}${e.detail ? ' — ' + esc(e.detail) : ''}</div>`).join('')
    : 'No events yet.';
}

document.getElementById('btn-vacuum').onclick = async () => { await api('/api/database/vacuum', { method: 'POST' }); refreshDatabase(); };
document.getElementById('btn-export-json').onclick = () => downloadUrl(`/api/export/${document.getElementById('export-table').value}?format=json`);
document.getElementById('btn-export-csv').onclick = () => downloadUrl(`/api/export/${document.getElementById('export-table').value}?format=csv`);

// ---------------------------------------------------------------- control
// Live-fetched model lists: Claude models come from Anthropic's own
// /v1/models (via ANTHROPIC_API_KEY), Hermes models come from Hermes
// Agent's own disk cache of every provider's live /v1/models response —
// see bot/models.py for why each falls back the way it does. Plain text
// inputs with a <datalist> rather than <select>: Hermes accepts any
// string, and a currently-set model that's fallen out of the live list
// (stale config, offline) must stay visible and editable either way.
let modelsCache = null;

function _defaultModelInputFocused() {
  const active = document.activeElement;
  return active && ['model-api', 'model-hermes_cli', 'model-hermes_gateway'].includes(active.id);
}

function _turnInputsFocused() {
  const active = document.activeElement;
  return active && ['turn-secret', 'turn-urls', 'turn-ttl'].includes(active.id);
}

async function refreshModels() {
  // refreshConfig() runs every 5s via refreshAll() — rebuilding these
  // inputs' backing <datalist> and resetting .value while the user has
  // one of them focused (typing, or picking from the native suggestion
  // popup) closes the popup and can stomp on what they were typing. Skip
  // the whole cycle while any of the three has focus.
  if (_defaultModelInputFocused()) return;
  const m = await api('/api/models');
  modelsCache = m;

  const apiOptions = (m.live && m.live.api) || [];
  document.getElementById('model-api-options').innerHTML = apiOptions.map(name => `<option value="${esc(name)}"></option>`).join('');
  document.getElementById('bot-new-model-options').innerHTML = apiOptions.map(name => `<option value="${esc(name)}"></option>`).join('');
  document.getElementById('model-api').value = m.current.api || '';
  document.getElementById('model-api-note').textContent = (m.live && m.live.api)
    ? `Live from your Anthropic account — ${apiOptions.length} models.`
    : 'No ANTHROPIC_API_KEY configured — no models to show. Set one to see live options.';

  const hermesGrouped = (m.live && m.live.hermes) || null;
  const hermesOptions = hermesGrouped ? [...new Set(Object.values(hermesGrouped).flat())].sort() : [];
  document.getElementById('model-hermes-options').innerHTML = hermesOptions.map(name => `<option value="${esc(name)}"></option>`).join('');
  document.getElementById('model-hermes_cli').value = m.current.hermes_cli || '';
  document.getElementById('model-hermes_gateway').value = m.current.hermes_gateway || '';
  document.getElementById('model-hermes-note').textContent = hermesGrouped
    ? `Live from Hermes's own provider cache — ${hermesOptions.length} models across ${Object.keys(hermesGrouped).length} providers.`
    : 'Hermes not detected on this machine — type a model name manually.';

  refreshBotModelOptions();
  refreshBots();
}

// A given bot instance can only ever reach the models its own backend's
// family exposes (Claude vs Hermes Agent) — see bot/models.py's
// BACKEND_FAMILY. Used both by the Add/Edit form's datalist and by each
// bot card's own quick-pick model <select>.
function modelOptionsForBackend(backend) {
  if (!modelsCache) return [];
  const family = (modelsCache.family || {})[backend] || 'claude';
  if (family === 'hermes') {
    const grouped = (modelsCache.live && modelsCache.live.hermes) || null;
    return grouped ? [...new Set(Object.values(grouped).flat())].sort() : [];
  }
  if (family === 'custom') {
    // Flattened as "<provider>/<model_id>" — matches the exact string
    // custom_model expects in bot_instances.model (see bot/providers.py's
    // parse_model_ref), unlike Claude/Hermes's bare model ids.
    const grouped = (modelsCache.live && modelsCache.live.custom) || null;
    if (!grouped) return [];
    const out = [];
    for (const [provider, models] of Object.entries(grouped)) {
      for (const m of models) out.push(`${provider}/${m}`);
    }
    return out.sort();
  }
  return (modelsCache.live && modelsCache.live.api) || [];
}

// Free-tier model ids follow a "…-free" or "…:free" suffix convention
// across every provider Hermes's cache has shown so far (OpenRouter,
// opencode-free, etc.) — no pricing metadata is available to check
// instead, so this is a naming-convention heuristic, not a guarantee.
function _isFreeModelId(id) {
  return /[-:_]free$/i.test(id);
}

// Same models as modelOptionsForBackend, but grouped by provider (Hermes's
// cache is already keyed by provider; the Claude family has exactly one
// provider) with free models sorted first within each group, for pickers
// that want to show that structure rather than one flat list.
function modelGroupsForBackend(backend) {
  if (!modelsCache) return [];
  const family = (modelsCache.family || {})[backend] || 'claude';
  const sortGroup = (ids) => {
    const uniq = [...new Set(ids)];
    const free = uniq.filter(_isFreeModelId).sort();
    const paid = uniq.filter(id => !_isFreeModelId(id)).sort();
    return [...free, ...paid];
  };
  if (family === 'hermes') {
    const grouped = (modelsCache.live && modelsCache.live.hermes) || null;
    if (!grouped) return [];
    return Object.keys(grouped).sort((a, b) => a.localeCompare(b))
      .map(provider => ({ provider, models: sortGroup(grouped[provider]) }))
      .filter(g => g.models.length);
  }
  if (family === 'custom') {
    const grouped = (modelsCache.live && modelsCache.live.custom) || null;
    if (!grouped) return [];
    // Each model id is stored/shown as "<provider>/<model_id>" (the exact
    // string custom_model expects — see bot/providers.py's
    // parse_model_ref), not the bare id hermes/claude groups use.
    return Object.keys(grouped).sort((a, b) => a.localeCompare(b))
      .map(provider => ({ provider, models: sortGroup(grouped[provider].map(m => `${provider}/${m}`)) }))
      .filter(g => g.models.length);
  }
  const apiOptions = (modelsCache.live && modelsCache.live.api) || [];
  return apiOptions.length ? [{ provider: 'Anthropic', models: sortGroup(apiOptions) }] : [];
}

// Builds the menu HTML (provider group headers, free-tagged options) plus
// the current selection's display label, for the custom-select model
// picker on each bot card.
function buildModelMenuHtml(backend, currentModel) {
  const groups = modelGroupsForBackend(backend);
  const allIds = new Set(groups.flatMap(g => g.models));
  let label = '(backend default)';
  let html = `<div class="custom-select-opt${!currentModel ? ' sel' : ''}" data-value="" data-label="(backend default)">(backend default)</div>`;
  if (currentModel && !allIds.has(currentModel)) {
    label = `${currentModel} (not in live list)`;
    html += `<div class="custom-select-opt sel" data-value="${esc(currentModel)}" data-label="${esc(label)}">${esc(label)}</div>`;
  }
  for (const { provider, models } of groups) {
    html += `<div class="custom-select-group">${esc(provider)}</div>`;
    for (const id of models) {
      const isSel = id === currentModel;
      if (isSel) label = id;
      const freeTag = _isFreeModelId(id) ? ' <span class="tag-free">free</span>' : '';
      html += `<div class="custom-select-opt${isSel ? ' sel' : ''}" data-value="${esc(id)}" data-label="${esc(id)}">${esc(id)}${freeTag}</div>`;
    }
  }
  return { label, html };
}

function refreshBotModelOptions() {
  const backend = document.getElementById('bot-new-backend').value;
  const options = modelOptionsForBackend(backend);
  document.getElementById('bot-new-model-options').innerHTML = options.map(name => `<option value="${esc(name)}"></option>`).join('');
  const help = document.getElementById('bot-new-model-help');
  const input = document.getElementById('bot-new-model');
  if (backend === 'custom_model' || backend === 'native_agent') {
    help.textContent = 'Required for this backend: "<provider>/<model_id>", where <provider> is one configured on the Models tab.';
    input.placeholder = 'e.g. local_ollama/llama3.1';
  } else {
    help.textContent = "Leave blank to use this backend's configured default. Options are live models actually available to this backend.";
    input.placeholder = 'e.g. claude-opus-5';
  }
}

document.getElementById('model-api').onchange = async (e) => {
  const value = e.target.value.trim() || null;
  await api('/api/config/set', { method: 'POST', body: JSON.stringify({ path: ['backends', 'api', 'model'], value }) });
  refreshConfig();
};
['hermes_cli', 'hermes_gateway'].forEach(name => {
  document.getElementById(`model-${name}`).onchange = async (e) => {
    const value = e.target.value.trim() || null;
    await api('/api/config/set', { method: 'POST', body: JSON.stringify({ path: ['backends', name, 'model'], value } ) });
    refreshConfig();
  };
});

async function refreshConfig() {
  const cfg = await api('/api/config');
  state.configCache = cfg;
  const current = cfg.current;
  refreshModels();

  document.querySelectorAll('#default-backend-seg-claude button').forEach(b => b.classList.toggle('active', b.dataset.b === current.default_backend));
  document.querySelectorAll('#default-backend-seg-hermes button').forEach(b => b.classList.toggle('active', b.dataset.b === current.default_hermes_backend));

  const agentMode = (current.agent_control || {}).mode || 'trust_all';
  document.querySelectorAll('#agent-control-seg button').forEach(b => b.classList.toggle('active', b.dataset.m === agentMode));
  refreshBotsCanTargetVisibility(agentMode);

  const overridesEl = document.getElementById('action-overrides-list');
  overridesEl.innerHTML = Object.entries(current.action_overrides || {}).map(([action, entry]) => `
    <div class="settingrow">
      <div><div class="st">${esc(action)} →</div><div class="sd">backup: ${JSON.stringify(entry.backup || [])}</div></div>
      <span class="chip good">${esc(entry.backend)}</span>
    </div>`).join('');

  setToggle('tg-ui-automation', !!(current.features || {}).ui_automation_enabled);
  setToggle('tg-confirm', !!(current.security || {}).confirm_destructive);
  setToggle('tg-verbose', !!(current.features || {}).verbose_telemetry);
  setToggle('tg-retention', (current.retention || {}).enabled !== false);
  if (document.activeElement.id !== 'retention-days') {
    document.getElementById('retention-days').value = (current.retention || {}).days ?? 90;
  }
  if (document.activeElement.id !== 'retention-vacuum-days') {
    document.getElementById('retention-vacuum-days').value = (current.retention || {}).auto_vacuum_every_days ?? 0;
  }

  setToggle('tg-turn-enabled', !!(current.turn || {}).enabled);
  if (!_turnInputsFocused()) {
    document.getElementById('turn-urls').value = ((current.turn || {}).urls || []).join(', ');
    document.getElementById('turn-ttl').value = (current.turn || {}).ttl_s ?? 3600;
    document.getElementById('turn-secret').placeholder = (current.turn || {}).secret_set
      ? '(set) — leave blank to keep unchanged'
      : '(not set) — leave blank to keep unchanged';
  }

  document.getElementById('config-history').innerHTML = cfg.history.map(h => `
    <div class="tlitem"><div class="v">v${h.version}</div><div class="d">${esc(h.summary)}</div><div class="m">${fmtTime(h.ts)} · ${esc(h.actor)}</div></div>`).join('')
    || '<p class="cardnote">No reloads recorded yet.</p>';
}

function setToggle(id, on) {
  const el = document.getElementById(id);
  el.classList.toggle('on', on);
}

document.querySelectorAll('.toggle[data-path]').forEach(el => {
  el.onclick = async () => {
    const [a, b] = el.dataset.path.split(',');
    const nowOn = !el.classList.contains('on');
    await api('/api/config/set', { method: 'POST', body: JSON.stringify({ path: [a, b], value: nowOn }) });
    refreshConfig();
  };
});

document.querySelectorAll('.default-backend-seg button').forEach(btn => {
  btn.onclick = async () => { await api(`/api/backend/default/${btn.dataset.b}`, { method: 'POST' }); refreshConfig(); refreshOverview(); };
});

document.getElementById('turn-secret').onchange = async (e) => {
  const value = e.target.value; // blank means "leave unchanged" — never clears an existing secret by accident
  e.target.value = '';
  if (!value) return;
  await api('/api/config/set', { method: 'POST', body: JSON.stringify({ path: ['turn', 'secret'], value }) });
  refreshConfig();
};
document.getElementById('turn-urls').onchange = async (e) => {
  const urls = e.target.value.split(',').map(s => s.trim()).filter(Boolean);
  await api('/api/config/set', { method: 'POST', body: JSON.stringify({ path: ['turn', 'urls'], value: urls }) });
  refreshConfig();
};
document.getElementById('turn-ttl').onchange = async (e) => {
  const value = Math.max(60, parseInt(e.target.value, 10) || 3600);
  await api('/api/config/set', { method: 'POST', body: JSON.stringify({ path: ['turn', 'ttl_s'], value }) });
  refreshConfig();
};
document.getElementById('retention-days').onchange = async (e) => {
  const value = Math.max(1, parseInt(e.target.value, 10) || 90);
  await api('/api/config/set', { method: 'POST', body: JSON.stringify({ path: ['retention', 'days'], value }) });
  refreshConfig();
};
document.getElementById('retention-vacuum-days').onchange = async (e) => {
  const value = Math.max(0, parseInt(e.target.value, 10) || 0);
  await api('/api/config/set', { method: 'POST', body: JSON.stringify({ path: ['retention', 'auto_vacuum_every_days'], value }) });
  refreshConfig();
};

document.querySelectorAll('#agent-control-seg button').forEach(btn => {
  btn.onclick = async () => {
    await api('/api/config/set', { method: 'POST', body: JSON.stringify({ path: ['agent_control', 'mode'], value: btn.dataset.m }) });
    refreshConfig();
  };
});

// Appearance (theme + UI scale) — per-browser, localStorage only, no
// server round trip. The <head> inline script applies whatever's saved
// here on the very next load, before first paint.
function applyAppearanceTheme(theme) {
  if (theme === 'light' || theme === 'dark') document.documentElement.setAttribute('data-theme', theme);
  else document.documentElement.removeAttribute('data-theme');
  document.querySelectorAll('#appearance-theme-seg button').forEach(b => b.classList.toggle('active', b.dataset.themeChoice === theme));
}
function applyAppearanceScale(scale) {
  document.documentElement.style.zoom = scale;
  document.querySelectorAll('#appearance-scale-seg button').forEach(b => b.classList.toggle('active', b.dataset.scaleChoice === String(scale)));
}
function initAppearance() {
  let theme = 'system', scale = '1';
  try {
    theme = localStorage.getItem('bs-ui-theme') || 'system';
    scale = localStorage.getItem('bs-ui-scale') || '1';
  } catch (_e) { /* localStorage unavailable — defaults above stand */ }
  applyAppearanceTheme(theme);
  applyAppearanceScale(scale);
}
document.querySelectorAll('#appearance-theme-seg button').forEach(btn => {
  btn.onclick = () => {
    const theme = btn.dataset.themeChoice;
    try { localStorage.setItem('bs-ui-theme', theme); } catch (_e) {}
    applyAppearanceTheme(theme);
  };
});
document.querySelectorAll('#appearance-scale-seg button').forEach(btn => {
  btn.onclick = () => {
    const scale = btn.dataset.scaleChoice;
    try { localStorage.setItem('bs-ui-scale', scale); } catch (_e) {}
    applyAppearanceScale(scale);
  };
});
initAppearance();

function _fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

async function refreshSnapshots() {
  const tbody = document.getElementById('snapshots-tbody');
  if (!getToken() || !tbody) return;
  let data;
  try {
    data = await api('/api/snapshots');
  } catch (_e) { return; }
  const list = data.snapshots || [];
  tbody.innerHTML = list.length ? list.map(s => `
    <tr>
      <td>${esc(s.name)}</td>
      <td>${esc(s.label || '')}</td>
      <td class="num">${_fmtBytes(s.size_bytes || 0)}</td>
      <td>
        <button class="btn small" data-snapshot-restore="${esc(s.name)}">Restore</button>
        <button class="btn small" data-snapshot-delete="${esc(s.name)}">Delete</button>
      </td>
    </tr>`).join('') : '<tr><td colspan="4" class="cardnote">No snapshots taken yet.</td></tr>';
  tbody.querySelectorAll('[data-snapshot-restore]').forEach(btn => btn.onclick = async () => {
    const name = btn.dataset.snapshotRestore;
    if (!confirm(`Restore snapshot "${name}"? This overwrites the current config and database with that snapshot's contents.`)) return;
    await api(`/api/snapshots/${encodeURIComponent(name)}/restore`, { method: 'POST' });
    refreshConfig();
    refreshOverview();
  });
  tbody.querySelectorAll('[data-snapshot-delete]').forEach(btn => btn.onclick = async () => {
    await api(`/api/snapshots/${encodeURIComponent(btn.dataset.snapshotDelete)}`, { method: 'DELETE' });
    refreshSnapshots();
  });
}

document.getElementById('btn-snapshot-create').onclick = async () => {
  const status = document.getElementById('snapshot-new-status');
  const label = document.getElementById('snapshot-new-label').value.trim();
  status.textContent = 'Taking snapshot…';
  try {
    await api('/api/snapshots', { method: 'POST', body: JSON.stringify({ label: label || null }) });
  } catch (e) {
    status.textContent = e.message || 'Failed to take snapshot.';
    return;
  }
  document.getElementById('snapshot-new-label').value = '';
  status.textContent = '';
  refreshSnapshots();
};

async function refreshHotReload() {
  const textEl = document.getElementById('hotreload-status-text');
  const tbody = document.getElementById('hotreload-events-tbody');
  if (!getToken() || !textEl) return;
  let data;
  try {
    data = await api('/api/hotreload/status');
  } catch (_e) { return; }
  if (data.degraded) {
    textEl.textContent = 'Degraded — restart required';
    document.getElementById('hotreload-status-detail').textContent = data.degraded;
  } else {
    textEl.textContent = data.enabled ? 'Watching for changes' : 'Disabled (hot_reload_enabled: false)';
    document.getElementById('hotreload-status-detail').textContent = '';
  }
  const events = data.recent_events || [];
  tbody.innerHTML = events.length ? events.map(e => `
    <tr>
      <td>${fmtTime(e.ts)}</td>
      <td>${esc(e.status)}</td>
      <td>${esc(e.detail)}</td>
    </tr>`).join('') : '<tr><td colspan="3" class="cardnote">No hot-reload events yet.</td></tr>';
}

document.getElementById('btn-hotreload-run').onclick = async () => {
  const btn = document.getElementById('btn-hotreload-run');
  btn.disabled = true;
  try {
    await api('/api/hotreload/run', { method: 'POST' });
  } catch (e) {
    alert(e.message || 'Reload failed.');
  }
  btn.disabled = false;
  refreshHotReload();
};

async function refreshEnv() {
  const e = await api('/api/env');
  document.getElementById('env-resolved').textContent = e.resolved_path + (e.resolved_exists ? '' : '  (missing)');
  document.getElementById('env-candidates').innerHTML = e.candidates.map(c => `
    <div class="settingrow">
      <div><div class="st">${esc(c.path)}</div><div class="sd">${c.exists ? 'found' : 'not found'}</div></div>
      <span class="chip ${c.exists ? 'good' : 'neutral'}">${c.path === e.resolved_path ? 'active' : (c.exists ? 'available' : 'missing')}</span>
    </div>`).join('');
  document.getElementById('env-custom-path').value = e.override || '';
}

document.getElementById('btn-env-set').onclick = async () => {
  const path = document.getElementById('env-custom-path').value.trim();
  if (!path) return;
  await api('/api/config/set', { method: 'POST', body: JSON.stringify({ path: ['env_file'], value: path }) });
  refreshEnv();
  refreshEnvEditor(true);
  refreshEnvBackups();
};
document.getElementById('btn-env-auto').onclick = async () => {
  await api('/api/config/set', { method: 'POST', body: JSON.stringify({ path: ['env_file'], value: null }) });
  refreshEnv();
  refreshEnvEditor(true);
  refreshEnvBackups();
};

let envEditorLoaded = false;
async function refreshEnvEditor(force) {
  const editor = document.getElementById('env-editor');
  if (!getToken()) {
    editor.value = '';
    editor.placeholder = 'Set the dashboard token above, then click "Reload from disk".';
    return;
  }
  if (envEditorLoaded && !force) return;
  const data = await api('/api/env/content');
  editor.value = data.content;
  envEditorLoaded = true;
}

async function refreshEnvBackups() {
  const tbody = document.getElementById('env-backups-tbody');
  if (!getToken()) {
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="4">Unlock with the dashboard token to view.</td></tr>';
    return;
  }
  const backups = await api('/api/env/backups');
  tbody.innerHTML = backups.length ? backups.map(b => `
    <tr>
      <td class="mono">${esc(b.name)}</td>
      <td class="mono">${fmtTime(b.mtime)}</td>
      <td class="num mono">${(b.size / 1024).toFixed(1)} KB</td>
      <td><button class="btn" data-restore-env="${esc(b.name)}" style="padding:3px 8px; font-size:11px;">Restore</button></td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="4">No backups yet — they appear after your first save.</td></tr>';

  document.querySelectorAll('[data-restore-env]').forEach(btn => btn.onclick = async () => {
    const name = btn.dataset.restoreEnv;
    if (!confirm(`Restore ${name}? The current .env is backed up first, then overwritten with this version. Restart the server afterward for it to take effect.`)) return;
    await api(`/api/env/backups/${encodeURIComponent(name)}/restore`, { method: 'POST' });
    await refreshEnvEditor(true);
    await refreshEnvBackups();
    document.getElementById('env-save-status').textContent = `Restored ${name}.`;
  });
}

document.getElementById('btn-env-save').onclick = async () => {
  const statusEl = document.getElementById('env-save-status');
  statusEl.textContent = 'Saving…';
  try {
    const res = await api('/api/env/content', {
      method: 'POST',
      body: JSON.stringify({ content: document.getElementById('env-editor').value }),
    });
    statusEl.textContent = res.backup
      ? `Saved (backup: ${res.backup}). Restart the server to apply.`
      : 'Saved. Restart the server to apply.';
    refreshEnvBackups();
  } catch (e) {
    statusEl.textContent = 'Save failed — check the dashboard token and try again.';
  }
};
document.getElementById('btn-env-editor-reload').onclick = () => refreshEnvEditor(true);

document.getElementById('btn-mcp-self-register').onclick = async () => {
  const statusEl = document.getElementById('mcp-self-register-status');
  statusEl.className = 'msg';
  statusEl.textContent = 'Registering…';
  try {
    const res = await api('/api/mcp/self-register', { method: 'POST' });
    statusEl.className = 'msg good';
    statusEl.textContent = `Registered as "${res.name}" (${res.command}). Restart Claude Desktop to pick it up.`;
    refreshMcp();
  } catch (e) {
    statusEl.className = 'msg bad';
    statusEl.textContent = 'Failed — check the dashboard token and try again.';
  }
};

async function refreshMcp() {
  const servers = await api('/api/mcp');
  document.getElementById('mcp-list').innerHTML = servers.length ? servers.map(s => `
    <div class="settingrow">
      <div><div class="st">${esc(s.name)}</div><div class="sd">${esc(s.command || '')}</div></div>
      <div class="toggle ${s.enabled ? 'on' : ''}" data-mcp="${esc(s.name)}" data-enabled="${s.enabled}"></div>
    </div>`).join('') : '<p class="cardnote">No MCP servers configured in claude_desktop_config.json.</p>';

  document.querySelectorAll('#mcp-list .toggle').forEach(t => t.onclick = async () => {
    const name = t.dataset.mcp;
    const enabled = t.dataset.enabled === 'true';
    await api(`/api/mcp/${encodeURIComponent(name)}/${enabled ? 'disable' : 'enable'}`, { method: 'POST' });
    refreshMcp();
  });
}

async function refreshAllowedUsers() {
  const users = await api('/api/security/allowed-users');
  document.getElementById('allowed-users-tbody').innerHTML = users.length ? users.map(u => `
    <tr><td class="mono">${esc(u.telegram_id)}</td><td>${esc(u.name || '')}</td>
    <td><button class="btn danger" data-remove="${esc(u.telegram_id)}" style="padding:3px 8px; font-size:11px;">Remove</button></td></tr>`).join('')
    : '<tr class="emptyrow"><td colspan="3">Only .env-configured owner is allowed.</td></tr>';

  document.querySelectorAll('[data-remove]').forEach(b => b.onclick = async () => {
    await api(`/api/security/allowed-users/${b.dataset.remove}`, { method: 'DELETE' });
    refreshAllowedUsers();
  });
}

document.getElementById('btn-add-user').onclick = async () => {
  const id = prompt('Telegram numeric user ID to allow:');
  if (!id) return;
  const name = prompt('Label (optional):', '') || '';
  await api(`/api/security/allowed-users/${encodeURIComponent(id)}?name=${encodeURIComponent(name)}`, { method: 'POST' });
  refreshAllowedUsers();
};

document.getElementById('btn-desktop-start').onclick = async () => { await api('/api/desktop/start', { method: 'POST' }); refreshOverview(); };
document.getElementById('btn-desktop-stop').onclick = async () => { if (confirm('Stop Claude Desktop?')) { await api('/api/desktop/stop', { method: 'POST' }); refreshOverview(); } };
document.getElementById('btn-desktop-restart').onclick = async () => { if (confirm('Restart Claude Desktop?')) { await api('/api/desktop/restart', { method: 'POST' }); refreshOverview(); } };
async function reloadConfig() { await api('/api/config/reload', { method: 'POST' }); refreshConfig(); refreshOverview(); }
document.getElementById('btn-reload-config').onclick = reloadConfig;
document.getElementById('btn-reload-config-2').onclick = reloadConfig;

// -------------------------------------------------------------------- logs
async function refreshLogs() {
  const level = state.logLevel === 'all' ? '' : `&level=${state.logLevel}`;
  const data = await api(`/api/logs?lines=120${level}`);
  const el = document.getElementById('log-lines');
  el.innerHTML = data.lines.map(formatLogLine).join('\n') || 'No log output yet.';
  el.scrollTop = el.scrollHeight;
}
function formatLogLine(line) {
  const m = line.match(/^(\S+ \S+) (\w+)\s+(.*)$/);
  if (!m) return esc(line);
  const [, ts, lvl, rest] = m;
  const cls = { INFO: 'lvl-info', WARNING: 'lvl-warn', WARN: 'lvl-warn', ERROR: 'lvl-error', DEBUG: 'lvl-debug' }[lvl] || 'lvl-info';
  return `<span class="ts">${esc(ts)}</span> <span class="${cls}">${esc(lvl)}</span> ${esc(rest)}`;
}
document.querySelectorAll('#log-filter button').forEach(btn => btn.onclick = () => {
  document.querySelectorAll('#log-filter button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  state.logLevel = btn.dataset.level;
  refreshLogs();
});

// ------------------------------------------------------------------- boot
// Skips a poll tick while the window isn't visible (backgrounded or
// minimized) instead of hitting the server every few seconds for no one
// to see — the interval itself keeps running (so it fires immediately on
// return, no separate "resume" wiring needed), it just no-ops while hidden.
function pollWhenVisible(fn, ms) {
  return setInterval(() => {
    if (document.hidden) return;
    fn();
  }, ms);
}

async function refreshAll() {
  const tasks = [refreshOverview(), refreshJobsTable(), refreshJobsCharts(), refreshTelemetry(), refreshDatabase(), refreshConfig(), refreshMcp(), refreshAllowedUsers(), refreshLogs(), refreshEnv()];
  await Promise.allSettled(tasks);
}

function startDashboardPolling() {
  refreshAll();
  pollWhenVisible(refreshAll, 5000);
  refreshEnvEditor();
  refreshEnvBackups();
  startChatPolling();
  startSessionsPolling();
  refreshPlatforms();
  pollWhenVisible(refreshPlatforms, 15000);
  refreshPersonas();
  refreshPlatformGuides();
  refreshBots();
  refreshBotsBackups();
  pollWhenVisible(refreshBots, 15000);
  refreshPairing();
  pollWhenVisible(refreshPairing, 15000);
  refreshProviders();
  pollWhenVisible(refreshProviders, 15000);
  refreshProviderCatalog();
  refreshModelsPage();
  pollWhenVisible(refreshModelsPage, 30000);
  refreshPlugins();
  pollWhenVisible(refreshPlugins, 15000);
  refreshSnapshots();
  pollWhenVisible(refreshSnapshots, 15000);
  refreshHotReload();
  pollWhenVisible(refreshHotReload, 15000);
  refreshKanban();
  pollWhenVisible(refreshKanban, 15000);
  refreshSchedules();
  pollWhenVisible(refreshSchedules, 15000);
  refreshSwarmInstanceLegend();
  refreshSwarms();
  refreshSwarmRuns();
  pollWhenVisible(refreshSwarms, 15000);
  refreshSwarmToolsPanel();
  pollWhenVisible(refreshSwarmToolsPanel, 15000);
  refreshContextDocs();
  pollWhenVisible(refreshContextDocs, 15000);
  refreshDelegationActivity();
  pollWhenVisible(refreshDelegationActivity, 15000);
  refreshSwarmBudget();
  pollWhenVisible(refreshSwarmBudget, 15000);
  refreshMobileKeys();
  pollWhenVisible(refreshMobileKeys, 15000);
  refreshPeers();
  pollWhenVisible(refreshPeers, 15000);
  connectDevicesSocket();
  refreshTrainingPhrases();
  refreshTrainingHealth();
  pollWhenVisible(refreshTrainingPhrases, 20000);
  pollWhenVisible(refreshTrainingHealth, 10000);
  startServerChatPolling();
}

// ---------------------------------------------------------------- bots ---
let botEditingId = null;

let platformGuidesCache = {};
async function refreshPlatformGuides() {
  try {
    platformGuidesCache = await api('/api/platform-guides');
  } catch (_e) { return; }
  renderPlatformGuide(document.getElementById('bot-new-platform').value);
}

// The one field shared by every platform (labeled "Bot token" or "Access
// token" depending on which) maps to a different credential key per
// platform — matches the mapping the create/edit handlers below build.
function _tokenFieldKey(platform) {
  return (platform === 'matrix' || platform === 'whatsapp') ? 'access_token' : 'bot_token';
}

function renderPlatformGuide(platform) {
  const guide = platformGuidesCache[platform];
  if (!guide) return;
  const tokenField = guide.fields[_tokenFieldKey(platform)];
  document.getElementById('bot-new-token-help').textContent = tokenField ? tokenField.help : '';
  const apptokenField = guide.fields.app_token;
  document.getElementById('bot-new-apptoken-help').textContent = apptokenField ? apptokenField.help : '';
  const guideField = document.getElementById('bot-new-setupguide-field');
  const guideList = document.getElementById('bot-new-setupguide-list');
  if (guide.setup_guide && guide.setup_guide.length) {
    guideField.style.display = '';
    guideList.innerHTML = guide.setup_guide.map(step => `<li>${esc(step)}</li>`).join('');
  } else {
    guideField.style.display = 'none';
  }
}

// Debounced live validation for one credential input — reuses the same
// .ok/.bad/.msg CSS the setup wizard's own fields already use.
function wireCredentialValidation(inputEl, platformGetter, fieldGetter) {
  let timer = null;
  inputEl.addEventListener('input', () => {
    clearTimeout(timer);
    const value = inputEl.value.trim();
    inputEl.classList.remove('ok', 'bad');
    let msgEl = inputEl.closest('.row').nextElementSibling;
    if (!msgEl || !msgEl.classList.contains('msg')) {
      msgEl = document.createElement('div');
      msgEl.className = 'msg';
      inputEl.closest('.row').insertAdjacentElement('afterend', msgEl);
    }
    if (!value) { msgEl.textContent = ''; msgEl.className = 'msg'; return; }
    timer = setTimeout(async () => {
      let res;
      try {
        res = await api('/api/validate-field', { method: 'POST', body: JSON.stringify({ platform: platformGetter(), field: fieldGetter(), value }) });
      } catch (_e) { return; }
      inputEl.classList.add(res.ok ? 'ok' : 'bad');
      msgEl.textContent = res.message;
      msgEl.className = 'msg ' + (res.ok ? 'good' : 'bad');
    }, 400);
  });
}

document.getElementById('bot-new-platform').onchange = (e) => {
  const p = e.target.value;
  document.getElementById('bot-new-apptoken-field').style.display = p === 'slack' ? '' : 'none';
  document.getElementById('bot-new-matrix-fields').style.display = p === 'matrix' ? '' : 'none';
  document.getElementById('bot-new-whatsapp-fields').style.display = p === 'whatsapp' ? '' : 'none';
  document.getElementById('bot-new-token-label').textContent = (p === 'matrix' || p === 'whatsapp') ? 'Access token' : 'Bot token';
  document.getElementById('bot-new-token').classList.remove('ok', 'bad');
  renderPlatformGuide(p);
};
wireCredentialValidation(document.getElementById('bot-new-token'), () => document.getElementById('bot-new-platform').value, () => _tokenFieldKey(document.getElementById('bot-new-platform').value));
wireCredentialValidation(document.getElementById('bot-new-apptoken'), () => 'slack', () => 'app_token');
wireCredentialValidation(document.getElementById('bot-new-matrix-homeserver'), () => 'matrix', () => 'homeserver');
wireCredentialValidation(document.getElementById('bot-new-matrix-userid'), () => 'matrix', () => 'user_id');
wireCredentialValidation(document.getElementById('bot-new-whatsapp-phoneid'), () => 'whatsapp', () => 'phone_number_id');
wireCredentialValidation(document.getElementById('bot-new-whatsapp-appsecret'), () => 'whatsapp', () => 'app_secret');
wireCredentialValidation(document.getElementById('bot-new-whatsapp-verifytoken'), () => 'whatsapp', () => 'verify_token');
document.getElementById('bot-new-backend').onchange = refreshBotModelOptions;

let botsCache = [];
let personasCache = [];
let selectedPersona = 'assistant';
let lastAgentMode = 'trust_all';

async function refreshPersonas() {
  try {
    personasCache = await api('/api/personas');
  } catch (_e) { return; }
  renderPersonaPicker(selectedPersona);
}

function renderPersonaPicker(active) {
  selectedPersona = active;
  const list = document.getElementById('bot-new-persona-list');
  list.className = 'persona-pick';
  list.innerHTML = personasCache.map(p => `
    <div class="persona-opt${p.id === active ? ' active' : ''}" data-persona-id="${p.id}">
      <span class="ic">${p.icon}</span>${esc(p.label)}
    </div>`).join('');
  const picked = personasCache.find(p => p.id === active);
  document.getElementById('bot-new-persona-desc').textContent = picked ? picked.description : '';
  list.querySelectorAll('[data-persona-id]').forEach(el => el.onclick = () => renderPersonaPicker(el.dataset.personaId));

  const presetRow = document.getElementById('bot-new-instructions-preset-row');
  const presetBtn = document.getElementById('btn-instructions-preset');
  if (picked && picked.instructions) {
    presetRow.style.display = '';
    presetBtn.textContent = `Insert ${picked.label} preset`;
    presetBtn.onclick = () => { document.getElementById('bot-new-instructions').value = picked.instructions; };
  } else {
    presetRow.style.display = 'none';
  }
}

function personaMeta(id) {
  return personasCache.find(p => p.id === id) || { id, label: id || 'Assistant', icon: '💬' };
}

function renderCanTargetCheckboxes(excludeId, checkedIds) {
  const list = document.getElementById('bot-new-cantarget-list');
  const others = botsCache.filter(b => b.id !== excludeId);
  if (!others.length) {
    list.innerHTML = '<span class="cardnote">No other bot instances yet.</span>';
    return;
  }
  list.innerHTML = others.map(b => `
    <label style="display:inline-flex; align-items:center; gap:5px; margin-right:14px; font-size:12.5px;">
      <input type="checkbox" data-cantarget-id="${b.id}" ${(checkedIds || []).includes(b.id) ? 'checked' : ''}> ${esc(b.name)}
    </label>`).join('');
}

function refreshBotsCanTargetVisibility(mode) {
  lastAgentMode = mode;
  document.getElementById('bot-new-cantarget-help').textContent = mode === 'allowlist'
    ? 'Bots this one manages as assistants. Control Center is in "Allowlist" agent-control mode, so this also restricts which instances it may command.'
    : 'Bots this one manages as assistants, shown as an org chart on its card. (Control Center\'s agent-control mode isn\'t "Allowlist", so this list doesn\'t additionally restrict commanding.)';
}

function _resetBotForm() {
  botEditingId = null;
  document.getElementById('bot-new-name').value = '';
  document.getElementById('bot-new-platform').value = 'telegram';
  document.getElementById('bot-new-backend').value = 'cli';
  document.getElementById('bot-new-model').value = '';
  document.getElementById('bot-new-token').value = '';
  document.getElementById('bot-new-apptoken').value = '';
  document.getElementById('bot-new-matrix-homeserver').value = '';
  document.getElementById('bot-new-matrix-userid').value = '';
  document.getElementById('bot-new-matrix-device').value = '';
  document.getElementById('bot-new-whatsapp-phoneid').value = '';
  document.getElementById('bot-new-whatsapp-appsecret').value = '';
  document.getElementById('bot-new-whatsapp-verifytoken').value = '';
  document.getElementById('bot-new-allowed').value = '';
  document.getElementById('bot-new-admins').value = '';
  document.getElementById('bot-new-instructions').value = '';
  document.getElementById('bot-new-enabled').checked = true;
  document.getElementById('bot-new-overrides').value = '{}';
  document.getElementById('bot-new-overrides-msg').textContent = '';
  document.getElementById('bot-new-advanced').open = false;
  document.getElementById('bot-new-apptoken-field').style.display = 'none';
  document.getElementById('bot-new-matrix-fields').style.display = 'none';
  document.getElementById('bot-new-whatsapp-fields').style.display = 'none';
  document.getElementById('bot-new-token-label').textContent = 'Bot token';
  document.getElementById('btn-bot-create').textContent = 'Add bot';
  renderPersonaPicker('assistant');
  refreshBotModelOptions();
  renderPlatformGuide(document.getElementById('bot-new-platform').value);
  renderCanTargetCheckboxes(null, []);
}

function _loadBotIntoForm(bot) {
  botEditingId = bot.id;
  document.getElementById('bot-new-name').value = bot.name;
  document.getElementById('bot-new-platform').value = bot.platform;
  document.getElementById('bot-new-backend').value = bot.backend;
  document.getElementById('bot-new-model').value = bot.model || '';
  document.getElementById('bot-new-token').value = (bot.platform === 'matrix' || bot.platform === 'whatsapp')
    ? (bot.credentials.access_token || '') : (bot.credentials.bot_token || '');
  document.getElementById('bot-new-apptoken').value = bot.credentials.app_token || '';
  document.getElementById('bot-new-apptoken-field').style.display = bot.platform === 'slack' ? '' : 'none';
  document.getElementById('bot-new-matrix-fields').style.display = bot.platform === 'matrix' ? '' : 'none';
  document.getElementById('bot-new-whatsapp-fields').style.display = bot.platform === 'whatsapp' ? '' : 'none';
  document.getElementById('bot-new-token-label').textContent =
    (bot.platform === 'matrix' || bot.platform === 'whatsapp') ? 'Access token' : 'Bot token';
  document.getElementById('bot-new-matrix-homeserver').value = bot.credentials.homeserver || '';
  document.getElementById('bot-new-matrix-userid').value = bot.credentials.user_id || '';
  document.getElementById('bot-new-matrix-device').value = bot.credentials.device_id || '';
  document.getElementById('bot-new-whatsapp-phoneid').value = bot.credentials.phone_number_id || '';
  document.getElementById('bot-new-whatsapp-appsecret').value = bot.credentials.app_secret || '';
  document.getElementById('bot-new-whatsapp-verifytoken').value = bot.credentials.verify_token || '';
  document.getElementById('bot-new-allowed').value = (bot.allowed_user_ids || []).join(', ');
  document.getElementById('bot-new-admins').value = (bot.admin_user_ids || []).join(', ');
  document.getElementById('bot-new-instructions').value = bot.custom_instructions || '';
  document.getElementById('bot-new-enabled').checked = !!bot.enabled;
  const overrides = bot.action_overrides && Object.keys(bot.action_overrides).length ? bot.action_overrides : {};
  document.getElementById('bot-new-overrides').value = JSON.stringify(overrides, null, 2);
  document.getElementById('bot-new-overrides-msg').textContent = '';
  document.getElementById('bot-new-advanced').open = Object.keys(overrides).length > 0;
  renderPersonaPicker(bot.persona || 'assistant');
  refreshBotModelOptions();
  renderPlatformGuide(bot.platform);
  renderCanTargetCheckboxes(bot.id, bot.can_target || []);
  document.getElementById('btn-bot-create').textContent = 'Save changes';
  document.getElementById('bots').scrollIntoView({ behavior: 'smooth' });
}

let kanbanBotPopulated = false;

function _kanbanSelectedBot() {
  const sel = document.getElementById('kanban-bot-select');
  return sel.value ? Number(sel.value) : null;
}

async function refreshKanban() {
  const sel = document.getElementById('kanban-bot-select');
  const cols = document.getElementById('kanban-columns');
  if (!getToken() || !sel || !cols) return;
  if ((botsCache || []).length && (!kanbanBotPopulated || sel.options.length !== botsCache.length)) {
    const prev = sel.value;
    sel.innerHTML = botsCache.map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join('');
    if (prev && botsCache.some(b => String(b.id) === prev)) sel.value = prev;
    kanbanBotPopulated = true;
  }
  const instanceId = _kanbanSelectedBot();
  if (!instanceId) {
    cols.innerHTML = '<p class="cardnote">No bots configured yet.</p>';
    return;
  }
  const board = document.getElementById('kanban-board-name').value.trim() || 'default';
  let data;
  try {
    data = await api(`/api/kanban/cards?instance_id=${instanceId}&board=${encodeURIComponent(board)}`);
  } catch (_e) { return; }
  const cards = data.cards || [];
  const columns = ['todo', 'doing', 'done'];
  for (const c of cards) if (!columns.includes(c.column_name)) columns.push(c.column_name);
  cols.style.gridTemplateColumns = `repeat(${columns.length}, 1fr)`;
  cols.innerHTML = columns.map(col => {
    const colCards = cards.filter(c => c.column_name === col);
    return `<div>
      <h4 style="margin:0 0 8px; font-size:12.5px; text-transform:uppercase; color:var(--ink-soft);">${esc(col)} (${colCards.length})</h4>
      ${colCards.map(c => `
        <div class="card" style="padding:8px 10px; margin-bottom:8px;">
          <div style="font-size:13px;">${esc(c.text)}</div>
          <div class="row" style="gap:4px; margin-top:6px;">
            ${columns.filter(x => x !== col).map(x => `<button class="btn small" data-kanban-move="${c.id}" data-col="${esc(x)}">→ ${esc(x)}</button>`).join('')}
            <button class="btn small" data-kanban-delete="${c.id}">Delete</button>
          </div>
        </div>`).join('') || '<p class="cardnote">Empty</p>'}
    </div>`;
  }).join('');

  cols.querySelectorAll('[data-kanban-move]').forEach(btn => btn.onclick = async () => {
    await api(`/api/kanban/cards/${btn.dataset.kanbanMove}/move`, {
      method: 'POST', body: JSON.stringify({ instance_id: instanceId, column: btn.dataset.col }),
    });
    refreshKanban();
  });
  cols.querySelectorAll('[data-kanban-delete]').forEach(btn => btn.onclick = async () => {
    await api(`/api/kanban/cards/${btn.dataset.kanbanDelete}?instance_id=${instanceId}`, { method: 'DELETE' });
    refreshKanban();
  });
}

document.getElementById('btn-kanban-refresh').onclick = refreshKanban;
document.getElementById('kanban-bot-select').onchange = refreshKanban;
document.getElementById('kanban-board-name').onchange = refreshKanban;
document.getElementById('btn-kanban-add').onclick = async () => {
  const instanceId = _kanbanSelectedBot();
  const text = document.getElementById('kanban-new-text').value.trim();
  if (!instanceId || !text) return;
  const board = document.getElementById('kanban-board-name').value.trim() || 'default';
  const column = document.getElementById('kanban-new-column').value;
  await api('/api/kanban/cards', {
    method: 'POST', body: JSON.stringify({ instance_id: instanceId, board, column, text }),
  });
  document.getElementById('kanban-new-text').value = '';
  refreshKanban();
};

let schedulesBotPopulated = false;

function _schedulesSelectedBot() {
  const sel = document.getElementById('schedules-bot-select');
  return sel.value ? Number(sel.value) : null;
}

async function refreshSchedules() {
  const sel = document.getElementById('schedules-bot-select');
  const tbody = document.getElementById('schedules-tbody');
  if (!getToken() || !sel || !tbody) return;
  if ((botsCache || []).length && (!schedulesBotPopulated || sel.options.length !== botsCache.length)) {
    const prev = sel.value;
    sel.innerHTML = botsCache.map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join('');
    if (prev && botsCache.some(b => String(b.id) === prev)) sel.value = prev;
    schedulesBotPopulated = true;
  }
  const instanceId = _schedulesSelectedBot();
  if (!instanceId) {
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="6">No bots configured yet.</td></tr>';
    return;
  }
  let rows;
  try {
    rows = await api(`/api/bots/${instanceId}/schedules`);
  } catch (_e) { return; }
  tbody.innerHTML = rows.length ? rows.map(r => `
    <tr>
      <td class="mono">${esc(r.chat_id)}</td>
      <td>${esc(r.kind)}</td>
      <td>${esc(r.prompt)}</td>
      <td class="mono">${r.interval_s}s</td>
      <td class="mono">${esc(r.next_run_at || '')}</td>
      <td>
        <button class="btn" data-schedule-toggle="${r.id}" style="padding:3px 8px; font-size:11px;">${r.enabled ? 'Pause' : 'Resume'}</button>
        <button class="btn" data-schedule-delete="${r.id}" style="padding:3px 8px; font-size:11px;">Delete</button>
      </td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="6">No schedules for this bot yet.</td></tr>';
  tbody.querySelectorAll('[data-schedule-toggle]').forEach(btn => btn.onclick = async () => {
    const id = btn.dataset.scheduleToggle;
    const row = rows.find(r => String(r.id) === id);
    await api(`/api/bots/${instanceId}/schedules/${id}/${row.enabled ? 'pause' : 'resume'}`, { method: 'POST' });
    refreshSchedules();
  });
  tbody.querySelectorAll('[data-schedule-delete]').forEach(btn => btn.onclick = async () => {
    if (!confirm('Delete this schedule?')) return;
    await api(`/api/bots/${instanceId}/schedules/${btn.dataset.scheduleDelete}`, { method: 'DELETE' });
    refreshSchedules();
  });
}

document.getElementById('schedules-bot-select').onchange = refreshSchedules;
document.getElementById('btn-schedule-create').onclick = async () => {
  const instanceId = _schedulesSelectedBot();
  const statusEl = document.getElementById('schedule-new-status');
  if (!instanceId) { statusEl.textContent = 'Select a bot first.'; return; }
  const chat_id = document.getElementById('schedule-new-chatid').value.trim();
  const kind = document.getElementById('schedule-new-kind').value;
  const interval = document.getElementById('schedule-new-interval').value.trim();
  const prompt = document.getElementById('schedule-new-prompt').value.trim();
  if (!chat_id || !interval || !prompt) { statusEl.textContent = 'Chat ID, interval, and prompt are all required.'; return; }
  statusEl.textContent = 'Adding…';
  try {
    await api(`/api/bots/${instanceId}/schedules`, { method: 'POST', body: JSON.stringify({ chat_id, kind, interval, prompt }) });
    statusEl.textContent = 'Added.';
    document.getElementById('schedule-new-chatid').value = '';
    document.getElementById('schedule-new-interval').value = '';
    document.getElementById('schedule-new-prompt').value = '';
    refreshSchedules();
  } catch (e) {
    statusEl.textContent = `Failed: ${e.message}`;
  }
};

async function refreshPairing() {
  const tbody = document.getElementById('pairing-tbody');
  if (!getToken() || !tbody) return;
  let data;
  try {
    data = await api('/api/pairing');
  } catch (_e) { return; }
  const pending = data.pending || [];
  const byId = Object.fromEntries((botsCache || []).map(b => [b.id, b.name]));
  tbody.innerHTML = pending.length ? pending.map(p => `
    <tr>
      <td>${esc(byId[p.instance_id] || ('#' + p.instance_id))}</td>
      <td>${esc(p.user_name || p.user_id)} (${esc(p.user_id)})</td>
      <td><code>${esc(p.code)}</code></td>
      <td>${esc(p.created_at)}</td>
      <td>${esc(p.expires_at)}</td>
      <td>
        <button class="btn small" data-pairing-approve="${p.id}">Approve</button>
        <button class="btn small" data-pairing-deny="${p.id}">Deny</button>
      </td>
    </tr>`).join('') : '<tr><td colspan="6" class="cardnote">No pending pairing requests.</td></tr>';
  tbody.querySelectorAll('[data-pairing-approve]').forEach(btn => btn.onclick = async () => {
    await api(`/api/pairing/${btn.dataset.pairingApprove}/approve`, { method: 'POST' });
    refreshPairing();
    refreshBots();
  });
  tbody.querySelectorAll('[data-pairing-deny]').forEach(btn => btn.onclick = async () => {
    await api(`/api/pairing/${btn.dataset.pairingDeny}/deny`, { method: 'POST' });
    refreshPairing();
  });
}

async function refreshProviders() {
  const tbody = document.getElementById('providers-tbody');
  if (!getToken() || !tbody) return;
  let data;
  try {
    data = await api('/api/providers');
  } catch (_e) { return; }
  const list = data.providers || [];
  tbody.innerHTML = list.length ? list.map(p => `
    <tr>
      <td>${esc(p.name)}</td>
      <td><code>${esc(p.base_url)}</code></td>
      <td>${p.api_key_env ? 'env: ' + esc(p.api_key_env) : (p.has_inline_key ? 'set' : '(none)')}</td>
      <td><button class="btn small" data-provider-delete="${esc(p.name)}">Remove</button></td>
    </tr>`).join('') : '<tr><td colspan="4" class="cardnote">No custom providers configured yet.</td></tr>';
  tbody.querySelectorAll('[data-provider-delete]').forEach(btn => btn.onclick = async () => {
    await api(`/api/providers/${encodeURIComponent(btn.dataset.providerDelete)}`, { method: 'DELETE' });
    refreshProviders();
    refreshModels();
    refreshModelsPage();
  });
}

// Known-provider picker backing provider-catalog-input's <datalist> —
// fetched once at startup (models.dev's catalog barely changes minute
// to minute) rather than on every refreshProviders() poll.
let providerCatalog = [];

async function refreshProviderCatalog() {
  if (!getToken()) return;
  let data;
  try {
    data = await api('/api/providers/catalog');
  } catch (_e) { return; }
  providerCatalog = data.providers || [];
  document.getElementById('provider-catalog-options').innerHTML =
    providerCatalog.map(p => `<option value="${esc(p.name)}"></option>`).join('');
}

document.getElementById('provider-catalog-input').addEventListener('input', (e) => {
  const match = providerCatalog.find(p => p.name === e.target.value);
  if (!match) return;
  document.getElementById('provider-new-name').value = match.id;
  document.getElementById('provider-new-url').value = match.api;
  const envHint = (match.env && match.env[0]) || '';
  document.getElementById('provider-new-key').placeholder = envHint
    ? `API key for ${match.name} (conventionally ${envHint})`
    : 'API key (optional)';
});

// ------------------------------------------------------------- Models page
// A separate cache/refresher from refreshModels() (Control Center's
// per-backend-family default-model pickers) — this one is the dedicated
// Models page: every model each configured custom_model/native_agent
// provider offers, with an individual enable/disable toggle, free-first
// per provider (pre-sorted by the backend).

async function refreshModelsPage() {
  const list = document.getElementById('models-page-list');
  const empty = document.getElementById('models-page-empty');
  if (!getToken() || !list) return;
  let providersData;
  try {
    providersData = await api('/api/providers');
  } catch (_e) { return; }
  const providersList = providersData.providers || [];
  empty.hidden = providersList.length > 0;
  if (!providersList.length) {
    list.innerHTML = '';
    return;
  }

  const openNames = new Set(
    [...list.querySelectorAll('details[data-provider]')].filter(d => d.open).map(d => d.dataset.provider)
  );

  const blocks = await Promise.all(providersList.map(async (p) => {
    let models = [];
    try {
      const data = await api(`/api/providers/${encodeURIComponent(p.name)}/models`);
      models = data.models || [];
    } catch (_e) { /* provider unreachable — show what we can */ }
    const rows = models.length ? models.map(m => `
      <tr>
        <td class="mono" style="font-size:12px;">${esc(m.id)}</td>
        <td>${m.free === true ? '<span class="pill"><span class="dot good"></span>free</span>' : (m.free === false ? '<span class="pill">paid</span>' : '<span class="pill">—</span>')}</td>
        <td>${m.input != null ? `$${(m.input * 1e6).toFixed(2)}/${(m.output * 1e6).toFixed(2)} per 1M in/out` : '—'}</td>
        <td><input type="checkbox" data-model-toggle="${esc(p.name)}" data-model-id="${esc(m.id)}" ${m.enabled ? 'checked' : ''}></td>
      </tr>`).join('') : '<tr><td colspan="4" class="cardnote">No models found yet — add a real API key, or this provider isn\'t in the known catalog and hasn\'t responded live.</td></tr>';
    const paidTotal = models.filter(m => m.free !== true).length;
    const paidHidden = models.filter(m => m.free !== true && !m.enabled).length;
    const hiddenNote = paidTotal
      ? `<span class="cardnote">${paidHidden} of ${paidTotal} paid/unpriced model${paidTotal === 1 ? '' : 's'} hidden by default</span>`
      : '';
    return `
      <details data-provider="${esc(p.name)}" ${openNames.has(p.name) ? 'open' : ''} style="margin-bottom:10px;">
        <summary style="cursor:pointer; font-weight:600; padding:6px 0;">${esc(p.name)} <span class="cardnote">(${models.length} model${models.length === 1 ? '' : 's'})</span></summary>
        <div style="margin:6px 0; display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
          <button type="button" class="secondary" data-toggle-paid="${esc(p.name)}" data-enable="0">Turn Off All Paid</button>
          <button type="button" class="secondary" data-toggle-paid="${esc(p.name)}" data-enable="1">Turn On All Paid Models</button>
          ${hiddenNote}
        </div>
        <div class="tablewrap" style="margin-top:6px;">
          <table>
            <thead><tr><th>Model</th><th>Free?</th><th>Price</th><th>Enabled</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </details>`;
  }));

  list.innerHTML = blocks.join('');
  list.querySelectorAll('[data-model-toggle]').forEach(cb => cb.onchange = async () => {
    try {
      await api(`/api/providers/${encodeURIComponent(cb.dataset.modelToggle)}/models/toggle`, {
        method: 'POST',
        body: JSON.stringify({ model_id: cb.dataset.modelId, enabled: cb.checked }),
      });
    } catch (e) {
      cb.checked = !cb.checked;
      alert(e.message || 'Failed to update that model\'s toggle.');
      return;
    }
    refreshModels();
  });
  list.querySelectorAll('[data-toggle-paid]').forEach(btn => btn.onclick = async () => {
    const providerName = btn.dataset.togglePaid;
    const enabled = btn.dataset.enable === '1';
    btn.disabled = true;
    try {
      await api(`/api/providers/${encodeURIComponent(providerName)}/models/toggle-paid`, {
        method: 'POST',
        body: JSON.stringify({ enabled }),
      });
    } catch (e) {
      alert(e.message || 'Failed to update paid models for that provider.');
    } finally {
      btn.disabled = false;
    }
    refreshModelsPage();
    refreshModels();
  });
}

async function refreshPlugins() {
  const tbody = document.getElementById('plugins-tbody');
  if (!getToken() || !tbody) return;
  let data;
  try {
    data = await api('/api/plugins');
  } catch (_e) { return; }
  const list = data.plugins || [];
  tbody.innerHTML = list.length ? list.map(p => `
    <tr>
      <td>${esc(p.name)}</td>
      <td>${esc(p.description)}</td>
      <td>${p.tools.length ? esc(p.tools.join(', ')) : '(none)'}</td>
      <td>${p.commands.length ? esc(p.commands.map(c => '/' + c).join(', ')) : '(none)'}</td>
      <td>${p.enabled ? 'enabled' : 'disabled'}</td>
      <td>
        <button class="btn small" data-plugin-toggle="${esc(p.name)}" data-plugin-enabled="${p.enabled ? '1' : '0'}">${p.enabled ? 'Disable' : 'Enable'}</button>
        <button class="btn small" data-plugin-delete="${esc(p.name)}">Remove</button>
      </td>
    </tr>`).join('') : '<tr><td colspan="6" class="cardnote">No plugins installed yet.</td></tr>';
  tbody.querySelectorAll('[data-plugin-toggle]').forEach(btn => btn.onclick = async () => {
    const action = btn.dataset.pluginEnabled === '1' ? 'disable' : 'enable';
    try {
      await api(`/api/plugins/${encodeURIComponent(btn.dataset.pluginToggle)}/${action}`, { method: 'POST' });
    } catch (e) {
      alert(e.message || `Failed to ${action} plugin.`);
    }
    refreshPlugins();
  });
  tbody.querySelectorAll('[data-plugin-delete]').forEach(btn => btn.onclick = async () => {
    await api(`/api/plugins/${encodeURIComponent(btn.dataset.pluginDelete)}`, { method: 'DELETE' });
    refreshPlugins();
  });
}

document.getElementById('btn-plugin-add').onclick = async () => {
  const status = document.getElementById('plugin-new-status');
  const path = document.getElementById('plugin-new-path').value.trim();
  status.textContent = '';
  try {
    await api('/api/plugins', { method: 'POST', body: JSON.stringify({ path }) });
  } catch (e) {
    status.textContent = e.message || 'Failed to install plugin.';
    return;
  }
  document.getElementById('plugin-new-path').value = '';
  refreshPlugins();
};

document.getElementById('btn-provider-add').onclick = async () => {
  const status = document.getElementById('provider-new-status');
  const name = document.getElementById('provider-new-name').value.trim();
  const base_url = document.getElementById('provider-new-url').value.trim();
  const api_key = document.getElementById('provider-new-key').value.trim();
  const catalogInput = document.getElementById('provider-catalog-input');
  const catalogMatch = providerCatalog.find(p => p.name === catalogInput.value.trim());
  status.textContent = '';
  try {
    await api('/api/providers', {
      method: 'POST',
      body: JSON.stringify({ name, base_url, api_key: api_key || null, catalog_id: catalogMatch ? catalogMatch.id : null }),
    });
  } catch (e) {
    status.textContent = e.message || 'Failed to add provider.';
    return;
  }
  document.getElementById('provider-new-name').value = '';
  document.getElementById('provider-new-url').value = '';
  document.getElementById('provider-new-key').value = '';
  document.getElementById('provider-new-key').placeholder = 'API key (optional)';
  catalogInput.value = '';
  refreshProviders();
  refreshModels();
  refreshModelsPage();
};

async function refreshBots() {
  const grid = document.getElementById('bots-grid');
  if (!getToken()) {
    grid.innerHTML = '<p class="cardnote">Unlock with the dashboard token to view.</p>';
    return;
  }
  // This runs on a 15s timer — rebuilding the grid's innerHTML while the
  // user has the Model dropdown open (or any other control focused) force-closes
  // it out from under them. Skip this cycle entirely while any control
  // inside the grid has focus, or a custom dropdown is open; the next tick
  // (or their own action, which calls refreshBots() directly) picks up the
  // real state once they're done.
  if (grid.contains(document.activeElement) || grid.querySelector('.custom-select.open')) return;
  let bots;
  try {
    bots = await api('/api/bots');
  } catch (_e) { return; }
  botsCache = bots;
  if (botEditingId == null) renderCanTargetCheckboxes(null, []);

  grid.innerHTML = bots.length ? bots.map(b => {
    const persona = personaMeta(b.persona);
    const manages = (b.can_target || []).map(id => bots.find(x => x.id === id)).filter(Boolean);
    const currentModel = b.model || '';
    const { label: currentModelLabel, html: modelMenuOptions } = buildModelMenuHtml(b.backend, currentModel);
    return `
    <div class="botcard">
      <div class="bc-head">
        <div class="bc-title"><span class="bc-persona-ic" title="${esc(persona.label)}">${persona.icon}</span>${esc(b.name)}</div>
        <span class="pill"><span class="dot ${b.enabled ? 'good' : ''}"></span>${b.enabled ? 'Enabled' : 'Disabled'}</span>
      </div>
      <div class="bc-meta">
        <span class="mono">${esc(b.platform)}</span> · <span class="mono">${esc(b.backend)}</span>
        <span class="pill"><span class="dot ${b.live_running ? 'good' : (b.last_error ? 'critical' : '')}"></span>${b.live_running ? 'Running' : (b.last_error ? 'Crashed' : 'Stopped')}</span>
        ${['ui', 'hermes_gateway'].includes(b.backend) ? `<span class="pill" title="Linked chat/session in the real desktop app">${b.desktop_session_key ? 'Session: ' + esc(b.desktop_session_key) : 'No session linked yet'}</span>` : ''}
      </div>
      <div class="bc-model">
        <label style="font-weight:600; color:var(--muted);">Model</label>
        <div class="custom-select" data-bot-model="${b.id}">
          <button type="button" class="custom-select-btn">${esc(currentModelLabel)}</button>
          <div class="custom-select-menu">${modelMenuOptions}</div>
        </div>
      </div>
      ${manages.length ? `<div class="bc-manages"><span style="font-weight:600; color:var(--muted);">Manages:</span> ${manages.map(m => `<span class="chip neutral">${personaMeta(m.persona).icon} ${esc(m.name)}</span>`).join('')}</div>` : ''}
      ${b.last_error ? `<div class="bc-error">${esc(b.last_error)}</div>` : ''}
      ${b.circuit && b.circuit.open ? `<div class="bc-error">Paused after ${b.circuit.consecutive_failures} consecutive failures — retrying automatically, or <a href="#" data-bot-circuit-reset="${b.id}">retry now</a>.</div>` : ''}
      <div class="bc-actions">
        <button class="btn" data-bot-edit="${b.id}" style="padding:3px 8px; font-size:11px;">Edit</button>
        <button class="btn" data-bot-toggle="${b.id}" style="padding:3px 8px; font-size:11px;">${b.enabled ? 'Disable' : 'Enable'}</button>
        ${b.enabled ? `<button class="btn" data-bot-startstop="${b.id}" style="padding:3px 8px; font-size:11px;">${b.live_running ? 'Stop' : 'Start'}</button>
        <button class="btn" data-bot-restart="${b.id}" style="padding:3px 8px; font-size:11px;">Restart</button>` : ''}
        ${['ui', 'hermes_gateway'].includes(b.backend) ? `<button class="btn" data-bot-newsession="${b.id}" style="padding:3px 8px; font-size:11px;" title="Opens a real new chat in Claude Desktop/Hermes and links it to this bot">New Session</button>` : ''}
        <button class="btn" data-bot-delete="${b.id}" style="padding:3px 8px; font-size:11px;">Delete</button>
      </div>
    </div>`;
  }).join('') : `<div class="card" style="text-align:center; padding:28px 20px;">
      <h3 style="margin-bottom:6px;">No bots yet</h3>
      <p class="cardnote">A "bot" here is one connection to a chat platform (Telegram, Discord, Slack, Matrix, or WhatsApp) paired with a backend that answers it (Claude, Hermes Agent, or any custom model). Fill in the form above and click "Add bot" to create your first one — nothing else needs to be set up first.</p>
    </div>`;
  if (!bots.length) {
    const nameField = document.getElementById('bot-new-name');
    if (document.activeElement === document.body) nameField.focus();
  }

  wireCustomSelects(grid, async (el, value) => {
    const id = Number(el.dataset.botModel);
    await api(`/api/bots/${id}`, { method: 'PUT', body: JSON.stringify({ model: value || null }) });
  });
  document.querySelectorAll('[data-bot-edit]').forEach(btn => btn.onclick = () => {
    const bot = bots.find(b => b.id === Number(btn.dataset.botEdit));
    if (bot) _loadBotIntoForm(bot);
  });
  document.querySelectorAll('[data-bot-circuit-reset]').forEach(a => a.onclick = async (e) => {
    e.preventDefault();
    await api(`/api/bots/${a.dataset.botCircuitReset}/circuit/reset`, { method: 'POST' });
    refreshBots();
  });
  document.querySelectorAll('[data-bot-toggle]').forEach(btn => btn.onclick = async () => {
    const id = Number(btn.dataset.botToggle);
    const bot = bots.find(b => b.id === id);
    await api(`/api/bots/${id}/${bot.enabled ? 'disable' : 'enable'}`, { method: 'POST' });
    refreshBots();
  });
  document.querySelectorAll('[data-bot-startstop]').forEach(btn => btn.onclick = async () => {
    const id = Number(btn.dataset.botStartstop);
    const bot = bots.find(b => b.id === id);
    await api(`/api/bots/${id}/${bot.live_running ? 'stop' : 'start'}`, { method: 'POST' });
    refreshBots();
  });
  document.querySelectorAll('[data-bot-restart]').forEach(btn => btn.onclick = async () => {
    await api(`/api/bots/${btn.dataset.botRestart}/restart`, { method: 'POST' });
    refreshBots();
  });
  document.querySelectorAll('[data-bot-newsession]').forEach(btn => btn.onclick = async () => {
    const id = Number(btn.dataset.botNewsession);
    const bot = bots.find(b => b.id === id);
    if (!confirm(`Open a brand-new chat in ${bot.backend === 'ui' ? 'Claude Desktop' : 'Hermes'} for "${bot.name}"? Future messages to this bot will go there instead of its current linked chat.`)) return;
    btn.disabled = true;
    btn.textContent = 'Opening…';
    try {
      await api(`/api/bots/${id}/session/new`, { method: 'POST' });
      refreshBots();
    } catch (e) {
      alert(`Could not create session: ${e.message}`);
      btn.disabled = false;
      btn.textContent = 'New Session';
    }
  });
  document.querySelectorAll('[data-bot-delete]').forEach(btn => btn.onclick = async () => {
    const id = Number(btn.dataset.botDelete);
    const bot = bots.find(b => b.id === id);
    if (!confirm(`Delete bot "${bot.name}"? It's stopped immediately; job/chat history stays but keeps this id as a reference. A backup is taken first.`)) return;
    await api(`/api/bots/${id}`, { method: 'DELETE' });
    if (botEditingId === id) _resetBotForm();
    refreshBots();
    refreshBotsBackups();
  });
}

document.getElementById('btn-bot-create').onclick = async () => {
  const statusEl = document.getElementById('bot-new-status');
  const platform = document.getElementById('bot-new-platform').value;
  let credentials;
  if (platform === 'matrix') {
    credentials = {
      homeserver: document.getElementById('bot-new-matrix-homeserver').value.trim(),
      user_id: document.getElementById('bot-new-matrix-userid').value.trim(),
      access_token: document.getElementById('bot-new-token').value.trim(),
    };
    const deviceId = document.getElementById('bot-new-matrix-device').value.trim();
    if (deviceId) credentials.device_id = deviceId;
  } else if (platform === 'whatsapp') {
    credentials = {
      phone_number_id: document.getElementById('bot-new-whatsapp-phoneid').value.trim(),
      access_token: document.getElementById('bot-new-token').value.trim(),
      app_secret: document.getElementById('bot-new-whatsapp-appsecret').value.trim(),
      verify_token: document.getElementById('bot-new-whatsapp-verifytoken').value.trim(),
    };
  } else {
    credentials = { bot_token: document.getElementById('bot-new-token').value.trim() };
    if (platform === 'slack') credentials.app_token = document.getElementById('bot-new-apptoken').value.trim();
  }
  const isStringId = platform === 'slack' || platform === 'matrix' || platform === 'whatsapp';
  const allowed_user_ids = document.getElementById('bot-new-allowed').value
    .split(',').map(s => s.trim()).filter(Boolean)
    .map(s => (isStringId ? s : Number(s)));
  const admin_user_ids = document.getElementById('bot-new-admins').value
    .split(',').map(s => s.trim()).filter(Boolean)
    .map(s => (isStringId ? s : Number(s)));
  const can_target = Array.from(document.querySelectorAll('#bot-new-cantarget-list [data-cantarget-id]'))
    .filter(cb => cb.checked).map(cb => Number(cb.dataset.cantargetId));
  const overridesText = document.getElementById('bot-new-overrides').value.trim() || '{}';
  const overridesMsg = document.getElementById('bot-new-overrides-msg');
  let action_overrides;
  try {
    action_overrides = JSON.parse(overridesText);
    overridesMsg.textContent = '';
  } catch (e) {
    overridesMsg.textContent = 'Advanced: backend overrides isn\'t valid JSON — ' + e.message;
    overridesMsg.className = 'msg bad';
    document.getElementById('bot-new-advanced').open = true;
    return;
  }
  const payload = {
    name: document.getElementById('bot-new-name').value.trim(),
    platform,
    backend: document.getElementById('bot-new-backend').value,
    model: document.getElementById('bot-new-model').value.trim() || null,
    persona: selectedPersona,
    credentials,
    allowed_user_ids,
    admin_user_ids,
    can_target,
    custom_instructions: document.getElementById('bot-new-instructions').value.trim() || null,
    enabled: document.getElementById('bot-new-enabled').checked,
    action_overrides,
  };
  statusEl.textContent = 'Saving…';
  try {
    if (botEditingId) {
      await api(`/api/bots/${botEditingId}`, { method: 'PUT', body: JSON.stringify(payload) });
      statusEl.textContent = 'Saved — use Restart on this bot\'s row to apply.';
    } else {
      await api('/api/bots', { method: 'POST', body: JSON.stringify(payload) });
      statusEl.textContent = 'Added and starting…';
    }
    _resetBotForm();
    refreshBots();
    refreshBotsBackups();
  } catch (e) {
    statusEl.textContent = `Failed: ${e.message}`;
  }
};

async function refreshBotsBackups() {
  const tbody = document.getElementById('bots-backups-tbody');
  if (!getToken()) {
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="4">Unlock with the dashboard token to view.</td></tr>';
    return;
  }
  let backups;
  try {
    backups = await api('/api/bots/backups');
  } catch (_e) { return; }
  tbody.innerHTML = backups.length ? backups.map(b => `
    <tr>
      <td class="mono">${esc(b.name)}</td>
      <td class="mono">${fmtTime(b.mtime)}</td>
      <td class="num mono">${(b.size / 1024).toFixed(1)} KB</td>
      <td><button class="btn" data-restore-bots="${esc(b.name)}" style="padding:3px 8px; font-size:11px;">Restore</button></td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="4">No backups yet.</td></tr>';

  document.querySelectorAll('[data-restore-bots]').forEach(btn => btn.onclick = async () => {
    const name = btn.dataset.restoreBots;
    if (!confirm(`Restore ${name}? The current bot list is backed up first, then replaced entirely with this version. Restart the server afterward for it to take effect.`)) return;
    await api(`/api/bots/backups/${encodeURIComponent(name)}/restore`, { method: 'POST' });
    _resetBotForm();
    refreshBots();
    refreshBotsBackups();
  });
}

// -------------------------------------------------------------- swarms ---
const SWARM_CONFIG_PLACEHOLDERS = {
  fanout_synthesize: '{\n  "members": [1, 2],\n  "synthesizer": 1\n}',
  leader_vote: '{\n  "members": [1, 2],\n  "leader": 1\n}',
  sequential_relay: '{\n  "members": [\n    {"instance_id": 1, "instruction": "draft an answer"},\n    {"instance_id": 2, "instruction": "critique and improve it"}\n  ]\n}',
  decompose_delegate: '{\n  "planner": 1,\n  "members": [1, 2],\n  "aggregator": 1\n}',
  custom: '{\n  "steps": [\n    {"id": "s1", "instance_id": 1, "depends_on": []},\n    {"id": "s2", "instance_id": 2, "depends_on": []},\n    {"id": "s3", "instance_id": 1, "depends_on": ["s1", "s2"], "role": "synthesize"}\n  ]\n}',
};

let swarmEditingId = null;
let swarmInstancesCache = [];

document.getElementById('swarm-new-strategy').onchange = () => {
  if (!document.getElementById('swarm-new-config').value.trim()) {
    document.getElementById('swarm-new-config').placeholder = SWARM_CONFIG_PLACEHOLDERS[document.getElementById('swarm-new-strategy').value];
  }
};
document.getElementById('swarm-new-config').placeholder = SWARM_CONFIG_PLACEHOLDERS.fanout_synthesize;

function _resetSwarmForm() {
  swarmEditingId = null;
  document.getElementById('swarm-new-name').value = '';
  document.getElementById('swarm-new-strategy').value = 'fanout_synthesize';
  document.getElementById('swarm-new-config').value = '';
  document.getElementById('swarm-new-config').placeholder = SWARM_CONFIG_PLACEHOLDERS.fanout_synthesize;
  document.getElementById('btn-swarm-create').textContent = 'Add swarm';
}

function _loadSwarmIntoForm(swarm) {
  swarmEditingId = swarm.id;
  document.getElementById('swarm-new-name').value = swarm.name;
  document.getElementById('swarm-new-strategy').value = swarm.strategy;
  document.getElementById('swarm-new-config').value = JSON.stringify(swarm.config, null, 2);
  document.getElementById('btn-swarm-create').textContent = 'Save changes';
  document.getElementById('swarms').scrollIntoView({ behavior: 'smooth' });
}

async function refreshSwarmInstanceLegend() {
  try {
    swarmInstancesCache = await api('/api/bots');
  } catch (_e) { return; }
  document.getElementById('swarm-instance-legend').textContent = swarmInstancesCache.length
    ? swarmInstancesCache.map(b => `${b.id}=${b.name}`).join(', ')
    : 'none yet — add one in the Bots tab first';
}

async function refreshSwarms() {
  const tbody = document.getElementById('swarms-tbody');
  const runSelect = document.getElementById('swarm-run-select');
  if (!getToken()) {
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="4">Unlock with the dashboard token to view.</td></tr>';
    return;
  }
  let swarms;
  try {
    swarms = await api('/api/swarms');
  } catch (_e) { return; }
  tbody.innerHTML = swarms.length ? swarms.map(s => `
    <tr>
      <td>${esc(s.name)}</td>
      <td class="mono">${esc(s.strategy)}</td>
      <td><span class="pill"><span class="dot ${s.enabled ? 'good' : ''}"></span>${s.enabled ? 'Enabled' : 'Disabled'}</span></td>
      <td style="white-space:nowrap;">
        <button class="btn" data-swarm-edit="${s.id}" style="padding:3px 8px; font-size:11px;">Edit</button>
        <button class="btn" data-swarm-toggle="${s.id}" style="padding:3px 8px; font-size:11px;">${s.enabled ? 'Disable' : 'Enable'}</button>
        <button class="btn" data-swarm-delete="${s.id}" style="padding:3px 8px; font-size:11px;">Delete</button>
      </td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="4">No swarms yet — add one above.</td></tr>';

  runSelect.innerHTML = swarms.length
    ? swarms.map(s => `<option value="${s.id}">${esc(s.name)} (${esc(s.strategy)})</option>`).join('')
    : '<option value="">no swarms configured</option>';

  document.querySelectorAll('[data-swarm-edit]').forEach(btn => btn.onclick = () => {
    const s = swarms.find(x => x.id === Number(btn.dataset.swarmEdit));
    if (s) _loadSwarmIntoForm(s);
  });
  document.querySelectorAll('[data-swarm-toggle]').forEach(btn => btn.onclick = async () => {
    const id = Number(btn.dataset.swarmToggle);
    const s = swarms.find(x => x.id === id);
    await api(`/api/swarms/${id}/${s.enabled ? 'disable' : 'enable'}`, { method: 'POST' });
    refreshSwarms();
  });
  document.querySelectorAll('[data-swarm-delete]').forEach(btn => btn.onclick = async () => {
    const id = Number(btn.dataset.swarmDelete);
    const s = swarms.find(x => x.id === id);
    if (!confirm(`Delete swarm "${s.name}"? Its run history stays but keeps this id as a reference.`)) return;
    await api(`/api/swarms/${id}`, { method: 'DELETE' });
    if (swarmEditingId === id) _resetSwarmForm();
    refreshSwarms();
  });
}

async function refreshSwarmToolsPanel() {
  const tbody = document.getElementById('swarm-tools-tbody');
  if (!getToken()) {
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="5">Unlock with the dashboard token to view.</td></tr>';
    return;
  }
  let data;
  try {
    data = await api('/api/hermes/swarm-tools-status');
  } catch (_e) { return; }
  const rows = data.instances || [];
  tbody.innerHTML = rows.length ? rows.map(r => `
    <tr>
      <td>${esc(r.name)}</td>
      <td class="mono">${esc(r.backend)}</td>
      <td class="mono" style="font-size:11px;">${r.hermes_home ? esc(r.hermes_home) : '<span style="color:var(--muted);">shared default</span>'}</td>
      <td><span class="pill"><span class="dot ${r.swarm_tools_enabled ? 'good' : ''}"></span>${r.swarm_tools_enabled ? 'Enabled' : 'Disabled'}</span></td>
      <td style="white-space:nowrap;">
        <button class="btn" data-swarm-tools-toggle="${r.id}" data-enabled="${r.swarm_tools_enabled}" style="padding:3px 8px; font-size:11px;">${r.swarm_tools_enabled ? 'Disable' : 'Enable'}</button>
      </td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="5">No Hermes-backed instances yet — add one in the Bots tab.</td></tr>';

  document.querySelectorAll('[data-swarm-tools-toggle]').forEach(btn => btn.onclick = async () => {
    const id = Number(btn.dataset.swarmToolsToggle);
    const enabled = btn.dataset.enabled === 'true';
    btn.disabled = true;
    try {
      await api(`/api/hermes/${id}/${enabled ? 'disable' : 'enable'}-swarm-tools`, { method: 'POST' });
    } finally {
      refreshSwarmToolsPanel();
    }
  });
}

let contextDocEditingName = null;

function _resetContextDocForm() {
  contextDocEditingName = null;
  document.getElementById('context-doc-name').value = '';
  document.getElementById('context-doc-name').disabled = false;
  document.getElementById('context-doc-content').value = '';
  document.getElementById('context-doc-status').textContent = '';
  document.getElementById('btn-context-doc-save').textContent = 'Save doc';
  document.getElementById('btn-context-doc-cancel').style.display = 'none';
}

async function _loadContextDocIntoForm(name) {
  let doc;
  try {
    doc = await api(`/api/context/${encodeURIComponent(name)}`);
  } catch (_e) { return; }
  contextDocEditingName = doc.name;
  document.getElementById('context-doc-name').value = doc.name;
  document.getElementById('context-doc-name').disabled = true;
  document.getElementById('context-doc-content').value = doc.content;
  document.getElementById('btn-context-doc-save').textContent = 'Save changes';
  document.getElementById('btn-context-doc-cancel').style.display = '';
  document.getElementById('swarms').scrollIntoView({ behavior: 'smooth' });
}

async function refreshContextDocs() {
  const tbody = document.getElementById('context-docs-tbody');
  if (!getToken()) {
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="5">Unlock with the dashboard token to view.</td></tr>';
    return;
  }
  let data;
  try {
    data = await api('/api/context');
  } catch (_e) { return; }
  const docs = data.docs || [];
  tbody.innerHTML = docs.length ? docs.map(d => `
    <tr>
      <td class="mono">${esc(d.name)}</td>
      <td>${d.size} chars</td>
      <td style="font-size:11px;">${esc(d.updated_at)}</td>
      <td>${esc(d.updated_by)}</td>
      <td style="white-space:nowrap;">
        <button class="btn" data-context-edit="${esc(d.name)}" style="padding:3px 8px; font-size:11px;">Edit</button>
        <button class="btn" data-context-delete="${esc(d.name)}" style="padding:3px 8px; font-size:11px;">Delete</button>
      </td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="5">No shared context docs yet — add one above.</td></tr>';

  document.querySelectorAll('[data-context-edit]').forEach(btn => btn.onclick = () => _loadContextDocIntoForm(btn.dataset.contextEdit));
  document.querySelectorAll('[data-context-delete]').forEach(btn => btn.onclick = async () => {
    const name = btn.dataset.contextDelete;
    if (!confirm(`Delete shared context doc "${name}"? This can't be undone.`)) return;
    await api(`/api/context/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (contextDocEditingName === name) _resetContextDocForm();
    refreshContextDocs();
  });
}

document.getElementById('btn-context-doc-save').onclick = async () => {
  const statusEl = document.getElementById('context-doc-status');
  const name = document.getElementById('context-doc-name').value.trim();
  const content = document.getElementById('context-doc-content').value;
  if (!name) {
    statusEl.textContent = 'Name is required.';
    return;
  }
  statusEl.textContent = 'Saving…';
  try {
    await api(`/api/context/${encodeURIComponent(name)}`, { method: 'POST', body: JSON.stringify({ content, actor: 'dashboard' }) });
    statusEl.textContent = 'Saved.';
    _resetContextDocForm();
    refreshContextDocs();
  } catch (e) {
    statusEl.textContent = `Failed: ${e.message}`;
  }
};

document.getElementById('btn-context-doc-cancel').onclick = () => _resetContextDocForm();

document.getElementById('btn-swarm-create').onclick = async () => {
  const statusEl = document.getElementById('swarm-new-status');
  let config;
  try {
    config = JSON.parse(document.getElementById('swarm-new-config').value || document.getElementById('swarm-new-config').placeholder);
  } catch (e) {
    statusEl.textContent = 'Config isn\'t valid JSON.';
    return;
  }
  const payload = {
    name: document.getElementById('swarm-new-name').value.trim(),
    strategy: document.getElementById('swarm-new-strategy').value,
    config,
  };
  statusEl.textContent = 'Saving…';
  try {
    if (swarmEditingId) {
      await api(`/api/swarms/${swarmEditingId}`, { method: 'PUT', body: JSON.stringify(payload) });
      statusEl.textContent = 'Saved.';
    } else {
      await api('/api/swarms', { method: 'POST', body: JSON.stringify(payload) });
      statusEl.textContent = 'Added.';
    }
    _resetSwarmForm();
    refreshSwarms();
  } catch (e) {
    statusEl.textContent = `Failed: ${e.message}`;
  }
};

let swarmRunPollTimer = null;

function renderSwarmSteps(steps) {
  const el = document.getElementById('swarm-run-steps');
  if (!steps || !steps.length) { el.innerHTML = '<p class="cardnote">Waiting for the first step…</p>'; return; }
  el.innerHTML = `<table><thead><tr><th>Step</th><th>Role</th><th>Status</th><th>Result</th></tr></thead><tbody>${
    steps.map(s => `<tr>
      <td class="mono">${esc(s.step)}</td>
      <td class="mono">${esc(s.role || '')}</td>
      <td><span class="pill"><span class="dot ${s.status === 'success' ? 'good' : (s.status === 'failed' ? 'critical' : '')}"></span>${esc(s.status)}</span></td>
      <td style="max-width:360px; white-space:pre-wrap;">${esc((s.result || s.error || '').slice(0, 400))}</td>
    </tr>`).join('')
  }</tbody></table>`;
}

async function pollSwarmRun(runId) {
  const progress = document.getElementById('swarm-run-progress');
  progress.style.display = '';
  const statusEl = document.getElementById('swarm-run-status');
  if (swarmRunPollTimer) clearInterval(swarmRunPollTimer);
  const tick = async () => {
    let run;
    try {
      run = await api(`/api/swarms/runs/${runId}`);
    } catch (_e) { return; }
    renderSwarmSteps(run.steps);
    document.getElementById('swarm-run-result').textContent = run.result || (run.error || '');
    if (run.status === 'running') {
      statusEl.textContent = 'Running…';
    } else {
      statusEl.textContent = `Finished: ${run.status}`;
      clearInterval(swarmRunPollTimer);
      refreshSwarmRuns();
    }
  };
  await tick();
  swarmRunPollTimer = pollWhenVisible(tick, 2500);
}

document.getElementById('btn-swarm-run').onclick = async () => {
  const statusEl = document.getElementById('swarm-run-status');
  const swarmId = document.getElementById('swarm-run-select').value;
  const prompt = document.getElementById('swarm-run-prompt').value.trim();
  if (!swarmId) { statusEl.textContent = 'No swarm selected.'; return; }
  if (!prompt) { statusEl.textContent = 'Enter a prompt.'; return; }
  statusEl.textContent = 'Starting…';
  try {
    const res = await api(`/api/swarms/${swarmId}/run`, { method: 'POST', body: JSON.stringify({ prompt }) });
    pollSwarmRun(res.swarm_run_id);
  } catch (e) {
    statusEl.textContent = `Failed to start: ${e.message}`;
  }
};

async function refreshSwarmRuns() {
  const tbody = document.getElementById('swarm-runs-tbody');
  if (!getToken()) return;
  let runs;
  try {
    runs = await api('/api/swarms/runs?limit=20');
  } catch (_e) { return; }
  tbody.innerHTML = runs.length ? runs.map(r => `
    <tr>
      <td class="mono">${fmtTime(r.created_at)}</td>
      <td style="max-width:320px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${esc(r.prompt)}</td>
      <td><span class="pill"><span class="dot ${r.status === 'success' ? 'good' : (r.status === 'failed' ? 'critical' : '')}"></span>${esc(r.status)}</span></td>
      <td><button class="btn" data-view-run="${esc(r.swarm_run_id)}" style="padding:3px 8px; font-size:11px;">View</button></td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="4">No runs yet.</td></tr>';

  document.querySelectorAll('[data-view-run]').forEach(btn => btn.onclick = () => pollSwarmRun(btn.dataset.viewRun));
}

const DELEGATION_KIND_LABELS = {
  agent_ask: 'ask_instance',
  swarm_dispatch: 'dispatch_swarm_goal',
  agent_delegate: 'delegate_to_instance',
  swarm_dispatch_blocked: 'blocked (budget)',
};

// Tracks which delegation-activity job_id currently has its detail row
// open, so a live job_tool_event/job_children_update push (see
// devicesSocket.onmessage above) can refresh it in place instead of
// waiting for the next 15s poll.
let openDelegationDetailJobId = null;

async function refreshDelegationActivity() {
  const tbody = document.getElementById('delegation-activity-tbody');
  if (!getToken()) {
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="4">Unlock with the dashboard token to view.</td></tr>';
    return;
  }
  let data;
  try {
    data = await api('/api/delegation-activity?limit=20');
  } catch (_e) { return; }
  const events = data.events || [];
  tbody.innerHTML = events.length ? events.map(e => `
    <tr>
      <td class="mono" style="font-size:11px; white-space:nowrap;">${fmtTime(e.ts)}</td>
      <td><span class="pill">${esc(DELEGATION_KIND_LABELS[e.action] || e.action)}</span></td>
      <td style="max-width:420px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${esc(e.actor.replace(/^agent:/, ''))} ${esc(e.detail)}</td>
      <td>${e.job_id ? `<button class="btn" data-view-job="${e.job_id}" style="padding:3px 8px; font-size:11px;">View</button>` : ''}</td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="4">No delegation activity yet.</td></tr>';

  document.querySelectorAll('[data-view-job]').forEach(btn => btn.onclick = () => toggleDelegationDetail(Number(btn.dataset.viewJob), btn));
}

async function toggleDelegationDetail(jobId, btn) {
  const row = btn.closest('tr');
  const existing = row.nextElementSibling;
  if (existing && existing.classList.contains('delegation-detail-row')) {
    existing.remove();
    if (openDelegationDetailJobId === jobId) openDelegationDetailJobId = null;
    return;
  }
  document.querySelectorAll('.delegation-detail-row').forEach(r => r.remove());
  openDelegationDetailJobId = jobId;
  const detailRow = document.createElement('tr');
  detailRow.className = 'delegation-detail-row';
  detailRow.dataset.jobId = String(jobId);
  detailRow.innerHTML = '<td colspan="4"><em>Loading...</em></td>';
  row.after(detailRow);
  await renderDelegationDetail(jobId);
}

async function renderDelegationDetail(jobId) {
  const detailRow = document.querySelector(`.delegation-detail-row[data-job-id="${jobId}"]`);
  if (!detailRow) return;
  let toolEvents = [], children = [];
  try {
    const [teData, chData] = await Promise.all([
      api(`/api/jobs/${jobId}/tool-events`),
      api(`/api/jobs/${jobId}/children`),
    ]);
    toolEvents = teData.events || [];
    children = chData.children || [];
  } catch (_e) {
    detailRow.innerHTML = '<td colspan="4"><em>Could not load detail.</em></td>';
    return;
  }
  const eventsHtml = toolEvents.length
    ? `<div style="font-size:11px; margin-bottom:8px;"><b>Live tool events</b> (top-level delegate_task calls only — no per-child data reaches this stream):<br>${
        toolEvents.map(e => `<span class="pill" style="margin:2px;">${esc(e.event_type)} @ ${fmtTime(e.ts)}</span>`).join('')
      }</div>`
    : '<div class="cardnote" style="margin-bottom:8px;">No live tool events recorded (either still starting, the dispatch finished before this loaded, or this Hermes gateway version has no SSE stream).</div>';
  const childrenHtml = children.length
    ? `<div style="font-size:11px;"><b>Per-child breakdown</b> (parsed from the dispatch's own final reply):</div>
       <table style="margin-top:4px;"><thead><tr><th>#</th><th>Goal</th><th>Model</th><th>Status</th><th>Result</th></tr></thead><tbody>${
         children.map(c => `<tr>
           <td>${c.child_index}</td>
           <td style="max-width:260px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${esc(c.goal)}</td>
           <td class="mono" style="font-size:11px;">${esc(c.model)}</td>
           <td><span class="pill"><span class="dot ${c.status === 'ok' ? 'good' : 'critical'}"></span>${esc(c.status)}</span></td>
           <td style="max-width:320px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${esc(c.result_excerpt)}</td>
         </tr>`).join('')
       }</tbody></table>`
    : '<div class="cardnote">No per-child breakdown yet — the dispatch hasn\'t finished, or its final reply didn\'t include the structured block.</div>';
  detailRow.innerHTML = `<td colspan="4" style="background:var(--panel-2, rgba(127,127,127,0.06));">${eventsHtml}${childrenHtml}</td>`;
}

async function refreshSwarmBudget() {
  if (!getToken()) return;
  let cfg;
  try { cfg = await api('/api/swarm-budget'); } catch (_e) { return; }
  document.getElementById('budget-enabled').checked = !!cfg.enabled;
  document.getElementById('budget-max-children').value = cfg.max_children;
  document.getElementById('budget-max-usd').value = cfg.max_estimated_usd;
  document.getElementById('budget-confirm-usd').value = cfg.require_confirm_above_usd;
  document.getElementById('budget-deny-unpriced').checked = !!cfg.deny_unpriced_paid_models;
}

document.getElementById('budget-save-btn').onclick = async () => {
  const status = document.getElementById('budget-save-status');
  status.textContent = 'Saving...';
  try {
    await api('/api/swarm-budget', {
      method: 'POST',
      body: JSON.stringify({
        enabled: document.getElementById('budget-enabled').checked,
        max_children: Number(document.getElementById('budget-max-children').value),
        max_estimated_usd: Number(document.getElementById('budget-max-usd').value),
        require_confirm_above_usd: Number(document.getElementById('budget-confirm-usd').value),
        deny_unpriced_paid_models: document.getElementById('budget-deny-unpriced').checked,
      }),
    });
    status.textContent = 'Saved.';
  } catch (e) {
    status.textContent = 'Failed: ' + e.message;
  }
  setTimeout(() => { status.textContent = ''; }, 3000);
};

// ------------------------------------------------------------------ chat -
// Per-instance panels: chatState.panels[id] holds each bot's own cursor,
// draft, and recipient so switching tabs is instant (no re-fetch flash)
// and never loses what you were mid-typing. Only the active panel is
// polled on the 2s interval — an inactive bot's messages just wait for
// the next time you switch to it (immediate refreshChat() on switch keeps
// that from feeling stale in practice for this single-user dashboard).
const chatState = { activeInstanceId: null, instances: null, panels: {}, mode: 'server' };

function panelFor(id) {
  if (!chatState.panels[id]) chatState.panels[id] = { lastId: 0, recipient: null, loaded: false, draft: '' };
  return chatState.panels[id];
}

// "server" = Send from Server: a real message OUT through outbox.py + a
// live platform SDK (POST /api/chat/send) — appears to that user as coming
// from the bot. "bot" = Chat with Bot: a real message FROM this app TO the
// bot (POST /api/chat/send-to-bot), through the same CmdContext/
// dispatch_command/router.ask() pipeline every Telegram/Discord/Slack
// message goes through. No recipient picker in this mode — the sender's
// identity comes from the request's own auth. Mirrors the same toggle in
// bot/dashboard/static/dashboard.html so both GUIs behave identically.
function setChatMode(mode) {
  chatState.mode = mode;
  const isBot = mode === 'bot';
  document.getElementById('chat-card').classList.toggle('mode-server', !isBot);
  document.getElementById('chat-card').classList.toggle('mode-bot', isBot);
  const switchBtn = document.getElementById('chat-mode-switch');
  switchBtn.setAttribute('aria-checked', String(isBot));
  switchBtn.setAttribute('aria-label', 'Chat mode: ' + (isBot ? 'Chat with Bot' : 'Send from Server') + ' — click to switch');
  document.getElementById('chat-mode-switch-text').textContent = isBot ? '💬 Chat with Bot' : '📤 Send from Server';
  document.getElementById('chat-mode-banner').textContent = isBot
    ? '💬 CHAT WITH BOT — you are talking directly to the bot. It receives this for real and replies for real.'
    : '📤 SEND FROM SERVER — messages you send go out for real, straight to the platform user picked below.';
  document.getElementById('chat-recipient-label').classList.toggle('hidden', isBot);
  document.getElementById('chat-recipient').classList.toggle('hidden', isBot);
  document.getElementById('chat-recipient-note').classList.toggle('hidden', isBot);
  const attachBtn = document.getElementById('btn-chat-attach');
  attachBtn.disabled = isBot;
  attachBtn.title = isBot ? 'Attachments aren\'t supported in Chat with Bot mode' : 'Attach file';
  const input = document.getElementById('chat-input');
  input.placeholder = isBot ? 'Message the bot…' : 'Message…';
  if (isBot && typeof chatPendingFile !== 'undefined' && chatPendingFile) {
    chatPendingFile = null;
    document.getElementById('chat-file-input').value = '';
    document.getElementById('chat-pending-attachment').classList.add('hidden');
  }
}
document.getElementById('chat-mode-switch').onclick = () => setChatMode(chatState.mode === 'server' ? 'bot' : 'server');
setChatMode('server');

function fmtChatTime(ts) {
  // ts is already a full ISO8601 string with explicit UTC offset
  // (datetime.isoformat() from Python) — no 'Z' needed or safe to append.
  const d = new Date(ts);
  if (isNaN(d)) return ts;
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// Generic authenticated-download helper for the JSON backup/export
// endpoints (sessions, chat, server chat) — same pattern as
// downloadAttachment below, since a plain <a href> can't carry the
// dashboard token header these endpoints require.
async function downloadUrl(path) {
  try {
    const headers = {};
    const token = getToken();
    if (token) headers['X-Dashboard-Token'] = token;
    const res = await fetch(API_BASE + path, { headers });
    if (!res.ok) throw new Error(await res.text());
    const disposition = res.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="([^"]+)"/);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = match ? match[1] : 'export.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('Export failed — check the dashboard token.');
  }
}

async function downloadAttachment(messageId, name) {
  try {
    const headers = {};
    const token = getToken();
    if (token) headers['X-Dashboard-Token'] = token;
    const res = await fetch(API_BASE + `/api/chat/attachments/${messageId}`, { headers });
    if (!res.ok) throw new Error(await res.text());
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name || 'file';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('Download failed — check the dashboard token.');
  }
}

// <img src> can't carry the X-Dashboard-Token header, so thumbnails load
// the same way downloadAttachment() does — fetch with auth, then a blob
// URL — rather than exposing the token in a query string.
async function loadThumbnail(imgEl, messageId) {
  try {
    const headers = {};
    const token = getToken();
    if (token) headers['X-Dashboard-Token'] = token;
    const res = await fetch(API_BASE + `/api/chat/attachments/${messageId}/thumbnail`, { headers });
    if (!res.ok) throw new Error('no thumbnail');
    const blob = await res.blob();
    imgEl.src = URL.createObjectURL(blob);
  } catch (_e) {
    imgEl.remove();
  }
}

function ensurePanel(instanceId) {
  const panels = document.getElementById('chat-panels');
  let win = panels.querySelector(`.chat-window[data-instance-id="${instanceId}"]`);
  if (!win) {
    win = document.createElement('div');
    win.className = 'chat-window';
    win.dataset.instanceId = String(instanceId);
    panels.appendChild(win);
  }
  return win;
}

function activatePanel(instanceId) {
  document.querySelectorAll('#chat-panels .chat-window').forEach(w => {
    w.classList.toggle('active', w.dataset.instanceId === String(instanceId));
  });
  document.querySelectorAll('#chat-tabs .chat-list-item').forEach(t => {
    t.classList.toggle('active', t.dataset.instanceId === String(instanceId));
  });
}

function chatInitials(name) {
  const words = (name || '?').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '?';
  return words.slice(0, 2).map(w => w[0]).join('').toUpperCase();
}

// The left pane is this dashboard's stand-in for Telegram's chat list: one
// row per bot instance (the closest thing to a "conversation" here, since
// each instance is its own bot identity with its own recipients). Typing
// in the search box filters it client-side — same instances array, no
// extra fetch.
function renderChatTabs() {
  const list = document.getElementById('chat-tabs');
  const instances = chatState.instances || [];
  const q = (document.getElementById('chat-list-search')?.value || '').trim().toLowerCase();
  const filtered = q ? instances.filter(i => i.name.toLowerCase().includes(q) || i.platform.toLowerCase().includes(q)) : instances;
  if (!filtered.length) {
    list.innerHTML = `<p class="chat-list-empty">${instances.length ? 'No bots match your search.' : 'Add a bot in the Bots tab first.'}</p>`;
  } else {
    list.innerHTML = filtered.map(i => `
      <button class="chat-list-item${i.id === chatState.activeInstanceId ? ' active' : ''}" data-instance-id="${i.id}">
        <span class="chat-list-avatar">${esc(chatInitials(i.name))}</span>
        <span class="chat-list-txt">
          <span class="chat-list-name">${esc(i.name)}</span>
          <span class="chat-list-meta">${esc(i.platform)}${i.connected ? '' : ' · offline'}</span>
        </span>
        <span class="chat-list-dot${i.connected ? ' online' : ''}"></span>
      </button>
    `).join('');
    list.querySelectorAll('.chat-list-item').forEach(btn => {
      btn.onclick = () => switchToInstance(Number(btn.dataset.instanceId));
    });
  }
  updateChatHeader();
}

function updateChatHeader() {
  const inst = (chatState.instances || []).find(i => i.id === chatState.activeInstanceId);
  const nameEl = document.getElementById('chat-header-name');
  const subEl = document.getElementById('chat-header-sub');
  const avEl = document.getElementById('chat-header-avatar');
  if (!inst) {
    nameEl.textContent = 'Select a bot';
    subEl.textContent = 'Pick one from the list';
    avEl.textContent = '—';
    avEl.classList.remove('online');
    return;
  }
  nameEl.textContent = inst.name;
  subEl.textContent = `${inst.platform} · ${inst.connected ? 'online' : 'not running'}`;
  avEl.textContent = chatInitials(inst.name);
  avEl.classList.toggle('online', !!inst.connected);
}

document.getElementById('chat-list-search').oninput = renderChatTabs;

function appendChatMessages(instanceId, rows) {
  const win = ensurePanel(instanceId);
  const panel = panelFor(instanceId);
  const atBottom = win.scrollHeight - win.scrollTop - win.clientHeight < 60;
  rows.forEach(m => {
    const row = document.createElement('div');
    row.className = 'chat-row ' + (m.direction === 'in' ? 'in' : 'out');
    row.dataset.msgId = String(m.id);
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble' + (m.source === 'dashboard' ? ' from-dashboard' : '') + (m.platform === 'app' && m.direction === 'in' ? ' from-app' : '');
    if (m.text) {
      const textEl = document.createElement('div');
      textEl.textContent = m.text;
      bubble.appendChild(textEl);
    }
    if (m.attachment_path) {
      if (m.thumbnail_path) {
        const img = document.createElement('img');
        img.className = 'chat-thumb';
        img.alt = m.attachment_name || 'image';
        img.onclick = () => downloadAttachment(m.id, m.attachment_name);
        bubble.appendChild(img);
        loadThumbnail(img, m.id);
      }
      const att = document.createElement('a');
      att.className = 'chat-attachment';
      att.textContent = '📎 ' + (m.attachment_name || 'file') + (m.attachment_size ? ` (${fmtBytes(m.attachment_size)})` : '');
      att.href = '#';
      att.onclick = (e) => { e.preventDefault(); downloadAttachment(m.id, m.attachment_name); };
      bubble.appendChild(att);
    }
    const meta = document.createElement('span');
    meta.className = 'chat-meta';
    const who = m.source === 'dashboard' ? 'sent from dashboard' : (m.direction === 'in' && m.username) ? m.username : '';
    meta.textContent = [fmtChatTime(m.ts), m.platform, who].filter(Boolean).join(' · ');
    bubble.appendChild(meta);
    row.appendChild(bubble);
    win.appendChild(row);
    panel.lastId = Math.max(panel.lastId, m.id);
  });
  if (rows.length && atBottom) win.scrollTop = win.scrollHeight;
}

function renderChatRecipients() {
  const recipientSelect = document.getElementById('chat-recipient');
  const note = document.getElementById('chat-recipient-note');
  const instances = chatState.instances || [];
  const inst = instances.find(i => i.id === chatState.activeInstanceId);
  if (!inst) return;
  const panel = panelFor(chatState.activeInstanceId);
  const ids = (inst.allowed_ids || []).map(String);
  if (!ids.length) {
    recipientSelect.innerHTML = '<option value="">no allowed users</option>';
    note.textContent = `${inst.name} has no allowed user IDs — edit it in the Bots tab.`;
    note.classList.remove('hidden');
    return;
  }
  recipientSelect.innerHTML = ids.map(id => `<option value="${esc(id)}">${esc(id)}</option>`).join('');
  if (!panel.recipient || !ids.includes(String(panel.recipient))) {
    panel.recipient = ids[0];
  }
  recipientSelect.value = String(panel.recipient);
  note.textContent = inst.connected ? '' : `${inst.name} is configured but not running right now — start it from the Bots tab.`;
  note.classList.toggle('hidden', !note.textContent);
}

async function refreshChatRecipients() {
  const instanceSelect = document.getElementById('chat-instance');
  try {
    const data = await api('/api/chat/recipients');
    const instances = (data.instances || []).filter(i => (i.allowed_ids || []).length > 0);
    chatState.instances = instances;
    if (!instances.length) {
      instanceSelect.innerHTML = '<option value="">no bots configured</option>';
      document.getElementById('chat-recipient').innerHTML = '';
      const note = document.getElementById('chat-recipient-note');
      note.textContent = '';
      note.classList.add('hidden');
      renderChatTabs();
      updateChatHeader();
      return;
    }
    const panelsContainer = document.getElementById('chat-panels');
    if (!panelsContainer.querySelector('.chat-window')) panelsContainer.innerHTML = '';
    instances.forEach(i => ensurePanel(i.id));
    instanceSelect.innerHTML = instances.map(i => `<option value="${i.id}">${esc(i.name)} (${esc(i.platform)})${i.connected ? '' : ' — not running'}</option>`).join('');
    if (!chatState.activeInstanceId || !instances.some(i => i.id === chatState.activeInstanceId)) {
      chatState.activeInstanceId = instances[0].id;
      activatePanel(chatState.activeInstanceId);
    }
    instanceSelect.value = String(chatState.activeInstanceId);
    renderChatTabs();
    renderChatRecipients();
  } catch (_e) { /* token not set yet — leave placeholder */ }
}

async function refreshChat(instanceId) {
  instanceId = instanceId || chatState.activeInstanceId;
  if (!instanceId) return;
  const panel = panelFor(instanceId);
  try {
    if (!panel.loaded) {
      ensurePanel(instanceId).innerHTML = '';
      const rows = await api(`/api/chat/messages?limit=100&instance_id=${instanceId}`);
      appendChatMessages(instanceId, rows);
      panel.loaded = true;
    } else {
      const rows = await api(`/api/chat/messages?after_id=${panel.lastId}&limit=200&instance_id=${instanceId}`);
      appendChatMessages(instanceId, rows);
    }
  } catch (_e) { /* token not set yet, or server not ready — try again next tick */ }
}

function startChatPolling() {
  refreshChatRecipients();
  pollWhenVisible(() => refreshChat(chatState.activeInstanceId), 2000);
  pollWhenVisible(refreshChatRecipients, 15000);
}

function switchToInstance(instanceId) {
  if (!instanceId || instanceId === chatState.activeInstanceId) return;
  const input = document.getElementById('chat-input');
  if (chatState.activeInstanceId) {
    panelFor(chatState.activeInstanceId).draft = input.value;
  }
  chatState.activeInstanceId = instanceId;
  ensurePanel(instanceId);
  activatePanel(instanceId);
  input.value = panelFor(instanceId).draft || '';
  document.getElementById('chat-instance').value = String(instanceId);
  renderChatTabs();
  renderChatRecipients();
  refreshChat(instanceId);
}

document.getElementById('chat-instance').onchange = (e) => {
  switchToInstance(Number(e.target.value));
};
document.getElementById('chat-recipient').onchange = (e) => {
  panelFor(chatState.activeInstanceId).recipient = e.target.value;
};
document.getElementById('btn-chat-export').onclick = () => {
  if (!chatState.activeInstanceId) return;
  downloadUrl(`/api/chat/messages/export?instance_id=${chatState.activeInstanceId}`);
};
document.getElementById('btn-chat-clear').onclick = async () => {
  const instanceId = chatState.activeInstanceId;
  if (!instanceId) return;
  const inst = (chatState.instances || []).find(i => i.id === instanceId);
  if (!confirm(`Delete this bot's entire chat history (every chat, every platform message logged for "${inst ? inst.name : instanceId}")? This permanently removes it — export first if you want a copy.`)) return;
  await api('/api/chat/messages', { method: 'DELETE', body: JSON.stringify({ instance_id: instanceId }) });
  const panel = panelFor(instanceId);
  panel.loaded = false;
  panel.lastId = 0;
  ensurePanel(instanceId).innerHTML = '';
  refreshChat(instanceId);
};

let chatPendingFile = null;
document.getElementById('btn-chat-attach').onclick = () => document.getElementById('chat-file-input').click();
document.getElementById('chat-file-input').onchange = (e) => {
  chatPendingFile = e.target.files[0] || null;
  const box = document.getElementById('chat-pending-attachment');
  if (chatPendingFile) {
    document.getElementById('chat-pending-name').textContent = chatPendingFile.name;
    box.classList.remove('hidden');
  } else {
    box.classList.add('hidden');
  }
};
document.getElementById('btn-chat-attach-clear').onclick = () => {
  chatPendingFile = null;
  document.getElementById('chat-file-input').value = '';
  document.getElementById('chat-pending-attachment').classList.add('hidden');
};

const ChatSpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
const chatMicBtn = document.getElementById('btn-chat-mic');
if (!ChatSpeechRec) {
  chatMicBtn.disabled = true;
  chatMicBtn.title = 'Voice input not supported in this browser/WebView';
} else {
  let chatRecognizer = null, chatRecording = false;
  chatMicBtn.onclick = () => {
    const input = document.getElementById('chat-input');
    if (chatRecording) { chatRecognizer.stop(); return; }
    chatRecognizer = new ChatSpeechRec();
    chatRecognizer.lang = navigator.language || 'en-US';
    chatRecognizer.interimResults = false;
    chatRecognizer.continuous = false;
    chatRecognizer.onresult = (e) => {
      const transcript = Array.from(e.results).map(r => r[0].transcript).join(' ');
      input.value = (input.value ? input.value + ' ' : '') + transcript;
    };
    chatRecognizer.onerror = () => { chatRecording = false; chatMicBtn.classList.remove('recording'); };
    chatRecognizer.onend = () => { chatRecording = false; chatMicBtn.classList.remove('recording'); };
    chatRecognizer.start();
    chatRecording = true;
    chatMicBtn.classList.add('recording');
  };
}

async function sendChatMessage() {
  const input = document.getElementById('chat-input');
  const statusEl = document.getElementById('chat-status');
  const text = input.value.trim();
  if (!text && !chatPendingFile) return;
  const panel = panelFor(chatState.activeInstanceId);
  if (chatState.mode !== 'bot' && !panel.recipient) {
    statusEl.textContent = 'No recipient — set up an allowed user for this bot first.';
    return;
  }
  const btn = document.getElementById('btn-chat-send');
  btn.disabled = true;
  statusEl.textContent = chatState.mode === 'bot' ? 'Waiting for bot reply…' : '';
  try {
    if (chatState.mode === 'bot') {
      await api('/api/chat/send-to-bot', { method: 'POST', body: JSON.stringify({ instance_id: chatState.activeInstanceId, text }) });
      statusEl.textContent = '';
    } else if (chatPendingFile) {
      await sendChatFile(chatPendingFile, text, panel, statusEl);
      chatPendingFile = null;
      document.getElementById('chat-file-input').value = '';
      document.getElementById('chat-pending-attachment').classList.add('hidden');
    } else {
      await api('/api/chat/send', { method: 'POST', body: JSON.stringify({ instance_id: chatState.activeInstanceId, chat_id: panel.recipient, text }) });
    }
    input.value = '';
    panel.draft = '';
    await refreshChat(chatState.activeInstanceId);
  } catch (e) {
    statusEl.textContent = chatState.mode === 'bot'
      ? 'Send failed — check the dashboard token and that this bot instance exists.'
      : 'Send failed — check the dashboard token and that this bot is running.';
  } finally {
    btn.disabled = false;
  }
}

// Chunked upload — init declares the file, then one PUT per chunk (any
// order, retriable), then complete assembles + relays. Small files still
// go through this same path (just one chunk); the old single-shot
// /api/chat/send-file route stays around server-side for any other caller,
// but the desktop UI always uses the resumable, progress-reporting path.
async function sendChatFile(file, text, panel, statusEl) {
  if (!file.size) throw new Error('empty file');
  const progressEl = document.getElementById('chat-upload-progress');
  const progressBar = document.getElementById('chat-upload-progress-bar');
  progressEl.classList.remove('hidden');
  progressBar.style.width = '0%';
  try {
    const initRes = await api('/api/uploads/init', {
      method: 'POST',
      body: JSON.stringify({
        instance_id: chatState.activeInstanceId,
        chat_id: panel.recipient,
        filename: file.name,
        total_size: file.size,
        mime: file.type || undefined,
        text,
      }),
    });
    const { session_id, chunk_size } = initRes;
    const headers = {};
    const token = getToken();
    if (token) headers['X-Dashboard-Token'] = token;
    let uploaded = 0;
    let index = 0;
    for (let offset = 0; offset < file.size; offset += chunk_size) {
      const chunk = file.slice(offset, offset + chunk_size);
      const res = await fetch(API_BASE + `/api/uploads/${session_id}/chunk/${index}`, { method: 'PUT', headers, body: chunk });
      if (!res.ok) throw new Error(await res.text());
      uploaded += chunk.size;
      index += 1;
      progressBar.style.width = Math.round((uploaded / file.size) * 100) + '%';
    }
    const completeRes = await fetch(API_BASE + `/api/uploads/${session_id}/complete`, { method: 'POST', headers });
    if (!completeRes.ok) throw new Error(await completeRes.text());
    const body = await completeRes.json();
    if (!body.relayed) {
      statusEl.textContent = 'File stored on the server (too large to relay through the bot) — pull it from any paired device.';
    }
  } finally {
    progressEl.classList.add('hidden');
  }
}
document.getElementById('btn-chat-send').onclick = sendChatMessage;
document.getElementById('chat-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChatMessage();
  }
});
attachSlashMenu(document.getElementById('chat-input'));

// ----------------------------------------------------------- server chat
// A permanent, bot-independent channel between this server's own devices
// (desktop + every paired phone) — see bot/db.py's server_chat_conversations
// comment. The desktop app is always device id 0 (db.SERVER_CHAT_DESKTOP_
// DEVICE_ID); every message not sent by us renders as "in" regardless of
// which other device sent it, same visual language as the platform Chat tab.
const SERVER_CHAT_MY_DEVICE_ID = 0;
const serverChatState = { conversations: [], activeId: null, lastId: 0 };
let serverChatPendingFile = null;

function serverChatInitials(name) {
  return (name || '?').trim().split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase();
}

function renderServerChatList() {
  const el = document.getElementById('serverchat-list');
  if (!serverChatState.conversations.length) {
    el.innerHTML = '<div class="chat-list-empty">No conversations yet.</div>';
    return;
  }
  el.innerHTML = serverChatState.conversations.map(c => {
    const preview = c.last_message
      ? esc(c.last_message.text || (c.last_message.attachment_name ? '📎 ' + c.last_message.attachment_name : ''))
      : 'No messages yet';
    return `
    <button class="chat-list-item ${c.id === serverChatState.activeId ? 'active' : ''}" data-serverchat-conv="${c.id}" type="button">
      <span class="chat-list-avatar">${esc(serverChatInitials(c.title))}</span>
      <span class="chat-list-txt">
        <span class="chat-list-name">${esc(c.title)}</span>
        <span class="chat-list-meta">${preview}</span>
      </span>
    </button>`;
  }).join('');
  el.querySelectorAll('[data-serverchat-conv]').forEach(btn => {
    btn.onclick = () => activateServerChatConversation(Number(btn.dataset.serverchatConv));
  });
}

async function refreshServerChatList() {
  try {
    serverChatState.conversations = await api('/api/server-chat/conversations');
  } catch (_e) { return; }
  renderServerChatList();
  if (serverChatState.activeId == null && serverChatState.conversations.length) {
    activateServerChatConversation(serverChatState.conversations[0].id);
  }
}

function activateServerChatConversation(id) {
  serverChatState.activeId = id;
  serverChatState.lastId = 0;
  document.getElementById('serverchat-panels').innerHTML = '';
  const conv = serverChatState.conversations.find(c => c.id === id);
  document.getElementById('serverchat-header-name').textContent = conv ? conv.title : 'Conversation';
  document.getElementById('serverchat-header-avatar').textContent = conv ? serverChatInitials(conv.title) : '—';
  renderServerChatList();
  refreshServerChatMessages();
}

document.getElementById('btn-serverchat-export').onclick = () => {
  if (!serverChatState.activeId) return;
  downloadUrl(`/api/server-chat/conversations/${serverChatState.activeId}/export`);
};
document.getElementById('btn-serverchat-clear').onclick = async () => {
  if (!serverChatState.activeId) return;
  const conv = serverChatState.conversations.find(c => c.id === serverChatState.activeId);
  if (!confirm(`Delete all history in "${conv ? conv.title : 'this conversation'}"? This permanently removes its messages and attachments — export first if you want a copy.`)) return;
  await api(`/api/server-chat/conversations/${serverChatState.activeId}`, { method: 'DELETE' });
  document.getElementById('serverchat-panels').innerHTML = '';
  serverChatState.lastId = 0;
  refreshServerChatList();
};

async function downloadServerChatAttachment(messageId, name) {
  try {
    const headers = {};
    const token = getToken();
    if (token) headers['X-Dashboard-Token'] = token;
    const res = await fetch(API_BASE + `/api/server-chat/attachments/${messageId}`, { headers });
    if (!res.ok) throw new Error(await res.text());
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name || 'file';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('Download failed — check the dashboard token.');
  }
}

function appendServerChatMessages(rows) {
  const win = document.getElementById('serverchat-panels');
  const atBottom = win.scrollHeight - win.scrollTop - win.clientHeight < 60;
  rows.forEach(m => {
    const row = document.createElement('div');
    row.className = 'chat-row ' + (m.sender_device_id === SERVER_CHAT_MY_DEVICE_ID ? 'out' : 'in');
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble';
    if (m.text) {
      const textEl = document.createElement('div');
      textEl.textContent = m.text;
      bubble.appendChild(textEl);
    }
    if (m.attachment_path) {
      const att = document.createElement('a');
      att.className = 'chat-attachment';
      att.textContent = '📎 ' + (m.attachment_name || 'file') + (m.attachment_size ? ` (${fmtBytes(m.attachment_size)})` : '');
      att.href = '#';
      att.onclick = (e) => { e.preventDefault(); downloadServerChatAttachment(m.id, m.attachment_name); };
      bubble.appendChild(att);
    }
    const meta = document.createElement('span');
    meta.className = 'chat-meta';
    meta.textContent = fmtChatTime(m.ts);
    bubble.appendChild(meta);
    row.appendChild(bubble);
    win.appendChild(row);
    serverChatState.lastId = Math.max(serverChatState.lastId, m.id);
  });
  if (rows.length && atBottom) win.scrollTop = win.scrollHeight;
}

async function refreshServerChatMessages() {
  if (serverChatState.activeId == null) return;
  try {
    const rows = await api(`/api/server-chat/messages?conversation_id=${serverChatState.activeId}&after_id=${serverChatState.lastId}&limit=200`);
    if (rows.length) appendServerChatMessages(rows);
  } catch (_e) { /* token not set yet, or server not ready — try again next tick */ }
}

async function sendServerChatMessage() {
  const input = document.getElementById('serverchat-input');
  const statusEl = document.getElementById('serverchat-status');
  const text = input.value.trim();
  if (!text && !serverChatPendingFile) return;
  if (serverChatState.activeId == null) {
    statusEl.textContent = 'Pick a conversation first.';
    return;
  }
  const btn = document.getElementById('btn-serverchat-send');
  btn.disabled = true;
  statusEl.textContent = '';
  try {
    if (serverChatPendingFile) {
      const fd = new FormData();
      fd.append('conversation_id', serverChatState.activeId);
      fd.append('text', text);
      fd.append('file', serverChatPendingFile);
      const headers = {};
      const token = getToken();
      if (token) headers['X-Dashboard-Token'] = token;
      const res = await fetch(API_BASE + '/api/server-chat/send-file', { method: 'POST', headers, body: fd });
      if (!res.ok) throw new Error(await res.text());
      serverChatPendingFile = null;
      document.getElementById('serverchat-file-input').value = '';
      document.getElementById('serverchat-pending-attachment').classList.add('hidden');
    } else {
      await api('/api/server-chat/send', { method: 'POST', body: JSON.stringify({ conversation_id: serverChatState.activeId, text }) });
    }
    input.value = '';
    await refreshServerChatMessages();
    await refreshServerChatList();
  } catch (e) {
    statusEl.textContent = 'Send failed — check the dashboard token.';
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('btn-serverchat-send').onclick = sendServerChatMessage;
document.getElementById('serverchat-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendServerChatMessage();
  }
});
document.getElementById('btn-serverchat-attach').onclick = () => document.getElementById('serverchat-file-input').click();
document.getElementById('serverchat-file-input').onchange = (e) => {
  serverChatPendingFile = e.target.files[0] || null;
  const box = document.getElementById('serverchat-pending-attachment');
  if (serverChatPendingFile) {
    document.getElementById('serverchat-pending-name').textContent = serverChatPendingFile.name;
    box.classList.remove('hidden');
  } else {
    box.classList.add('hidden');
  }
};
document.getElementById('btn-serverchat-attach-clear').onclick = () => {
  serverChatPendingFile = null;
  document.getElementById('serverchat-file-input').value = '';
  document.getElementById('serverchat-pending-attachment').classList.add('hidden');
};

function startServerChatPolling() {
  refreshServerChatList();
  pollWhenVisible(refreshServerChatMessages, 2000);
  pollWhenVisible(refreshServerChatList, 15000);
}

// --------------------------------------------------------------- sessions
const sessionsState = { list: [], activeId: null };

function fmtSessionTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d)) return ts;
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function instanceNameFor(instanceId) {
  const inst = (chatState.instances || []).find(i => i.id === instanceId);
  return inst ? inst.name : `instance ${instanceId}`;
}

async function refreshSessionsInstanceFilter() {
  const select = document.getElementById('sessions-instance');
  const current = select.value;
  const instances = chatState.instances || (await api('/api/chat/recipients')).instances || [];
  select.innerHTML = '<option value="">All bots</option>' + instances.map(i => `<option value="${i.id}">${esc(i.name)}</option>`).join('');
  select.value = current;
}

function renderSessionsList() {
  const list = document.getElementById('sessions-list');
  if (!sessionsState.list.length) {
    list.innerHTML = '<p class="cardnote">No sessions yet — start a conversation in the Chat tab.</p>';
    return;
  }
  list.innerHTML = sessionsState.list.map(s => `
    <div class="session-row" data-session-id="${esc(String(s.id))}">
      <div>
        <div class="st">${esc(s.title || 'Untitled')}</div>
        <div class="sm">${esc(instanceNameFor(s.instance_id))} · ${s.item_count} item${s.item_count === 1 ? '' : 's'}</div>
      </div>
      <div class="sm" style="display:flex; align-items:center; gap:10px;">
        ${esc(fmtSessionTime(s.last_activity_at))}
        <button class="btn small" data-session-row-export="${esc(String(s.id))}" title="Download this session as JSON">⬇</button>
        <button class="btn small" data-session-row-delete="${esc(String(s.id))}" title="Delete this session permanently" style="color:var(--critical);">🗑</button>
      </div>
    </div>
  `).join('');
  list.querySelectorAll('.session-row').forEach(row => {
    row.onclick = () => openSession(row.dataset.sessionId);
  });
  list.querySelectorAll('[data-session-row-export]').forEach(btn => btn.onclick = (e) => {
    e.stopPropagation();
    downloadUrl(`/api/sessions/${encodeURIComponent(btn.dataset.sessionRowExport)}/export`);
  });
  list.querySelectorAll('[data-session-row-delete]').forEach(btn => btn.onclick = async (e) => {
    e.stopPropagation();
    const id = btn.dataset.sessionRowDelete;
    const row = sessionsState.list.find(s => String(s.id) === id);
    if (!confirm(`Delete session "${row ? (row.title || 'Untitled') : id}"? This permanently removes its messages and jobs — export first if you want a copy.`)) return;
    await api(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (sessionsState.activeId === id) document.getElementById('session-detail').classList.add('hidden');
    refreshSessions();
  });
}

async function refreshSessions() {
  const instanceId = document.getElementById('sessions-instance').value;
  const q = document.getElementById('sessions-search').value.trim();
  const since = document.getElementById('sessions-since').value;
  const until = document.getElementById('sessions-until').value;
  const params = new URLSearchParams();
  if (instanceId) params.set('instance_id', instanceId);
  if (q) params.set('q', q);
  if (since) params.set('since', since);
  if (until) params.set('until', until);
  try {
    sessionsState.list = await api('/api/sessions?' + params.toString());
    renderSessionsList();
  } catch (_e) { /* token not set yet */ }
}

function renderSessionItem(kind, item) {
  const row = document.createElement('div');
  const bubble = document.createElement('div');
  if (kind === 'message') {
    row.className = 'chat-row ' + (item.direction === 'in' ? 'in' : 'out');
    bubble.className = 'chat-bubble' + (item.source === 'dashboard' ? ' from-dashboard' : '');
    if (item.text) bubble.appendChild(Object.assign(document.createElement('div'), { textContent: item.text }));
    if (item.attachment_path) {
      if (item.thumbnail_path) {
        const img = document.createElement('img');
        img.className = 'chat-thumb';
        img.alt = item.attachment_name || 'image';
        img.onclick = () => downloadAttachment(item.id, item.attachment_name);
        bubble.appendChild(img);
        loadThumbnail(img, item.id);
      }
      const att = document.createElement('a');
      att.className = 'chat-attachment';
      att.textContent = '📎 ' + (item.attachment_name || 'file') + (item.attachment_size ? ` (${fmtBytes(item.attachment_size)})` : '');
      att.href = '#';
      att.onclick = (e) => { e.preventDefault(); downloadAttachment(item.id, item.attachment_name); };
      bubble.appendChild(att);
    }
    const meta = document.createElement('span');
    meta.className = 'chat-meta';
    meta.textContent = [fmtChatTime(item.ts), item.platform].filter(Boolean).join(' · ');
    bubble.appendChild(meta);
  } else {
    row.className = 'chat-row in';
    const promptBubble = bubble;
    promptBubble.className = 'chat-bubble job-prompt';
    promptBubble.appendChild(Object.assign(document.createElement('div'), { innerHTML: '<span class="chat-kind">Ask</span>' }));
    promptBubble.appendChild(Object.assign(document.createElement('div'), { textContent: item.prompt || '' }));
    const meta = document.createElement('span');
    meta.className = 'chat-meta';
    meta.textContent = [fmtChatTime(item.created_at), item.backend, item.status].filter(Boolean).join(' · ');
    promptBubble.appendChild(meta);
  }
  row.appendChild(bubble);
  return row;
}

async function openSession(sessionId) {
  sessionsState.activeId = sessionId;
  const detail = await api('/api/sessions/' + encodeURIComponent(sessionId));
  const card = document.getElementById('session-detail');
  const items = document.getElementById('session-detail-items');
  document.getElementById('session-detail-title').textContent = detail.session.title || 'Session';
  document.getElementById('session-detail-meta').textContent =
    `${instanceNameFor(detail.session.instance_id)} · ${detail.messages.length + detail.jobs.length} item(s)`;
  const timeline = [
    ...detail.messages.map(m => ({ kind: 'message', ts: m.ts || m.created_at, item: m })),
    ...detail.jobs.map(j => ({ kind: 'job', ts: j.created_at, item: j })),
  ].sort((a, b) => new Date(a.ts) - new Date(b.ts));
  items.innerHTML = '';
  const win = document.createElement('div');
  win.className = 'chat-window active';
  timeline.forEach(t => win.appendChild(renderSessionItem(t.kind, t.item)));
  items.appendChild(win);
  card.classList.remove('hidden');
  card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  document.getElementById('btn-session-export').onclick = () => downloadUrl(`/api/sessions/${encodeURIComponent(sessionId)}/export`);
  document.getElementById('btn-session-delete').onclick = async () => {
    if (!confirm(`Delete session "${detail.session.title || 'Untitled'}"? This permanently removes its messages and jobs — export first if you want a copy.`)) return;
    await api(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
    card.classList.add('hidden');
    refreshSessions();
  };
  document.getElementById('btn-session-continue').onclick = () => {
    const instanceId = detail.session.instance_id;
    if (!instanceId) return;
    document.getElementById('chat').scrollIntoView({ behavior: 'smooth' });
    switchToInstance(instanceId);
    const lastMsg = detail.messages[detail.messages.length - 1];
    if (lastMsg) {
      setTimeout(() => {
        const target = document.querySelector(`#chat-panels .chat-window[data-instance-id="${instanceId}"] [data-msg-id="${lastMsg.id}"]`);
        if (target) target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }, 400);
    }
  };
}

document.getElementById('btn-session-close').onclick = () => {
  document.getElementById('session-detail').classList.add('hidden');
};
document.getElementById('btn-sessions-export-all').onclick = () => {
  const instanceId = document.getElementById('sessions-instance').value;
  const params = instanceId ? `?instance_id=${encodeURIComponent(instanceId)}` : '';
  downloadUrl(`/api/sessions/export${params}`);
};
document.getElementById('sessions-instance').onchange = refreshSessions;
document.getElementById('sessions-since').onchange = refreshSessions;
document.getElementById('sessions-until').onchange = refreshSessions;
let sessionsSearchTimer = null;
document.getElementById('sessions-search').oninput = () => {
  clearTimeout(sessionsSearchTimer);
  sessionsSearchTimer = setTimeout(refreshSessions, 300);
};

async function startSessionsPolling() {
  await refreshSessionsInstanceFilter();
  refreshSessions();
  pollWhenVisible(refreshSessions, 15000);
}

// -------------------------------------------------------------- platforms
async function refreshPlatforms() {
  const container = document.getElementById('platforms-list');
  try {
    const status = await api('/api/platforms/status');
    container.innerHTML = Object.entries(status).map(([key, p]) => `
      <div class="card" style="margin-top:14px;">
        <div class="platform-head">
          <h3>${esc(p.label)}</h3>
          <span class="pill"><span class="dot ${p.configured ? 'good' : ''}"></span>${p.configured ? 'Configured' : 'Not configured'}</span>
        </div>
        <details class="platform-guide">
          <summary>Setup guide</summary>
          <ol>${p.setup_guide.map(step => `<li>${esc(step)}</li>`).join('')}</ol>
        </details>
        <div data-platform-fields="${key}">
          ${Object.entries(p.fields).map(([fkey, f]) => `
            <div class="wizard-field">
              <label>${esc(f.label)}</label>
              <div class="help">${esc(f.help)}</div>
              <div class="row">
                <input type="text" data-field="${fkey}" placeholder="${f.present ? 'already set — leave blank to keep' : ''}" autocomplete="off" spellcheck="false">
              </div>
              ${f.present ? `<div class="msg ${f.valid ? 'good' : 'bad'}">${esc(f.message)}</div>` : ''}
            </div>`).join('')}
        </div>
        <div class="wizard-foot">
          <span class="cardnote" data-platform-status="${key}"></span>
          <button class="btn primary" data-save-platform="${key}">Save ${esc(p.label)}</button>
        </div>
      </div>`).join('');

    container.querySelectorAll('[data-save-platform]').forEach(btn => btn.onclick = async () => {
      const key = btn.dataset.savePlatform;
      const fieldsEl = container.querySelector(`[data-platform-fields="${key}"]`);
      const payload = {};
      fieldsEl.querySelectorAll('input[data-field]').forEach(inp => {
        if (inp.value.trim()) payload[inp.dataset.field] = inp.value.trim();
      });
      const statusEl = container.querySelector(`[data-platform-status="${key}"]`);
      statusEl.textContent = 'Saving…';
      try {
        await api('/api/platforms/apply', { method: 'POST', body: JSON.stringify(payload) });
        statusEl.textContent = 'Saved — restart the server for this to take effect.';
        refreshPlatforms();
        refreshChatRecipients();
      } catch (e) {
        statusEl.textContent = 'Save failed — check the dashboard token and try again.';
      }
    });
  } catch (_e) { /* token not set yet */ }
}

// ------------------------------------------------------------- mobile ----
function fmtMobileTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  return isNaN(d) ? ts : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

// keys: everything issued (from /api/mobile-keys, desktop-only, includes
// revoked). devices: live presence for currently-paired ones only (from
// /api/devices or pushed over the WebSocket) — kept as separate state so a
// WS delta can re-render presence without re-fetching the full key list.
// edits: label text the user has typed but not yet saved, keyed by key id —
// the table re-renders on a 15s poll and on every presence push, which
// would otherwise wipe an in-progress edit out from under someone mid-type.
const mobileKeysState = { keys: [], devices: [], edits: {} };

function renderMobileKeysTable() {
  const tbody = document.getElementById('mobile-keys-tbody');
  const presenceById = new Map(mobileKeysState.devices.map(d => [d.id, d]));
  const keys = mobileKeysState.keys;
  tbody.innerHTML = keys.length ? keys.map(k => {
    const presence = presenceById.get(k.id);
    const online = !k.revoked_at && !!(presence && presence.online);
    const statusLabel = k.revoked_at ? 'Revoked' : (online ? 'Online' : 'Offline');
    const statusDot = k.revoked_at ? '' : (online ? 'good' : '');
    // Real hardware identity (device_model/os_version — reported by the app
    // itself via X-Device-Model/X-Device-OS-Version on every request) takes
    // priority over the bare platform string and over a generic user-typed
    // label like "Browser Test Phone" — falls back gracefully for devices
    // that haven't reported one yet (older presence rows, or a key that's
    // never actually been used).
    const deviceInfo = presence && presence.device_model
      ? esc(presence.device_model) + (presence.os_version ? ` <span class="cardnote" style="display:inline;">(${esc(presence.os_version)})</span>` : '')
      : (presence && presence.platform ? esc(presence.platform) : '—');
    const lastSeen = presence && presence.last_seen ? fmtMobileTime(presence.last_seen) : '—';
    return `
    <tr>
      <td><input type="text" class="mono" data-mobile-label="${k.id}" value="${esc(mobileKeysState.edits[k.id] ?? k.label)}" ${k.revoked_at ? 'disabled' : ''} style="width:100%; background:transparent; border:1px solid transparent; border-radius:5px; padding:3px 5px;" onfocus="this.style.borderColor='var(--line)'" onblur="this.style.borderColor='transparent'"></td>
      <td>${deviceInfo}</td>
      <td class="mono">${fmtMobileTime(k.created_at)}</td>
      <td class="mono">${lastSeen}</td>
      <td><span class="pill"><span class="dot ${statusDot}"></span>${statusLabel}</span></td>
      <td>${k.revoked_at ? '' : `
        <button class="btn" data-mobile-send-apk="${k.id}" style="padding:3px 8px; font-size:11px;">Send APK</button>
        <button class="btn" data-mobile-revoke="${k.id}" style="padding:3px 8px; font-size:11px;">Revoke</button>`}</td>
    </tr>`;
  }).join('') : '<tr class="emptyrow"><td colspan="6">No devices paired yet.</td></tr>';

  tbody.querySelectorAll('[data-mobile-label]').forEach(el => el.oninput = () => {
    mobileKeysState.edits[el.dataset.mobileLabel] = el.value;
  });

  tbody.querySelectorAll('[data-mobile-revoke]').forEach(btn => btn.onclick = async () => {
    if (!confirm('Revoke this device\'s key? It loses access to chat/sessions/jobs/bots immediately.')) return;
    delete mobileKeysState.edits[btn.dataset.mobileRevoke];
    await api(`/api/mobile-keys/${btn.dataset.mobileRevoke}`, { method: 'DELETE' });
    refreshMobileKeys();
  });

  tbody.querySelectorAll('[data-mobile-send-apk]').forEach(btn => btn.onclick = async () => {
    const statusEl = document.getElementById('mobile-devices-status');
    btn.disabled = true;
    try {
      await api('/api/android/apk/send', { method: 'POST', body: JSON.stringify({ api_key_id: Number(btn.dataset.mobileSendApk) }) });
      statusEl.textContent = 'Queued — the device will download it next time the app is open.';
    } catch (e) {
      statusEl.textContent = `Failed to send: ${e.message}`;
    } finally {
      btn.disabled = false;
    }
  });
}

document.getElementById('btn-mobile-send-apk-all').onclick = async () => {
  const statusEl = document.getElementById('mobile-devices-status');
  const btn = document.getElementById('btn-mobile-send-apk-all');
  btn.disabled = true;
  try {
    const res = await api('/api/android/apk/send-all', { method: 'POST' });
    statusEl.textContent = `Queued for ${res.sent_to} device(s) — each downloads it next time the app is open.`;
  } catch (e) {
    statusEl.textContent = `Failed to send: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
};

document.getElementById('btn-mobile-save').onclick = async () => {
  const statusEl = document.getElementById('mobile-devices-status');
  const byId = new Map(mobileKeysState.keys.map(k => [String(k.id), k]));
  const changed = Object.entries(mobileKeysState.edits).filter(([id, value]) => {
    const current = byId.get(id);
    return current && value.trim() && value.trim() !== current.label;
  });
  if (!changed.length) {
    statusEl.textContent = 'Nothing to save — no labels were changed.';
    return;
  }
  statusEl.textContent = `Saving ${changed.length} label(s)…`;
  try {
    await Promise.all(changed.map(([id, value]) =>
      api(`/api/mobile-keys/${id}`, { method: 'PUT', body: JSON.stringify({ label: value.trim() }) })
    ));
    changed.forEach(([id]) => { delete mobileKeysState.edits[id]; });
    statusEl.textContent = `Saved ${changed.length} label(s).`;
    refreshMobileKeys();
  } catch (e) {
    statusEl.textContent = `Failed to save: ${e.message}`;
  }
};

document.getElementById('btn-mobile-clear-revoked').onclick = async () => {
  const statusEl = document.getElementById('mobile-devices-status');
  const revokedCount = mobileKeysState.keys.filter(k => k.revoked_at).length;
  if (!revokedCount) {
    statusEl.textContent = 'No revoked devices to clear.';
    return;
  }
  if (!confirm(`Permanently remove ${revokedCount} revoked device(s) from this list? This can't be undone (the devices already lost access when revoked — this only tidies the list).`)) return;
  try {
    const res = await api('/api/mobile-keys/purge-revoked', { method: 'POST' });
    statusEl.textContent = `Cleared ${res.purged} revoked device(s).`;
    refreshMobileKeys();
  } catch (e) {
    statusEl.textContent = `Failed to clear: ${e.message}`;
  }
};

async function refreshMobileKeys() {
  try {
    mobileKeysState.keys = await api('/api/mobile-keys');
  } catch (_e) { return; }
  try {
    mobileKeysState.devices = await api('/api/devices');
  } catch (_e) { /* presence is best-effort — the table still renders with keys alone */ }
  renderMobileKeysTable();
}

document.getElementById('btn-mobile-generate').onclick = async () => {
  const label = document.getElementById('mobile-new-label').value.trim() || 'Unnamed device';
  const host = document.getElementById('mobile-new-host').value.trim();
  const host2 = document.getElementById('mobile-new-host2').value.trim();
  const btn = document.getElementById('btn-mobile-generate');
  btn.disabled = true;
  try {
    const res = await api('/api/mobile-keys', { method: 'POST', body: JSON.stringify({ label, host, host2 }) });
    document.getElementById('mobile-new-key').textContent = res.key;
    document.getElementById('mobile-new-qr').src = `data:image/png;base64,${res.qr_png_base64}`;
    document.getElementById('mobile-new-result').classList.remove('hidden');
    document.getElementById('mobile-new-label').value = '';
    refreshMobileKeys();
  } catch (e) {
    alert('Failed to generate key — check the dashboard token.');
  } finally {
    btn.disabled = false;
  }
};

// ---------------------------------------------------------- linked servers
let peersCache = [];

// api()'s thrown Error carries the raw response body (FastAPI's
// {"detail": "..."} JSON), not a plain message — unwrap it so the peer
// link form's status line reads as a real sentence instead of raw JSON.
function _peerErrorDetail(raw) {
  try {
    const parsed = JSON.parse(raw);
    return (parsed && parsed.detail) || raw;
  } catch (_e) {
    return raw;
  }
}

async function refreshPeers() {
  const tbody = document.getElementById('peers-tbody');
  try {
    peersCache = await api('/api/peers');
  } catch (_e) { return; }
  tbody.innerHTML = peersCache.length ? peersCache.map(p => `
    <tr>
      <td>${esc(p.name)}</td>
      <td class="mono">${esc(p.base_url) || '<em>unknown (can\'t call back)</em>'}</td>
      <td class="mono">${fmtMobileTime(p.linked_at)}</td>
      <td>${p.last_error
        ? `<span class="pill"><span class="dot critical"></span>Unreachable</span>`
        : `<span class="pill"><span class="dot good"></span>Online</span>`}</td>
      <td>
        <button class="btn" data-peer-view="${p.id}" style="padding:3px 8px; font-size:11px;" ${p.base_url ? '' : 'disabled title="no known address"'}>Manage bots</button>
        <button class="btn danger" data-peer-unlink="${p.id}" style="padding:3px 8px; font-size:11px;">Unlink</button>
      </td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="5">No linked servers yet.</td></tr>';

  tbody.querySelectorAll('[data-peer-unlink]').forEach(btn => btn.onclick = async () => {
    if (!confirm('Unlink this server? It will no longer be able to reach this dashboard, and you\'ll no longer be able to manage its bots from here.')) return;
    await api(`/api/peers/${btn.dataset.peerUnlink}`, { method: 'DELETE' });
    refreshPeers();
  });

  tbody.querySelectorAll('[data-peer-view]').forEach(btn => btn.onclick = () => openPeerBots(Number(btn.dataset.peerView)));
}

async function openPeerBots(peerId) {
  const peer = peersCache.find(p => p.id === peerId);
  if (!peer) return;
  const card = document.getElementById('peer-bots-card');
  const tbody = document.getElementById('peer-bots-tbody');
  const statusEl = document.getElementById('peer-bots-status');
  document.getElementById('peer-bots-server-name').textContent = peer.name;
  card.classList.remove('hidden');
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  tbody.innerHTML = '<tr class="emptyrow"><td colspan="5">Loading…</td></tr>';
  statusEl.textContent = '';

  let bots;
  try {
    bots = await api(`/api/peers/${peerId}/bots`);
  } catch (e) {
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="5">Could not reach this server.</td></tr>';
    return;
  }
  tbody.innerHTML = bots.length ? bots.map(b => `
    <tr>
      <td>${esc(b.name)}</td>
      <td>${esc(b.platform)}</td>
      <td>${esc(b.backend)}</td>
      <td><span class="pill"><span class="dot ${b.enabled ? 'good' : ''}"></span>${b.enabled ? 'Enabled' : 'Disabled'}</span></td>
      <td>
        <button class="btn" data-peer-bot-action="${peerId}:${b.id}:${b.enabled ? 'disable' : 'enable'}" style="padding:3px 8px; font-size:11px;">${b.enabled ? 'Disable' : 'Enable'}</button>
        <button class="btn" data-peer-bot-action="${peerId}:${b.id}:restart" style="padding:3px 8px; font-size:11px;">Restart</button>
      </td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="5">No bots on this server yet.</td></tr>';

  tbody.querySelectorAll('[data-peer-bot-action]').forEach(btn => btn.onclick = async () => {
    const [pid, bid, action] = btn.dataset.peerBotAction.split(':');
    btn.disabled = true;
    try {
      await api(`/api/peers/${pid}/bots/${bid}/${action}`, { method: 'POST' });
      openPeerBots(Number(pid));
    } catch (e) {
      statusEl.textContent = `Failed: ${_peerErrorDetail(e.message)}`;
      btn.disabled = false;
    }
  });
}

document.getElementById('btn-peer-bots-close').onclick = () => {
  document.getElementById('peer-bots-card').classList.add('hidden');
};

document.getElementById('btn-peer-link').onclick = async () => {
  const name = document.getElementById('peer-new-name').value.trim();
  const pairing_token = document.getElementById('peer-new-token').value.trim();
  const my_base_url = document.getElementById('peer-my-url').value.trim() || undefined;
  const statusEl = document.getElementById('peer-link-status');
  if (!name || !pairing_token) {
    statusEl.textContent = 'Name and its pairing token are both required.';
    return;
  }
  const btn = document.getElementById('btn-peer-link');
  btn.disabled = true;
  statusEl.textContent = 'Linking…';
  try {
    const res = await api('/api/peers/link', { method: 'POST', body: JSON.stringify({ name, pairing_token, my_base_url }) });
    statusEl.textContent = `Linked to ${res.peer.name}.`;
    document.getElementById('peer-new-name').value = '';
    document.getElementById('peer-new-token').value = '';
    document.getElementById('peer-my-url').value = '';
    refreshPeers();
  } catch (e) {
    statusEl.textContent = `Failed to link: ${_peerErrorDetail(e.message)}`;
  } finally {
    btn.disabled = false;
  }
};

// Pre-fill the optional "let it call you back" field with this server's
// own auto-detected address — a real query to the backend (which knows
// its actual outbound-facing LAN IP), not a guess from the page's own URL.
// This works correctly even in the Tauri desktop app, where the webview's
// own origin is meaningless for this (it always talks to its own dashboard
// over 127.0.0.1 via API_BASE) but the query itself still reaches the real
// backend process and gets its real LAN IP back. Silently left blank if
// detection fails (offline machine) — the field is optional.
(async function prefillOwnAddress() {
  try {
    const res = await api('/api/peers/self-address');
    if (res.base_url) {
      const myUrlEl = document.getElementById('peer-my-url');
      if (myUrlEl && !myUrlEl.value) myUrlEl.value = res.base_url;
    }
  } catch (_e) { /* not logged in yet, or detection failed — leave blank */ }
})();

// Surfaces the single most common reason linking silently fails:
// DASHBOARD_HOST=0.0.0.0 makes the app itself listen on every interface,
// but does nothing to the OS firewall, which drops unsolicited inbound
// connections by default — a timeout on the OTHER end, not a clean error
// here, which is exactly what made this hard to diagnose the first time.
// Windows-only (see bot/firewall.py); silently hidden elsewhere.
async function refreshFirewallStatus() {
  const row = document.getElementById('peer-firewall-row');
  const pill = document.getElementById('peer-firewall-pill');
  const note = document.getElementById('peer-firewall-note');
  const btn = document.getElementById('btn-peer-firewall-open');
  try {
    const res = await api('/api/peers/firewall-status');
    if (!res.supported) { row.style.display = 'none'; return; }
    row.style.display = '';
    if (res.rule_present === true) {
      pill.innerHTML = '<span class="dot good"></span>Firewall OK';
      note.textContent = `An inbound rule for TCP ${res.port} is present.`;
      btn.style.display = 'none';
    } else if (res.rule_present === false) {
      pill.innerHTML = '<span class="dot critical"></span>Firewall blocking';
      note.textContent = `No inbound rule found for TCP ${res.port} — other devices on your network likely can't reach you.`;
      btn.style.display = '';
    } else {
      pill.innerHTML = '<span class="dot warning"></span>Firewall unknown';
      note.textContent = `Couldn't determine whether TCP ${res.port} is open.`;
      btn.style.display = '';
    }
  } catch (_e) { row.style.display = 'none'; }
}
refreshFirewallStatus();

document.getElementById('btn-peer-firewall-open').onclick = async () => {
  const btn = document.getElementById('btn-peer-firewall-open');
  const note = document.getElementById('peer-firewall-note');
  btn.disabled = true;
  note.textContent = 'Waiting for the Windows UAC prompt — approve it to add the rule…';
  try {
    const res = await api('/api/peers/firewall-open', { method: 'POST' });
    note.textContent = res.ok ? res.message : `Failed: ${res.message}`;
  } catch (e) {
    note.textContent = `Failed: ${_peerErrorDetail(e.message)}`;
  } finally {
    btn.disabled = false;
    refreshFirewallStatus();
  }
};

// One shared countdown so re-clicking "Generate" replaces the old timer
// instead of stacking a second one ticking against a token that's already
// been superseded (server only ever keeps one pending token alive too).
let _peerTokenCountdownTimer = null;

document.getElementById('btn-peer-gen-token').onclick = async () => {
  const btn = document.getElementById('btn-peer-gen-token');
  const valueEl = document.getElementById('peer-gen-token-value');
  const expiryEl = document.getElementById('peer-gen-token-expiry');
  // Blank unless the Advanced override is filled in — the server
  // auto-detects its own address when none is given.
  const base_url = document.getElementById('peer-gen-token-address').value.trim() || undefined;
  btn.disabled = true;
  expiryEl.textContent = 'Generating…';
  try {
    const res = await api('/api/peers/pairing-token', { method: 'POST', body: JSON.stringify({ base_url }) });
    valueEl.value = res.pairing_token;
    valueEl.select();
    const expiresAt = new Date(res.expires_at).getTime();
    if (_peerTokenCountdownTimer) clearInterval(_peerTokenCountdownTimer);
    const tick = () => {
      const remainingMs = expiresAt - Date.now();
      if (remainingMs <= 0) {
        expiryEl.textContent = `for ${res.base_url} — expired, generate a new one`;
        clearInterval(_peerTokenCountdownTimer);
        return;
      }
      const m = Math.floor(remainingMs / 60000);
      const s = Math.floor((remainingMs % 60000) / 1000).toString().padStart(2, '0');
      expiryEl.textContent = `for ${res.base_url} — expires in ${m}:${s} or as soon as it's used`;
    };
    tick();
    _peerTokenCountdownTimer = setInterval(tick, 1000);
  } catch (e) {
    expiryEl.textContent = `Failed: ${_peerErrorDetail(e.message)}`;
  } finally {
    btn.disabled = false;
  }
};

// ----------------------------------------------------------- training ----
let trainingIntents = [];

async function refreshTrainingHealth() {
  let h;
  try {
    h = await api('/api/support-bot/health');
  } catch (_e) { return; }
  document.getElementById('health-total').textContent = h.total;
  document.getElementById('health-agreement').textContent = h.total ? `${(h.agreement_rate * 100).toFixed(0)}%` : '—';
  document.getElementById('health-unknown').textContent = h.total ? `${(h.unknown_rate * 100).toFixed(0)}%` : '—';
  document.getElementById('health-tfidf-conf').textContent = h.total ? h.avg_tfidf_confidence.toFixed(2) : '—';
  document.getElementById('health-nn-conf').textContent = h.total ? h.avg_nn_confidence.toFixed(2) : '—';
}

async function refreshTrainingPhrases() {
  let data;
  try {
    data = await api('/api/support-bot/training');
  } catch (_e) { return; }
  trainingIntents = data.intents || [];
  const select = document.getElementById('training-intent-select');
  const prevValue = select.value;
  select.innerHTML = trainingIntents.map(i => `<option value="${esc(i)}">${esc(i)}</option>`).join('');
  if (trainingIntents.includes(prevValue)) select.value = prevValue;

  const tbody = document.getElementById('training-phrases-tbody');
  const phrases = data.phrases || [];
  tbody.innerHTML = phrases.length ? phrases.map(p => `
    <tr>
      <td>${esc(p.phrase)}</td>
      <td class="mono">${esc(p.intent)}</td>
      <td><button class="btn" data-phrase-delete="${p.id}" style="padding:3px 8px; font-size:11px;">Delete</button></td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="3">No custom phrases added yet.</td></tr>';
  document.querySelectorAll('[data-phrase-delete]').forEach(btn => btn.onclick = async () => {
    await api(`/api/support-bot/training/${btn.dataset.phraseDelete}`, { method: 'DELETE' });
    refreshTrainingPhrases();
  });
}

document.getElementById('btn-training-add').onclick = async () => {
  const phraseInput = document.getElementById('training-phrase-input');
  const statusEl = document.getElementById('training-status');
  const phrase = phraseInput.value.trim();
  const intent = document.getElementById('training-intent-select').value;
  if (!phrase || !intent) {
    statusEl.textContent = 'Enter a phrase and pick an intent.';
    return;
  }
  try {
    await api('/api/support-bot/training', { method: 'POST', body: JSON.stringify({ phrase, intent }) });
    phraseInput.value = '';
    statusEl.textContent = 'Added and retrained.';
    refreshTrainingPhrases();
  } catch (e) {
    statusEl.textContent = 'Failed to add phrase — check the dashboard token.';
  }
};

// ------------------------------------------------------------ updates ----
let latestUpdateInfo = null;

async function checkForUpdate(showBusyState) {
  if (!IS_TAURI) return;
  const { invoke } = window.__TAURI__.core;
  const statusEl = document.getElementById('update-status');
  if (showBusyState) statusEl.textContent = 'Checking GitHub for the latest release…';
  try {
    const info = await invoke('check_for_update');
    latestUpdateInfo = info;
    document.getElementById('update-current-version').textContent = info.current_version;
    document.getElementById('update-latest-version').textContent = info.latest_version;
    document.getElementById('update-available-pill').classList.toggle('hidden', !info.update_available);
    document.getElementById('update-uptodate-pill').classList.toggle('hidden', info.update_available);
    document.getElementById('btn-update-install').classList.toggle('hidden', !info.update_available || !info.download_url);
    const notesWrap = document.getElementById('update-notes-wrap');
    if (info.update_available && info.release_notes) {
      document.getElementById('update-notes').textContent = info.release_notes;
      notesWrap.classList.remove('hidden');
    } else {
      notesWrap.classList.add('hidden');
    }
    statusEl.textContent = info.update_available
      ? `Version ${info.latest_version} is available.`
      : 'You\'re on the latest version.';
  } catch (e) {
    statusEl.textContent = `Couldn't check for updates: ${e}`;
  }
}

function formatMB(bytes) {
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function setUpdateProgress(percent) {
  const wrap = document.getElementById('update-progress-wrap');
  const bar = document.getElementById('update-progress-bar');
  if (percent === null) {
    wrap.classList.add('hidden');
    return;
  }
  wrap.classList.remove('hidden');
  bar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
}

async function installUpdateFlow() {
  if (!latestUpdateInfo || !latestUpdateInfo.download_url) return;
  const proceed = confirm(
    `Download and install version ${latestUpdateInfo.latest_version}?\n\n` +
    'The installer will run silently, then this app will restart automatically. ' +
    'Any unsaved work in other apps is unaffected — this only restarts Bot Server.'
  );
  if (!proceed) return;
  const { invoke } = window.__TAURI__.core;
  const { listen } = window.__TAURI__.event;
  const statusEl = document.getElementById('update-status');
  const installBtn = document.getElementById('btn-update-install');
  installBtn.disabled = true;

  const unlistenProgress = await listen('update-download-progress', (event) => {
    const { downloaded_bytes, total_bytes, percent } = event.payload;
    setUpdateProgress(percent !== null && percent !== undefined ? percent : null);
    statusEl.textContent = total_bytes
      ? `Downloading installer… ${formatMB(downloaded_bytes)} / ${formatMB(total_bytes)} (${Math.round(percent)}%)`
      : `Downloading installer… ${formatMB(downloaded_bytes)}`;
  });
  const unlistenPhase = await listen('update-phase', (event) => {
    if (event.payload === 'installing') {
      setUpdateProgress(null);
      statusEl.textContent = 'Running the installer and restarting…';
    }
  });

  try {
    statusEl.textContent = 'Downloading installer…';
    setUpdateProgress(0);
    const installerPath = await invoke('download_update', { url: latestUpdateInfo.download_url });
    statusEl.textContent = 'Installing and restarting…';
    await invoke('install_update', { installerPath });
    // If we get here, install_update's std::process::exit(0) didn't fire —
    // something upstream failed silently rather than via a thrown error.
  } catch (e) {
    statusEl.textContent = 'Update failed: ' + e;
    installBtn.disabled = false;
    setUpdateProgress(null);
  } finally {
    unlistenProgress();
    unlistenPhase();
  }
}

function initUpdatesPanel() {
  if (!IS_TAURI) return;
  document.getElementById('btn-update-check').onclick = () => checkForUpdate(true);
  document.getElementById('btn-update-install').onclick = installUpdateFlow;
  checkForUpdate(false);
}

// -------------------------------------------------------- setup wizard ---
function renderWizardBackends(status) {
  const container = document.getElementById('wizard-backends');
  if (!container || !status.backends) return;
  container.innerHTML = Object.entries(status.backends).map(([name, info]) => `
    <div class="settingrow">
      <div>
        <div class="st">${esc(name)}${info.in_use ? ' <span class="optional-tag">routed to</span>' : ''}</div>
        <div class="sd">${info.ready ? 'ready' : esc(info.reason || 'not set up')}</div>
      </div>
      <span class="row" style="gap:8px;">
        ${(name === 'cli' && !info.ready) ? '<button class="btn" data-install-cli type="button">Install/update CLI</button>' : ''}
        <span class="pill"><span class="dot ${info.ready ? 'good' : (info.in_use ? 'warning' : '')}"></span>${info.ready ? 'ready' : 'not ready'}</span>
      </span>
    </div>`).join('');

  const installBtn = container.querySelector('[data-install-cli]');
  if (installBtn) {
    installBtn.onclick = async () => {
      installBtn.disabled = true;
      installBtn.textContent = 'Installing…';
      try {
        const res = await api('/api/setup/install-cli', { method: 'POST' });
        const fresh = await api('/api/setup/status');
        renderWizardFields(fresh);
        if (!res.ok) {
          document.getElementById('wizard-status').textContent = 'CLI install failed: ' + (res.output || '').slice(-300);
        }
      } catch (e) {
        installBtn.disabled = false;
        installBtn.textContent = 'Install/update CLI';
      }
    };
  }
}

function renderWizardFields(status) {
  document.getElementById('wizard-envpath').textContent = status.env_path;
  renderWizardBackends(status);
  const container = document.getElementById('wizard-fields');
  container.innerHTML = Object.entries(status.fields).map(([key, f]) => {
    const isToken = key === 'DASHBOARD_TOKEN';
    const isDesktop = key === 'CLAUDE_DESKTOP_EXE';
    const already = f.present && f.valid;
    const placeholder = already ? 'already set — leave blank to keep' : (isDesktop ? 'optional — Auto-detect or paste a path' : '');
    const extraBtn = isToken ? `<button class="btn" data-generate="${key}" type="button">Generate</button>`
      : isDesktop ? `<button class="btn" data-detect="${key}" type="button">Auto-detect</button>` : '';
    const msgText = f.present ? f.message : (f.required ? 'not set yet' : '');
    const msgClass = f.present ? (f.valid ? 'good' : 'bad') : '';
    return `
      <div class="wizard-field">
        <label>${esc(f.label)}${f.required ? '' : ' <span class="optional-tag">optional</span>'}</label>
        <div class="help">${esc(f.help)}</div>
        <div class="row">
          <input type="text" data-field="${key}" placeholder="${esc(placeholder)}" autocomplete="off" spellcheck="false">
          ${extraBtn}
        </div>
        ${msgText ? `<div class="msg ${msgClass}">${esc(msgText)}</div>` : ''}
      </div>`;
  }).join('');

  container.querySelectorAll('[data-generate]').forEach(btn => btn.onclick = async () => {
    const res = await api('/api/setup/generate-token', { method: 'POST' });
    container.querySelector(`input[data-field="${btn.dataset.generate}"]`).value = res.token;
  });
  container.querySelectorAll('[data-detect]').forEach(btn => btn.onclick = async () => {
    const res = await api('/api/setup/detect-desktop');
    const input = container.querySelector(`input[data-field="${btn.dataset.detect}"]`);
    if (res.path) {
      input.value = res.path;
    } else {
      document.getElementById('wizard-status').textContent = 'No Claude Desktop install auto-detected — enter the path manually, or leave blank.';
    }
  });
}

function openWizard(status) {
  renderWizardFields(status);
  document.getElementById('wizard-status').textContent = '';
  document.getElementById('wizard').classList.remove('hidden');
}
function closeWizard() {
  document.getElementById('wizard').classList.add('hidden');
}

document.getElementById('btn-wizard-save').onclick = async () => {
  const statusEl = document.getElementById('wizard-status');
  const container = document.getElementById('wizard-fields');
  const payload = {};
  container.querySelectorAll('input[data-field]').forEach(inp => {
    if (inp.value.trim()) payload[inp.dataset.field] = inp.value.trim();
  });
  statusEl.textContent = 'Saving…';
  try {
    const res = await api('/api/setup/apply', { method: 'POST', body: JSON.stringify(payload) });
    await autoFillToken();
    if (res.status.ready) {
      closeWizard();
      startDashboardPolling();
    } else {
      statusEl.textContent = 'Saved what you entered — required fields below still need attention.';
      renderWizardFields(res.status);
    }
  } catch (e) {
    statusEl.textContent = 'Save failed — check the dashboard token and try again.';
  }
};

document.getElementById('btn-wizard-skip').onclick = () => {
  closeWizard();
  startDashboardPolling();
};

document.getElementById('btn-open-wizard').onclick = async () => {
  const status = await api('/api/setup/status');
  openWizard(status);
};

async function checkSetupAndProceed(onReady) {
  try {
    const headers = {};
    const token = getToken();
    if (token) headers['X-Dashboard-Token'] = token;
    const res = await fetch(API_BASE + '/api/setup/status', { headers });
    if (!res.ok) { onReady(); return; } // 401 etc — let the normal dashboard flow prompt for the token
    const status = await res.json();
    if (status.ready) onReady(); else openWizard(status);
  } catch (e) {
    onReady(); // don't block the whole UI on a network hiccup
  }
}

// --------------------------------------------------------- tauri boot ----
function bootLine(text, cls) {
  const el = document.getElementById('boot-lines');
  const div = document.createElement('div');
  div.className = 'line' + (cls ? ' ' + cls : '');
  div.textContent = text;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

async function waitForServerReady(maxAttempts = 300) {
  for (let i = 0; i < maxAttempts; i++) {
    // A per-attempt timeout so one slow/hung request can't silently eat a
    // big chunk of the overall retry budget — plain fetch() has no
    // built-in timeout of its own.
    const controller = new AbortController();
    const abortTimer = setTimeout(() => controller.abort(), 3000);
    try {
      // /healthz specifically, not a data endpoint like /api/overview:
      // this check only needs to know "is the process up and the DB
      // readable," and it runs before the dashboard token is guaranteed
      // to be attached to requests yet, so it must hit a route that's
      // unauthenticated by design (see bot/dashboard/server.py's comment
      // on /healthz) — otherwise every attempt 401s and this loop churns
      // through its whole retry budget looking "stuck" even when the
      // server answered instantly and correctly the entire time.
      const res = await fetch(API_BASE + '/healthz', { cache: 'no-store', signal: controller.signal });
      if (res.ok) return true;
    } catch (_e) { /* not up yet, or that attempt timed out */ }
    finally { clearTimeout(abortTimer); }
    await new Promise(r => setTimeout(r, 500));
  }
  return false;
}

// The boot panel is a small floating status pill, not a full-screen
// overlay — the dashboard shell underneath is always visible and usable.
// setBootPill() drives the collapsed pill's dot color/text; expandBoot()/
// collapseBoot() toggle the log/detail panel, which the user can also
// toggle manually at any time by clicking the pill.
function setBootPill(status, text) {
  const pill = document.getElementById('boot-pill');
  pill.classList.remove('ok', 'err');
  if (status) pill.classList.add(status);
  document.getElementById('boot-pill-text').textContent = text;
}

function expandBoot() { document.getElementById('boot').classList.add('expanded'); }
function collapseBoot() { document.getElementById('boot').classList.remove('expanded'); }

document.getElementById('boot-pill').onclick = () => {
  document.getElementById('boot').classList.toggle('expanded');
};

function hideBootOverlay() {
  setBootPill('ok', 'Bot Server running');
  setTimeout(collapseBoot, 400);
}

async function autoFillToken() {
  if (!IS_TAURI) return;
  try {
    const { invoke } = window.__TAURI__.core;
    const token = await invoke('get_dashboard_token');
    if (token) setToken(token);
  } catch (_e) { /* no .env resolved yet, or python missing — fall back to manual entry */ }
}

async function initTauriBoot() {
  const { listen } = window.__TAURI__.event;
  const { invoke } = window.__TAURI__.core;

  await autoFillToken();
  document.getElementById('boot-envline').textContent = 'spawning python -m bot.main';
  document.getElementById('server-controls').classList.remove('hidden');
  setBootPill('starting', 'Starting the bot process…');
  expandBoot(); // first boot: show progress by default, same as before — but as a corner panel, not a full-screen block

  await listen('server-log', (evt) => {
    const { stream, line } = evt.payload;
    bootLine(line, stream === 'stderr' ? 'stderr' : 'stdout');
  });
  await listen('server-status', (evt) => {
    const { running, pid } = evt.payload;
    document.getElementById('boot-pid').textContent = pid || '—';
    if (!running) {
      document.getElementById('boot-status').innerHTML = '';
      const err = document.createElement('span');
      err.className = 'boot-error';
      err.textContent = 'Server process exited. Restart it above once you’ve checked the log.';
      document.getElementById('boot-status').appendChild(err);
      document.getElementById('boot-spinner').classList.add('hidden');
      setBootPill('err', 'Server stopped — click for details');
      expandBoot();
    }
  });
  await listen('server-resources', (evt) => {
    const { cpu_percent, mem_mb } = evt.payload;
    document.getElementById('boot-cpu').textContent = cpu_percent.toFixed(1) + '%';
    document.getElementById('boot-mem').textContent = mem_mb.toFixed(0) + ' MB';
  });

  document.getElementById('btn-server-stop').onclick = async () => {
    if (!confirm('Stop the bot server process? The Telegram bot and dashboard API will go offline until restarted.')) return;
    await invoke('stop_server');
    bootLine('— server stopped from the GUI —', 'meta');
    document.getElementById('boot-status').textContent = 'Stopped. Click Restart server to bring it back.';
    document.getElementById('boot-spinner').classList.add('hidden');
    setBootPill('err', 'Server stopped');
    expandBoot();
  };
  document.getElementById('btn-server-restart').onclick = async () => {
    document.getElementById('boot-lines').innerHTML = '';
    document.getElementById('boot-spinner').classList.remove('hidden');
    document.getElementById('boot-status').textContent = 'Restarting…';
    setBootPill('starting', 'Restarting…');
    expandBoot();
    bootLine('— restarting server —', 'meta');
    await invoke('restart_server');
    const ready = await waitForServerReady();
    if (ready) { hideBootOverlay(); refreshAll(); }
  };

  bootLine('waiting for the dashboard API to answer on 127.0.0.1:8787 …', 'meta');
  const ready = await waitForServerReady();
  if (ready) {
    bootLine('— ready —', 'meta');
    document.getElementById('boot-status').textContent = 'Ready.';
    hideBootOverlay();
  } else {
    document.getElementById('boot-status').innerHTML = '';
    const err = document.createElement('span');
    err.className = 'boot-error';
    err.textContent = 'Taking longer than expected — still trying in the background. Check the log above.';
    document.getElementById('boot-status').appendChild(err);
    document.getElementById('boot-spinner').classList.add('hidden');
    setBootPill('err', 'Still waiting — click for details');
    expandBoot();
    bootLine('— giving up on the fast path, retrying quietly in the background —', 'meta');
    // A slow-but-not-dead server (first-run JIT warmup, AV scanning a
    // freshly built exe, a one-off hiccup) shouldn't leave this panel
    // stuck showing a stale "timed out" error forever — keep checking
    // until it actually answers, then recover to Ready like normal.
    (async () => {
      let recovered = false;
      while (!recovered) {
        recovered = await waitForServerReady(10);
      }
      bootLine('— ready —', 'meta');
      document.getElementById('boot-status').textContent = 'Ready.';
      setBootPill('ok', 'Bot Server running');
      hideBootOverlay();
    })();
  }
  checkSetupAndProceed(startDashboardPolling);
}

if (IS_TAURI) {
  initTauriBoot();
  initAndroidPanel();
  initUpdatesPanel();
} else {
  document.getElementById('boot').classList.add('hidden');
  checkSetupAndProceed(startDashboardPolling);
}

// ------------------------------------------------------------ android ----
// Desktop-shell-only: builds the Android app with Gradle, installs it on a
// connected device with adb, and auto-pairs it via the botserver://pair deep
// link (see android.rs) — the browser-served dashboard.html has no OS
// process access and doesn't get this section at all.
const androidState = { apkPath: null, deviceState: {}, deviceModels: {} };

function androidLog(line, cls) {
  const wrap = document.getElementById('android-log-wrap');
  const box = document.getElementById('android-log');
  wrap.classList.remove('hidden');
  const el = document.createElement('div');
  if (cls) el.style.color = cls === 'stderr' ? '#f08b8b' : '#c9cdd3';
  el.textContent = line;
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
}

function androidStep(text) { document.getElementById('android-step').textContent = text; }

async function refreshAndroidStatus() {
  const { invoke } = window.__TAURI__.core;
  const el = document.getElementById('android-env-status');
  try {
    const s = await invoke('android_env_status');
    if (s.sdk_found && s.project_found) {
      el.textContent = `Ready — project at ${s.project_dir}, adb at ${s.adb_path}.`;
    } else if (!s.project_found) {
      el.textContent = 'android-app/ source tree not found next to this app — this feature only works on the machine used for development.';
    } else {
      el.textContent = 'Android SDK not found (checked ANDROID_HOME/ANDROID_SDK_ROOT and android-app/local.properties).';
    }
    const disabled = !(s.sdk_found && s.project_found);
    ['btn-android-build-install-pair', 'btn-android-build-install', 'btn-android-build'].forEach(id => {
      document.getElementById(id).disabled = disabled;
    });
  } catch (e) {
    el.textContent = 'Could not check Android build environment.';
  }
}

async function refreshAndroidDevices() {
  const { invoke } = window.__TAURI__.core;
  const select = document.getElementById('android-device-select');
  try {
    const devices = await invoke('list_adb_devices');
    androidState.deviceState = {};
    androidState.deviceModels = {};
    devices.forEach(d => { androidState.deviceState[d.serial] = d.state; androidState.deviceModels[d.serial] = d.model; });
    select.innerHTML = devices.length
      ? devices.map(d => `<option value="${esc(d.serial)}">${esc(d.model)} — ${esc(d.serial)}${d.state !== 'device' ? ` (${esc(d.state)})` : ''}</option>`).join('')
      : '<option value="">No devices found</option>';
  } catch (e) {
    select.innerHTML = '<option value="">Couldn\'t list devices — is adb available?</option>';
  }
}

function selectedAndroidDevice() {
  const select = document.getElementById('android-device-select');
  const serial = select.value;
  if (!serial) { alert('No device selected — connect a phone with USB debugging enabled and click "Refresh devices".'); return null; }
  if (androidState.deviceState[serial] !== 'device') {
    alert(`That device isn't ready (state: ${androidState.deviceState[serial]}) — check your phone for a USB-debugging authorization prompt.`);
    return null;
  }
  return serial;
}

function buildAndroidApk() {
  const { invoke } = window.__TAURI__.core;
  return new Promise((resolve) => {
    document.getElementById('android-log').innerHTML = '';
    androidStep('Building APK…');
    androidLog('$ gradlew.bat assembleDebug', 'meta');
    window.__androidBuildResolve = resolve;
    invoke('build_android_apk').catch(e => {
      androidStep('Build failed to start.');
      androidLog(String(e), 'stderr');
      resolve({ success: false, error: String(e) });
    });
  });
}

async function installAndroidApk(serial) {
  const { invoke } = window.__TAURI__.core;
  if (!androidState.apkPath) { alert('Build the APK first.'); return false; }
  androidStep('Installing on device…');
  try {
    await invoke('install_android_apk', { serial, apkPath: androidState.apkPath });
    androidStep('Installed.');
    return true;
  } catch (e) {
    androidStep('Install failed.');
    alert(`Install failed: ${e}`);
    return false;
  }
}

async function pairAndroidDevice(serial) {
  const { invoke } = window.__TAURI__.core;
  const hostEl = document.getElementById('mobile-new-host');
  const host2El = document.getElementById('mobile-new-host2');
  // The field is normally already pre-filled by autoFillMobileHosts() at
  // startup — this is just a last-resort safety net (e.g. detection was
  // still in flight, or genuinely found nothing) so pairing still works
  // without ever requiring the user to type an address by hand.
  if (!hostEl.value.trim()) await autoFillMobileHosts();
  const host = hostEl.value.trim();
  const host2 = host2El.value.trim();
  if (!host) {
    androidStep('Pairing failed.');
    alert("Couldn't auto-detect this machine's address (no network connection?) — check your network and try again, or type a host:port above manually.");
    return false;
  }
  androidStep('Generating a pairing key…');
  let key;
  try {
    // Prefer the real hardware model adb already reported for this device
    // (see list_adb_devices/AdbDevice::model in android.rs) over a generic
    // date-stamped placeholder — a phone should show up in the Devices list
    // as "Pixel 8 Pro", not "Android (auto-paired 8/19/2026)".
    const detectedModel = (androidState.deviceModels || {})[serial];
    const label = document.getElementById('mobile-new-label').value.trim()
      || detectedModel
      || `Android (auto-paired ${new Date().toLocaleDateString()})`;
    const res = await api('/api/mobile-keys', { method: 'POST', body: JSON.stringify({ label, host, host2 }) });
    key = res.key;
    refreshMobileKeys();
  } catch (e) {
    androidStep('Key generation failed.');
    alert('Failed to generate a pairing key — check the dashboard token.');
    return false;
  }
  androidStep('Pairing device…');
  try {
    await invoke('pair_android_device', { serial, host, host2, key });
    androidStep('Paired — check the device.');
    return true;
  } catch (e) {
    androidStep('Pairing failed.');
    alert(`Pairing failed: ${e}`);
    return false;
  }
}

async function autoFillMobileHosts() {
  // Nobody should have to go find their own LAN IP or Tailscale address —
  // pre-fill both host fields the moment the app starts, so pairing (QR,
  // shared link, or the Android auto-pair flow below) never blocks on
  // typing an address in by hand. Only fills in blank fields — never
  // overwrites something the user already typed or edited.
  const { invoke } = window.__TAURI__.core;
  const hostEl = document.getElementById('mobile-new-host');
  const host2El = document.getElementById('mobile-new-host2');
  try {
    const [lan, tailscale] = await Promise.all([
      invoke('detect_lan_host').catch(() => null),
      invoke('detect_tailscale_host').catch(() => null),
    ]);
    if (!hostEl.value.trim() && lan) hostEl.value = lan;
    if (!host2El.value.trim() && tailscale && tailscale !== hostEl.value.trim()) host2El.value = tailscale;
  } catch (_e) { /* best-effort — manual entry still works if detection fails */ }
}

function initAndroidPanel() {
  const { listen } = window.__TAURI__.event;
  document.getElementById('android-card').classList.remove('hidden');
  autoFillMobileHosts();
  refreshAndroidStatus();
  refreshAndroidDevices();

  listen('android-build-log', (evt) => androidLog(evt.payload.line, evt.payload.stream));
  listen('android-build-done', (evt) => {
    const payload = evt.payload;
    if (payload.success) {
      androidState.apkPath = payload.apk_path;
      androidStep(`Built ${payload.apk_path}`);
    } else {
      androidStep('Build failed.');
      androidLog(payload.error || 'Unknown build error.', 'stderr');
    }
    if (window.__androidBuildResolve) {
      window.__androidBuildResolve(payload);
      window.__androidBuildResolve = null;
    }
  });

  document.getElementById('btn-android-refresh-devices').onclick = refreshAndroidDevices;

  document.getElementById('btn-android-build').onclick = async () => {
    document.getElementById('btn-android-build').disabled = true;
    await buildAndroidApk();
    document.getElementById('btn-android-build').disabled = false;
  };

  document.getElementById('btn-android-build-install').onclick = async () => {
    const serial = selectedAndroidDevice();
    if (!serial) return;
    const btn = document.getElementById('btn-android-build-install');
    btn.disabled = true;
    try {
      const result = await buildAndroidApk();
      if (!result.success) return;
      await installAndroidApk(serial);
    } finally {
      btn.disabled = false;
    }
  };

  document.getElementById('btn-android-install').onclick = async () => {
    const serial = selectedAndroidDevice();
    if (!serial) return;
    document.getElementById('btn-android-install').disabled = true;
    await installAndroidApk(serial);
    document.getElementById('btn-android-install').disabled = false;
  };

  document.getElementById('btn-android-pair').onclick = async () => {
    const serial = selectedAndroidDevice();
    if (!serial) return;
    document.getElementById('btn-android-pair').disabled = true;
    await pairAndroidDevice(serial);
    document.getElementById('btn-android-pair').disabled = false;
  };

  document.getElementById('btn-android-build-install-pair').onclick = async () => {
    const serial = selectedAndroidDevice();
    if (!serial) return;
    const btn = document.getElementById('btn-android-build-install-pair');
    btn.disabled = true;
    try {
      const result = await buildAndroidApk();
      if (!result.success) return;
      if (!(await installAndroidApk(serial))) return;
      await pairAndroidDevice(serial);
    } finally {
      btn.disabled = false;
    }
  };
}

// ---------------------------------------------------------------- sidenav
// Telegram Desktop's own chat list doubles as navigation + a live filter —
// mirrored here: typing narrows the section list, and the section actually
// in view is highlighted as you scroll, the same "current chat" affordance.
(function initSidenav() {
  const searchInput = document.getElementById('sidenav-search');
  const links = Array.from(document.querySelectorAll('#sidenav a'));
  if (searchInput) {
    searchInput.oninput = () => {
      const q = searchInput.value.trim().toLowerCase();
      links.forEach(a => {
        const hit = !q || (a.dataset.kw || '').includes(q);
        a.classList.toggle('hidden-by-search', !hit);
      });
    };
  }

  const sections = links
    .map(a => document.querySelector(a.getAttribute('href')))
    .filter(Boolean);
  const setActive = (id) => {
    links.forEach(a => a.classList.toggle('active', a.getAttribute('href') === '#' + id));
  };
  if (sections.length && 'IntersectionObserver' in window) {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach(entry => { if (entry.isIntersecting) setActive(entry.target.id); });
    }, { rootMargin: '-35% 0px -55% 0px', threshold: 0 });
    sections.forEach(s => observer.observe(s));
  }
})();

// -------------------------------------------------------- slash commands
// Mirrors bot/commands.py's HELP_TEXT — the same commands Telegram,
// Discord, Slack, and the Support Bot's dispatch_command() all accept.
// Typing "/" in any composer pops this list so you don't have to remember
// the exact syntax; picking one just fills the input, same as Telegram's
// own slash-command menu.
const SLASH_COMMANDS = [
  { cmd: 'ask', args: '<text>', desc: 'Send a prompt (add --backend=api|cli|ui|hermes_cli|hermes_gateway to override)' },
  { cmd: 'status', args: '', desc: 'Health snapshot' },
  { cmd: 'backend', args: 'show | set <action|default> <backend>', desc: 'Router config' },
  { cmd: 'model', args: 'show | set <backend> <model>', desc: 'Per-backend model' },
  { cmd: 'mcp', args: 'list | enable <name> | disable <name> | logs <name>', desc: 'MCP servers' },
  { cmd: 'start_desktop', args: '', desc: 'Launch Claude Desktop' },
  { cmd: 'stop_desktop', args: '', desc: 'Stop Claude Desktop' },
  { cmd: 'restart_desktop', args: '', desc: 'Restart Claude Desktop' },
  { cmd: 'project', args: 'open <path>', desc: 'Set working dir for the next /ask' },
  { cmd: 'new_session', args: '', desc: 'Open a fresh linked chat in Claude Desktop/Hermes for this bot' },
  { cmd: 'help', args: '', desc: 'List available commands' },
];

// Attaches a Telegram-style "/" autocomplete popup to a composer textarea.
// Captures keydown ahead of each composer's own Enter-to-send listener
// (registered separately, earlier in this file / in initSupportBot below)
// so that Enter/Tab select a highlighted command instead of sending the
// message while the menu is open — capture phase always runs first,
// regardless of listener registration order.
function attachSlashMenu(input) {
  const composer = input.closest('.chat-composer');
  if (!composer) return;
  let menu = null;
  let items = [];
  let activeIndex = -1;

  function close() {
    if (menu) { menu.remove(); menu = null; }
    items = [];
    activeIndex = -1;
  }

  function highlight() {
    if (!menu) return;
    Array.from(menu.children).forEach((el, i) => el.classList.toggle('active', i === activeIndex));
  }

  function select(c) {
    input.value = '/' + c.cmd + ' ';
    close();
    input.focus();
  }

  function render(filtered) {
    close();
    if (!filtered.length) return;
    items = filtered;
    activeIndex = 0;
    menu = document.createElement('div');
    menu.className = 'slash-menu';
    filtered.forEach((c, i) => {
      const row = document.createElement('div');
      row.className = 'slash-menu-item' + (i === 0 ? ' active' : '');
      row.innerHTML = `<span class="cmd">/${esc(c.cmd)}</span><span class="desc">${esc(c.args ? c.args + ' — ' : '')}${esc(c.desc)}</span>`;
      row.onmousedown = (e) => { e.preventDefault(); select(c); };
      menu.appendChild(row);
    });
    composer.appendChild(menu);
  }

  function update() {
    const value = input.value;
    // Only offer completions while typing the bare command name itself —
    // once there's a space (args started), get out of the way.
    if (!value.startsWith('/') || /\s/.test(value)) { close(); return; }
    const query = value.slice(1).toLowerCase();
    render(SLASH_COMMANDS.filter(c => c.cmd.startsWith(query)));
  }

  input.addEventListener('input', update);
  input.addEventListener('focus', update);
  input.addEventListener('blur', () => setTimeout(close, 150));
  input.addEventListener('keydown', (e) => {
    if (!menu) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); e.stopPropagation(); activeIndex = Math.min(activeIndex + 1, items.length - 1); highlight(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); e.stopPropagation(); activeIndex = Math.max(activeIndex - 1, 0); highlight(); }
    else if (e.key === 'Tab' || e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); select(items[activeIndex]); }
    else if (e.key === 'Escape') { e.stopPropagation(); close(); }
  }, true);
}

// ------------------------------------------------------------ support bot
// The local, dependency-free assistant (bot/support_bot/) — a plain
// request/reply chat, no polling, no history to load: each message is a
// single POST to /api/support-bot/ask, with an inline Confirm chip for
// destructive replies that POSTs /api/support-bot/confirm.
(function initSupportBot() {
  const win = document.getElementById('support-bot-window');
  const input = document.getElementById('support-bot-input');
  const sendBtn = document.getElementById('btn-support-bot-send');
  const status = document.getElementById('support-bot-status');
  if (!win || !input || !sendBtn) return;

  function addBubble(text, dir) {
    const row = document.createElement('div');
    row.className = 'chat-row ' + dir;
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble';
    bubble.innerHTML = esc(text).replace(/\n/g, '<br>');
    row.appendChild(bubble);
    win.appendChild(row);
    win.scrollTop = win.scrollHeight;
    return bubble;
  }

  function addConfirmChip(bubble, token) {
    const btn = document.createElement('button');
    btn.className = 'btn danger';
    btn.style.marginTop = '8px';
    btn.style.display = 'block';
    btn.textContent = 'Confirm';
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const reply = await api('/api/support-bot/confirm', {
          method: 'POST',
          body: JSON.stringify({ token }),
        });
        btn.remove();
        addBubble(reply.text, 'in');
      } catch (e) {
        status.textContent = 'Confirm failed: ' + e.message;
      }
    };
    bubble.appendChild(btn);
  }

  async function send() {
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    addBubble(text, 'out');
    status.textContent = '';
    try {
      const reply = await api('/api/support-bot/ask', {
        method: 'POST',
        body: JSON.stringify({ text }),
      });
      const bubble = addBubble(reply.text, 'in');
      if (reply.needs_confirm && reply.confirm_token) {
        addConfirmChip(bubble, reply.confirm_token);
      }
    } catch (e) {
      addBubble('Error: ' + e.message, 'in');
    }
  }

  sendBtn.onclick = send;
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  attachSlashMenu(input);
})();
