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
};

function esc(s) { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; }
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

  const t = await api('/api/telemetry');
  const events = t.connection_events.slice(0, 8);
  document.getElementById('db-recent').innerHTML = events.length
    ? events.map(e => `<div><span class="ts">${fmtTime(e.ts)}</span> ${esc(e.component)}.${esc(e.event)}${e.detail ? ' — ' + esc(e.detail) : ''}</div>`).join('')
    : 'No events yet.';
}

document.getElementById('btn-vacuum').onclick = async () => { await api('/api/database/vacuum', { method: 'POST' }); refreshDatabase(); };

// ---------------------------------------------------------------- control
async function refreshConfig() {
  const cfg = await api('/api/config');
  state.configCache = cfg;
  const current = cfg.current;

  document.querySelectorAll('#default-backend-seg button').forEach(b => b.classList.toggle('active', b.dataset.b === current.default_backend));

  const overridesEl = document.getElementById('action-overrides-list');
  overridesEl.innerHTML = Object.entries(current.action_overrides || {}).map(([action, entry]) => `
    <div class="settingrow">
      <div><div class="st">${esc(action)} →</div><div class="sd">backup: ${JSON.stringify(entry.backup || [])}</div></div>
      <span class="chip good">${esc(entry.backend)}</span>
    </div>`).join('');

  setToggle('tg-ui-automation', !!(current.features || {}).ui_automation_enabled);
  setToggle('tg-confirm', !!(current.security || {}).confirm_destructive);
  setToggle('tg-verbose', !!(current.features || {}).verbose_telemetry);

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

document.querySelectorAll('#default-backend-seg button').forEach(btn => {
  btn.onclick = async () => { await api(`/api/backend/default/${btn.dataset.b}`, { method: 'POST' }); refreshConfig(); refreshOverview(); };
});

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
async function refreshAll() {
  const tasks = [refreshOverview(), refreshJobsTable(), refreshJobsCharts(), refreshTelemetry(), refreshDatabase(), refreshConfig(), refreshMcp(), refreshAllowedUsers(), refreshLogs(), refreshEnv()];
  await Promise.allSettled(tasks);
}

function startDashboardPolling() {
  refreshAll();
  setInterval(refreshAll, 5000);
  refreshEnvEditor();
  refreshEnvBackups();
  startChatPolling();
  refreshPlatforms();
  setInterval(refreshPlatforms, 15000);
}

// ------------------------------------------------------------------ chat -
const chatState = { lastId: 0, platform: 'telegram', recipient: null, loaded: false, recipients: null };

function fmtChatTime(ts) {
  // ts is already a full ISO8601 string with explicit UTC offset
  // (datetime.isoformat() from Python) — no 'Z' needed or safe to append.
  const d = new Date(ts);
  if (isNaN(d)) return ts;
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function appendChatMessages(rows) {
  const win = document.getElementById('chat-window');
  const atBottom = win.scrollHeight - win.scrollTop - win.clientHeight < 60;
  rows.forEach(m => {
    const row = document.createElement('div');
    row.className = 'chat-row ' + (m.direction === 'in' ? 'in' : 'out');
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble' + (m.source === 'dashboard' ? ' from-dashboard' : '');
    const textEl = document.createElement('div');
    textEl.textContent = m.text;
    bubble.appendChild(textEl);
    const meta = document.createElement('span');
    meta.className = 'chat-meta';
    const who = m.source === 'dashboard' ? 'sent from dashboard' : (m.direction === 'in' && m.username) ? m.username : '';
    meta.textContent = [fmtChatTime(m.ts), m.platform, who].filter(Boolean).join(' · ');
    bubble.appendChild(meta);
    row.appendChild(bubble);
    win.appendChild(row);
    chatState.lastId = Math.max(chatState.lastId, m.id);
  });
  if (rows.length && atBottom) win.scrollTop = win.scrollHeight;
}

function renderChatRecipients() {
  const recipientSelect = document.getElementById('chat-recipient');
  const note = document.getElementById('chat-recipient-note');
  const data = chatState.recipients;
  if (!data) return;
  const ids = (data[chatState.platform] || []).map(String);
  if (!ids.length) {
    recipientSelect.innerHTML = '<option value="">no allowed users</option>';
    note.textContent = `Set up ${chatState.platform} allowlist in the Platforms tab.`;
    return;
  }
  recipientSelect.innerHTML = ids.map(id => `<option value="${esc(id)}">${esc(id)}</option>`).join('');
  if (!chatState.recipient || !ids.includes(String(chatState.recipient))) {
    chatState.recipient = ids[0];
  }
  recipientSelect.value = String(chatState.recipient);
  const connected = data.connected.includes(chatState.platform);
  note.textContent = connected ? '' : `${chatState.platform} is configured but not connected right now — restart the server.`;
}

async function refreshChatRecipients() {
  const platformSelect = document.getElementById('chat-platform');
  try {
    const data = await api('/api/chat/recipients');
    chatState.recipients = data;
    const platforms = ['telegram', 'discord', 'slack'].filter(p => (data[p] || []).length > 0);
    if (!platforms.length) {
      platformSelect.innerHTML = '<option value="">none configured</option>';
      document.getElementById('chat-recipient').innerHTML = '';
      document.getElementById('chat-recipient-note').textContent = 'Set up a platform in the Platforms tab first.';
      return;
    }
    platformSelect.innerHTML = platforms.map(p => `<option value="${p}">${p}${data.connected.includes(p) ? '' : ' (not connected)'}</option>`).join('');
    if (!chatState.platform || !platforms.includes(chatState.platform)) chatState.platform = platforms[0];
    platformSelect.value = chatState.platform;
    renderChatRecipients();
  } catch (_e) { /* token not set yet — leave placeholder */ }
}

async function refreshChat() {
  const win = document.getElementById('chat-window');
  try {
    if (!chatState.loaded) {
      win.innerHTML = '';
      const rows = await api('/api/chat/messages?limit=100');
      appendChatMessages(rows);
      chatState.loaded = true;
    } else {
      const rows = await api(`/api/chat/messages?after_id=${chatState.lastId}&limit=200`);
      appendChatMessages(rows);
    }
  } catch (_e) { /* token not set yet, or server not ready — try again next tick */ }
}

function startChatPolling() {
  refreshChatRecipients();
  refreshChat();
  setInterval(refreshChat, 2000);
  setInterval(refreshChatRecipients, 15000);
}

document.getElementById('chat-platform').onchange = (e) => {
  chatState.platform = e.target.value;
  chatState.recipient = null;
  renderChatRecipients();
};
document.getElementById('chat-recipient').onchange = (e) => { chatState.recipient = e.target.value; };

async function sendChatMessage() {
  const input = document.getElementById('chat-input');
  const statusEl = document.getElementById('chat-status');
  const text = input.value.trim();
  if (!text) return;
  if (!chatState.recipient) {
    statusEl.textContent = 'No recipient — set up an allowed user for this platform first.';
    return;
  }
  const btn = document.getElementById('btn-chat-send');
  btn.disabled = true;
  statusEl.textContent = '';
  try {
    await api('/api/chat/send', { method: 'POST', body: JSON.stringify({ platform: chatState.platform, chat_id: chatState.recipient, text }) });
    input.value = '';
    await refreshChat();
  } catch (e) {
    statusEl.textContent = 'Send failed — check the dashboard token and that this platform is connected.';
  } finally {
    btn.disabled = false;
  }
}
document.getElementById('btn-chat-send').onclick = sendChatMessage;
document.getElementById('chat-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChatMessage();
  }
});

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
    try {
      const res = await fetch(API_BASE + '/api/overview', { cache: 'no-store' });
      if (res.ok) return true;
    } catch (_e) { /* not up yet */ }
    await new Promise(r => setTimeout(r, 500));
  }
  return false;
}

