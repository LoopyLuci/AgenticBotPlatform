// The dashboard's main script. It used to be one inline <script> in dashboard.html;
// it is a file so the page's Content-Security-Policy can forbid inline script.
const state = { jobFilter: 'all', logLevel: 'all', configCache: null };

// Never typed in: the server places the auto-generated token in this page when it is loaded from this machine
// (bot/dashboard/server.py, _page_with_token).
function getToken() { return window.__ABP_TOKEN__ || ''; }

// ---- network guard ---------------------------------------------------------
// Every api() request times out (a wedged server must not pile up hanging
// requests), and while the server is unreachable the pollers back off
// exponentially (5 s doubling to 30 s) instead of firing ~120 failing requests
// a minute — which, left open for a while, exhausted the browser's connection
// pool (ERR_INSUFFICIENT_RESOURCES). Explicit user actions are never delayed.
const _net = { failures: 0, until: 0 };
const API_TIMEOUT_MS = 20000;
function _netFailed() {
  _net.failures += 1;
  _net.until = Date.now() + Math.min(30000, 5000 * Math.pow(2, _net.failures - 1));
}
function _netOk() { _net.failures = 0; _net.until = 0; }
async function _timedFetch(url, opts) {
  if (opts && opts.signal) return fetch(url, opts);  // the caller manages its own cancellation
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), API_TIMEOUT_MS);
  try {
    const res = await fetch(url, Object.assign({}, opts, { signal: ctl.signal }));
    _netOk();
    return res;
  } catch (e) {
    _netFailed();
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  const token = getToken();
  if (token) headers['X-Dashboard-Token'] = token;
  if (opts.method && opts.method !== 'GET') {
    headers['Content-Type'] = 'application/json';
  }
  const res = await _timedFetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401 || res.status === 503) {
    showAuthNotice();
    throw new Error('unauthorized');
  }
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// Sidebar collapse (desktop/tablet icon-rail) + mobile overlay drawer.
// Independent of the rest of dashboard startup so it works even before
// the first API call resolves.
(function initSidebar() {
  var sideToggle = document.getElementById('btn-side-toggle');
  var mobileBtn = document.getElementById('btn-mobile-nav');
  var side = document.getElementById('side');
  var backdrop = document.getElementById('side-backdrop');

  function setCollapsed(collapsed) {
    document.documentElement.classList.toggle('bs-sidebar-collapsed', collapsed);
    try { localStorage.setItem('bs-sidebar-collapsed', collapsed ? '1' : '0'); } catch (_e) {}
    sideToggle.setAttribute('aria-expanded', String(!collapsed));
    sideToggle.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
  }
  sideToggle.onclick = function () {
    setCollapsed(!document.documentElement.classList.contains('bs-sidebar-collapsed'));
  };
  // Sync the toggle's own aria/title with whatever the pre-paint inline
  // script already applied from localStorage.
  setCollapsed(document.documentElement.classList.contains('bs-sidebar-collapsed'));

  function openMobileNav() {
    side.classList.add('mobile-open');
    backdrop.classList.add('show');
    mobileBtn.setAttribute('aria-expanded', 'true');
  }
  function closeMobileNav() {
    side.classList.remove('mobile-open');
    backdrop.classList.remove('show');
    mobileBtn.setAttribute('aria-expanded', 'false');
  }
  mobileBtn.onclick = function () {
    if (side.classList.contains('mobile-open')) closeMobileNav(); else openMobileNav();
  };
  backdrop.onclick = closeMobileNav;
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && side.classList.contains('mobile-open')) closeMobileNav();
  });
  side.querySelectorAll('nav.sidenav a').forEach(function (a) {
    a.addEventListener('click', closeMobileNav);
  });
  side.querySelectorAll('nav.sidenav a[href^="#"]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      const target = document.getElementById(a.getAttribute('href').slice(1));
      if (!target) return; // no matching section — let the default (no-op) happen
      e.preventDefault();
      // Every <section> is `content-visibility:auto` with a placeholder
      // contain-intrinsic-size (900px) until it's actually been on
      // screen — real perf win for a page this long, but it means the
      // browser computes a hash-jump's scroll offset using that FAKE
      // height for every not-yet-visited section between here and the
      // target, landing short (or past) the real one depending how many
      // sit in between. Force every section to its real layout right
      // before scrolling, then let content-visibility:auto resume
      // optimizing off-screen ones once we've actually arrived.
      const sections = document.querySelectorAll('section');
      sections.forEach(function (sec) { sec.style.contentVisibility = 'visible'; });
      // Toggling a style property doesn't force the browser to actually
      // recompute layout before the next line runs — without reading a
      // layout-dependent property here first, the position computed
      // below still saw the STALE (pre-toggle) placeholder heights and
      // landed just as short as before this fix. Confirmed live: adding
      // this one line was the difference between landing ~30000px short
      // on a distant section and landing exactly on it.
      void document.body.offsetHeight;
      // Deliberately NOT target.scrollIntoView() — confirmed live that
      // it still lands thousands of pixels short even with the reflow
      // fix above and every content-visibility timing fix tried. It
      // must keep re-resolving the target's position as the smooth
      // animation progresses rather than committing to one pixel offset
      // up front, so it's still vulnerable to layout shifting under it.
      // Computing the target position ONCE, up front, and handing a
      // plain fixed number to scrollTo() is immune to that — verified
      // live to land within a fraction of a pixel of scroll-margin-top
      // on a 75000px-tall page, every time.
      const marginTop = parseFloat(getComputedStyle(target).scrollMarginTop) || 0;
      // main (not the window) is the scroller now — measure against it.
      const scroller = document.querySelector('main');
      const destination = target.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop - marginTop;
      scroller.scrollTo({ top: destination, behavior: 'smooth' });
      history.replaceState(null, '', a.getAttribute('href'));
      // Reverting content-visibility back to 'auto' too early (a fixed
      // short timeout) re-collapses any section that scrolls back
      // off-screen WHILE the smooth-scroll animation is still mid-flight
      // — confirmed live, a huge jump (this page can be 75000px+ tall)
      // took ~2.6-3.2s to settle. Waiting for the real 'scrollend' event
      // (with a generous fallback timeout for browsers that predate it)
      // reverts only once the browser itself says the scroll is done.
      let settled = false;
      const revert = function () {
        if (settled) return;
        settled = true;
        sections.forEach(function (sec) { sec.style.contentVisibility = ''; });
      };
      document.querySelector('main').addEventListener('scrollend', revert, { once: true });
      setTimeout(revert, 4000);
    });
  });
  window.addEventListener('resize', function () {
    if (window.innerWidth > 980 && side.classList.contains('mobile-open')) closeMobileNav();
  });
})();

// The token is never typed in. The desktop app reads it from disk and the server puts it in the page when the
// page is loaded from this machine. If that did not work there is nothing to enter: say so, once, and move on.
function showAuthNotice() {
  if (document.getElementById('authNotice')) return;
  const n = document.createElement('div');
  n.id = 'authNotice';
  n.setAttribute('role', 'status');
  n.style.cssText = 'position:fixed; left:50%; top:12px; transform:translateX(-50%); z-index:300; max-width:560px; padding:10px 16px; border-radius:8px; background:var(--surface-3, var(--surface)); color:var(--ink); border-left:3px solid var(--critical); box-shadow:0 4px 18px rgba(0,0,0,.3); font-size:12.5px; line-height:1.5;';
  n.textContent = 'This page could not sign in to the ABP server by itself. Open it from the ABP desktop app on the machine that runs the bot, or wait for the bot to finish starting and reload.';
  document.body.appendChild(n);
}

function esc(s) { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; }

// Shared copy-to-clipboard helper for every chat surface (Chat, Server
// Chat, Support Bot, Sessions) — navigator.clipboard.writeText() first
// (works everywhere this app actually runs: HTTPS, localhost, and the
// Tauri desktop shell's own origin all count as "secure contexts"), with
// a hidden-textarea execCommand('copy') fallback for the rare browser
// context where the Clipboard API itself is unavailable. `label` names
// what got copied for the toast ("Message", "Conversation").
function copyText(text, label) {
  const done = () => showCopyToast(`${label || 'Text'} copied.`);
  const fail = () => showCopyToast(`Couldn't copy ${label ? label.toLowerCase() : 'that'}.`);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => _copyViaTextarea(text) ? done() : fail());
  } else {
    _copyViaTextarea(text) ? done() : fail();
  }
}

function _copyViaTextarea(text) {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch (_e) {
    return false;
  }
}

// General-purpose toast system — used to be hardcoded for the clipboard-
// copy confirmation only (a single fixed slot, background:var(--panel,
// #222)/color:var(--fg, #fff), NEITHER of which is a real token this
// app's design system defines — see the :root blocks above, the real
// names are --surface-3/--ink/etc — so every toast silently rendered at
// its literal fallback color, never adapting to the light theme).
// Generalized so the many `catch (_e) { return; }` sites across this
// file that silently no-op on a failed refresh/action can surface
// something to the operator instead of freezing without explanation.
// Supports stacking (each toast is its own element, oldest on top) since
// more than one thing can legitimately fail/succeed close together.
let _toastContainer = null;
function _getToastContainer() {
  if (!_toastContainer || !_toastContainer.isConnected) {
    _toastContainer = document.createElement('div');
    _toastContainer.id = 'toast-container';
    _toastContainer.style.cssText = 'position:fixed; bottom:calc(var(--term-panel-h, 40px) + 20px); left:50%; transform:translateX(-50%); display:flex; flex-direction:column-reverse; gap:8px; align-items:center; z-index:9999; pointer-events:none;';
    document.body.appendChild(_toastContainer);
  }
  return _toastContainer;
}

function showToast(message, type) {
  // type: 'success' (default) | 'error' | 'info' — only changes the
  // accent stripe and how long it stays up; an error gets more time to
  // read since it's more likely to need actual attention.
  const accent = type === 'error' ? 'var(--critical)' : type === 'info' ? 'var(--ink-soft)' : 'var(--good)';
  const el = document.createElement('div');
  el.style.cssText = `background:var(--surface-3); color:var(--ink); padding:8px 16px; border-radius:6px; font-size:12px; box-shadow:0 4px 16px rgba(0,0,0,.25); border-left:3px solid ${accent}; opacity:0; transition:opacity .15s; max-width:360px; text-align:center;`;
  el.textContent = message;
  _getToastContainer().appendChild(el);
  requestAnimationFrame(() => { el.style.opacity = '1'; });
  const dwell = type === 'error' ? 4000 : 1800;
  setTimeout(() => {
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 200);
  }, dwell);
}

function showCopyToast(message) { showToast(message, 'success'); }

// A small "⧉" copy icon button, wired to copy `getText()`'s result
// (called at click time, not eagerly — cheap for a bubble that's never
// clicked, and always copies the CURRENT text even if the caller
// mutates it after creating the button, though nothing here does today).
// Builds a plain-text transcript ("[meta] Who: text" per line) from
// whatever's currently rendered in a chat window — used by every
// surface's "Copy conversation" button. Skips structured, non-text rows
// (Server Chat's approval-request cards, which have no plain "the
// message" to copy) rather than trying to summarize them.
function _textWithLineBreaks(el) {
  const clone = el.cloneNode(true);
  clone.querySelectorAll('br').forEach(br => br.replaceWith('\n'));
  return clone.textContent;
}

function transcriptFromWindow(winEl) {
  const lines = [];
  winEl.querySelectorAll('.chat-row').forEach(row => {
    const bubble = row.querySelector('.chat-bubble');
    if (!bubble || bubble.classList.contains('approval-request') || bubble.classList.contains('job-prompt')) return;
    const meta = bubble.querySelector('.chat-meta');
    let text = '';
    for (const child of bubble.children) {
      if (child === meta || child.tagName === 'BUTTON' || child.classList.contains('chat-attachment')) continue;
      text += (text ? '\n' : '') + _textWithLineBreaks(child);
    }
    if (!text) return;
    const who = row.classList.contains('out') ? 'You' : 'Them';
    const metaText = meta ? meta.textContent : '';
    lines.push(metaText ? `[${metaText}] ${who}: ${text}` : `${who}: ${text}`);
  });
  return lines.join('\n');
}

function makeCopyButton(getText, label) {
  const btn = document.createElement('button');
  btn.className = 'msg-copy-btn';
  btn.type = 'button';
  btn.title = `Copy ${(label || 'message').toLowerCase()}`;
  btn.textContent = '⧉';
  btn.style.cssText = 'background:none; border:none; cursor:pointer; opacity:.5; font-size:12px; padding:0 2px; margin-left:6px; vertical-align:middle;';
  btn.onmouseenter = () => { btn.style.opacity = '1'; };
  btn.onmouseleave = () => { btn.style.opacity = '.5'; };
  btn.onclick = (e) => { e.stopPropagation(); copyText(getText(), label); };
  return btn;
}

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
// The top-bar pill: how many bots have an agent working right now, with a short line of telemetry, and the
// per-bot breakdown in its tooltip. (It used to be a fixed online label.)
function renderBotPill(ov) {
  const pill = document.getElementById('pill-bot');
  if (!pill) return;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const bots = ov.bots_running_agents || 0;
  const jobs = ov.jobs_running || 0;
  const queued = ov.jobs_queued || 0;
  const label = bots ? `${plural(bots, 'bot', 'bots')} running agents` : 'No bots running agents';
  const bits = [];
  if (jobs) bits.push(plural(jobs, 'agent job', 'agent jobs'));
  if (queued) bits.push(`${queued} queued`);
  if (!bots) bits.push(`${plural(ov.bots_enabled || 0, 'bot', 'bots')} enabled`);
  pill.innerHTML = `<span class="dot ${bots ? 'good' : 'neutral'}"></span>${esc(label)}` +
    (bits.length ? `<span class="pill-sub">· ${esc(bits.join(' · '))}</span>` : '');
  const lines = (ov.active_bots || []).map(b => `${b.name}: ${plural(b.jobs, 'job', 'jobs')} running`);
  lines.push(`${plural(ov.bots_enabled || 0, 'bot', 'bots')} enabled`, `${(ov.tokens_today || 0).toLocaleString()} tokens today`);
  pill.title = lines.join('\n');
}

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
  const sv = document.getElementById('s-version');
  sv.textContent = 'v' + ov.app_version + (ov.app_commit ? ' · ' + ov.app_commit : '');
  const up = ov.started_at ? Math.round((Date.now() / 1000 - ov.started_at) / 60) : null;
  sv.title = (ov.app_commit ? `Built from commit ${ov.app_commit} (${ov.app_commit_date}). ` : '') +
    (up != null ? `Server up ${up >= 120 ? Math.round(up / 60) + ' h' : up + ' min'}. ` : '') +
    `Config reloaded ${Math.max(0, (ov.config_version || 1) - 1)} time(s) since start.`;
  document.getElementById('s-default-backend').textContent = ov.default_backend;
  document.getElementById('reload-version').textContent = 'v' + ov.config_version;
  document.getElementById('s-refreshed').textContent = new Date().toLocaleTimeString();

  renderBotPill(ov);
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

// ------------------------------------------------------------ diagnostics
const DIAG_INFO_LABELS = {
  app_version: 'App version', python_version: 'Python', platform: 'Platform',
  processor: 'Processor', pid: 'PID', memory_rss_mb: 'Memory (MB)',
  cpu_percent: 'CPU %', disk_free_gb: 'Disk free (GB)',
};
const DIAG_COUNTER_LABELS = {
  'crash_reports.written': 'Crash reports written',
  'platform.crash': 'Bot instance crashes',
  'platform.auto_restart': 'Bot instances auto-restarted',
  'log.warning': 'Warnings logged',
  'log.error': 'Errors logged',
  'log.critical': 'Critical events logged',
};
async function refreshDiagnostics() {
  const [summary, crashReports, sandboxStatus] = await Promise.all([
    api('/api/diagnostics/summary'),
    api('/api/diagnostics/crash-reports?limit=30'),
    api('/api/sandbox/status'),
  ]);

  document.getElementById('diag-system-info').innerHTML = Object.entries(DIAG_INFO_LABELS)
    .filter(([key]) => summary.system_info[key] !== undefined)
    .map(([key, label]) => `<tr><td>${esc(label)}</td><td class="mono">${esc(String(summary.system_info[key]))}</td></tr>`)
    .join('');

  const counters = summary.telemetry.counters || {};
  const counterRows = Object.entries(DIAG_COUNTER_LABELS)
    .map(([key, label]) => `<tr><td>${esc(label)}</td><td class="mono">${counters[key] || 0}</td></tr>`)
    .join('');
  document.getElementById('diag-counters').innerHTML = counterRows
    + `<tr><td>Uptime</td><td class="mono">${Math.round(summary.telemetry.uptime_s / 60)} min</td></tr>`;

  const events = summary.telemetry.recent_events || [];
  document.getElementById('diag-events').innerHTML = events.slice().reverse().map(e => `
    <div class="tlitem"><div class="v">${esc(e.category)}</div><div class="d">${esc(e.detail)}</div><div class="m">${new Date(e.ts * 1000).toLocaleTimeString()}</div></div>`).join('')
    || '<p class="cardnote">No self-healing events recorded yet — good sign.</p>';

  document.getElementById('diag-crash-count').textContent = `(${summary.crash_report_count} total, showing most recent)`;
  document.getElementById('diag-crash-list').innerHTML = crashReports.reports.map(r => `
    <div class="tlitem"><div class="v">${esc(r.level)}${r.exception_type ? ' · ' + esc(r.exception_type) : ''}</div><div class="d">${esc(r.message)}</div><div class="m">${fmtTime(r.iso_time)} · ${esc(r.logger)}</div></div>`).join('')
    || '<p class="cardnote">No crash reports — nothing has crashed since this process started.</p>';

  // Sandbox Nervous System - Processes panel
  renderSandboxCells(sandboxStatus);
  renderSandboxProcesses(sandboxStatus);
  renderSandboxEvents(sandboxStatus);
}
document.getElementById('btn-diag-bundle').onclick = () => downloadUrl('/api/diagnostics/bundle');