function hideBootOverlay() {
  document.getElementById('boot').classList.add('hidden');
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
      document.getElementById('boot').classList.remove('hidden');
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
    document.getElementById('boot').classList.remove('hidden');
    bootLine('— server stopped from the GUI —', 'meta');
    document.getElementById('boot-status').textContent = 'Stopped. Click Restart server to bring it back.';
    document.getElementById('boot-spinner').classList.add('hidden');
  };
  document.getElementById('btn-server-restart').onclick = async () => {
    document.getElementById('boot').classList.remove('hidden');
    document.getElementById('boot-lines').innerHTML = '';
    document.getElementById('boot-spinner').classList.remove('hidden');
    document.getElementById('boot-status').textContent = 'Restarting…';
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
    setTimeout(hideBootOverlay, 400);
  } else {
    document.getElementById('boot-status').innerHTML = '';
    const err = document.createElement('span');
    err.className = 'boot-error';
    err.textContent = 'Timed out waiting for the server to come up — check the log above.';
    document.getElementById('boot-status').appendChild(err);
    document.getElementById('boot-spinner').classList.add('hidden');
  }
  checkSetupAndProceed(startDashboardPolling);
}

if (IS_TAURI) {
  initTauriBoot();
} else {
  hideBootOverlay();
  checkSetupAndProceed(startDashboardPolling);
}