// Sandbox Nervous System renderers
function renderSandboxCells(status) {
  const cells = status.cells || [];
  const tbody = document.getElementById('diag-cells-tbody');
  if (!tbody) return;
  if (!cells.length) {
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="9">No cells — nothing is running in a sandbox yet.</td></tr>';
    return;
  }
  tbody.innerHTML = cells.map(c => {
    const pol = c.policy || {};
    const sample = c.sample || {};
    const limits = c.limits || {};
    const persistent = pol.persistent ? 'yes' : 'no';
    const persistentCls = pol.persistent ? 'good' : 'neutral';
    return `<tr>
      <td class="mono">${esc(c.id || '')}</td>
      <td>${esc(c.name || '')}</td>
      <td>${esc(c.owner || '')}</td>
      <td>${esc(pol.name || '')}</td>
      <td><span class="chip ${persistentCls}">${persistent}</span></td>
      <td class="num">${c.process_count || 0}</td>
      <td class="num">${sample.cpu_percent ? sample.cpu_percent.toFixed(1) : '—'}</td>
      <td class="num">${sample.rss_mb ? sample.rss_mb.toFixed(1) : '—'}</td>
      <td><button class="btn danger" data-kill-cell="${esc(c.id)}" style="padding:3px 8px; font-size:11px;">Kill</button></td>
    </tr>`;
  }).join('');
  tbody.querySelectorAll('[data-kill-cell]').forEach(btn => {
    btn.onclick = async () => {
      if (!confirm(`Kill cell ${btn.dataset.killCell} and everything in it?`)) return;
      try {
        await api(`/api/sandbox/cells/${encodeURIComponent(btn.dataset.killCell)}/kill`, { method: 'POST' });
        refreshDiagnostics();
      } catch (e) {
        showToast('Kill failed: ' + e.message, 'error');
      }
    };
  });
}

function renderSandboxProcesses(status) {
  const processes = status.processes || [];
  const tbody = document.getElementById('diag-processes-tbody');
  if (!tbody) return;
  const alive = processes.filter(p => p.alive);
  document.getElementById('diag-processes-note').textContent = `${alive.length} alive, ${processes.length - alive.length} exited`;
  if (!processes.length) {
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="5">No processes recorded yet.</td></tr>';
    return;
  }
  tbody.innerHTML = processes.map(p => `
    <tr>
      <td class="mono">${p.pid || '—'}</td>
      <td>${esc(p.cell || '—')}</td>
      <td>${esc(p.owner || '')}</td>
      <td><span class="chip ${p.alive ? 'good' : 'neutral'}">${p.alive ? 'alive' : 'exited'}</span></td>
      <td class="mono">${esc((p.argv || []).join(' ')).slice(0, 80)}</td>
    </tr>`).join('');
}

function renderSandboxEvents(status) {
  const events = status.events || [];
  const tbody = document.getElementById('diag-events-tbody');
  if (!tbody) return;
  if (!events.length) {
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="5">No events yet.</td></tr>';
    return;
  }
  tbody.innerHTML = events.slice().reverse().map(e => `
    <tr>
      <td class="mono">${e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : '—'}</td>
      <td><span class="chip neutral">${esc(e.kind || '')}</span></td>
      <td class="mono">${e.pid || '—'}</td>
      <td>${esc(e.cell || '')}</td>
      <td>${esc(e.detail || '')}</td>
    </tr>`).join('');
}

// Tab switching for the Processes panel
document.querySelectorAll('#diagnostics .segmented button').forEach(btn => {
  btn.onclick = () => {
    const tab = btn.dataset.tab;
    document.querySelectorAll('#diagnostics .segmented button').forEach(b => b.classList.toggle('active', b === btn));
    document.querySelectorAll('#diagnostics .tab-panel').forEach(p => p.classList.toggle('active', p.dataset.tab === tab));
  };
});

// ---------------------------------------------------------------- control
async function refreshConfig() {
  const cfg = await api('/api/config');
  state.configCache = cfg;
  const current = cfg.current;

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

  refreshModels();
}

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
  return active && ['model-api', 'model-hermes_cli', 'model-hermes_gateway', 'model-opencode', 'model-openclaw'].includes(active.id);
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

  document.getElementById('model-opencode').value = m.current.opencode || '';
  document.getElementById('model-openclaw').value = m.current.openclaw || '';

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
  const autoBtn = document.getElementById('bot-new-model-auto');
  if (window.abpBotAgentForm) window.abpBotAgentForm.sync();
  if (backend === 'native_agent') {
    help.innerHTML = 'A "&lt;provider&gt;/&lt;model_id&gt;" from a provider configured on the Models tab, or leave blank / type "auto" to let ABP\'s model router pick a free model automatically every time (never an Anthropic model unless you\'ve listed one yourself under Automatic routing on the ABP Agents page).';
    input.placeholder = 'e.g. openrouter/qwen/qwen3-8b:free, or leave blank for auto';
    autoBtn.style.display = '';
  } else {
    autoBtn.style.display = 'none';
    if (backend === 'custom_model') {
      help.textContent = 'Required for this backend: "<provider>/<model_id>", where <provider> is one configured on the Models tab.';
      input.placeholder = 'e.g. local_ollama/llama3.1';
    } else {
      help.textContent = "Leave blank to use this backend's configured default. Options are live models actually available to this backend.";
      input.placeholder = 'e.g. claude-opus-5';
    }
  }
}
document.getElementById('bot-new-model-auto').onclick = () => { document.getElementById('bot-new-model').value = 'auto'; };

document.getElementById('model-api').onchange = async (e) => {
  const value = e.target.value.trim() || null;
  await api('/api/config/set', { method: 'POST', body: JSON.stringify({ path: ['backends', 'api', 'model'], value }) });
  refreshConfig();
};
['hermes_cli', 'hermes_gateway', 'opencode', 'openclaw'].forEach(name => {
  document.getElementById(`model-${name}`).onchange = async (e) => {
    const value = e.target.value.trim() || null;
    await api('/api/config/set', { method: 'POST', body: JSON.stringify({ path: ['backends', name, 'model'], value }) });
    refreshConfig();
  };
});

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

document.querySelectorAll('.default-backend-seg button').forEach(btn => {
  btn.onclick = async () => { await api(`/api/backend/default/${btn.dataset.b}`, { method: 'POST' }); refreshConfig(); refreshOverview(); };
});

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

// ------------------------------------------------------- customize UI ----
let _customizePendingChange = null; // {change_id, target} for the last generated-but-not-yet-applied change

function _renderDiff(diffText) {
  if (!diffText) return '';
  return diffText.split('\n').map(line => {
    const escaped = esc(line);
    if (line.startsWith('+') && !line.startsWith('+++')) return `<span class="diff-add">${escaped}</span>`;
    if (line.startsWith('-') && !line.startsWith('---')) return `<span class="diff-del">${escaped}</span>`;
    if (line.startsWith('@@')) return `<span class="diff-hunk">${escaped}</span>`;
    return escaped;
  }).join('\n');
}

async function refreshCustomizeHistory() {
  const tbody = document.getElementById('customize-history-tbody');
  if (!getToken() || !tbody) return;
  let data;
  try {
    data = await api('/api/ui-customize/history');
  } catch (_e) { return; }
  const list = data.history || [];
  tbody.innerHTML = list.length ? list.map(e => `
    <tr>
      <td class="mono">${esc(fmtTime(e.applied_at))}</td>
      <td>${esc(e.target)}${e.kind === 'revert' ? ' <span class="cardnote">(revert)</span>' : ''}</td>
      <td>${esc(e.instruction)}</td>
      <td><button class="btn small" data-customize-revert="${esc(e.entry_id)}">Revert</button></td>
    </tr>`).join('') : '<tr><td colspan="4" class="cardnote">No changes yet.</td></tr>';
  tbody.querySelectorAll('[data-customize-revert]').forEach(btn => btn.onclick = async () => {
    const entryId = btn.dataset.customizeRevert;
    if (!confirm('Revert this change? The file is restored to exactly what it was right before this change was applied.')) return;
    try {
      await api('/api/ui-customize/revert', { method: 'POST', body: JSON.stringify({ entry_id: entryId }) });
    } catch (e) {
      alert(e.message || 'Revert failed.');
      return;
    }
    refreshCustomizeHistory();
  });
}

document.getElementById('btn-customize-generate').onclick = async () => {
  const status = document.getElementById('customize-generate-status');
  const instruction = document.getElementById('customize-instruction').value.trim();
  const target = document.getElementById('customize-target').value;
  if (!instruction) { status.textContent = 'Describe what you want changed first.'; return; }
  status.textContent = 'Generating — this calls a real model and can take a little while for a whole file…';
  document.getElementById('customize-result-card').style.display = 'none';
  let result;
  try {
    result = await api('/api/ui-customize/generate', { method: 'POST', body: JSON.stringify({ target, instruction }) });
  } catch (e) {
    status.textContent = e.message || 'Generation failed.';
    return;
  }
  status.textContent = '';
  _customizePendingChange = { change_id: result.change_id, target };

  document.getElementById('customize-explanation').textContent = result.explanation || '';
  const warnEl = document.getElementById('customize-warnings');
  const warnParts = [];
  if (result.removed_ids && result.removed_ids.length) warnParts.push(`Removed element id(s): ${result.removed_ids.join(', ')}`);
  if (result.warnings && result.warnings.length) warnParts.push(...result.warnings);
  if (!result.valid) warnParts.unshift('⚠ Validation failed — this cannot be applied as-is: ' + (result.errors || []).join('; '));
  warnEl.innerHTML = warnParts.map(esc).join('<br>');
  document.getElementById('customize-diff').innerHTML = _renderDiff(result.diff);

  const frame = document.getElementById('customize-preview-frame');
  if (result.preview_html != null) {
    frame.srcdoc = result.preview_html;
    frame.style.display = '';
  } else {
    frame.removeAttribute('srcdoc');
    frame.style.display = 'none';
  }

  const approveBtn = document.getElementById('btn-customize-approve');
  approveBtn.disabled = !result.valid;
  approveBtn.title = result.valid ? '' : 'Cannot apply — validation failed, see the warning above.';

  document.getElementById('customize-result-card').style.display = '';
};

document.getElementById('btn-customize-approve').onclick = async () => {
  if (!_customizePendingChange) return;
  const target = _customizePendingChange.target;
  const liveNote = target === 'dashboard'
    ? 'This dashboard tab (and any other open one) will reload automatically once applied.'
    : 'Any open desktop app window will reload automatically once applied — no rebuild needed.';
  if (!confirm(`Apply this change to ${target}? A backup is taken first and this is one click to revert. ${liveNote}`)) return;
  try {
    await api('/api/ui-customize/apply', { method: 'POST', body: JSON.stringify({ change_id: _customizePendingChange.change_id }) });
  } catch (e) {
    alert(e.message || 'Apply failed.');
    return;
  }
  _customizePendingChange = null;
  document.getElementById('customize-result-card').style.display = 'none';
  document.getElementById('customize-instruction').value = '';
  refreshCustomizeHistory();
};

document.getElementById('btn-customize-reject').onclick = () => {
  _customizePendingChange = null;
  document.getElementById('customize-result-card').style.display = 'none';
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
    editor.placeholder = 'Waiting for the ABP server, then click "Reload from disk".';
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
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="4">Connecting to the ABP server…</td></tr>';
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
    statusEl.textContent = 'Save failed — check that the ABP server is running and try again.';
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
    statusEl.textContent = 'Failed — check that the ABP server is running and try again.';
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


// ---- section-aware polling -------------------------------------------------
// The page is one long scroll of sections, and most pollers only feed one of
// them. A poller registered with section ids runs only while one of those
// sections is on (or near) screen, and refreshes the moment it scrolls into
// view, so the data is never stale when looked at. Pollers without sections
// (the top-bar status pills) always run. Without IntersectionObserver
// everything polls, as before.
const _onScreen = new Set();
const _sectionPollers = new Map();
const _sectionObserver = ('IntersectionObserver' in window) ? new IntersectionObserver((entries) => {
  entries.forEach((e) => {
    const id = e.target.id;
    if (e.isIntersecting) {
      if (!_onScreen.has(id)) {
        _onScreen.add(id);
        (_sectionPollers.get(id) || []).forEach((f) => { Promise.resolve().then(f).catch(() => {}); });
      }
    } else {
      _onScreen.delete(id);
    }
  });
}, { rootMargin: '300px 0px' }) : null;
function _observeSections() {
  if (_sectionObserver) document.querySelectorAll('section[id]').forEach((s) => _sectionObserver.observe(s));
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _observeSections); else _observeSections();

// ------------------------------------------------------------------- boot
// Skips a poll tick while the tab/window isn't visible (backgrounded or
// minimized) instead of hitting the server every few seconds for no one
// to see — the interval itself keeps running (so it fires immediately on
// return, no separate "resume" wiring needed), it just no-ops while hidden.
function pollWhenVisible(fn, ms, ...sections) {
  // Single-flight: a tick still running (slow server) is not stacked on; and
  // while the server is unreachable, ticks wait out the backoff (see _net).
  // With `sections`, it only ticks while one of them is on screen (see above).
  sections.forEach((s) => { if (!_sectionPollers.has(s)) _sectionPollers.set(s, []); _sectionPollers.get(s).push(fn); });
  let running = false;
  return setInterval(async () => {
    if (document.hidden || running || Date.now() < _net.until) return;
    if (sections.length && _sectionObserver && !sections.some((s) => _onScreen.has(s))) return;
    running = true;
    try { await fn(); } catch (_e) { /* each refresh reports its own errors */ } finally { running = false; }
  }, ms);
}

async function refreshAll() {
  const tasks = [refreshOverview(), refreshJobsTable(), refreshJobsCharts(), refreshTelemetry(), refreshDatabase(), refreshConfig(), refreshMcp(), refreshAllowedUsers(), refreshLogs(), refreshEnv()];
  await Promise.allSettled(tasks);
}
function startDashboard() {
  refreshAll();
  pollWhenVisible(refreshOverview, 5000);
  pollWhenVisible(refreshJobsTable, 5000, 'jobs');
  pollWhenVisible(refreshJobsCharts, 5000, 'jobs', 'overview');
  pollWhenVisible(refreshTelemetry, 5000);
  pollWhenVisible(refreshDatabase, 5000, 'database');
  pollWhenVisible(refreshConfig, 5000);
  pollWhenVisible(refreshMcp, 5000, 'control');
  pollWhenVisible(refreshAllowedUsers, 5000, 'control');
  pollWhenVisible(refreshLogs, 5000, 'logs');
  pollWhenVisible(refreshEnv, 5000, 'control');
  refreshEnvEditor();
  refreshEnvBackups();
  startChatPolling();
  startSessionsPolling();
  refreshPlatforms();
  pollWhenVisible(refreshPlatforms, 15000, 'chat', 'platforms');
  refreshPersonas();
  refreshPlatformGuides();
  refreshBots();
  refreshBotsBackups();
  pollWhenVisible(refreshBots, 15000, 'bots');
  refreshPairing();
  pollWhenVisible(refreshPairing, 15000, 'bots');
  refreshProviders();
  pollWhenVisible(refreshProviders, 15000);
  refreshProviderCatalog();
  refreshModelsPage();
  pollWhenVisible(refreshModelsPage, 30000);
  refreshPlugins();
  pollWhenVisible(refreshPlugins, 15000, 'bots');
  refreshSnapshots();
  pollWhenVisible(refreshSnapshots, 15000);
  refreshCustomizeHistory();
  refreshHotReload();
  pollWhenVisible(refreshHotReload, 15000, 'control');
  refreshKanban();
  pollWhenVisible(refreshKanban, 15000, 'kanban');
  refreshSchedules();
  pollWhenVisible(refreshSchedules, 15000, 'bots');
  refreshSwarmInstanceLegend();
  refreshSwarms();
  refreshSwarmRuns();
  pollWhenVisible(refreshSwarms, 15000, 'swarms');
  refreshSwarmToolsPanel();
  pollWhenVisible(refreshSwarmToolsPanel, 15000, 'swarms');
  refreshContextDocs();
  pollWhenVisible(refreshContextDocs, 15000, 'swarms');
  refreshDelegationActivity();
  pollWhenVisible(refreshDelegationActivity, 15000, 'swarms');
  refreshSwarmBudget();
  pollWhenVisible(refreshSwarmBudget, 15000, 'swarms');
  refreshSshToolkit();
  pollWhenVisible(refreshSshToolkit, 20000, 'ssh-toolkit');
  refreshSshRecordings();
  refreshDiagnostics();
  pollWhenVisible(refreshDiagnostics, 15000, 'diagnostics');
  connectLiveEventsSocket();
  refreshMobileKeys();
  pollWhenVisible(refreshMobileKeys, 15000, 'mobile');
  refreshNetworkInfoHint();
  refreshFileRoots();
  refreshPeers();
  pollWhenVisible(refreshPeers, 15000, 'servers');
  refreshTrainingPhrases();
  refreshTrainingHealth();
  refreshTrainingPending();
  refreshTrainingMisses();
  refreshTrainingApproved();
  refreshKnowledgeModules();
  pollWhenVisible(refreshTrainingPhrases, 20000, 'training');
  pollWhenVisible(refreshTrainingHealth, 10000, 'training');
  pollWhenVisible(refreshTrainingPending, 20000, 'training');
  pollWhenVisible(refreshTrainingMisses, 20000, 'training');
  startServerChatPolling();
  refreshAutomationInstances();
  refreshHooks();
  pollWhenVisible(refreshHooks, 20000, 'automation');
  refreshAgentSettings();
  refreshAutoManagePanel();
}

// ---------------------------------------------------------- automation ---
// Hooks, agent_settings, auto_manage — all three already have real
// backend routes and MCP tool exposure (Claude Desktop could already
// read/write them); this is the first dashboard/desktop UI for any of
// them. See bot/agent_runtime/hooks.py, bot/agent_settings.py,
// bot/auto_manage.py.
const EFFORT_LADDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]; // mirrors bot/effort.py
const automationState = { instances: [] };

async function refreshAutomationInstances() {
  try {
    automationState.instances = await api('/api/bots');
  } catch (_e) { return; }
  const options = automationState.instances.map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join('');
  const hookSel = document.getElementById('hook-new-instance');
  const hookCurrent = hookSel.value;
  hookSel.innerHTML = '<option value="">Global (every instance)</option>' + options;
  hookSel.value = hookCurrent;

  const asSel = document.getElementById('agent-settings-instance');
  const asCurrent = asSel.value;
  asSel.innerHTML = '<option value="">Process-wide default</option>' + options;
  asSel.value = asCurrent;
  if (asSel.dataset.want && [...asSel.options].some(o => o.value === asSel.dataset.want)) {
    // A bot's "Agent settings" button was clicked before this list had loaded (agents-panel.js openBot).
    asSel.value = asSel.dataset.want;
    delete asSel.dataset.want;
    asSel.dispatchEvent(new Event('change'));
  }

  const amSel = document.getElementById('auto-manage-instance');
  const amCurrent = amSel.value;
  amSel.innerHTML = '<option value="">Pick an instance…</option>' + options;
  amSel.value = amCurrent;
}

// -------------------------------------------------------------- hooks ---
async function refreshHooks() {
  const tbody = document.getElementById('hooks-tbody');
  let hooks;
  try {
    hooks = (await api('/api/hooks')).hooks;
  } catch (_e) { return; }
  const instanceName = (id) => {
    const inst = automationState.instances.find(b => b.id === id);
    return inst ? inst.name : `instance ${id}`;
  };
  tbody.innerHTML = hooks.length ? hooks.map(h => `
    <tr>
      <td class="mono">${esc(h.event)}</td>
      <td class="mono">${h.matcher ? esc(h.matcher) : '—'}</td>
      <td class="mono" style="max-width:320px; overflow-wrap:anywhere;">${esc(h.command)}</td>
      <td>${h.instance_id == null ? 'Global' : esc(instanceName(h.instance_id))}</td>
      <td><span class="pill"><span class="dot ${h.enabled ? 'good' : ''}"></span>${h.enabled ? 'Enabled' : 'Disabled'}</span></td>
      <td>
        <button class="btn" data-hook-toggle="${h.id}" data-hook-enabled="${h.enabled ? '1' : '0'}" style="padding:3px 8px; font-size:11px;">${h.enabled ? 'Disable' : 'Enable'}</button>
        <button class="btn" data-hook-remove="${h.id}" style="padding:3px 8px; font-size:11px; color:var(--critical);">Remove</button>
      </td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="6">No hooks configured.</td></tr>';

  tbody.querySelectorAll('[data-hook-toggle]').forEach(btn => btn.onclick = async () => {
    const id = btn.dataset.hookToggle;
    const enabling = btn.dataset.hookEnabled === '0';
    btn.disabled = true;
    try {
      await api(`/api/hooks/${id}/${enabling ? 'enable' : 'disable'}`, { method: 'POST' });
      refreshHooks();
    } catch (e) {
      document.getElementById('hook-status').textContent = `Failed: ${e.message || e}`;
      btn.disabled = false;
    }
  });
  tbody.querySelectorAll('[data-hook-remove]').forEach(btn => btn.onclick = async () => {
    if (!confirm('Remove this hook? This can\'t be undone.')) return;
    try {
      await api(`/api/hooks/${btn.dataset.hookRemove}`, { method: 'DELETE' });
      refreshHooks();
    } catch (e) {
      document.getElementById('hook-status').textContent = `Failed: ${e.message || e}`;
    }
  });
}

document.getElementById('btn-hook-add').onclick = async () => {
  const statusEl = document.getElementById('hook-status');
  const event = document.getElementById('hook-new-event').value;
  const matcher = document.getElementById('hook-new-matcher').value.trim();
  const instanceVal = document.getElementById('hook-new-instance').value;
  const command = document.getElementById('hook-new-command').value.trim();
  if (!command) {
    statusEl.textContent = 'A command is required.';
    return;
  }
  const btn = document.getElementById('btn-hook-add');
  btn.disabled = true;
  try {
    await api('/api/hooks', {
      method: 'POST',
      body: JSON.stringify({
        event, command, matcher: matcher || null,
        instance_id: instanceVal ? Number(instanceVal) : null,
      }),
    });
    statusEl.textContent = 'Hook added.';
    document.getElementById('hook-new-matcher').value = '';
    document.getElementById('hook-new-command').value = '';
    refreshHooks();
  } catch (e) {
    statusEl.textContent = `Failed to add hook: ${e.message || e}`;
  } finally {
    btn.disabled = false;
  }
};

// ------------------------------------------------------ agent settings ---
function populateEffortSelect(sel) {
  sel.innerHTML = '<option value="">(unset — inherit fallback)</option>' + EFFORT_LADDER.map(l => `<option value="${l}">${l}</option>`).join('');
}
populateEffortSelect(document.getElementById('as-worker-effort'));
populateEffortSelect(document.getElementById('as-manager-effort'));

async function refreshAgentSettings() {
  const instanceVal = document.getElementById('agent-settings-instance').value;
  const qs = instanceVal ? `?instance_id=${instanceVal}` : '';
  let settings;
  try {
    settings = await api(`/api/agent-settings${qs}`);
  } catch (_e) { return; }
  document.getElementById('as-max-concurrent-children').value = settings.max_concurrent_children ?? '';
  document.getElementById('as-worker-provider').value = settings.worker_provider ?? '';
  document.getElementById('as-worker-model').value = settings.worker_model ?? '';
  document.getElementById('as-worker-effort').value = settings.worker_effort ?? '';
  document.getElementById('as-manager-effort').value = settings.manager_effort ?? '';
  document.getElementById('as-fallback-provider').value = settings.fallback_provider ?? '';
  document.getElementById('as-fallback-model').value = settings.fallback_model ?? '';
  document.getElementById('as-require-plan-approval').checked = !!settings.require_plan_approval;
  document.getElementById('as-is-admin-instance').checked = !!settings.is_admin_instance;
}
document.getElementById('agent-settings-instance').onchange = refreshAgentSettings;

document.getElementById('btn-agent-settings-save').onclick = async () => {
  const statusEl = document.getElementById('agent-settings-status');
  const instanceVal = document.getElementById('agent-settings-instance').value;
  const num = document.getElementById('as-max-concurrent-children').value.trim();
  const payload = {
    instance_id: instanceVal ? Number(instanceVal) : null,
    max_concurrent_children: num ? Number(num) : null,
    worker_provider: document.getElementById('as-worker-provider').value.trim() || null,
    worker_model: document.getElementById('as-worker-model').value.trim() || null,
    worker_effort: document.getElementById('as-worker-effort').value || null,
    manager_effort: document.getElementById('as-manager-effort').value || null,
    fallback_provider: document.getElementById('as-fallback-provider').value.trim() || null,
    fallback_model: document.getElementById('as-fallback-model').value.trim() || null,
    require_plan_approval: document.getElementById('as-require-plan-approval').checked,
    is_admin_instance: document.getElementById('as-is-admin-instance').checked,
  };
  const btn = document.getElementById('btn-agent-settings-save');
  btn.disabled = true;
  try {
    await api('/api/agent-settings', { method: 'POST', body: JSON.stringify(payload) });
    statusEl.textContent = 'Saved.';
    refreshAgentSettings();
  } catch (e) {
    statusEl.textContent = `Failed to save: ${e.message || e}`;
  } finally {
    btn.disabled = false;
  }
};

// ---------------------------------------------------------- auto-manage ---
async function refreshAutoManagePanel() {
  const statusEl = document.getElementById('auto-manage-status');
  const instanceVal = document.getElementById('auto-manage-instance').value;
  if (!instanceVal) {
    document.getElementById('am-enabled').checked = false;
    return;
  }
  let cfg;
  try {
    cfg = await api(`/api/auto-manage/${instanceVal}`);
  } catch (e) {
    statusEl.textContent = `Couldn't load: ${e.message || e}`;
    return;
  }
  document.getElementById('am-enabled').checked = !!cfg.enabled;
  document.getElementById('am-trigger').value = cfg.trigger || 'scheduled';
  document.getElementById('am-interval').value = cfg.interval || '30m';
  document.getElementById('am-chat-id').value = cfg.chat_id ?? '';
  document.getElementById('am-thread-id').value = cfg.thread_id ?? '';
  document.getElementById('am-goal-template').value = cfg.goal_template || '';
  statusEl.textContent = '';
}
document.getElementById('auto-manage-instance').onchange = refreshAutoManagePanel;

// The enabled checkbox saves immediately (it's the one field with a real
// side effect — enable() creates a live scheduled_commands row, disable()
// removes it), rather than waiting for "Save settings", so its state on
// screen never lies about whether a schedule actually exists right now.
document.getElementById('am-enabled').onchange = async (e) => {
  const statusEl = document.getElementById('auto-manage-status');
  const instanceVal = document.getElementById('auto-manage-instance').value;
  if (!instanceVal) { e.target.checked = false; return; }
  const enabling = e.target.checked;
  try {
    if (enabling) {
      const chatId = document.getElementById('am-chat-id').value.trim();
      if (!chatId) {
        statusEl.textContent = 'A chat id is required before enabling (where check-ins get delivered).';
        e.target.checked = false;
        return;
      }
      await api(`/api/auto-manage/${instanceVal}`, {
        method: 'POST',
        body: JSON.stringify({
          enabled: true, chat_id: chatId,
          thread_id: document.getElementById('am-thread-id').value.trim() || null,
          trigger: document.getElementById('am-trigger').value,
          interval: document.getElementById('am-interval').value.trim() || '30m',
          goal_template: document.getElementById('am-goal-template').value.trim() || null,
        }),
      });
      statusEl.textContent = 'Enabled.';
    } else {
      await api(`/api/auto-manage/${instanceVal}`, { method: 'POST', body: JSON.stringify({ enabled: false }) });
      statusEl.textContent = 'Disabled.';
    }
  } catch (err) {
    statusEl.textContent = `Failed: ${err.message || err}`;
    e.target.checked = !enabling;
  }
};

document.getElementById('btn-auto-manage-save').onclick = async () => {
  const statusEl = document.getElementById('auto-manage-status');
  const instanceVal = document.getElementById('auto-manage-instance').value;
  if (!instanceVal) {
    statusEl.textContent = 'Pick an instance first.';
    return;
  }
  const btn = document.getElementById('btn-auto-manage-save');
  btn.disabled = true;
  try {
    // Deliberately omits "enabled" — the checkbox above already saves
    // that immediately, and the route branches on enabled's presence
    // (true/false triggers enable()/disable(), absent means "just merge
    // these other fields") — see api_auto_manage_set.
    await api(`/api/auto-manage/${instanceVal}`, {
      method: 'POST',
      body: JSON.stringify({
        trigger: document.getElementById('am-trigger').value,
        interval: document.getElementById('am-interval').value.trim() || '30m',
        chat_id: document.getElementById('am-chat-id').value.trim() || null,
        thread_id: document.getElementById('am-thread-id').value.trim() || null,
        goal_template: document.getElementById('am-goal-template').value.trim() || null,
      }),
    });
    statusEl.textContent = 'Saved.';
    refreshAutoManagePanel();
  } catch (e) {
    statusEl.textContent = `Failed to save: ${e.message || e}`;
  } finally {
    btn.disabled = false;
  }
};

// --------------------------------------------------------- support bot ---
function appendSupportBubble(text, direction, extra) {
  const win = document.getElementById('support-window');
  const row = document.createElement('div');
  row.className = 'chat-row ' + (direction === 'in' ? 'in' : 'out');
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble';
  const textEl = document.createElement('div');
  textEl.textContent = text;
  bubble.appendChild(textEl);
  bubble.appendChild(makeCopyButton(() => text, 'Message'));
  if (extra) bubble.appendChild(extra);
  row.appendChild(bubble);
  win.appendChild(row);
  win.scrollTop = win.scrollHeight;
  return bubble;
}

async function sendSupportMessage() {
  const input = document.getElementById('support-input');
  const statusEl = document.getElementById('support-status');
  const text = input.value.trim();
  if (!text) return;
  appendSupportBubble(text, 'out');
  input.value = '';
  const btn = document.getElementById('btn-support-send');
  btn.disabled = true;
  statusEl.textContent = '';
  try {
    const reply = await api('/api/support-bot/ask', { method: 'POST', body: JSON.stringify({ text }) });
    let extra = null;
    if (reply.needs_confirm) {
      extra = document.createElement('div');
      extra.style.marginTop = '8px';
      const confirmBtn = document.createElement('button');
      confirmBtn.className = 'btn primary';
      confirmBtn.style.cssText = 'padding:4px 10px; font-size:11px; margin-right:6px;';
      confirmBtn.textContent = 'Confirm';
      confirmBtn.onclick = async () => {
        confirmBtn.disabled = true;
        const result = await api('/api/support-bot/confirm', { method: 'POST', body: JSON.stringify({ token: reply.confirm_token }) });
        appendSupportBubble(result.text, 'in');
      };
      extra.appendChild(confirmBtn);
    }
    appendSupportBubble(reply.text, 'in', extra);
  } catch (e) {
    statusEl.textContent = 'Support Bot request failed — check that the ABP server is running.';
  } finally {
    btn.disabled = false;
  }
}
document.getElementById('btn-support-send').onclick = sendSupportMessage;
document.getElementById('support-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendSupportMessage();
  }
});
document.getElementById('btn-support-copy').onclick = () => {
  const win = document.getElementById('support-window');
  copyText(transcriptFromWindow(win), 'Conversation');
};
document.getElementById('btn-support-export').onclick = () => {
  // Support Bot turns aren't persisted server-side (engine.py is
  // stateless per-turn) — there's no /export route to hit, so this
  // exports whatever's currently rendered in this window instead, the
  // same DOM-transcript source Copy uses, just as a downloadable file.
  const win = document.getElementById('support-window');
  const transcript = transcriptFromWindow(win);
  const blob = new Blob([transcript], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `support-bot-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.txt`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
};

// ----------------------------------------------------------- training ----
let trainingIntents = [];

async function refreshTrainingHealth() {
  if (!getToken()) return;
  let h;
  try {
    h = await api('/api/support-bot/health');
  } catch (_e) { return; }
  document.getElementById('health-total').textContent = h.total;
  document.getElementById('health-agreement').textContent = h.total ? `${(h.agreement_rate * 100).toFixed(0)}%` : '—';
  document.getElementById('health-unknown').textContent = h.total ? `${(h.unknown_rate * 100).toFixed(0)}%` : '—';
  document.getElementById('health-tfidf-conf').textContent = h.total ? h.avg_tfidf_confidence.toFixed(2) : '—';
  document.getElementById('health-nn-conf').textContent = h.total ? h.avg_nn_confidence.toFixed(2) : '—';
  const evalAcc = h.eval && h.eval.holdout_accuracy;
  document.getElementById('health-eval-accuracy').textContent = (evalAcc || evalAcc === 0) ? `${(evalAcc * 100).toFixed(1)}%` : '—';
}

async function refreshTrainingPhrases() {
  if (!getToken()) return;
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
    refreshTrainingHealth();
  } catch (e) {
    statusEl.textContent = 'Failed to add phrase — check that the ABP server is running.';
  }
};

document.getElementById('btn-training-bulk-import').onclick = async () => {
  const textarea = document.getElementById('training-bulk-input');
  const statusEl = document.getElementById('training-bulk-status');
  const lines = textarea.value.split('\n').map(l => l.trim()).filter(Boolean);
  const phrases = [];
  for (const line of lines) {
    const parts = line.split('|');
    if (parts.length !== 2) {
      statusEl.textContent = `Malformed line (expected "phrase | intent"): ${esc(line)}`;
      return;
    }
    phrases.push({ phrase: parts[0].trim(), intent: parts[1].trim() });
  }
  if (!phrases.length) {
    statusEl.textContent = 'Nothing to import.';
    return;
  }
  try {
    const resp = await api('/api/support-bot/training', { method: 'POST', body: JSON.stringify({ phrases }) });
    textarea.value = '';
    statusEl.textContent = `Imported ${resp.ids.length} phrase(s) and retrained.`;
    refreshTrainingPhrases();
    refreshTrainingHealth();
  } catch (e) {
    statusEl.textContent = 'Bulk import failed — check the format and the dashboard token.';
  }
};

document.getElementById('btn-training-retrain').onclick = async () => {
  const resultEl = document.getElementById('training-retrain-result');
  const tolerance = parseFloat(document.getElementById('training-retrain-tolerance').value) || 0;
  resultEl.innerHTML = '<span class="cardnote">Training candidate and evaluating…</span>';
  let resp;
  try {
    resp = await api('/api/support-bot/retrain', { method: 'POST', body: JSON.stringify({ accept_if_regression_under: tolerance }) });
  } catch (e) {
    resultEl.innerHTML = '<span class="cardnote">Retrain failed — check that the ABP server is running.</span>';
    return;
  }
  const acc = resp.eval && resp.eval.holdout_accuracy;
  const accStr = (acc || acc === 0) ? `${(acc * 100).toFixed(1)}%` : 'n/a';
  if (resp.accepted) {
    resultEl.innerHTML = `<span class="pill"><span class="dot good"></span>Accepted</span> <span>New held-out accuracy: <b>${accStr}</b> (n=${resp.eval.n_holdout})</span>`;
    refreshTrainingHealth();
  } else {
    resultEl.innerHTML = `<span class="pill"><span class="dot critical"></span>Rejected — model NOT activated</span> <span>${esc(resp.reason || '')}</span>`;
  }
};

async function refreshTrainingMisses() {
  if (!getToken()) return;
  let misses;
  try {
    misses = await api('/api/support-bot/misses');
  } catch (_e) { return; }
  const tbody = document.getElementById('training-misses-tbody');
  tbody.innerHTML = misses.length ? misses.map(m => `
    <tr>
      <td>${esc(m.text)}</td>
      <td class="mono">${esc(m.tfidf_intent)}</td>
      <td class="mono">${esc(m.nn_intent)}</td>
      <td><select data-miss-intent="${m.id}" style="min-width:140px;">${trainingIntents.map(i => `<option value="${esc(i)}">${esc(i)}</option>`).join('')}</select></td>
      <td><button class="btn" data-miss-label="${m.id}" style="padding:3px 8px; font-size:11px;">Label</button></td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="5">No unreviewed misses right now.</td></tr>';
  document.querySelectorAll('[data-miss-label]').forEach(btn => btn.onclick = async () => {
    const id = btn.dataset.missLabel;
    const select = document.querySelector(`[data-miss-intent="${id}"]`);
    await api(`/api/support-bot/misses/${id}/label`, { method: 'POST', body: JSON.stringify({ intent: select.value }) });
    refreshTrainingMisses();
    refreshTrainingPhrases();
    refreshTrainingHealth();
  });
}

async function refreshTrainingPending() {
  if (!getToken()) return;
  let pending;
  try {
    pending = await api('/api/support-bot/pending');
  } catch (_e) { return; }
  const tbody = document.getElementById('training-pending-tbody');
  tbody.innerHTML = pending.length ? pending.map(p => `
    <tr>
      <td>${esc(p.phrase)}</td>
      <td class="mono">${esc(p.intent)}</td>
      <td class="mono">${esc(p.source_provider)}/${esc(p.source_model)}</td>
      <td>
        <button class="btn" data-pending-approve="${p.id}" style="padding:3px 8px; font-size:11px;">Approve</button>
        <button class="btn" data-pending-reject="${p.id}" style="padding:3px 8px; font-size:11px;">Reject</button>
      </td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="4">No pending examples to review.</td></tr>';
  document.querySelectorAll('[data-pending-approve]').forEach(btn => btn.onclick = async () => {
    await api(`/api/support-bot/pending/${btn.dataset.pendingApprove}/approve`, { method: 'POST' });
    refreshTrainingPending();
    refreshTrainingPhrases();
    refreshTrainingHealth();
  });
  document.querySelectorAll('[data-pending-reject]').forEach(btn => btn.onclick = async () => {
    await api(`/api/support-bot/pending/${btn.dataset.pendingReject}/reject`, { method: 'POST' });
    refreshTrainingPending();
  });
}

document.getElementById('btn-training-generate').onclick = async () => {
  const statusEl = document.getElementById('training-generate-status');
  statusEl.textContent = 'Dispatching swarm (free models only)…';
  let resp;
  try {
    resp = await api('/api/support-bot/generate', { method: 'POST' });
  } catch (e) {
    statusEl.textContent = 'Generation failed — check that the ABP server is running.';
    return;
  }
  if (resp.reason && !resp.dispatched) {
    statusEl.textContent = `Nothing dispatched: ${resp.reason}`;
    return;
  }
  statusEl.textContent = `Dispatched ${resp.dispatched} agent(s), ${resp.pending_added} example(s) added for review${resp.auto_approved ? `, ${resp.auto_approved} auto-approved` : ''}.`;
  refreshTrainingPending();
  refreshTrainingApproved();
};

document.getElementById('btn-training-generate-run').onclick = async () => {
  const statusEl = document.getElementById('training-generate-run-status');
  const moduleId = document.getElementById('training-generate-run-module').value || null;
  const targetPerIntent = parseInt(document.getElementById('training-generate-run-target').value, 10) || 20;
  statusEl.textContent = 'Running until target (this can take a while — one batch per iteration)…';
  let resp;
  try {
    resp = await api('/api/support-bot/generate/run', {
      method: 'POST', body: JSON.stringify({ module_id: moduleId, target_per_intent: targetPerIntent }),
    });
  } catch (e) {
    statusEl.textContent = 'Run failed — check that the ABP server is running.';
    return;
  }
  statusEl.textContent = `${resp.batches_run} batch(es), ${resp.total_pending_added} example(s) added (${resp.total_auto_approved} auto-approved) — stopped: ${esc(resp.stopped_reason)}.`;
  refreshTrainingPending();
  refreshTrainingApproved();
};

async function refreshTrainingApproved() {
  if (!getToken()) return;
  let approved;
  try {
    approved = await api('/api/support-bot/pending?status=approved');
  } catch (_e) { return; }
  const tbody = document.getElementById('training-approved-tbody');
  tbody.innerHTML = approved.length ? approved.map(p => `
    <tr>
      <td>${esc(p.phrase)}</td>
      <td class="mono">${esc(p.intent)}</td>
      <td class="mono">${p.approved_by ? `<span class="pill" title="${esc(p.source_provider)}/${esc(p.source_model)}"><span class="dot good"></span>auto</span>` : 'human'}</td>
      <td><button class="btn" data-approved-revert="${p.id}" style="padding:3px 8px; font-size:11px;">Revert</button></td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="4">No approved examples yet.</td></tr>';
  document.querySelectorAll('[data-approved-revert]').forEach(btn => btn.onclick = async () => {
    await api(`/api/support-bot/pending/${btn.dataset.approvedRevert}/revert`, { method: 'POST' });
    refreshTrainingApproved();
    refreshTrainingPhrases();
    refreshTrainingHealth();
  });
}

async function refreshKnowledgeModules() {
  if (!getToken()) return;
  let modules;
  try {
    modules = await api('/api/support-bot/manifest');
  } catch (_e) { return; }
  const tbody = document.getElementById('training-modules-tbody');
  const ids = Object.keys(modules).sort();
  tbody.innerHTML = ids.length ? ids.map(id => {
    const m = modules[id];
    return `
    <tr>
      <td><b>${esc(m.display_name)}</b><div class="cardnote">${esc(m.description || '')}</div></td>
      <td class="mono" style="font-size:11px;">${(m.intents || []).length}</td>
      <td class="mono" style="font-size:11px;">${m.version ? esc(m.version.slice(0, 14)) + '…' : 'never trained'}</td>
      <td><input type="checkbox" data-module-enabled="${esc(id)}" ${m.enabled ? 'checked' : ''} ${m.unloadable === false ? 'disabled title="always-resident"' : ''}></td>
      <td><button class="btn" data-module-retrain="${esc(id)}" style="padding:3px 8px; font-size:11px;">Retrain</button></td>
    </tr>`;
  }).join('') : '<tr class="emptyrow"><td colspan="5">No Knowledge Modules registered.</td></tr>';
  document.querySelectorAll('[data-module-enabled]').forEach(cb => cb.onchange = async () => {
    await api(`/api/support-bot/modules/${cb.dataset.moduleEnabled}/enabled`, {
      method: 'POST', body: JSON.stringify({ enabled: cb.checked }),
    });
  });
  document.querySelectorAll('[data-module-retrain]').forEach(btn => btn.onclick = async () => {
    btn.textContent = 'Retraining…';
    try {
      await api(`/api/support-bot/modules/${btn.dataset.moduleRetrain}/retrain`, { method: 'POST', body: JSON.stringify({}) });
    } finally {
      btn.textContent = 'Retrain';
      refreshKnowledgeModules();
    }
  });

  const select = document.getElementById('training-generate-run-module');
  const prevValue = select.value;
  select.innerHTML = '<option value="">Every intent (system-wide)</option>' +
    ids.map(id => `<option value="${esc(id)}">${esc(modules[id].display_name)}</option>`).join('');
  if (ids.includes(prevValue)) select.value = prevValue;
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

// Channels added later (e-mail, SMS, Signal, iMessage) draw their credential fields from /api/platform-guides, so a new
// channel needs no new form markup. Fields marked optional in the guide are skipped when left empty.
const GENERIC_PLATFORMS = ['email', 'sms', 'signal', 'imessage', 'googlechat', 'teams'];
const STRING_ID_PLATFORMS = ['slack', 'matrix', 'whatsapp', ...GENERIC_PLATFORMS];

function renderGenericFields(platform, values) {
  const box = document.getElementById('bot-new-generic-fields');
  const tokenField = document.getElementById('bot-new-token-field');
  const generic = GENERIC_PLATFORMS.includes(platform);
  if (tokenField) tokenField.style.display = (generic || platform === 'app') ? 'none' : '';
  box.style.display = generic ? '' : 'none';
  if (!generic) { box.innerHTML = ''; box.dataset.platform = ''; return; }
  if (!values && box.dataset.platform === platform && box.children.length) return;   // keep what the person already typed
  const guide = platformGuidesCache[platform];
  if (!guide) return;
  box.innerHTML = '';
  box.dataset.platform = platform;
  for (const [name, meta] of Object.entries(guide.fields)) {
    const secret = /token|secret|password|json/i.test(name);
    const wrap = document.createElement('div');
    wrap.innerHTML = `<label style="margin-top:8px; display:block;">${esc(meta.label || name)}${meta.optional ? ' (optional)' : ''}</label>` +
      `<div class="row"><input type="${secret ? 'password' : 'text'}" data-gen-field="${esc(name)}" autocomplete="off" spellcheck="false"></div>` +
      (meta.help ? `<div class="help">${esc(meta.help)}</div>` : '');
    box.appendChild(wrap);
    const input = wrap.querySelector('input');
    input.value = (values && values[name]) || '';
    if (!meta.optional) wireCredentialValidation(input, () => platform, () => name);
  }
}

function collectGenericCredentials() {
  const creds = {};
  document.querySelectorAll('#bot-new-generic-fields [data-gen-field]').forEach(el => {
    const v = el.value.trim();
    if (v) creds[el.dataset.genField] = v;
  });
  return creds;
}

function renderPlatformGuide(platform) {
  renderGenericFields(platform);
  const allowedField = document.getElementById('bot-new-allowed-field');
  if (platform === 'app') {
    document.getElementById('bot-new-allowed-label').textContent = 'Allowed user ID(s) (optional)';
    document.getElementById('bot-new-allowed-help').textContent = 'Not needed for an app-only bot — access is controlled by the dashboard token or a paired device, not a user-ID allowlist. Leave blank.';
  } else {
    document.getElementById('bot-new-allowed-label').textContent = 'Allowed user ID(s)';
    document.getElementById('bot-new-allowed-help').textContent = 'Comma-separated. Numeric IDs for Telegram/Discord, Slack member IDs (U.../W...) for Slack, full Matrix user IDs (@name:server) for Matrix, phone numbers with country code and no "+" for WhatsApp.';
  }
  const guide = platformGuidesCache[platform];
  if (!guide) return;
  const tokenField = guide.fields[_tokenFieldKey(platform)];
  document.getElementById('bot-new-token-help').textContent = tokenField ? tokenField.help : '';
  const apptokenField = guide.fields.app_token;
  document.getElementById('bot-new-apptoken-help').textContent = apptokenField ? apptokenField.help : '';
  const guideField = document.getElementById('bot-new-setupguide-field');
  const guideList = document.getElementById('bot-new-setupguide-list');
  const guideSummary = document.getElementById('bot-new-setupguide-summary');
  if (guideSummary) guideSummary.textContent = platform === 'app' ? 'How this works' : 'How do I get these? (step-by-step)';
  if (guide.setup_guide && guide.setup_guide.length) {
    guideField.style.display = '';
    guideList.innerHTML = guide.setup_guide.map(step => `<li>${esc(step)}</li>`).join('');
  } else {
    guideField.style.display = 'none';
  }
}

// Debounced live validation for one credential input — reuses the same
// .ok/.bad/.msg CSS the setup wizard's own fields already use
// (bot/dashboard/static/dashboard.html's .wizard-field rules).
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
  document.getElementById('bot-new-backend').value = 'native_agent';  // ABP Agent is the default for a new bot
  document.getElementById('bot-new-model').value = '';
  document.getElementById('bot-new-token').value = '';
  document.getElementById('bot-new-apptoken').value = '';
  document.getElementById('bot-new-matrix-homeserver').value = '';
  document.getElementById('bot-new-matrix-userid').value = '';
  document.getElementById('bot-new-matrix-device').value = '';
  document.getElementById('bot-new-whatsapp-phoneid').value = '';
  document.getElementById('bot-new-whatsapp-appsecret').value = '';
  document.getElementById('bot-new-whatsapp-verifytoken').value = '';
  document.getElementById('bot-new-generic-fields').innerHTML = '';
  document.getElementById('bot-new-generic-fields').dataset.platform = '';
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
  if (window.abpBotAgentForm) window.abpBotAgentForm.reset();
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
  renderGenericFields(bot.platform, bot.credentials);
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
  if (window.abpBotAgentForm) window.abpBotAgentForm.load(bot.id);
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
    const name = btn.dataset.providerDelete;
    try {
      await api(`/api/providers/${encodeURIComponent(name)}`, { method: 'DELETE' });
    } catch (e) {
      showToast(`Could not remove ${name}: ${e.message || e}`, 'error');
      return;
    }
    showToast(`Removed ${name}. You can restore it from Deleted providers below.`, 'info');
    refreshProviders();
    refreshModels();
    refreshModelsPage();
  });
  refreshDeletedProviders();
}

// The provider store keeps every removed provider (bot/provider_store.py): restore puts one back as it was,
// forget deletes the copy for good. The API never returns a key, only whether one is kept.
async function refreshDeletedProviders() {
  const tbody = document.getElementById('providers-deleted-tbody');
  if (!getToken() || !tbody) return;
  let data;
  try {
    data = await api('/api/providers/store?status=deleted');
  } catch (_e) { return; }
  const list = data.providers || [];
  const removedAt = (p) => {
    const when = p.deleted_at ? new Date(p.deleted_at).toLocaleString() : '';
    const rebuilt = (p.source === 'history' || p.source === 'recovered') ? ' <span class="cardnote">(rebuilt from history, no key)</span>' : '';
    return esc(when) + rebuilt;
  };
  tbody.innerHTML = list.length ? list.map(p => `
    <tr>
      <td>${esc(p.name)}</td>
      <td><code>${esc(p.base_url)}</code></td>
      <td>${p.api_key_env ? 'env: ' + esc(p.api_key_env) : (p.has_key ? 'kept (encrypted)' : '(none)')}</td>
      <td>${removedAt(p)}</td>
      <td>
        <input type="text" class="provider-restore-key" placeholder="new API key (optional)" autocomplete="off" spellcheck="false" style="max-width:180px;">
        <button class="btn small" data-provider-restore="${esc(p.name)}">Restore</button>
        <button class="btn small" data-provider-forget="${esc(p.name)}">Forget</button>
      </td>
    </tr>`).join('') : '<tr><td colspan="5" class="cardnote">Nothing removed. A provider you remove is kept here so you can restore it.</td></tr>';
  tbody.querySelectorAll('[data-provider-restore]').forEach(btn => btn.onclick = async () => {
    const name = btn.dataset.providerRestore;
    const key = btn.closest('tr').querySelector('.provider-restore-key').value.trim();
    try {
      await api(`/api/providers/store/${encodeURIComponent(name)}/restore`, {
        method: 'POST',
        body: JSON.stringify(key ? { api_key: key } : {}),
      });
    } catch (e) {
      showToast(`Could not restore ${name}: ${e.message || e}`, 'error');
      return;
    }
    showToast(`Restored ${name}`, 'success');
    refreshProviders();
    refreshModels();
    refreshModelsPage();
  });
  tbody.querySelectorAll('[data-provider-forget]').forEach(btn => btn.onclick = async () => {
    const name = btn.dataset.providerForget;
    if (!confirm(`Forget ${name} for good? Its saved key and model settings are deleted and it cannot be restored.`)) return;
    try {
      await api(`/api/providers/store/${encodeURIComponent(name)}`, { method: 'DELETE' });
    } catch (e) {
      showToast(`Could not forget ${name}: ${e.message || e}`, 'error');
      return;
    }
    showToast(`Forgot ${name}`, 'info');
    refreshDeletedProviders();
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
// A separate cache/refresher from refreshModels() above (Control
// Center's per-backend-family default-model pickers) — this one is the
// dedicated Models page: every model each configured custom_model/
// native_agent provider offers, with an individual enable/disable
// toggle, free-first per provider (pre-sorted by the backend).

async function refreshModelsPage(refreshProviderName) {
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

  // Preserve which <details> the user already had open across a refresh.
  const openNames = new Set(
    [...list.querySelectorAll('details[data-provider]')].filter(d => d.open).map(d => d.dataset.provider)
  );

  const blocks = await Promise.all(providersList.map(async (p) => {
    let models = [];
    try {
      // Only the provider the user clicked "Refresh" on bypasses caches
      // (model_pricing's 24h models.dev cache — the provider's own live
      // /models fetch is already always live) — every other provider's
      // block still uses its cache, so clicking one Refresh doesn't
      // silently re-fetch everything and stall the whole page.
      const qs = p.name === refreshProviderName ? '?refresh=true' : '';
      const data = await api(`/api/providers/${encodeURIComponent(p.name)}/models${qs}`);
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
          <button type="button" class="secondary" data-refresh-models="${esc(p.name)}" title="Re-fetch this provider's model list and pricing, bypassing caches">⟳ Refresh</button>
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
  list.querySelectorAll('[data-refresh-models]').forEach(btn => btn.onclick = async () => {
    // refreshModelsPage() rebuilds the whole list's innerHTML on
    // completion (replacing this exact button), so there's nothing to
    // re-enable afterward — the "Refreshing…" label is the only
    // feedback needed for the brief window before that happens.
    btn.disabled = true;
    btn.textContent = '⟳ Refreshing…';
    await refreshModelsPage(btn.dataset.refreshModels);
  });
  list.querySelectorAll('[data-model-toggle]').forEach(cb => cb.onchange = async () => {
    try {
      await api(`/api/providers/${encodeURIComponent(cb.dataset.modelToggle)}/models/toggle`, {
        method: 'POST',
        body: JSON.stringify({ model_id: cb.dataset.modelId, enabled: cb.checked }),
      });
    } catch (e) {
      cb.checked = !cb.checked; // revert on failure
      alert(e.message || 'Failed to update that model\'s toggle.');
      return;
    }
    refreshModels(); // pickers elsewhere should reflect the change promptly
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
    grid.innerHTML = '<p class="cardnote">Connecting to the ABP server…</p>';
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
        ${b.platform === 'app' ? `<span class="pill"><span class="dot good"></span>App-only — no connection to start or stop</span>`
          : b.served_by ? `<span class="pill" title="${esc(b.served_by)}"><span class="dot good"></span>Served by Hermes</span>`
          : `<span class="pill"><span class="dot ${b.live_running ? 'good' : (b.last_error ? 'critical' : '')}"></span>${b.live_running ? 'Running' : (b.last_error ? 'Crashed' : 'Stopped')}</span>`}
      </div>
      <div class="bc-model">
        <label style="font-weight:600; color:var(--muted);">Model</label>
        <div class="custom-select" data-bot-model="${b.id}">
          <button type="button" class="custom-select-btn">${esc(currentModelLabel)}</button>
          <div class="custom-select-menu">${modelMenuOptions}</div>
        </div>
      </div>
      ${manages.length ? `<div class="bc-manages"><span style="font-weight:600; color:var(--muted);">Manages:</span> ${manages.map(m => `<span class="chip neutral">${personaMeta(m.persona).icon} ${esc(m.name)}</span>`).join('')}</div>` : ''}
      ${b.served_by ? `<div class="bc-error">${esc(b.served_by)} — ABP is deliberately not polling this bot's token. Start/stop it from the Hermes gateway page.</div>` : ''}
      ${b.last_error && !b.served_by ? `<div class="bc-error">${esc(b.last_error)}</div>` : ''}
      ${b.circuit && b.circuit.open ? `<div class="bc-error">Paused after ${b.circuit.consecutive_failures} consecutive failures — retrying automatically, or <a href="#" data-bot-circuit-reset="${b.id}">retry now</a>.</div>` : ''}
      <div class="bc-actions">
        <button class="btn" data-bot-edit="${b.id}" style="padding:3px 8px; font-size:11px;">Edit</button>
        ${['native_agent', 'api', 'custom_model'].includes(b.backend) ? `<button class="btn" data-bot-agent="${b.id}" style="padding:3px 8px; font-size:11px;">Agent settings</button>` : ''}
        <button class="btn" data-bot-toggle="${b.id}" style="padding:3px 8px; font-size:11px;">${b.enabled ? 'Disable' : 'Enable'}</button>
        ${b.enabled && b.platform !== 'app' ? `<button class="btn" data-bot-startstop="${b.id}" style="padding:3px 8px; font-size:11px;">${b.live_running ? 'Stop' : 'Start'}</button>
        <button class="btn" data-bot-restart="${b.id}" style="padding:3px 8px; font-size:11px;">Restart</button>` : ''}
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
  document.querySelectorAll('[data-bot-agent]').forEach(btn => btn.onclick = () => window.abpAgents && window.abpAgents.openBot(btn.dataset.botAgent));
  document.querySelectorAll('[data-go-agents]').forEach(a => a.onclick = (e) => { e.preventDefault(); window.abpAgents && window.abpAgents.goTo('agents:overview'); });
  document.querySelectorAll('[data-bot-edit]').forEach(btn => btn.onclick = () => {
    const bot = bots.find(b => b.id === Number(btn.dataset.botEdit));
    if (bot) _loadBotIntoForm(bot);
  });
  // Every mutating bot-lifecycle action below shares the same guard: no
  // disable-while-pending meant a fast double-click could fire two
  // mutating requests before the grid re-rendered (which itself can take
  // up to 15s if the user's focus is inside it — see the note above),
  // and no try/catch meant a thrown error (401, network blip) was an
  // unhandled promise rejection — refreshBots() never ran, the button
  // never recovered any state, and the user saw literally nothing happen
  // with no explanation. _guardedBotAction wraps both fixes in one place
  // so each handler below stays a one-liner.
  async function _guardedBotAction(btn, action, failureVerb) {
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      await action();
    } catch (e) {
      showToast(e.message || `Couldn't ${failureVerb} that bot.`, 'error');
    } finally {
      btn.disabled = false;
    }
  }
  document.querySelectorAll('[data-bot-circuit-reset]').forEach(a => a.onclick = (e) => {
    e.preventDefault();
    _guardedBotAction(a, async () => {
      await api(`/api/bots/${a.dataset.botCircuitReset}/circuit/reset`, { method: 'POST' });
      refreshBots();
    }, 'reset the circuit breaker for');
  });
  document.querySelectorAll('[data-bot-toggle]').forEach(btn => btn.onclick = () => {
    const id = Number(btn.dataset.botToggle);
    const bot = bots.find(b => b.id === id);
    _guardedBotAction(btn, async () => {
      await api(`/api/bots/${id}/${bot.enabled ? 'disable' : 'enable'}`, { method: 'POST' });
      refreshBots();
    }, bot.enabled ? 'disable' : 'enable');
  });
  document.querySelectorAll('[data-bot-startstop]').forEach(btn => btn.onclick = () => {
    const id = Number(btn.dataset.botStartstop);
    const bot = bots.find(b => b.id === id);
    _guardedBotAction(btn, async () => {
      await api(`/api/bots/${id}/${bot.live_running ? 'stop' : 'start'}`, { method: 'POST' });
      refreshBots();
    }, bot.live_running ? 'stop' : 'start');
  });
  document.querySelectorAll('[data-bot-restart]').forEach(btn => btn.onclick = () => {
    _guardedBotAction(btn, async () => {
      await api(`/api/bots/${btn.dataset.botRestart}/restart`, { method: 'POST' });
      refreshBots();
    }, 'restart');
  });
  document.querySelectorAll('[data-bot-delete]').forEach(btn => btn.onclick = () => {
    const id = Number(btn.dataset.botDelete);
    const bot = bots.find(b => b.id === id);
    if (!confirm(`Delete bot "${bot.name}"? It's stopped immediately; job/chat history stays but keeps this id as a reference. A backup is taken first.`)) return;
    _guardedBotAction(btn, async () => {
      await api(`/api/bots/${id}`, { method: 'DELETE' });
      if (botEditingId === id) _resetBotForm();
      refreshBots();
      refreshBotsBackups();
    }, 'delete');
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
  } else if (GENERIC_PLATFORMS.includes(platform)) {
    credentials = collectGenericCredentials();
  } else if (platform === 'whatsapp') {
    credentials = {
      phone_number_id: document.getElementById('bot-new-whatsapp-phoneid').value.trim(),
      access_token: document.getElementById('bot-new-token').value.trim(),
      app_secret: document.getElementById('bot-new-whatsapp-appsecret').value.trim(),
      verify_token: document.getElementById('bot-new-whatsapp-verifytoken').value.trim(),
    };
  } else if (platform === 'app') {
    credentials = {};   // no external platform — nothing to store
  } else {
    credentials = { bot_token: document.getElementById('bot-new-token').value.trim() };
    if (platform === 'slack') credentials.app_token = document.getElementById('bot-new-apptoken').value.trim();
  }
  const isStringId = STRING_ID_PLATFORMS.includes(platform);
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
    let savedId = botEditingId;
    if (botEditingId) {
      await api(`/api/bots/${botEditingId}`, { method: 'PUT', body: JSON.stringify(payload) });
      statusEl.textContent = 'Saved — use Restart on this bot\'s row to apply.';
    } else {
      const created = await api('/api/bots', { method: 'POST', body: JSON.stringify(payload) });
      savedId = created.id;
      statusEl.textContent = 'Added and starting…';
    }
    // The ABP Agent settings in the form (permission mode, sub-agent limits, models, effort) belong to the bot that now exists.
    if (window.abpBotAgentForm && savedId) await window.abpBotAgentForm.save(savedId);
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
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="4">Connecting to the ABP server…</td></tr>';
    return;
  }
  let backups;
  try {
    backups = await api('/api/bots/backups');
  } catch (_e) { return; }
  // Defense-in-depth row cap: the backend now prunes this directory to
  // retention.bot_instances_backups_max_count (default 50) on every
  // write, but render at most 50 here regardless — a directory that
  // somehow grows unbounded again (pre-fix leftovers, a future bug)
  // should never again turn into a 47,000-row table that makes the
  // whole dashboard sluggish to resize/scroll.
  backups = backups.slice(0, 50);
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
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="4">Connecting to the ABP server…</td></tr>';
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
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="5">Connecting to the ABP server…</td></tr>';
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
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="5">Connecting to the ABP server…</td></tr>';
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
// connectLiveEventsSocket below) can refresh it in place instead of
// waiting for the next 15s poll.
let openDelegationDetailJobId = null;

async function refreshDelegationActivity() {
  const tbody = document.getElementById('delegation-activity-tbody');
  if (!getToken()) {
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="4">Connecting to the ABP server…</td></tr>';
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

// ------------------------------------------------------------ SSH Toolkit -
async function refreshSshToolkit() {
  const availability = document.getElementById('ssh-toolkit-availability');
  const tbody = document.getElementById('ssh-toolkit-tbody');
  if (!getToken()) return;
  try {
    const auto = await api('/api/ssh-toolkit/auto-update');
    document.getElementById('ssh-auto-update-mode').value = auto.mode;
  } catch (_e) { /* leave the select at its default */ }
  let avail;
  try { avail = await api('/api/ssh-toolkit/status'); } catch (_e) { return; }
  if (!avail.available) {
    availability.textContent = `SSH Toolkit is not available: ${avail.reason || ''}`;
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="6">SSH Toolkit is not available.</td></tr>';
    return;
  }
  availability.textContent = 'SSH Toolkit is available.';
  let conns;
  try { conns = (await api('/api/ssh-toolkit/connections')).connections; } catch (e) {
    tbody.innerHTML = `<tr class="emptyrow"><td colspan="6">Failed to load connections: ${esc(e.message)}</td></tr>`;
    return;
  }
  sshMonitorPopulateConnections(conns);
  tbody.innerHTML = conns.length ? conns.map(c => `
    <tr>
      <td>${esc(c.Name)}</td>
      <td class="mono">${esc(c.HostName)}</td>
      <td>${esc(String(c.Port ?? ''))}</td>
      <td>${esc(c.User || '')}</td>
      <td>${esc(c.Tags || '')}</td>
      <td style="white-space:nowrap;">
        <button class="btn" data-ssh-test="${esc(c.Name)}" style="padding:3px 8px; font-size:11px;">Test</button>
        <button class="btn" data-ssh-remove="${esc(c.Name)}" style="padding:3px 8px; font-size:11px;">Remove</button>
      </td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="6">No connections yet — add one above.</td></tr>';

  document.querySelectorAll('[data-ssh-test]').forEach(btn => btn.onclick = async () => {
    const name = btn.dataset.sshTest;
    const status = document.getElementById('ssh-new-status');
    status.textContent = `Testing ${name}…`;
    try {
      const res = await api(`/api/ssh-toolkit/connections/${encodeURIComponent(name)}/test`, { method: 'POST' });
      status.textContent = `${name}: ${res.reachable ? 'reachable' : 'not reachable'}`;
    } catch (e) {
      status.textContent = `Failed: ${e.message}`;
    }
  });
  document.querySelectorAll('[data-ssh-remove]').forEach(btn => btn.onclick = async () => {
    const name = btn.dataset.sshRemove;
    if (!confirm(`Remove connection "${name}"?`)) return;
    try {
      await api(`/api/ssh-toolkit/connections/${encodeURIComponent(name)}`, { method: 'DELETE' });
    } catch (e) {
      document.getElementById('ssh-new-status').textContent = `Failed: ${e.message}`;
      return;
    }
    refreshSshToolkit();
  });
}

document.getElementById('btn-ssh-add').onclick = async () => {
  const status = document.getElementById('ssh-new-status');
  const name = document.getElementById('ssh-new-name').value.trim();
  const host = document.getElementById('ssh-new-host').value.trim();
  const user = document.getElementById('ssh-new-user').value.trim();
  if (!name || !host) { status.textContent = 'Name and host are both required.'; return; }
  status.textContent = 'Adding…';
  try {
    await api('/api/ssh-toolkit/connections', {
      method: 'POST',
      body: JSON.stringify({ name, host_name: host, user: user || undefined }),
    });
    status.textContent = `Added "${name}".`;
    document.getElementById('ssh-new-name').value = '';
    document.getElementById('ssh-new-host').value = '';
    document.getElementById('ssh-new-user').value = '';
  } catch (e) {
    status.textContent = `Failed: ${e.message}`;
    return;
  }
  refreshSshToolkit();
};

document.getElementById('btn-ssh-check-update').onclick = async () => {
  const status = document.getElementById('ssh-update-status');
  status.textContent = 'Checking…';
  try {
    const check = await api('/api/ssh-toolkit/update/check');
    document.getElementById('ssh-toolkit-version').textContent =
      `Installed: ${check.InstalledVersion} · Latest: ${check.LatestVersion}`;
    status.textContent = check.UpdateAvailable ? 'An update is available.' : 'Already up to date.';
  } catch (e) {
    status.textContent = `Failed: ${e.message}`;
  }
};

document.getElementById('btn-ssh-apply-update').onclick = async () => {
  const status = document.getElementById('ssh-update-status');
  if (!confirm('Apply the SSH Toolkit update now?')) return;
  status.textContent = 'Applying…';
  try {
    await api('/api/ssh-toolkit/update/apply', { method: 'POST' });
    status.textContent = 'Updated.';
  } catch (e) {
    status.textContent = `Failed: ${e.message}`;
    return;
  }
  refreshSshToolkit();
};

document.getElementById('ssh-auto-update-mode').onchange = async (ev) => {
  const status = document.getElementById('ssh-update-status');
  try {
    await api('/api/ssh-toolkit/auto-update', { method: 'POST', body: JSON.stringify({ mode: ev.target.value }) });
    status.textContent = `Auto-update set to "${ev.target.value}".`;
  } catch (e) {
    status.textContent = `Failed: ${e.message}`;
  }
};

// -------------------------------------------------- SSH session monitor --
// Structured live events over the existing /api/ws socket (see
// connectLiveEventsSocket's dispatch below) - never video/screen-share.
// One "session" = one watched remote command; a recording persists its
// exact event sequence (bot/ssh_session_monitor.py) for playback later.
const sshMonitor = {
  sessionId: null,
  recordingId: null,
  recordingPaused: false,
  mode: 'live', // 'live' | 'playback'
  reconcileTimer: null,
  playback: { events: [], playing: false, speed: 1, startWall: 0, elapsedAtPause: 0, shown: 0, duration: 0, timer: null },
};

function sshMonitorPopulateConnections(conns) {
  const sel = document.getElementById('ssh-monitor-connection');
  const prev = sel.value;
  sel.innerHTML = conns.map(c => `<option value="${esc(c.Name)}">${esc(c.Name)} (${esc(c.HostName)})</option>`).join('')
    || '<option value="">no connections configured</option>';
  if (conns.some(c => c.Name === prev)) sel.value = prev;
}

function sshFeedClear() {
  document.getElementById('ssh-monitor-feed').innerHTML = '';
}

function sshFeedAppend(type, text) {
  const feed = document.getElementById('ssh-monitor-feed');
  const line = document.createElement('div');
  const colors = { start: 'var(--muted)', stderr: '#e5484d', exit: 'var(--ink)', note: 'var(--muted)' };
  line.style.color = colors[type] || 'inherit';
  if (type === 'exit' || type === 'start') line.style.fontWeight = '600';
  if (type === 'note') line.style.fontStyle = 'italic';
  line.textContent = text;
  feed.appendChild(line);
  feed.scrollTop = feed.scrollHeight;
}

function sshFeedLineForEvent(ev) {
  switch (ev.type) {
    case 'start': return `▶ start: ${ev.command}`;
    case 'stdout': return ev.text;
    case 'stderr': return ev.text;
    case 'exit': return `■ exit code: ${ev.code === null || ev.code === undefined ? '(timed out)' : ev.code}`;
    case 'note': return ev.text;
    default: return null;
  }
}

function sshApplyMetric(ev) {
  if (typeof ev.cpu_percent === 'number') {
    document.getElementById('ssh-monitor-cpu-bar').style.width = `${Math.min(100, ev.cpu_percent)}%`;
    document.getElementById('ssh-monitor-cpu-text').textContent = `${ev.cpu_percent.toFixed(0)}%`;
  }
  if (typeof ev.mem_used_percent === 'number') {
    document.getElementById('ssh-monitor-mem-bar').style.width = `${Math.min(100, ev.mem_used_percent)}%`;
    document.getElementById('ssh-monitor-mem-text').textContent = `${ev.mem_used_percent.toFixed(0)}%`;
  }
}

function sshMonitorSetRunningUi(running) {
  document.getElementById('btn-ssh-monitor-run').disabled = running;
  document.getElementById('btn-ssh-monitor-stop').disabled = !running;
  document.getElementById('btn-ssh-record-start').disabled = !running || !!sshMonitor.recordingId;
}

function sshMonitorSetRecordingUi(state) {
  const indicator = document.getElementById('ssh-record-indicator');
  const startBtn = document.getElementById('btn-ssh-record-start');
  const pauseBtn = document.getElementById('btn-ssh-record-pause');
  const resumeBtn = document.getElementById('btn-ssh-record-resume');
  const stopBtn = document.getElementById('btn-ssh-record-stop');
  if (state === 'recording') {
    indicator.style.display = 'inline-block';
    startBtn.disabled = true;
    pauseBtn.disabled = false; pauseBtn.style.display = '';
    resumeBtn.style.display = 'none';
    stopBtn.disabled = false;
  } else if (state === 'paused') {
    indicator.style.display = 'inline-block';
    indicator.style.background = '#f5a623';
    pauseBtn.style.display = 'none';
    resumeBtn.style.display = ''; resumeBtn.disabled = false;
    stopBtn.disabled = false;
  } else { // stopped / idle
    indicator.style.display = 'none';
    indicator.style.background = '#e5484d';
    startBtn.disabled = !sshMonitor.sessionId;
    pauseBtn.disabled = true; pauseBtn.style.display = '';
    resumeBtn.style.display = 'none'; resumeBtn.disabled = true;
    stopBtn.disabled = true;
  }
}

document.getElementById('btn-ssh-monitor-run').onclick = async () => {
  const name = document.getElementById('ssh-monitor-connection').value;
  const command = document.getElementById('ssh-monitor-command').value.trim();
  const status = document.getElementById('ssh-monitor-status');
  if (!name || !command) { status.textContent = 'Pick a connection and enter a command.'; return; }
  sshPlaybackExit();
  sshFeedClear();
  document.getElementById('ssh-monitor-feed-title').textContent = 'Live feed';
  try {
    sshMonitor.sessionId = await api('/api/ssh-toolkit/session/start', {
      method: 'POST', body: JSON.stringify({ name, command }),
    }).then(r => r.session_id);
  } catch (e) {
    status.textContent = `Failed: ${e.message}`;
    return;
  }
  status.textContent = `Session ${sshMonitor.sessionId} running on ${name}...`;
  sshMonitorSetRunningUi(true);
  sshMonitorStartReconcilePoll();
};

// Reconciliation poll: a fallback safety net alongside the live WebSocket
// push, not a replacement for it - a live/mesh WS can miss a message (not
// yet connected, a brief drop, a reconnect gap), and this feature's whole
// point is showing an agent's actions accurately, so "the button silently
// stays stuck because one push happened to be missed" is not acceptable.
// Polls only while a session is being watched client-side; stops itself
// once that session is confirmed finished/stopped either way.
function sshMonitorStartReconcilePoll() {
  if (sshMonitor.reconcileTimer) clearInterval(sshMonitor.reconcileTimer);
  sshMonitor.reconcileTimer = setInterval(async () => {
    if (sshMonitor.mode !== 'live' || !sshMonitor.sessionId) {
      clearInterval(sshMonitor.reconcileTimer);
      sshMonitor.reconcileTimer = null;
      return;
    }
    let sessions;
    try { sessions = (await api('/api/ssh-toolkit/session')).sessions; } catch (_e) { return; }
    const mine = sessions.find(s => s.session_id === sshMonitor.sessionId);
    const uiStillThinksRunning = document.getElementById('btn-ssh-monitor-stop').disabled === false;
    if (mine && (mine.status === 'finished' || mine.status === 'stopped') && uiStillThinksRunning) {
      document.getElementById('ssh-monitor-status').textContent =
        `Session ${sshMonitor.sessionId} ${mine.status}${mine.exit_code !== null && mine.exit_code !== undefined ? ` (exit ${mine.exit_code})` : ''}.`;
      sshMonitorSetRunningUi(false);
      clearInterval(sshMonitor.reconcileTimer);
      sshMonitor.reconcileTimer = null;
    }
  }, 2000);
}

document.getElementById('btn-ssh-monitor-stop').onclick = async () => {
  if (!sshMonitor.sessionId) return;
  await api(`/api/ssh-toolkit/session/${sshMonitor.sessionId}/stop`, { method: 'POST' });
};

document.getElementById('btn-ssh-record-start').onclick = async () => {
  if (!sshMonitor.sessionId) return;
  try {
    sshMonitor.recordingId = await api(`/api/ssh-toolkit/session/${sshMonitor.sessionId}/record/start`, { method: 'POST' })
      .then(r => r.recording_id);
    document.getElementById('ssh-record-status').textContent = `Recording #${sshMonitor.recordingId}`;
  } catch (e) {
    document.getElementById('ssh-record-status').textContent = `Failed: ${e.message}`;
  }
};
document.getElementById('btn-ssh-record-pause').onclick = () =>
  api(`/api/ssh-toolkit/session/${sshMonitor.sessionId}/record/pause`, { method: 'POST' });
document.getElementById('btn-ssh-record-resume').onclick = () =>
  api(`/api/ssh-toolkit/session/${sshMonitor.sessionId}/record/resume`, { method: 'POST' });
document.getElementById('btn-ssh-record-stop').onclick = async () => {
  await api(`/api/ssh-toolkit/session/${sshMonitor.sessionId}/record/stop`, { method: 'POST' });
  refreshSshRecordings();
};

function onSshSessionEvent(msg) {
  // msg.event (not msg itself) carries the actual start/stdout/stderr/exit/
  // metric payload - kept nested rather than flattened onto msg, since
  // msg.type is always the envelope type "ssh_session_event" (msg.event.type
  // is the real one) - see _emit()'s own comment for why that distinction
  // matters here specifically.
  if (sshMonitor.mode !== 'live' || msg.session_id !== sshMonitor.sessionId) return;
  const ev = msg.event;
  if (ev.type === 'metric') { sshApplyMetric(ev); return; }
  const line = sshFeedLineForEvent(ev);
  if (line !== null) sshFeedAppend(ev.type, line);
  if (ev.type === 'exit') {
    document.getElementById('ssh-monitor-status').textContent =
      `Session ${sshMonitor.sessionId} finished (exit ${ev.code === null || ev.code === undefined ? '?' : ev.code}).`;
    sshMonitorSetRunningUi(false);
    if (sshMonitor.reconcileTimer) { clearInterval(sshMonitor.reconcileTimer); sshMonitor.reconcileTimer = null; }
  }
}

function onSshSessionStopped(msg) {
  if (msg.session_id !== sshMonitor.sessionId) return;
  document.getElementById('ssh-monitor-status').textContent = `Session ${sshMonitor.sessionId} stopped.`;
  sshMonitorSetRunningUi(false);
  if (sshMonitor.reconcileTimer) { clearInterval(sshMonitor.reconcileTimer); sshMonitor.reconcileTimer = null; }
}

function onSshRecordingState(msg) {
  if (msg.session_id !== sshMonitor.sessionId) return;
  if (msg.state === 'stopped') sshMonitor.recordingId = null;
  sshMonitor.recordingPaused = msg.state === 'paused';
  sshMonitorSetRecordingUi(msg.state === 'stopped' ? 'idle' : msg.state);
  if (msg.state !== 'idle') document.getElementById('ssh-record-status').textContent =
    msg.state === 'stopped' ? `Recording #${msg.recording_id} saved.` : `Recording #${msg.recording_id} — ${msg.state}`;
  if (msg.state === 'stopped') refreshSshRecordings();
}

// ---- recordings list + playback (replays a saved event sequence at its
// original relative timing - a scrubbable, speed-adjustable "action replay",
// not a video) ----
async function refreshSshRecordings() {
  const tbody = document.getElementById('ssh-recordings-tbody');
  if (!getToken()) return;
  let recordings;
  try { recordings = await api('/api/ssh-toolkit/recordings'); } catch (_e) { return; }
  recordings = recordings.recordings || recordings;
  tbody.innerHTML = recordings.length ? recordings.map(r => `
    <tr>
      <td>${esc(r.connection_name)}</td>
      <td class="mono">${esc(r.command)}</td>
      <td>${esc(r.status)}</td>
      <td>${esc((r.started_at || '').replace('T', ' ').slice(0, 19))}</td>
      <td>${r.event_count ?? 0}</td>
      <td style="white-space:nowrap;">
        <button class="btn" data-ssh-play="${r.id}" style="padding:3px 8px; font-size:11px;">Play</button>
        <button class="btn" data-ssh-delete-recording="${r.id}" style="padding:3px 8px; font-size:11px;">Delete</button>
      </td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="6">No recordings yet.</td></tr>';

  document.querySelectorAll('[data-ssh-play]').forEach(btn => btn.onclick = () => sshPlaybackStart(Number(btn.dataset.sshPlay)));
  document.querySelectorAll('[data-ssh-delete-recording]').forEach(btn => btn.onclick = async () => {
    if (!confirm('Delete this recording?')) return;
    await api(`/api/ssh-toolkit/recordings/${btn.dataset.sshDeleteRecording}`, { method: 'DELETE' });
    refreshSshRecordings();
  });
}

async function sshPlaybackStart(recordingId) {
  const rec = await api(`/api/ssh-toolkit/recordings/${recordingId}`);
  sshPlaybackStop_();
  sshMonitor.mode = 'playback';
  sshFeedClear();
  document.getElementById('ssh-monitor-feed-title').textContent =
    `Playback: recording #${recordingId} — ${rec.connection_name} › ${rec.command}`;
  document.getElementById('btn-ssh-playback-exit').style.display = '';
  document.getElementById('ssh-playback-controls').style.display = 'flex';
  const events = rec.events.map(e => ({ ...e.payload, _seq: e.seq }));
  const t0 = events.length ? events[0].ts : 0;
  const withRel = events.map(e => ({ ...e, _rel: e.ts - t0 }));
  sshMonitor.playback = {
    events: withRel, playing: false, speed: Number(document.getElementById('ssh-playback-speed').value),
    startWall: 0, elapsedAtPause: 0, shown: 0,
    duration: withRel.length ? withRel[withRel.length - 1]._rel : 0, timer: null,
  };
  document.getElementById('ssh-playback-scrubber').value = 0;
  document.getElementById('btn-ssh-playback-toggle').textContent = 'Play';
  document.getElementById('ssh-monitor-cpu-text').textContent = '—';
  document.getElementById('ssh-monitor-mem-text').textContent = '—';
  document.getElementById('ssh-monitor-cpu-bar').style.width = '0%';
  document.getElementById('ssh-monitor-mem-bar').style.width = '0%';
}

function sshPlaybackRenderUpTo(relSeconds) {
  const pb = sshMonitor.playback;
  sshFeedClear();
  document.getElementById('ssh-monitor-cpu-text').textContent = '—';
  document.getElementById('ssh-monitor-mem-text').textContent = '—';
  let shown = 0;
  for (const ev of pb.events) {
    if (ev._rel > relSeconds) break;
    if (ev.type === 'metric') { sshApplyMetric(ev); } else {
      const line = sshFeedLineForEvent(ev);
      if (line !== null) sshFeedAppend(ev.type, line);
    }
    shown++;
  }
  pb.shown = shown;
  const scrubber = document.getElementById('ssh-playback-scrubber');
  scrubber.value = pb.duration > 0 ? Math.min(100, (relSeconds / pb.duration) * 100) : 100;
}

function sshPlaybackTick() {
  const pb = sshMonitor.playback;
  const elapsed = pb.elapsedAtPause + (Date.now() - pb.startWall) / 1000 * pb.speed;
  sshPlaybackRenderUpTo(elapsed);
  if (elapsed >= pb.duration) {
    sshPlaybackPause_();
    document.getElementById('btn-ssh-playback-toggle').textContent = 'Replay';
    pb.elapsedAtPause = pb.duration;
  }
}

function sshPlaybackPause_() {
  const pb = sshMonitor.playback;
  if (pb.timer) { clearInterval(pb.timer); pb.timer = null; }
  pb.playing = false;
}

function sshPlaybackStop_() {
  sshPlaybackPause_();
  sshMonitor.playback = { events: [], playing: false, speed: 1, startWall: 0, elapsedAtPause: 0, shown: 0, duration: 0, timer: null };
}

document.getElementById('btn-ssh-playback-toggle').onclick = () => {
  const pb = sshMonitor.playback;
  const btn = document.getElementById('btn-ssh-playback-toggle');
  if (pb.playing) {
    pb.elapsedAtPause = pb.elapsedAtPause + (Date.now() - pb.startWall) / 1000 * pb.speed;
    sshPlaybackPause_();
    btn.textContent = 'Play';
  } else {
    if (btn.textContent === 'Replay') pb.elapsedAtPause = 0;
    pb.startWall = Date.now();
    pb.playing = true;
    pb.timer = setInterval(sshPlaybackTick, 100);
    btn.textContent = 'Pause';
  }
};

document.getElementById('ssh-playback-scrubber').oninput = (ev) => {
  const pb = sshMonitor.playback;
  sshPlaybackPause_();
  document.getElementById('btn-ssh-playback-toggle').textContent = 'Play';
  const relSeconds = (Number(ev.target.value) / 100) * pb.duration;
  pb.elapsedAtPause = relSeconds;
  sshPlaybackRenderUpTo(relSeconds);
};

document.getElementById('ssh-playback-speed').onchange = (ev) => {
  const pb = sshMonitor.playback;
  if (pb.playing) { pb.elapsedAtPause += (Date.now() - pb.startWall) / 1000 * pb.speed; pb.startWall = Date.now(); }
  pb.speed = Number(ev.target.value);
};

function sshPlaybackExit() {
  sshPlaybackStop_();
  sshMonitor.mode = 'live';
  document.getElementById('btn-ssh-playback-exit').style.display = 'none';
  document.getElementById('ssh-playback-controls').style.display = 'none';
  document.getElementById('ssh-monitor-feed-title').textContent = 'Live feed';
  sshFeedClear();
  document.getElementById('ssh-monitor-cpu-text').textContent = '—';
  document.getElementById('ssh-monitor-mem-text').textContent = '—';
  document.getElementById('ssh-monitor-cpu-bar').style.width = '0%';
  document.getElementById('ssh-monitor-mem-bar').style.width = '0%';
}
document.getElementById('btn-ssh-playback-exit').onclick = sshPlaybackExit;

// ------------------------------------------------------- live job events -
// A small, dashboard.html-local WebSocket to /api/ws, mirroring the same
// device-presence socket desktop-app/ui/main.js already keeps — this file
// previously had no WS consumer at all (chat_message/job_update were
// already broadcast server-side but nothing here read them), discovered
// while wiring up job_tool_event/job_children_update for this feature.
let liveEventsSocket = null;
let liveEventsRetryTimer = null;

function liveEventsWsBase() {
  return location.origin.replace(/^http/, 'ws');
}

function connectLiveEventsSocket() {
  const token = getToken();
  if (!token) return;
  if (liveEventsSocket && (liveEventsSocket.readyState === WebSocket.OPEN || liveEventsSocket.readyState === WebSocket.CONNECTING)) return;
  clearTimeout(liveEventsRetryTimer);
  try {
    liveEventsSocket = new WebSocket(`${liveEventsWsBase()}/api/ws?token=${encodeURIComponent(token)}`);
  } catch (_e) {
    liveEventsRetryTimer = setTimeout(connectLiveEventsSocket, 4000);
    return;
  }
  // This socket retries every 4 s on its own; the moment it gets through, the
  // server is back, so the pollers' backoff (see _net) ends right away.
  liveEventsSocket.onopen = () => _netOk();
  liveEventsSocket.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      if ((msg.type === 'job_tool_event' || msg.type === 'job_children_update') && msg.job_id === openDelegationDetailJobId) {
        renderDelegationDetail(msg.job_id);
      }
      if (msg.type === 'activity_entry' && typeof window.onActivityEntry === 'function') {
        window.onActivityEntry(msg.entry);
      }
      if (msg.type === 'static_file_changed' && msg.target === 'dashboard') {
        // Someone (possibly this same tab) approved a Customize UI change
        // that touched this exact page — reload so it's actually reflected,
        // same "the file on disk is the source of truth" model every other
        // static-file-serving route already assumes.
        setTimeout(() => location.reload(), 800);
      }
      if (msg.type === 'ssh_session_event') onSshSessionEvent(msg);
      if (msg.type === 'ssh_session_stopped') onSshSessionStopped(msg);
      if (msg.type === 'ssh_recording_state') onSshRecordingState(msg);
    } catch (_e) { /* malformed frame, ignore */ }
  };
  liveEventsSocket.onclose = liveEventsSocket.onerror = () => {
    liveEventsSocket = null;
    liveEventsRetryTimer = setTimeout(connectLiveEventsSocket, 4000);
  };
}

// ------------------------------------------------------------------ chat -
// Per-instance panels: chatState.panels[id] holds each bot's own cursor,
// draft, and recipient so switching tabs is instant (no re-fetch flash)
// and never loses what you were mid-typing. Only the active panel is
// polled on the 2s interval — an inactive bot's messages just wait for
// the next time you switch to it (immediate refreshChat() on switch keeps
// that from feeling stale in practice for this single-user dashboard).
const chatState = { activeInstanceId: null, instances: null, panels: {}, mode: 'bot' };  // Chat with Bot is the default seat; Send from Server is one click away
// Declared here, above setChatMode(), which runs at page load and reads it. A `let` is not usable before its
// declaration runs, so leaving this further down made the load-time setChatMode('bot') throw and abort the whole script.
let chatPendingFile = null;

// "server" = Send from Server: the dashboard pushes a real message OUT,
// through outbox.py + a live platform SDK, to a real Telegram/Discord/Slack
// user (POST /api/chat/send) — appears to that user as coming from the bot.
// "bot" = Chat with Bot: a real message FROM the dashboard operator TO the
// bot (POST /api/chat/send-to-bot), through the exact same
// CmdContext/dispatch_command/router.ask() pipeline every real Telegram/
// Discord/Slack message goes through — a genuine reply comes back, nothing
// simulated. No recipient picker in this mode: the sender's identity comes
// from the request's own auth, not a client-chosen value. One mode applies
// to the whole Chat tab at a time — deliberately global, not per-instance-
// panel, so there's exactly one place to look to know which seat you're in.
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
setChatMode('bot');

function panelFor(id) {
  if (!chatState.panels[id]) chatState.panels[id] = { lastId: 0, recipient: null, loaded: false, draft: '' };
  return chatState.panels[id];
}

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
    const res = await fetch(path, { headers });
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
    alert('Export failed — check that the ABP server is running.');
  }
}

async function downloadAttachment(messageId, name) {
  try {
    const headers = {};
    const token = getToken();
    if (token) headers['X-Dashboard-Token'] = token;
    const res = await fetch(`/api/chat/attachments/${messageId}`, { headers });
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
    alert('Download failed — check that the ABP server is running.');
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
  document.querySelectorAll('#chat-tabs .chat-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.instanceId === String(instanceId));
  });
}

function renderChatTabs() {
  const tabs = document.getElementById('chat-tabs');
  const instances = chatState.instances || [];
  tabs.innerHTML = instances.map(i =>
    `<button class="chat-tab${i.id === chatState.activeInstanceId ? ' active' : ''}" data-instance-id="${i.id}">${esc(i.name)}</button>`
  ).join('');
  tabs.querySelectorAll('.chat-tab').forEach(btn => {
    btn.onclick = () => switchToInstance(Number(btn.dataset.instanceId));
  });
}

function appendChatMessages(instanceId, rows) {
  const win = ensurePanel(instanceId);
  const panel = panelFor(instanceId);
  const atBottom = win.scrollHeight - win.scrollTop - win.clientHeight < 60;
  rows.forEach(m => {
    const row = document.createElement('div');
    row.className = 'chat-row ' + (m.direction === 'in' ? 'in' : 'out');
    row.dataset.msgId = String(m.id);
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble' + (m.source === 'dashboard' && m.direction === 'out' ? ' from-dashboard' : '') + (m.platform === 'app' && m.direction === 'in' ? ' from-app' : '');
    if (m.text) {
      const textEl = document.createElement('div');
      textEl.textContent = m.text;
      bubble.appendChild(textEl);
      bubble.appendChild(makeCopyButton(() => m.text, 'Message'));
    }
    if (m.attachment_path) {
      const att = document.createElement('a');
      att.className = 'chat-attachment';
      att.textContent = '📎 ' + (m.attachment_name || 'file');
      att.href = '#';
      att.onclick = (e) => { e.preventDefault(); downloadAttachment(m.id, m.attachment_name); };
      bubble.appendChild(att);
    }
    const meta = document.createElement('span');
    meta.className = 'chat-meta';
    const who = m.source === 'dashboard' && m.direction === 'out' ? 'sent from dashboard' : (m.direction === 'in' && m.username) ? m.username : '';
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
    return;
  }
  recipientSelect.innerHTML = ids.map(id => `<option value="${esc(id)}">${esc(id)}</option>`).join('');
  if (!panel.recipient || !ids.includes(String(panel.recipient))) {
    panel.recipient = ids[0];
  }
  recipientSelect.value = String(panel.recipient);
  note.textContent = inst.connected ? '' : `${inst.name} is configured but not running right now — start it from the Bots tab.`;
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
      document.getElementById('chat-recipient-note').textContent = 'Add a bot in the Bots tab first.';
      document.getElementById('chat-tabs').innerHTML = '';
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
  pollWhenVisible(() => refreshChat(chatState.activeInstanceId), 2000, 'chat');
  pollWhenVisible(refreshChatRecipients, 15000, 'chat');
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
document.getElementById('btn-chat-copy').onclick = () => {
  const win = document.querySelector('#chat-panels .chat-window.active');
  if (!win) return;
  copyText(transcriptFromWindow(win), 'Conversation');
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
      // Raw fetch, not the shared api() helper — api() always sets
      // Content-Type: application/json, which would break FormData's
      // auto-generated multipart boundary header.
      const fd = new FormData();
      fd.append('instance_id', chatState.activeInstanceId);
      fd.append('chat_id', panel.recipient);
      fd.append('text', text);
      fd.append('file', chatPendingFile);
      const headers = {};
      const token = getToken();
      if (token) headers['X-Dashboard-Token'] = token;
      const res = await fetch('/api/chat/send-file', { method: 'POST', headers, body: fd });
      if (!res.ok) throw new Error(await res.text());
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
      ? 'Send failed — check that the ABP server is running and that this bot instance exists.'
      : 'Send failed — check that the ABP server is running and that this bot is running.';
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

// ----------------------------------------------------------- server chat
// A permanent, bot-independent channel between this server's own devices
// (desktop + every paired phone) — see bot/db.py's server_chat_conversations
// comment. The desktop dashboard is always device id 0 (db.
// SERVER_CHAT_DESKTOP_DEVICE_ID); every message not sent by us renders as
// "in" regardless of which other device sent it.
const SERVER_CHAT_MY_DEVICE_ID = 0;
const serverChatState = { conversations: [], activeId: null, lastId: 0 };
let serverChatPendingFile = null;

async function refreshServerChatList() {
  const select = document.getElementById('serverchat-conversation');
  try {
    serverChatState.conversations = await api('/api/server-chat/conversations');
  } catch (_e) { return; }
  const prevValue = select.value;
  select.innerHTML = serverChatState.conversations.map(c => {
    const preview = c.last_message ? (c.last_message.text || (c.last_message.attachment_name ? '📎 ' + c.last_message.attachment_name : '')) : '';
    return `<option value="${c.id}">${esc(c.title)}${preview ? ' — ' + esc(preview.slice(0, 30)) : ''}</option>`;
  }).join('');
  if (serverChatState.conversations.some(c => String(c.id) === prevValue)) {
    select.value = prevValue;
  } else if (serverChatState.conversations.length) {
    select.value = String(serverChatState.conversations[0].id);
  }
  const newActiveId = select.value ? Number(select.value) : null;
  if (newActiveId !== serverChatState.activeId) {
    activateServerChatConversation(newActiveId);
  }
}

function activateServerChatConversation(id) {
  serverChatState.activeId = id;
  serverChatState.lastId = 0;
  document.getElementById('serverchat-window').innerHTML = '';
  refreshServerChatMessages();
}

document.getElementById('serverchat-conversation').onchange = (e) => {
  activateServerChatConversation(e.target.value ? Number(e.target.value) : null);
};
document.getElementById('btn-serverchat-export').onclick = () => {
  if (!serverChatState.activeId) return;
  downloadUrl(`/api/server-chat/conversations/${serverChatState.activeId}/export`);
};
document.getElementById('btn-serverchat-copy').onclick = () => {
  const win = document.getElementById('serverchat-window');
  copyText(transcriptFromWindow(win), 'Conversation');
};
document.getElementById('btn-serverchat-clear').onclick = async () => {
  if (!serverChatState.activeId) return;
  const conv = serverChatState.conversations.find(c => c.id === serverChatState.activeId);
  if (!confirm(`Delete all history in "${conv ? conv.title : 'this conversation'}"? This permanently removes its messages and attachments — export first if you want a copy.`)) return;
  await api(`/api/server-chat/conversations/${serverChatState.activeId}`, { method: 'DELETE' });
  document.getElementById('serverchat-window').innerHTML = '';
  serverChatState.lastId = 0;
  refreshServerChatList();
};

async function downloadServerChatAttachment(messageId, name) {
  try {
    const headers = {};
    const token = getToken();
    if (token) headers['X-Dashboard-Token'] = token;
    const res = await fetch(`/api/server-chat/attachments/${messageId}`, { headers });
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
    alert('Download failed — check that the ABP server is running.');
  }
}

async function deleteServerChatMessage(messageId, rowEl) {
  if (!confirm('Delete this message?')) return;
  try {
    await api(`/api/server-chat/messages/${messageId}`, { method: 'DELETE' });
    rowEl.remove();
  } catch (e) {
    alert('Delete failed: ' + (e.message || e));
  }
}

// Resolves a dangerous-tool approval raised by the Server Chat admin
// pipeline (bot/server_chat_admin.py) — outcome is one of "once" |
// "session" | "always" | "deny" (bot/agent_runtime/approval.py). Mutates
// [bubble] in place rather than tracking resolved state separately,
// since this row is never re-rendered by a later poll (appendServerChatMessages
// only ever fetches messages after lastId) — the same reasoning the
// Android app's ApprovalRequestCard states for its own client-side-only
// resolved tracking, just simpler here because there's no list re-render
// to guard against.
async function resolveServerChatApproval(approvalId, outcome, bubble) {
  const actions = bubble.querySelector('.approval-actions');
  if (actions) actions.querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    await api(`/api/server-chat/approvals/${approvalId}/resolve`, { method: 'POST', body: JSON.stringify({ outcome }) });
    if (actions) actions.remove();
    const resolved = document.createElement('div');
    resolved.className = 'approval-resolved ' + (outcome === 'deny' ? 'deny' : 'approve');
    resolved.textContent = outcome === 'deny' ? 'Denied' : `Approved (${outcome})`;
    bubble.appendChild(resolved);
  } catch (e) {
    if (actions) actions.querySelectorAll('button').forEach(b => b.disabled = false);
    alert("Couldn't resolve that request — it may have already timed out. " + (e.message || e));
  }
}

function appendServerChatMessages(rows) {
  const win = document.getElementById('serverchat-window');
  const atBottom = win.scrollHeight - win.scrollTop - win.clientHeight < 60;
  rows.forEach(m => {
    const row = document.createElement('div');
    row.dataset.serverchatMsgId = String(m.id);

    if (m.kind === 'approval_request' && m.approval_id != null) {
      row.className = 'chat-row in';
      const bubble = document.createElement('div');
      bubble.className = 'chat-bubble approval-request';
      const title = document.createElement('div');
      title.className = 'approval-title';
      title.textContent = '📋 Approval needed';
      bubble.appendChild(title);
      if (m.text) {
        const textEl = document.createElement('div');
        textEl.textContent = m.text;
        bubble.appendChild(textEl);
      }
      const meta = document.createElement('span');
      meta.className = 'chat-meta';
      meta.textContent = fmtChatTime(m.ts);
      bubble.appendChild(meta);

      const actions = document.createElement('div');
      actions.className = 'approval-actions';
      const approveBtn = document.createElement('button');
      approveBtn.type = 'button';
      approveBtn.className = 'btn primary';
      approveBtn.textContent = 'Approve';
      approveBtn.onclick = () => resolveServerChatApproval(m.approval_id, 'once', bubble);
      actions.appendChild(approveBtn);
      const denyBtn = document.createElement('button');
      denyBtn.type = 'button';
      denyBtn.className = 'btn danger';
      denyBtn.textContent = 'Deny';
      denyBtn.onclick = () => resolveServerChatApproval(m.approval_id, 'deny', bubble);
      actions.appendChild(denyBtn);

      // "More" reveals the other two outcomes (session/always) — kept
      // out of the primary two buttons to match the Android card's own
      // primary-Approve/Deny-plus-overflow shape.
      const more = document.createElement('div');
      more.className = 'approval-more';
      const moreBtn = document.createElement('button');
      moreBtn.type = 'button';
      moreBtn.className = 'btn';
      moreBtn.textContent = 'More ▾';
      const moreMenu = document.createElement('div');
      moreMenu.className = 'approval-more-menu hidden';
      const sessionBtn = document.createElement('button');
      sessionBtn.type = 'button';
      sessionBtn.textContent = 'Approve for this session';
      sessionBtn.onclick = () => { moreMenu.classList.add('hidden'); resolveServerChatApproval(m.approval_id, 'session', bubble); };
      const alwaysBtn = document.createElement('button');
      alwaysBtn.type = 'button';
      alwaysBtn.textContent = 'Always approve this tool';
      alwaysBtn.onclick = () => { moreMenu.classList.add('hidden'); resolveServerChatApproval(m.approval_id, 'always', bubble); };
      moreMenu.appendChild(sessionBtn);
      moreMenu.appendChild(alwaysBtn);
      moreBtn.onclick = () => moreMenu.classList.toggle('hidden');
      more.appendChild(moreBtn);
      more.appendChild(moreMenu);
      actions.appendChild(more);

      bubble.appendChild(actions);
      row.appendChild(bubble);
      win.appendChild(row);
      serverChatState.lastId = Math.max(serverChatState.lastId, m.id);
      return;
    }

    const isOut = m.sender_device_id === SERVER_CHAT_MY_DEVICE_ID;
    row.className = 'chat-row ' + (isOut ? 'out' : 'in');
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble';
    if (m.text) {
      const textEl = document.createElement('div');
      textEl.textContent = m.text;
      bubble.appendChild(textEl);
      bubble.appendChild(makeCopyButton(() => m.text, 'Message'));
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
    if (isOut) {
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'chat-msg-delete';
      del.textContent = 'delete';
      del.onclick = () => deleteServerChatMessage(m.id, row);
      bubble.appendChild(del);
    }
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
      const res = await fetch('/api/server-chat/send-file', { method: 'POST', headers, body: fd });
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
    statusEl.textContent = 'Send failed — check that the ABP server is running.';
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
  pollWhenVisible(refreshServerChatMessages, 2000, 'server-chat');
  pollWhenVisible(refreshServerChatList, 15000, 'server-chat');
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
    if (item.text) {
      bubble.appendChild(Object.assign(document.createElement('div'), { textContent: item.text }));
      bubble.appendChild(makeCopyButton(() => item.text, 'Message'));
    }
    if (item.attachment_path) {
      const att = document.createElement('a');
      att.className = 'chat-attachment';
      att.textContent = '📎 ' + (item.attachment_name || 'file');
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
  document.getElementById('btn-session-copy').onclick = () => copyText(transcriptFromWindow(win), 'Conversation');
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
  pollWhenVisible(refreshSessions, 15000, 'sessions');
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
        statusEl.textContent = 'Save failed — check that the ABP server is running and try again.';
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

async function refreshMobileKeys() {
  const tbody = document.getElementById('mobile-keys-tbody');
  let keys;
  try {
    keys = await api('/api/mobile-keys');
  } catch (_e) { return; }
  const TIER_OPTIONS = [['none', 'No admin access'], ['standard', 'Standard'], ['elevated', 'Elevated'], ['unrestricted', 'Unrestricted']];
  tbody.innerHTML = keys.length ? keys.map(k => `
    <tr>
      <td>${esc(k.label)}</td>
      <td class="mono">${fmtMobileTime(k.created_at)}</td>
      <td class="mono">${fmtMobileTime(k.last_used_at)}</td>
      <td>${k.revoked_at ? esc((TIER_OPTIONS.find(([v]) => v === k.permission_tier) || [null, k.permission_tier])[1]) : `
        <select data-mobile-tier="${k.id}" style="font-size:11.5px; padding:3px 5px;">
          ${TIER_OPTIONS.map(([v, label]) => `<option value="${v}" ${v === k.permission_tier ? 'selected' : ''}>${esc(label)}</option>`).join('')}
        </select>`}</td>
      <td><span class="pill"><span class="dot ${k.revoked_at ? '' : 'good'}"></span>${k.revoked_at ? 'Revoked' : 'Active'}</span></td>
      <td>${k.revoked_at ? '' : `
        <button class="btn" data-mobile-send-apk="${k.id}" style="padding:3px 8px; font-size:11px;">Send APK</button>
        <button class="btn" data-mobile-revoke="${k.id}" style="padding:3px 8px; font-size:11px;">Revoke</button>`}</td>
    </tr>`).join('') : '<tr class="emptyrow"><td colspan="6">No devices paired yet.</td></tr>';

  tbody.querySelectorAll('[data-mobile-revoke]').forEach(btn => btn.onclick = async () => {
    if (!confirm('Revoke this device\'s key? It loses access to chat/sessions/jobs/bots immediately.')) return;
    await api(`/api/mobile-keys/${btn.dataset.mobileRevoke}`, { method: 'DELETE' });
    refreshMobileKeys();
  });

  // The dashboard's own DASHBOARD_TOKEN caller is the unconditional top
  // authority (bot/device_tiers.py — can_manage/can_mint aren't checked
  // for it at all, see _resolve_actor_tier's caller_device_id is None
  // branch), so unlike the Android app's own tier picker, there's no
  // "capped at my own tier" ceiling to compute here — every tier is
  // always offered and always takes effect immediately on change.
  tbody.querySelectorAll('[data-mobile-tier]').forEach(sel => {
    const original = sel.value;
    sel.onchange = async () => {
      const keyId = sel.dataset.mobileTier;
      const newTier = sel.value;
      sel.disabled = true;
      try {
        await api(`/api/mobile-keys/${keyId}/tier`, { method: 'POST', body: JSON.stringify({ tier: newTier }) });
        document.getElementById('mobile-devices-status').textContent = `Permission tier updated to "${newTier}".`;
      } catch (e) {
        sel.value = original;
        document.getElementById('mobile-devices-status').textContent = `Couldn't change permission tier: ${e.message || e}`;
      } finally {
        sel.disabled = false;
      }
    };
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

async function refreshNetworkInfoHint() {
  // Mirrors the desktop app's own autoFillMobileHosts() (main.js) exactly:
  // pre-fill both host fields the moment the Mobile tab loads, so pairing
  // never blocks on the operator going to find their own LAN IP or
  // Tailscale address. Only fills in blank fields — never overwrites
  // something already typed or edited.
  const hostInput = document.getElementById('mobile-new-host');
  const host2Input = document.getElementById('mobile-new-host2');
  const host3Input = document.getElementById('mobile-new-host3');
  const note = document.getElementById('mobile-network-info-note');
  if (!hostInput || !getToken()) return;
  let info;
  try {
    info = await api('/api/network-info');
  } catch (_e) { return; }
  const parts = [];
  if (info.lan) {
    parts.push(`LAN ${info.lan}`);
    if (!hostInput.value.trim()) hostInput.value = info.lan;
  }
  if (info.tailscale && info.tailscale !== hostInput.value.trim()) {
    parts.push(`Tailscale ${info.tailscale}`);
    if (!host2Input.value.trim()) host2Input.value = info.tailscale;
  }
  if (info.funnel && info.funnel !== hostInput.value.trim() && info.funnel !== host2Input.value.trim()) {
    parts.push(`Funnel ${info.funnel}`);
    if (!host3Input.value.trim()) host3Input.value = info.funnel;
  }
  if (note && parts.length) {
    note.textContent = `Auto-filled with this machine's own detected addresses (${parts.join(', ')}) — edit them only if wrong. Each is an independent path; the app tries them in order and switches automatically if one stops answering.`;
  }
}

document.getElementById('btn-mobile-generate').onclick = async () => {
  const label = document.getElementById('mobile-new-label').value.trim() || 'Unnamed device';
  const host = document.getElementById('mobile-new-host').value.trim();
  const host2 = document.getElementById('mobile-new-host2').value.trim();
  const host3 = document.getElementById('mobile-new-host3').value.trim();
  const tier = document.getElementById('mobile-new-tier').value;
  const btn = document.getElementById('btn-mobile-generate');
  btn.disabled = true;
  try {
    const res = await api('/api/mobile-keys', { method: 'POST', body: JSON.stringify({ label, host, host2, host3, tier }) });
    document.getElementById('mobile-new-key').textContent = res.pairing_code;
    document.getElementById('mobile-new-qr').src = `data:image/png;base64,${res.qr_png_base64}`;
    const hostsNote = [res.host && `primary ${res.host}`, res.host2 && `fallback ${res.host2}`, res.host3 && `public ${res.host3}`].filter(Boolean).join(', ');
    document.getElementById('mobile-new-hosts-used').textContent = hostsNote ? `Paired with: ${hostsNote}` : 'No host configured yet — add one later from this device.';
    document.getElementById('mobile-new-result').classList.remove('hidden');
    document.getElementById('mobile-new-label').value = '';
    document.getElementById('mobile-new-host').value = '';
    document.getElementById('mobile-new-host2').value = '';
    document.getElementById('mobile-new-host3').value = '';
    document.getElementById('mobile-new-tier').value = 'none';
    refreshMobileKeys();
  } catch (e) {
    alert('Failed to generate key — check that the ABP server is running.');
  } finally {
    btn.disabled = false;
  }
};

// ------------------------------------------------------------------ files
// Allowlisted directory browsing/download (bot/file_share.py) — how a
// file that lives outside AgenticBotPlatform's own data (e.g. a freshly built
// Android APK) gets reached from any device over the same Funnel/
// Tailscale/LAN paths + auth as everything else here.
const filesState = { root: null, path: '' };

function fmtFileSize(bytes) {
  if (bytes === null || bytes === undefined) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

async function refreshFileRoots(selectRoot) {
  const select = document.getElementById('files-root-select');
  let roots;
  try {
    roots = await api('/api/files');
  } catch (_e) { return; }
  const names = Object.keys(roots).sort();
  select.innerHTML = names.length
    ? names.map(n => `<option value="${esc(n)}" ${!roots[n].exists ? 'style="color:var(--warning);"' : ''}>${esc(n)}${roots[n].exists ? '' : ' (missing)'}</option>`).join('')
    : '<option value="">No folders added yet</option>';
  const target = selectRoot && names.includes(selectRoot) ? selectRoot : (names.includes(filesState.root) ? filesState.root : names[0]);
  select.value = target || '';
  filesState.root = target || null;
  filesState.path = '';
  await refreshFilesList();
}

async function refreshFilesList() {
  const tbody = document.getElementById('files-tbody');
  const breadcrumb = document.getElementById('files-breadcrumb');
  if (!filesState.root) {
    tbody.innerHTML = '<tr class="emptyrow"><td colspan="4">Add a folder above to start browsing.</td></tr>';
    breadcrumb.textContent = '';
    return;
  }
  breadcrumb.textContent = `${filesState.root}${filesState.path ? '/' + filesState.path : ''}`;
  let entries;
  try {
    const res = await api(`/api/files/${encodeURIComponent(filesState.root)}?path=${encodeURIComponent(filesState.path)}`);
    entries = res.entries;
  } catch (e) {
    tbody.innerHTML = `<tr class="emptyrow"><td colspan="4">Couldn't list this folder: ${esc(e.message)}</td></tr>`;
    return;
  }
  const rows = [];
  if (filesState.path) {
    rows.push(`<tr><td colspan="4"><button class="btn" data-files-up style="padding:3px 8px; font-size:11px;">.. (up one level)</button></td></tr>`);
  }
  rows.push(...entries.map(en => `
    <tr>
      <td>${en.is_dir ? '📁 ' : '📄 '}${en.is_dir ? `<a href="#" data-files-open="${esc(en.name)}">${esc(en.name)}</a>` : esc(en.name)}</td>
      <td class="mono">${fmtFileSize(en.size)}</td>
      <td class="mono">${fmtMobileTime(en.modified_at)}</td>
      <td>${en.is_dir ? '' : `<button class="btn" data-files-download="${esc(en.name)}" style="padding:3px 8px; font-size:11px;">Download</button>`}</td>
    </tr>`));
  tbody.innerHTML = rows.length ? rows.join('') : '<tr class="emptyrow"><td colspan="4">Empty folder.</td></tr>';

  tbody.querySelectorAll('[data-files-open]').forEach(a => a.onclick = (ev) => {
    ev.preventDefault();
    filesState.path = filesState.path ? `${filesState.path}/${a.dataset.filesOpen}` : a.dataset.filesOpen;
    refreshFilesList();
  });
  const upBtn = tbody.querySelector('[data-files-up]');
  if (upBtn) upBtn.onclick = () => {
    const parts = filesState.path.split('/');
    parts.pop();
    filesState.path = parts.join('/');
    refreshFilesList();
  };
  tbody.querySelectorAll('[data-files-download]').forEach(btn => btn.onclick = () => {
    const filePath = filesState.path ? `${filesState.path}/${btn.dataset.filesDownload}` : btn.dataset.filesDownload;
    downloadUrl(`/api/files/${encodeURIComponent(filesState.root)}/download?path=${encodeURIComponent(filePath)}`);
  });
}

document.getElementById('files-root-select').onchange = (ev) => {
  filesState.root = ev.target.value || null;
  filesState.path = '';
  refreshFilesList();
};

document.getElementById('btn-files-add-root').onclick = async () => {
  const name = document.getElementById('files-new-name').value.trim();
  const path = document.getElementById('files-new-path').value.trim();
  if (!name || !path) { alert('A name and a path are both required.'); return; }
  const btn = document.getElementById('btn-files-add-root');
  btn.disabled = true;
  try {
    await api('/api/files', { method: 'POST', body: JSON.stringify({ name, path }) });
    document.getElementById('files-new-name').value = '';
    document.getElementById('files-new-path').value = '';
    await refreshFileRoots(name);
  } catch (e) {
    alert(`Couldn't add that folder: ${e.message}`);
  } finally {
    btn.disabled = false;
  }
};

document.getElementById('btn-files-remove-root').onclick = async () => {
  if (!filesState.root) return;
  if (!confirm(`Remove "${filesState.root}" from the browsable list? The folder itself is untouched — this only stops it being reachable from here.`)) return;
  await api(`/api/files/${encodeURIComponent(filesState.root)}`, { method: 'DELETE' });
  refreshFileRoots();
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
  const setup_ssh = document.getElementById('peer-setup-ssh').checked;
  const statusEl = document.getElementById('peer-link-status');
  if (!name || !pairing_token) {
    statusEl.textContent = 'Name and its pairing token are both required.';
    return;
  }
  const btn = document.getElementById('btn-peer-link');
  btn.disabled = true;
  statusEl.textContent = 'Linking…';
  try {
    const res = await api('/api/peers/link', { method: 'POST', body: JSON.stringify({ name, pairing_token, my_base_url, setup_ssh }) });
    let msg = `Linked to ${res.peer.name}.`;
    const ssh = res.peer.ssh_setup;
    if (ssh) {
      if (ssh.trusted_remote_key && ssh.connection_registered) msg += ' SSH connection set up automatically.';
      else if (ssh.trusted_remote_key || ssh.connection_registered) msg += ' SSH partially set up (see Linked Servers for details).';
      else if (setup_ssh) msg += ' SSH auto-setup did not complete (SSH Toolkit may be unavailable) — the link itself is fine.';
    }
    statusEl.textContent = msg;
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

// One shared countdown so re-clicking "Generate" replaces the old timer
// instead of stacking a second one ticking against a token that's already
// been superseded (server only ever keeps one pending token alive too).
let _peerTokenCountdownTimer = null;

// Pre-fill the optional "let it call you back" field with this server's
// own auto-detected address — a real query to the backend (which knows
// its actual outbound-facing LAN IP), not a guess from the page's own URL,
// so it's correct even when the dashboard was opened via 127.0.0.1 or
// (in the desktop app) isn't served from that address at all. Silently
// left blank if detection fails (offline machine) — the field is optional.
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
    const isDesktop = key === 'CLAUDE_DESKTOP_EXE';
    const already = f.present && f.valid;
    const placeholder = already ? 'already set — leave blank to keep' : (isDesktop ? 'optional — Auto-detect or paste a path' : '');
    const extraBtn = isDesktop ? `<button class="btn" data-detect="${key}" type="button">Auto-detect</button>` : '';
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
    if (res.status.ready) {
      closeWizard();
      startDashboard();
    } else {
      statusEl.textContent = 'Saved what you entered — required fields below still need attention.';
      renderWizardFields(res.status);
    }
  } catch (e) {
    statusEl.textContent = 'Save failed — check that the ABP server is running and try again.';
  }
};

document.getElementById('btn-wizard-skip').onclick = () => {
  closeWizard();
  startDashboard();
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
    const res = await fetch('/api/setup/status', { headers });
    if (!res.ok) { onReady(); return; } // 401 etc — let the normal dashboard flow prompt for the token
    const status = await res.json();
    if (status.ready) onReady(); else openWizard(status);
  } catch (e) {
    onReady(); // don't block the whole UI on a network hiccup
  }
}

checkSetupAndProceed(startDashboard);

// Was an inline onclick attribute (not allowed under the page's CSP).
(function () {
  var el = document.getElementById('peer-gen-token-value');
  if (el) el.addEventListener('click', function () { el.select(); });
})();
