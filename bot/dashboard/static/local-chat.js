// Local Chat: ABP's first screen. A chat with local models on the ModelMistress hub, with ABP's own agent
// (bot/localchat, /api/local-chat). Sessions on the left, the conversation in the middle, the session's model, mode,
// autonomy, cloud assist and workspace in the top bar, model and session details on the right, and the hub's state in a
// status strip at the bottom. The model picker shows every model the hub can run with each figure labelled by where it
// came from (measured, planned, metadata, estimated). Replies stream from /api/ws (chat_stream frames for chat_id
// "localchat:<sid>"); a tool that needs a person arrives as a local_chat_approval frame and is decided inline.
// Sessions (S2): the sidebar groups them (Pinned, Needs you, Working now, user groups, Today, Earlier, Archived) with a
// status dot, unread, model / workspace / branch chips, filters and a right-click menu; each pane has a session header with
// its worktree (branch, ahead / behind, changed files) and Diff / Sync / Commit / Merge (through the push queue) / Keep /
// Discard; a turn that changed files can be undone; a message sent while it works steers it or waits in the queue; two
// sessions can sit side by side (split view); the browser notifies when a session needs you or finishes out of sight.
// Linked sessions (S3): Fork, New task and Hand off from a session's menu; children are listed under their parent (and in
// its header), a child's turn end lands in the parent as a note with an Open button, and Factory workers are read-only.
// Agent and Factory (P4): Factory mode turns a goal into a conductor plan with a Run button (confined workers; the first
// run in a repository asks), the result's push-queue entry can be approved from the chat (approving never pushes), cloud
// assist adds "Ask the cloud" for one message, and the right panel shows the context meter, tokens (local / cloud), the
// agent's autonomy and tools, the session's Factory board, the manager's digest and escalations, and WatchDog incidents.
// Long sessions (S4): the agent's task list and next-step question show under its last reply (answer buttons send the
// answer), compactions and chapters are visible dividers (Chapters jumps to one), and Compact / Export are one click.
// The `/` palette (P5) also searches ABP's capability index (/api/capabilities): pages, settings, actions, tools.
//
// This file and local-chat.css are identical in the dashboard (bot/dashboard/static/) and the desktop app
// (desktop-app/ui/); tests/test_local_chat_ui.py fails if they differ. It uses the page's own api() and draws into #lc-root.
// The page forwards every socket frame as a window "abp:ws-frame" event; nothing here opens a socket of its own.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) { /* plain text */ } return String(m); };
  const toast = (m, t) => { if (typeof window.showToast === 'function') window.showToast(m, t); };
  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });
  const patch = (path, body) => api(path, { method: 'PATCH', body: JSON.stringify(body || {}) });
  const store = { get(k) { try { return localStorage.getItem('lc-' + k); } catch (_) { return null; } }, set(k, v) { try { localStorage.setItem('lc-' + k, v); } catch (_) { /* private mode */ } } };

  const MODES = [['chat', 'Chat', 'Plain chat, no tools'], ['build', 'Build', 'The agent edits and runs commands; edits ask as usual'],
    ['plan', 'Plan', 'Proposes a plan for your approval first'], ['review', 'Review', 'Reads only'],
    ['factory', 'Factory', 'A goal becomes a conductor plan; confined workers run it only when you press Run']];
  const AUTONOMY = ['0 Locked', '1 Cautious', '2 Supervised', '3 Trusted', '4 Autonomous', '5 Sovereign'];
  const CLOUD = [['off', 'Cloud off'], ['ask', 'Cloud: ask'], ['auto', 'Cloud: auto']];
  const GROUP_LABEL = { loaded: 'Loaded', gpu: 'Fits on the GPU', cpu: 'Runs on the CPU (slow)', too_big: 'Too big for this machine', other: 'Other (not planned by the hub)' };
  const SOURCE_LABEL = { measured: 'measured', estimated: 'estimated', rough_estimate: 'rough estimate', planned: 'planned', metadata: 'metadata', unknown: 'unknown' };
  const RUNS_ON = { gpu: 'GPU', cpu: 'CPU', none: 'does not fit', unknown: 'unknown' };
  const STATUS_LABEL = { working: 'working', needs_you: 'needs you', idle: 'idle', error: 'error', done: 'done' };
  const SLASH = [['/new', 'Start a new session'], ['/model', 'Open the model picker'], ['/mode chat|build|plan|review|factory', 'Switch the agent type'],
    ['/autonomy 0-5', 'Set autonomy'], ['/cloud off|ask|auto', 'Set cloud assist'], ['/export', 'Download this session as Markdown'],
    ['/settings', 'Open the settings']];

  // ---- state ---------------------------------------------------------------------------------------------------------
  const S = {
    sessions: [], groups: [], query: '', showArchived: false, filter: { status: '', model: '', workspace: '' },
    collapsed: (() => { try { return JSON.parse(store.get('collapsed') || '{}'); } catch (_) { return {}; } })(),
    catalog: null, catalogErr: '', status: null, setup: null,
    panes: [],                // [{sid, session, messages, running, draft:{text, tools}, approvals, err, el}]
    active: 0, pickerFor: -1, rightOpen: store.get('right') !== '0', visible: false, timer: null,
    defaults: { mode: 'build', autonomy: 2, cloud_assist: 'ask', workspace: store.get('workspace') || '' },
  };
  const ABP = window.ABP = window.ABP || {};

  // ---- markdown (everything escaped first; only a fixed set of tags is produced) --------------------------------------
  function inline(s) {
    return E(s)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<i>$2</i>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }
  function blocks(text) {
    const out = [];
    let list = null;
    let para = [];
    const flushPara = () => { if (para.length) { out.push('<p>' + para.map(inline).join('<br>') + '</p>'); para = []; } };
    const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.map((i) => '<li>' + inline(i) + '</li>').join('')}</${list.tag}>`); list = null; } };
    String(text).split('\n').forEach((line) => {
      let m;
      if (!line.trim()) { flushPara(); flushList(); return; }
      if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { flushPara(); flushList(); out.push(`<h${m[1].length + 2}>${inline(m[2])}</h${m[1].length + 2}>`); return; }
      if ((m = line.match(/^\s*[-*]\s+(.*)$/)) || (m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
        flushPara();
        const tag = /^\s*\d/.test(line) ? 'ol' : 'ul';
        if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
        list.items.push(m[1]);
        return;
      }
      if ((m = line.match(/^>\s?(.*)$/))) { flushPara(); flushList(); out.push('<blockquote>' + inline(m[1]) + '</blockquote>'); return; }
      flushList();
      para.push(line);
    });
    flushPara(); flushList();
    return out.join('');
  }
  function md(src) {
    return String(src || '').split('```').map((part, i) => {
      if (!(i % 2)) return blocks(part);
      const nl = part.indexOf('\n');
      const lang = nl >= 0 ? part.slice(0, nl).trim() : '';
      const code = (nl >= 0 ? part.slice(nl + 1) : part).replace(/\n$/, '');
      return `<div class="lc-code"><div class="lc-code-bar"><span>${E(lang || 'code')}</span><button type="button" class="lc-mini" data-lc="copy-code" aria-label="Copy this code">Copy</button></div><pre><code>${E(code)}</code></pre></div>`;
    }).join('');
  }
  ABP.localChatMarkdown = md;     // for the tests and for other panels that want the same rendering

  // ---- helpers -------------------------------------------------------------------------------------------------------
  const pane = (i) => S.panes[i == null ? S.active : i];
  const modelRow = (id) => ((S.catalog && S.catalog.models) || []).find((r) => r.id === id) || null;
  const shortModel = (id) => { const r = modelRow(id); const s = String((r && r.name) || id || ''); return s.split(':').pop().split('/').pop(); };
  const fmtTps = (v) => (v ? (v >= 10 ? Math.round(v) : v.toFixed(1)) + ' tok/s' : '');
  const gb = (mib) => (mib ? (mib / 1024).toFixed(1) + ' GB' : '?');
  const badge = (src) => `<span class="lc-src lc-src-${E(src || 'unknown')}" title="Where this figure came from">${E(SOURCE_LABEL[src] || src || 'unknown')}</span>`;
  const when = (t) => { if (!t) return ''; const d = new Date(t * 1000); const today = new Date(); return d.toDateString() === today.toDateString() ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : d.toLocaleDateString(); };

  function download(name, text, type) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: type || 'text/plain' }));
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }
  function asMarkdown(p) {
    const s = p.session || {};
    const lines = [`# ${s.title || 'Local Chat session'}`, '', `Model: ${s.model || '-'} · mode: ${s.mode || '-'} · workspace: ${s.workspace || '-'}`, ''];
    p.messages.forEach((m) => { lines.push(`## ${m.role}${m.model && m.role === 'assistant' ? ' (' + shortModel(m.model) + ')' : ''}`, '', m.text, ''); });
    return lines.join('\n');
  }

  // ---- loading -------------------------------------------------------------------------------------------------------
  async function loadSessions() {
    try {
      const q = S.query ? '&q=' + encodeURIComponent(S.query) : '';
      const [d, g] = await Promise.all([api('/api/local-chat/sessions?archived=' + (S.showArchived ? 'true' : 'false') + q),
        api('/api/local-chat/groups').catch(() => ({ groups: [] }))]);
      S.sessions = d.sessions || [];
      S.groups = g.groups || [];
    } catch (e) { S.sessions = []; S.sessionsErr = errText(e); }
    renderSide();
  }
  async function loadWorktree(i) {
    const p = pane(i);
    if (!p || !p.sid) return;
    const sid = p.sid;
    try {
      const [w, c, q] = await Promise.all([api('/api/local-chat/sessions/' + encodeURIComponent(sid) + '/worktree').catch((e) => ({ worktree: null, err: errText(e) })),
        api('/api/local-chat/sessions/' + encodeURIComponent(sid) + '/checkpoints').catch(() => ({ checkpoints: [] })),
        api('/api/local-chat/sessions/' + encodeURIComponent(sid) + '/queued').catch(() => ({ queued: [] }))]);
      if (p.sid !== sid) return;
      p.wt = w.worktree; p.wtErr = w.err || '';
      p.checkpoints = c.checkpoints || [];
      p.queued = q.queued || [];
      p.children = (await api('/api/local-chat/sessions/' + encodeURIComponent(sid) + '/children').catch(() => ({ children: [] }))).children || [];
      const [ctx, fb] = await Promise.all([api('/api/local-chat/sessions/' + encodeURIComponent(sid) + '/context').catch(() => null),
        api('/api/local-chat/sessions/' + encodeURIComponent(sid) + '/factory').catch(() => null)]);
      p.ctx = ctx; p.factory = fb;
      loadSide();
    } catch (_) { /* the header just stays as it was */ }
    renderHead(i); renderMsgs(i); renderComposerState(i); renderRight(i);
  }
  async function loadSide() {                // the manager's digest and escalations, WatchDog incidents (shared by every pane)
    if (S.sideAt && Date.now() - S.sideAt < 30000) return;
    S.sideAt = Date.now();
    const [dg, es, rb] = await Promise.all([api('/api/manager/digest?text=true').catch(() => null), api('/api/manager/escalations?limit=5').catch(() => null),
      api('/api/redblue/summary?limit=3').catch(() => null)]);
    S.side = { digest: dg, escalations: es, watchdog: rb };
    S.panes.forEach((_, n) => renderRight(n));
  }
  function markRead(i) {
    const p = pane(i);
    if (!p || !p.sid || document.hidden) return;
    const row = S.sessions.find((r) => r.id === p.sid);
    if (row && !row.unread && !(p.session && p.session.unread)) return;
    if (row) row.unread = false;
    post('/api/local-chat/sessions/' + encodeURIComponent(p.sid) + '/read').then(() => renderSide()).catch(() => {});
  }
  async function loadCatalog(refresh) {
    try {
      S.catalog = await api('/api/local-chat/models' + (refresh ? '?refresh=true' : ''));
      S.catalogErr = '';
    } catch (e) { S.catalogErr = errText(e); }
    S.panes.forEach((p, i) => { renderTop(i); renderRight(i); if (!p.sid) renderMsgs(i); });
    if (S.pickerFor >= 0) renderPicker();
    renderStatus();
  }
  async function loadStatus() {
    try { S.status = await api('/api/local-chat/status'); } catch (e) { S.status = { error: errText(e) }; }
    if (S.status && S.status.settings && !S.defaultsLoaded) {
      const st = S.status.settings;
      S.defaults = { ...S.defaults, mode: st.default_mode || 'build', autonomy: st.default_autonomy == null ? 2 : st.default_autonomy, cloud_assist: st.cloud_assist || 'ask' };
      S.defaultsLoaded = true;
      S.panes.forEach((p, i) => { if (!p.sid) renderTop(i); });
    }
    renderStatus();
  }
  async function loadSetup() {
    try { S.setup = await api('/api/setup/status'); } catch (_) { S.setup = null; }
    renderHint();
  }
  async function openSession(sid, i) {
    const p = pane(i);
    if (!p) return;
    p.sid = sid; p.session = null; p.messages = []; p.draft = null; p.approvals = []; p.err = ''; p.wt = null; p.checkpoints = []; p.queued = [];
    if (sid) store.set('last', sid);
    renderPane(i == null ? S.active : i);
    if (!sid) return;
    try {
      const d = await api('/api/local-chat/sessions/' + encodeURIComponent(sid));
      if (p.sid !== sid) return;
      p.session = d.session; p.messages = d.messages || []; p.running = !!d.running;
      if (p.running && !p.draft) p.draft = { text: '', tools: [] };
    } catch (e) { p.err = errText(e); }
    renderPane(i == null ? S.active : i);
    renderSide();
    loadWorktree(i == null ? S.active : i);
    markRead(i == null ? S.active : i);
  }
  async function refreshMessages(i) {
    const p = pane(i);
    if (!p || !p.sid) return;
    const last = p.messages.length ? p.messages[p.messages.length - 1].id : 0;
    try {
      const d = await api('/api/local-chat/sessions/' + encodeURIComponent(p.sid) + '?after=' + last);
      p.session = d.session;
      if ((d.messages || []).length) p.messages = p.messages.concat(d.messages);
      const was = p.running;
      p.running = !!d.running;
      if (was && !p.running) { p.draft = null; loadWorktree(i); markRead(i); }
    } catch (e) { p.err = errText(e); }
    renderMsgs(i); renderTop(i); renderRight(i); renderComposerState(i);
  }

  // ---- changing a session --------------------------------------------------------------------------------------------
  async function setField(i, field, value) {
    const p = pane(i);
    if (!p.sid) {
      S.defaults[field] = value;
      if (field === 'workspace') store.set('workspace', value);
      renderTop(i);
      return;
    }
    try {
      p.session = await patch('/api/local-chat/sessions/' + encodeURIComponent(p.sid), { [field]: value });
      if (field === 'workspace') store.set('workspace', value);
    } catch (e) { toast(errText(e), 'error'); }
    renderTop(i); renderRight(i);
    if (field === 'title' || field === 'pinned' || field === 'archived' || field === 'model') loadSessions();
  }
  async function newSession(i) {
    await openSession('', i);
    const el = pane(i) && pane(i).el && pane(i).el.querySelector('.lc-input');
    if (el) el.focus();
  }
  async function ensureSession(i, firstText) {
    const p = pane(i);
    if (p.sid) return p.sid;
    const d = S.defaults;
    const title = String(firstText || '').split(/\s+/).join(' ').slice(0, 60);     // also names the worktree's branch
    const s = await post('/api/local-chat/sessions', { title, model: d.model || '', mode: d.mode, autonomy: d.autonomy, cloud_assist: d.cloud_assist, workspace: d.workspace || '' });
    p.sid = s.id; p.session = s; p.messages = [];
    store.set('last', s.id);
    loadSessions();
    loadWorktree(i);
    return s.id;
  }

  // ---- sending -------------------------------------------------------------------------------------------------------
  function slash(i, text) {
    const [cmd, arg] = text.trim().split(/\s+/, 2);
    const p = pane(i);
    if (cmd === '/new') { newSession(i); return true; }
    if (cmd === '/model') { openPicker(i); return true; }
    if (cmd === '/mode' && MODES.some((m) => m[0] === arg)) { setField(i, 'mode', arg); return true; }
    if (cmd === '/autonomy' && /^[0-5]$/.test(arg || '')) { setField(i, 'autonomy', Number(arg)); return true; }
    if (cmd === '/cloud' && CLOUD.some((c) => c[0] === arg)) { setField(i, 'cloud_assist', arg); return true; }
    if (cmd === '/export') { if (p.sid) download((p.session.title || 'session').replace(/[^\w.-]+/g, '_') + '.md', asMarkdown(p), 'text/markdown'); return true; }
    if (cmd === '/settings') { const a = document.querySelector('nav.sidenav a[href="#settings"]') || document.querySelector('nav.sidenav a[href="#control"]'); if (a) a.click(); return true; }
    return false;
  }
  async function send(i, textArg, cloud) {
    const p = pane(i);
    const box = p.el.querySelector('.lc-input');
    const text = (textArg != null ? textArg : box.value).trim();
    if (!text) return;
    if (text.startsWith('/') && textArg == null && slash(i, text)) { box.value = ''; hideSlash(i); return; }
    if (p.running) { steer(i, text, 'steer'); return; }
    p.err = '';
    try {
      const sid = await ensureSession(i, text);
      if (textArg == null) box.value = '';
      hideSlash(i);
      p.messages.push({ id: (p.messages.length ? p.messages[p.messages.length - 1].id : 0) + 0.5, role: 'user', text, created: Date.now() / 1000, pending: true });
      p.running = true; p.draft = { text: '', tools: [] };
      renderMsgs(i); renderComposerState(i);
      const r = await post('/api/local-chat/sessions/' + encodeURIComponent(sid) + '/send', cloud ? { text, cloud: true } : { text });
      p.turn = r.turn_id;
      p.messages = p.messages.filter((m) => !m.pending);
      await refreshMessages(i);
      if (p.running && !p.draft) p.draft = { text: '', tools: [] };
      renderMsgs(i); renderComposerState(i);
    } catch (e) {
      p.messages = p.messages.filter((m) => !m.pending);
      p.running = false; p.draft = null; p.err = errText(e);
      if (textArg == null && !box.value) box.value = text;
      renderMsgs(i); renderComposerState(i);
    }
  }
  async function steer(i, textArg, how) {
    const p = pane(i);
    const box = p.el.querySelector('.lc-input');
    const text = (textArg != null ? textArg : box.value).trim();
    if (!text || !p.sid) return;
    try {
      const r = await post('/api/local-chat/sessions/' + encodeURIComponent(p.sid) + '/steer', { text, how });
      if (box.value.trim() === text) box.value = '';
      toast(r.steered ? 'Sent to the running turn.' : 'Queued: it starts when this turn finishes.');
      if (r.steered) refreshMessages(i); else loadWorktree(i);
    } catch (e) { toast(errText(e), 'error'); }
  }
  async function cancel(i) {
    const p = pane(i);
    if (!p.sid || !p.running) return;
    try { await post('/api/local-chat/sessions/' + encodeURIComponent(p.sid) + '/cancel'); } catch (e) { toast(errText(e), 'error'); }
    refreshMessages(i);
  }

  // ---- socket frames -------------------------------------------------------------------------------------------------
  function onFrame(msg) {
    if (!msg || typeof msg !== 'object') return;
    const sid = msg.type === 'chat_stream' ? (String(msg.chat_id || '').startsWith('localchat:') ? msg.chat_id.slice(10) : null) : msg.session_id;
    if (!sid) return;
    S.panes.forEach((p, i) => {
      if (p.sid !== sid) return;
      if (msg.type === 'chat_stream') {
        if (!p.draft) p.draft = { text: '', tools: [] };
        p.running = true;
        if (msg.kind === 'text_delta') p.draft.text += msg.text || '';
        else if (msg.kind === 'reset') p.draft.text = '';
        else if (msg.kind === 'tool_start') p.draft.tools.push({ name: msg.tool, state: 'running' });
        else if (msg.kind === 'tool_end') { const t = p.draft.tools.slice().reverse().find((x) => x.name === msg.tool && x.state === 'running'); if (t) t.state = 'done'; }
        renderDraft(i);
      } else if (msg.type === 'local_chat_approval') {
        p.approvals = (p.approvals || []).filter((a) => a.approval_id !== msg.approval_id).concat([msg]);
        renderMsgs(i);
      } else if (msg.type === 'local_chat_turn') {
        p.running = false; p.draft = null; p.approvals = [];
        p.lastTurn = msg;
        refreshMessages(i).then(() => { loadWorktree(i); markRead(i); });
        renderStatus();
      }
    });
    if (msg.type === 'local_chat_turn') loadSessions();
    if (msg.type === 'local_chat_session' && msg.session) onSessionFrame(msg);
    if (msg.type === 'local_chat_compacted') S.panes.forEach((p, i) => { if (p.sid === msg.session_id) refreshMessages(i); });
    if (msg.type === 'local_chat_note') S.panes.forEach((p, i) => { if (p.sid === msg.session_id) { refreshMessages(i); loadWorktree(i); } });
  }
  function onSessionFrame(msg) {
    const s = msg.session;
    const row = S.sessions.find((r) => r.id === s.id);
    const was = row ? row.status : '';
    if (row) Object.assign(row, s);
    else if (!S.showArchived && !s.archived) { loadSessions(); }
    const open = S.panes.some((p) => p.sid === s.id);
    S.panes.forEach((p, i) => { if (p.sid === s.id) { p.session = { ...(p.session || {}), ...s }; renderHead(i); if (s.unread) markRead(i); } });
    renderSide();
    if (was !== s.status && (s.status === 'needs_you' || s.status === 'done' || s.status === 'error') && (document.hidden || !open)) {
      notify(s);
    }
  }
  function notify(s) {
    const text = s.status === 'needs_you' ? 'needs you (an approval is waiting)' : s.status === 'error' ? 'stopped with an error' : 'finished';
    if (typeof Notification === 'undefined' || Notification.permission !== 'granted' || store.get('notify') !== '1') {
      toast((s.title || 'A session') + ' ' + text + '.');
      return;
    }
    try {
      const n = new Notification('Local Chat: ' + (s.title || 'session'), { body: text, tag: 'lc-' + s.id });
      n.onclick = () => { window.focus(); openSession(s.id, S.active); n.close(); };
    } catch (_) { toast((s.title || 'A session') + ' ' + text + '.'); }
  }
  window.addEventListener('abp:ws-frame', (ev) => onFrame(ev.detail));

  async function decide(i, approvalId, outcome) {
    const p = pane(i);
    try {
      await post('/api/approvals/' + encodeURIComponent(approvalId) + '/resolve', { outcome });
      p.approvals = (p.approvals || []).filter((a) => String(a.approval_id) !== String(approvalId));
    } catch (e) { toast(errText(e), 'error'); }
    renderMsgs(i);
  }

  // ---- rendering: skeleton -------------------------------------------------------------------------------------------
  function render() {
    const root = $('lc-root');
    if (!root) return;
    if (!root.querySelector('.lc')) {
      root.innerHTML = `<div class="lc${S.rightOpen ? '' : ' lc-noright'}">
  <aside class="lc-side" aria-label="Local Chat sessions">
    <div class="lc-side-head"><button type="button" class="btn primary lc-new" data-lc="new">+ New session</button>
      <button type="button" class="lc-icon" data-lc="new-group" aria-label="New group" title="New group">⊞</button>
      <button type="button" class="lc-icon" data-lc="notify" aria-label="Desktop notifications" title="Notify me when a session needs me or finishes">🔔</button></div>
    <input type="search" class="lc-search" id="lc-search" aria-label="Search sessions and messages" placeholder="Search titles and messages">
    <div class="lc-filters" id="lc-filters"></div>
    <label class="lc-arch"><input type="checkbox" id="lc-archived"> Archived</label>
    <div class="lc-list" id="lc-list" role="list"></div>
    <button type="button" class="lc-mini lc-prune" data-lc="prune" title="Remove worktrees no session owns (never ones with uncommitted work)">Clean up worktrees</button>
  </aside>
  <div class="lc-panes" id="lc-panes"></div>
</div>
<div class="lc-hint hidden" id="lc-hint" role="status"></div>
<div class="lc-status" id="lc-status" role="status" aria-live="polite"></div>
<div class="lc-picker hidden" id="lc-picker" role="dialog" aria-label="Choose a model"></div>
<div class="lc-picker lc-modal hidden" id="lc-modal" role="dialog" aria-modal="true" aria-label="Local Chat"></div>`;
    }
    const host = $('lc-panes');
    while (host.children.length > S.panes.length) host.lastChild.remove();
    S.panes.forEach((p, i) => {
      if (!host.children[i]) {
        const div = document.createElement('div');
        div.className = 'lc-pane';
        div.dataset.pane = String(i);
        div.innerHTML = paneSkeleton(i);
        host.appendChild(div);
      }
      p.el = host.children[i];
      renderPane(i);
    });
    host.classList.toggle('lc-split', S.panes.length > 1);
    renderSide(); renderHint(); renderStatus();
  }
  function paneSkeleton(i) {
    return `<div class="lc-top" data-part="top"></div>
<div class="lc-head" data-part="head"></div>
<div class="lc-body"><div class="lc-center">
  <div class="lc-msgs" data-part="msgs" role="log" aria-live="polite" aria-label="Conversation"></div>
  <div class="lc-composer">
    <div class="lc-slash hidden" data-part="slash" role="listbox" aria-label="Commands"></div>
    <textarea class="lc-input" rows="3" aria-label="Message to the local model" placeholder="Message the local model. Ctrl+Enter sends, Tab switches the agent type, / for commands. Drop text files to add them."></textarea>
    <div class="lc-compose-bar">
      <span class="lc-note" data-part="cnote"></span>
      <button type="button" class="btn danger hidden" data-lc="cancel" aria-label="Stop the reply (Ctrl+C)">Stop</button>
      <button type="button" class="btn hidden" data-lc="queue" aria-label="Queue this message for the next turn">Queue</button>
      <button type="button" class="btn hidden" data-lc="cloud-send" aria-label="Send this one message to the cloud model" title="Cloud assist: this message only">Ask the cloud</button>
      <button type="button" class="btn primary" data-lc="send" aria-label="Send (Ctrl+Enter)">Send</button>
    </div>
  </div>
</div><aside class="lc-right" data-part="right" aria-label="Details"></aside></div>`;
  }
  function renderPane(i) {
    const p = pane(i);
    if (!p || !p.el) return;
    p.el.classList.toggle('lc-active', i === S.active && S.panes.length > 1);
    renderTop(i); renderHead(i); renderMsgs(i); renderRight(i); renderComposerState(i);
  }
  const part = (i, name) => { const p = pane(i); return p && p.el ? p.el.querySelector(`[data-part="${name}"]`) : null; };

  // ---- the top bar ---------------------------------------------------------------------------------------------------
  function renderTop(i) {
    const el = part(i, 'top');
    if (!el) return;
    const p = pane(i);
    const s = p.session || S.defaults;
    const model = (p.session && p.session.model) || S.defaults.model || (S.catalog && S.catalog.default_model) || '';
    const row = modelRow(model);
    const runs = row ? `<span class="lc-chip">${E(RUNS_ON[row.runs_on] || row.runs_on)}</span>` : '';
    const sp = row && row.speed && row.speed.tokens_per_s ? `<span class="lc-chip">${E(fmtTps(row.speed.tokens_per_s))}</span>` : '';
    el.innerHTML = `<button type="button" class="lc-model" data-lc="picker" aria-haspopup="dialog" aria-label="Choose the model" title="${E(model)}">
  <span class="lc-model-name">${E(model ? shortModel(model) : (S.catalogErr ? 'ModelMistress is not answering' : 'Pick a model'))}</span>${runs}${sp}<span aria-hidden="true">▾</span></button>
<div class="lc-seg" role="radiogroup" aria-label="Agent type">${MODES.map(([id, label, tip]) =>
    `<button type="button" role="radio" aria-checked="${s.mode === id}" class="${s.mode === id ? 'on' : ''}" data-lc="mode" data-v="${id}" title="${E(tip)}">${E(label)}</button>`).join('')}</div>
<select class="lc-sel" data-lc="autonomy" aria-label="Autonomy" title="How much the agent may do before it asks. Destructive actions always ask.">${AUTONOMY.map((label, n) =>
    `<option value="${n}"${Number(s.autonomy) === n ? ' selected' : ''}>${E(label)}</option>`).join('')}</select>
<select class="lc-sel" data-lc="cloud" aria-label="Cloud assist" title="Off: local only. Ask: one click per message to ask a cloud model. Auto: the capacity governor decides.">${CLOUD.map(([v, label]) =>
    `<option value="${v}"${s.cloud_assist === v ? ' selected' : ''}>${E(label)}</option>`).join('')}</select>
<input type="text" class="lc-ws" data-lc="workspace" aria-label="Workspace folder" placeholder="Workspace folder (optional)" value="${E(s.workspace || '')}">
<span class="lc-top-gap"></span>
${p.sid ? `<select class="lc-sel" data-lc="chapter-jump" aria-label="Jump to a chapter"><option value="">Chapters</option>${p.messages.filter((m) => m.meta && m.meta.chapter).map((m) =>
    `<option value="${E(m.id)}">${E(m.meta.chapter)}</option>`).join('')}<option value="+">+ Add a chapter here…</option></select>
<button type="button" class="lc-icon" data-lc="export" aria-label="Download this session as Markdown" title="Export as Markdown">⤓</button>
<button type="button" class="lc-icon" data-lc="export-json" aria-label="Download this session as JSON" title="Export as JSON">{}</button>` : ''}
${S.panes.length > 1 ? `<button type="button" class="lc-icon" data-lc="close-pane" aria-label="Close this pane" title="Close pane">✕</button>` : ''}
<button type="button" class="lc-icon" data-lc="right" aria-label="${S.rightOpen ? 'Hide' : 'Show'} the details panel" aria-expanded="${S.rightOpen}" title="Details">☰</button>`;
  }

  // ---- messages ------------------------------------------------------------------------------------------------------
  function msgHtml(m, i, isLastUser) {
    const role = m.role;
    const mm = m.meta || {};
    if (mm.chapter) return `<div class="lc-divider lc-chapter" id="lc-ch-${E(m.id)}" data-id="${E(m.id)}"><span>${E(mm.chapter)}</span></div>`;
    if (mm.compaction) {
      return `<div class="lc-divider lc-compacted" data-id="${E(m.id)}"><span>Compacted: ${E(mm.compaction.messages)} earlier message(s) summarized${mm.compaction.source === 'extract' ? ' (without a model)' : ''}</span>
<details><summary>The summary the agent continues from</summary><pre class="lc-pre">${E(mm.compaction.summary)}</pre></details></div>`;
    }
    const head = role === 'assistant'
      ? `<span class="lc-who">${E(shortModel(m.model) || 'assistant')}</span>${m.tokens_per_s ? `<span class="lc-meta">${E(fmtTps(m.tokens_per_s))} ${badge('measured')}</span>` : ''}${m.tokens ? `<span class="lc-meta">${E(m.tokens)} tokens</span>` : ''}`
      : `<span class="lc-who">${E(role === 'user' ? 'You' : role === 'error' ? 'Error' : 'ABP')}</span>`;
    const body = role === 'assistant' ? md(m.text) : `<p>${E(m.text).replace(/\n/g, '<br>')}</p>`;
    const actions = [`<button type="button" class="lc-mini" data-lc="copy-msg" data-id="${E(m.id)}" aria-label="Copy this message">Copy</button>`];
    const p = pane(i);
    const cp = role === 'user' && m.turn_id && !m.reverted ? (p.checkpoints || []).find((c) => c.turn_id === m.turn_id && !c.rewound) : null;
    const plan = m.meta && m.meta.factory_plan;
    if (plan) actions.push(`<button type="button" class="btn primary" data-lc="factory-run" data-plan="${E(plan.id)}" aria-label="Run this plan with confined workers">Run (confined workers)</button>`);
    const qid = m.meta && m.meta.queue_id;
    if (qid) {
      const q = ((p.factory && p.factory.queue) || []).find((x) => x.id === qid);
      if (!q || q.status === 'awaiting_approval') {
        actions.push(`<button type="button" class="btn primary" data-lc="queue-approve" data-id="${E(qid)}" aria-label="Approve this integration branch (does not push)">Approve</button>`);
        actions.push(`<button type="button" class="btn" data-lc="queue-reject" data-id="${E(qid)}" aria-label="Reject this integration branch">Reject</button>`);
      } else actions.push(`<span class="lc-chip">push queue: ${E(q.status)}</span>`);
    }
    const child = m.meta && m.meta.child;
    if (child) actions.push(`<button type="button" class="lc-mini" data-lc="open" data-sid="${E(child)}" aria-label="Open the linked session">Open</button>`);
    if (cp && !p.running) actions.push(`<button type="button" class="lc-mini" data-lc="rewind" data-turn="${E(m.turn_id)}" aria-label="Undo this turn's file changes">Undo this turn</button>`);
    if (role === 'user' && isLastUser) {
      actions.push(`<button type="button" class="lc-mini" data-lc="retry" data-id="${E(m.id)}" aria-label="Send this message again">Retry</button>`);
      actions.push(`<button type="button" class="lc-mini" data-lc="edit" data-id="${E(m.id)}" aria-label="Edit this message and send it again">Edit</button>`);
    }
    const tags = (m.reverted ? '<span class="lc-chip">reverted</span>' : '') + (m.meta && m.meta.steered ? '<span class="lc-chip">steered</span>' : '')
      + (m.meta && m.meta.cloud ? '<span class="lc-chip lc-cloud">cloud</span>' : '');
    const steps = mm.steps ? `<ul class="lc-steps" aria-label="Task list">${mm.steps.map((st) => `<li class="${st.done ? 'done' : ''}">${st.done ? '☑' : '☐'} ${E(st.text)}</li>`).join('')}</ul>` : '';
    const nxt = mm.next && isLastAssistant(i, m) ? `<div class="lc-next" role="group" aria-label="Next step"><p>${E(mm.next.recap)}</p>${mm.next.ask ? `<p><b>${E(mm.next.ask)}</b></p>` : ''}
${(mm.next.options || []).map((o) => `<button type="button" class="btn" data-lc="answer" data-v="${E(o)}">${E(o)}</button>`).join('')}</div>` : '';
    return `<div class="lc-msg lc-${E(role)}${m.reverted ? ' lc-reverted' : ''}" data-id="${E(m.id)}"><div class="lc-msg-head">${head}<span class="lc-meta">${E(when(m.created))}</span>${tags}</div>${steps}${nxt}
<div class="lc-msg-body">${body}</div><div class="lc-msg-actions">${actions.join('')}</div></div>`;
  }
  function isLastAssistant(i, m) {
    const p = pane(i);
    const last = [...p.messages].reverse().find((x) => x.role === 'assistant');
    return !!last && last.id === m.id && !p.running;
  }
  function approvalHtml(a) {
    const pv = a.preview || {};
    return `<div class="lc-approval" role="alert"><div class="lc-msg-head"><span class="lc-who">Needs you: ${E(a.tool)}</span></div>
<p>${E(pv.summary || a.tool)}</p>${pv.body ? `<pre class="lc-pre ${pv.kind === 'diff' ? 'lc-diff' : ''}">${E(pv.body)}</pre>` : ''}
<div class="lc-msg-actions"><button type="button" class="btn primary" data-lc="approve" data-v="once" data-id="${E(a.approval_id)}">Allow once</button>
<button type="button" class="btn" data-lc="approve" data-v="session" data-id="${E(a.approval_id)}">Allow for this session</button>
<button type="button" class="btn danger" data-lc="approve" data-v="deny" data-id="${E(a.approval_id)}">Deny</button></div></div>`;
  }
  function draftHtml(p) {
    const d = p.draft;
    if (!d) return '';
    const tools = d.tools.length ? `<div class="lc-tools">${d.tools.map((t) => `<span class="lc-tool lc-tool-${E(t.state)}">${t.state === 'running' ? '⟳' : '✓'} ${E(t.name)}</span>`).join('')}</div>` : '';
    const text = d.text ? md(d.text) : '<p class="lc-note">Thinking…</p>';
    return `<div class="lc-msg lc-assistant lc-draft"><div class="lc-msg-head"><span class="lc-who">${E(shortModel(p.session && p.session.model) || 'assistant')}</span><span class="lc-meta">answering…</span></div>${tools}<div class="lc-msg-body">${text}</div></div>`;
  }
  function emptyHtml() {
    const model = S.catalog && S.catalog.default_model;
    return `<div class="lc-empty"><h3>Local Chat</h3><p>Chat and code with local models on ModelMistress. Nothing here needs a cloud backend or the network.</p>
${S.catalogErr ? `<p class="lc-warn">${E(S.catalogErr)}</p>` : model ? `<p class="lc-note">New sessions start on <b>${E(shortModel(model))}</b>. Pick another in the top bar.</p>` : '<p class="lc-note">Load or download a model on ModelMistress to start.</p>'}
<p class="lc-note">Chat: no tools. Build: the agent edits files in the workspace (edits ask first). Plan: it proposes a plan for your approval. Review: it only reads.</p></div>`;
  }
  function renderMsgs(i) {
    const el = part(i, 'msgs');
    if (!el) return;
    const p = pane(i);
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    if (!p.sid && !p.messages.length) { el.innerHTML = emptyHtml(); return; }
    const lastUser = p.messages.map((m) => m.role).lastIndexOf('user');
    el.innerHTML = p.messages.map((m, n) => msgHtml(m, i, n === lastUser && !p.running)).join('')
      + (p.approvals || []).map(approvalHtml).join('')
      + `<div data-part="draft">${draftHtml(p)}</div>`
      + (p.err ? `<div class="lc-msg lc-error"><div class="lc-msg-body"><p>${E(p.err)}</p></div></div>` : '');
    if (atBottom || p.running) el.scrollTop = el.scrollHeight;
  }
  function renderDraft(i) {
    const el = part(i, 'draft');
    if (!el) { renderMsgs(i); return; }
    el.innerHTML = draftHtml(pane(i));
    const box = part(i, 'msgs');
    if (box) box.scrollTop = box.scrollHeight;
    renderComposerState(i);
  }
  function renderComposerState(i) {
    const p = pane(i);
    if (!p || !p.el) return;
    const ro = !!(p.session && p.session.link === 'worker');
    p.el.querySelector('.lc-input').disabled = ro;
    p.el.querySelector('[data-lc="send"]').disabled = ro;
    p.el.querySelector('[data-lc="cancel"]').classList.toggle('hidden', !p.running);
    p.el.querySelector('[data-lc="queue"]').classList.toggle('hidden', !p.running);
    const assist = (p.session || S.defaults).cloud_assist;
    const mode = (p.session || S.defaults).mode;
    p.el.querySelector('[data-lc="cloud-send"]').classList.toggle('hidden', p.running || ro || assist === 'off' || mode === 'factory');
    const sendBtn = p.el.querySelector('[data-lc="send"]');
    sendBtn.textContent = p.running ? 'Steer' : 'Send';
    sendBtn.setAttribute('aria-label', p.running ? 'Add this to the running turn (Ctrl+Enter)' : 'Send (Ctrl+Enter)');
    const note = part(i, 'cnote');
    const q = (p.queued || []).length;
    if (note) note.textContent = ro ? 'A Factory worker is read-only: talk to the session that started the run.'
      : (p.running ? 'Answering… Steer adds to this turn, Queue waits for the next. Ctrl+C stops it.' : '') + (q ? ` ${q} queued.` : '');
  }

  // ---- the session header: worktree and its actions --------------------------------------------------------------------
  function renderHead(i) {
    const el = part(i, 'head');
    if (!el) return;
    const p = pane(i);
    const s = p.session;
    if (!s) { el.innerHTML = ''; el.classList.add('hidden'); return; }
    el.classList.remove('hidden');
    const parentRow = s.parent_id ? S.sessions.find((r) => r.id === s.parent_id) : null;
    const linkTxt = { fork: 'fork of', task: 'task from', handoff: 'handed off from', worker: 'worker for' }[s.link] || 'linked to';
    const up = s.parent_id ? `<button type="button" class="lc-mini" data-lc="open" data-sid="${E(s.parent_id)}" aria-label="Open the parent session">↰ ${E(linkTxt)} ${E((parentRow && parentRow.title) || 'its parent')}</button>` : '';
    const kids = (p.children || []).length ? `<span class="lc-note">linked:</span>${p.children.map((c) =>
      `<button type="button" class="lc-mini" data-lc="open" data-sid="${E(c.id)}" aria-label="Open the linked session ${E(c.title)}"><span class="lc-dot lc-dot-${E(c.status || 'idle')}" aria-hidden="true"></span> ${E(c.title || c.id)}</button>`).join('')}` : '';
    const st = `${up}<span class="lc-dot lc-dot-${E(s.status || 'idle')}" aria-hidden="true"></span><span class="lc-note">${E(STATUS_LABEL[s.status] || s.status || 'idle')}</span>${kids}`;
    const w = p.wt;
    if (s.link === 'worker') {
      el.innerHTML = `${st}<span class="lc-note">A Factory worker: confined, read-only here.</span>${w && w.branch ? `<span class="lc-chip">⎇ ${E(w.branch)}</span>` : ''}`;
      return;
    }
    if (!w) {
      el.innerHTML = `${st}${s.workspace ? `<span class="lc-note">${E(s.workspace)}</span>
<button type="button" class="lc-mini" data-lc="wt-create" aria-label="Give this session its own git worktree">Make a worktree</button>` : '<span class="lc-note">no workspace</span>'}
${p.wtErr ? `<span class="lc-warn">${E(p.wtErr)}</span>` : ''}`;
      return;
    }
    const changed = (w.dirty || []).length;
    el.innerHTML = `${st}<span class="lc-chip" title="${E(w.path || '')}">⎇ ${E(w.branch)}</span><span class="lc-note">from ${E(w.base)}</span>
<span class="lc-note" title="commits ahead of / behind the base">↑${E(w.ahead)} ↓${E(w.behind)}</span>
<span class="lc-note">${changed ? `${changed} changed` : 'clean'}</span>
${(w.conflicts || []).length ? `<span class="lc-warn">conflicts: ${E(w.conflicts.join(', '))}</span>` : ''}
<span class="lc-top-gap"></span>
<button type="button" class="lc-mini" data-lc="wt-diff" aria-label="Show the session's changes against its base">Diff</button>
<button type="button" class="lc-mini" data-lc="wt-sync" aria-label="Merge the base branch into this session">Sync${w.behind ? ' (' + E(w.behind) + ')' : ''}</button>
<button type="button" class="lc-mini" data-lc="wt-commit" aria-label="Commit the changes"${changed ? '' : ' disabled'}>Commit</button>
<button type="button" class="lc-mini" data-lc="wt-merge" aria-label="Ask to merge this branch (push queue, needs approval)"${w.ahead ? '' : ' disabled'}>Merge…</button>
<label class="lc-note lc-keep"><input type="checkbox" data-lc="wt-keep"${w.keep ? ' checked' : ''}> keep</label>
<button type="button" class="lc-mini" data-lc="wt-discard" aria-label="Discard this worktree">Discard</button>`;
  }
  function openModal(title, body, actions) {
    const el = $('lc-modal');
    if (!el) return;
    el.innerHTML = `<div class="lc-picker-head"><b>${E(title)}</b><span class="lc-top-gap"></span>
<button type="button" class="lc-icon" data-lcx="close" aria-label="Close">✕</button></div><div class="lc-picker-body lc-pad">${body}</div>
<div class="lc-modal-foot">${(actions || []).map((a, n) => `<button type="button" class="btn${n === 0 ? ' primary' : ''}${a.danger ? ' danger' : ''}" data-lcx="${n}">${E(a.label)}</button>`).join('')}</div>`;
    el.__actions = actions || [];
    el.classList.remove('hidden');
    const first = el.querySelector('textarea,button[data-lcx="0"]');
    if (first) first.focus();
  }
  function closeModal() { const el = $('lc-modal'); if (el) { el.classList.add('hidden'); el.innerHTML = ''; } }
  function diffHtml(text) {
    return `<pre class="lc-pre lc-diff lc-diffview">${String(text || '').split('\n').map((ln) => {
      const cls = ln.startsWith('+') && !ln.startsWith('+++') ? 'lc-add' : ln.startsWith('-') && !ln.startsWith('---') ? 'lc-del' : ln.startsWith('@@') ? 'lc-hunk' : '';
      return `<span class="${cls}">${E(ln)}</span>`;
    }).join('\n')}</pre>`;
  }
  async function worktreeAction(i, a, t) {
    const p = pane(i);
    if (!p.sid) return;
    const base = '/api/local-chat/sessions/' + encodeURIComponent(p.sid) + '/worktree';
    try {
      if (a === 'wt-create') { await post(base, {}); toast('This session now has its own worktree.'); }
      else if (a === 'wt-diff') {
        const d = await api(base + '/diff');
        openModal('Changes against ' + d.base, `<p class="lc-note">${E(d.stat || 'No changes.')}</p>${d.untracked.length ? `<p class="lc-note">New files: ${E(d.untracked.join(', '))}</p>` : ''}
${d.clipped ? '<p class="lc-warn">Long diff: only the first part is shown.</p>' : ''}${diffHtml(d.diff)}`, [{ label: 'Close', run: closeModal }]);
        return;
      } else if (a === 'wt-sync') {
        const r = await post(base + '/sync', {});
        if (r.conflicts && r.conflicts.length) {
          openModal('Sync stopped on conflicts', `<p>Merging ${E(p.wt.base)} stopped on conflicts in:</p><ul>${r.conflicts.map((f) => `<li>${E(f)}</li>`).join('')}</ul>
<p class="lc-note">The merge is left in progress. Ask the agent to resolve them, or resolve them yourself and commit.</p>`,
          [{ label: 'Ask the agent to resolve them', run: () => { closeModal(); send(i, r.prompt); } }, { label: 'Later', run: closeModal }]);
        } else toast(r.merged ? 'Synced with ' + p.wt.base + '.' : 'Already up to date.');
      } else if (a === 'wt-commit') {
        const d = await api(base + '/commit-message');
        openModal('Commit the changes', `<p class="lc-note">Message (${E(d.source)}; edit it as you like):</p>
<textarea class="lc-input" id="lc-commit-msg" rows="8" aria-label="Commit message">${E(d.message)}</textarea>`,
        [{ label: 'Commit', run: async () => { const m = ($('lc-commit-msg') || {}).value || ''; try { await post(base + '/commit', { message: m }); closeModal(); toast('Committed.'); loadWorktree(i); } catch (e) { toast(errText(e), 'error'); } } },
          { label: 'Cancel', run: closeModal }]);
        return;
      } else if (a === 'wt-merge') {
        openModal('Merge this branch', `<p>This puts <b>${E(p.wt.branch)}</b> (${E(p.wt.ahead)} commit(s)) in the push queue for <b>${E(p.wt.base)}</b>.</p>
<p class="lc-note">Nothing is merged or pushed until you approve it in the push queue (Coordination → Conductor), and the pusher checks it again before it pushes.</p>`,
        [{ label: 'Put it in the push queue', run: async () => { try { const r = await post(base + '/queue-merge', {}); closeModal(); toast('Queued for approval (' + r.queued.id + ').'); } catch (e) { toast(errText(e), 'error'); } } },
          { label: 'Cancel', run: closeModal }]);
        return;
      } else if (a === 'wt-keep') { await post(base + '/keep', { keep: !!t.checked }); }
      else if (a === 'wt-discard') {
        try { await post(base + '/discard', {}); toast('Worktree removed (it had nothing unmerged).'); }
        catch (e) {
          let d = null;
          try { d = JSON.parse(e.message).detail; } catch (_) { /* not the 409 */ }
          if (!d || !d.would_lose) throw e;
          openModal('Discard work?', `<p class="lc-warn">${E(d.message)}.</p>
${d.would_lose.uncommitted.length ? `<p>Uncommitted:</p><ul>${d.would_lose.uncommitted.slice(0, 30).map((f) => `<li>${E(f)}</li>`).join('')}</ul>` : ''}
${d.would_lose.commits.length ? `<p>Commits not on the base:</p><ul>${d.would_lose.commits.slice(0, 30).map((c) => `<li>${E(c)}</li>`).join('')}</ul>` : ''}`,
          [{ label: 'Keep it', run: closeModal }, { label: 'Discard it all', danger: true, run: async () => { try { await post(base + '/discard', { confirm: true }); closeModal(); toast('Discarded.'); loadWorktree(i); } catch (x) { toast(errText(x), 'error'); } } }]);
          return;
        }
      }
    } catch (e) { toast(errText(e), 'error'); }
    loadWorktree(i);
  }
  async function serverExport(p, fmt) {
    try {
      const headers = {};
      const token = typeof getToken === 'function' ? getToken() : '';
      if (token) headers['X-Dashboard-Token'] = token;
      const base = typeof API_BASE === 'string' ? API_BASE : '';
      const res = await fetch(base + '/api/local-chat/sessions/' + encodeURIComponent(p.sid) + '/export?format=' + fmt, { headers });
      if (!res.ok) throw new Error(await res.text());
      download((p.session.title || 'session').replace(/[^\w.-]+/g, '_') + '.' + fmt, await res.text(), fmt === 'json' ? 'application/json' : 'text/markdown');
    } catch (_) {                              // offline from the server: the copy in the page still exports
      if (fmt === 'md') download((p.session.title || 'session').replace(/[^\w.-]+/g, '_') + '.md', asMarkdown(p), 'text/markdown');
      else download((p.session.title || 'session').replace(/[^\w.-]+/g, '_') + '.json', JSON.stringify({ session: p.session, messages: p.messages }, null, 1), 'application/json');
    }
  }
  async function chapterJump(i, v) {
    const p = pane(i);
    if (!v) return;
    if (v === '+') {
      const title = window.prompt('Chapter title');
      if (!title) return;
      try { await post('/api/local-chat/sessions/' + encodeURIComponent(p.sid) + '/chapters', { title }); await refreshMessages(i); } catch (e) { toast(errText(e), 'error'); }
      return;
    }
    const el = p.el.querySelector(`[data-id="${CSS && CSS.escape ? CSS.escape(v) : v}"]`);
    if (el) el.scrollIntoView({ block: 'start' });
  }
  async function factoryRun(i, planId, confirmed) {
    const p = pane(i);
    try {
      const r = await post('/api/local-chat/sessions/' + encodeURIComponent(p.sid) + '/factory/run', { plan_id: planId, confirm: !!confirmed });
      toast(`Factory run started: ${r.tasks} task(s), workers confined. They appear under this session.`);
      loadWorktree(i); loadSessions();
    } catch (e) {
      let d = null;
      try { d = JSON.parse(e.message).detail; } catch (_) { /* not the 409 */ }
      if (d && d.needs_confirmation) {
        openModal('First Factory run here', `<p>${E(d.message)}.</p><p class="lc-note">Workers are ABP's agent on your local model, each in its own worktree, with commands only inside a Windows AppContainer without network. The reviewer checks every change; the green result waits in the push queue for your approval.</p>`,
          [{ label: 'Run it', run: () => { closeModal(); factoryRun(i, planId, true); } }, { label: 'Not now', run: closeModal }]);
        return;
      }
      toast(errText(e), 'error');
    }
  }
  async function queueDecide(i, id, approve) {
    const note = approve ? '' : (window.prompt('Why reject it? (optional)') || '');
    try {
      const r = await post('/api/conductor/queue/' + encodeURIComponent(id) + '/' + (approve ? 'approve' : 'reject'), { by: 'owner (Local Chat)', note });
      toast(approve ? 'Approved. Nothing was pushed: ' + (r.push_command || 'the pusher pushes it when enabled') : 'Rejected.');
    } catch (e) { toast(errText(e), 'error'); }
    loadWorktree(i);
  }
  async function rewind(i, turnId) {
    const p = pane(i);
    if (!window.confirm('Undo this turn? The files go back to how they were before it (and every later turn). The messages stay, marked reverted, and the current state is kept as a checkpoint.')) return;
    try {
      const r = await post('/api/local-chat/sessions/' + encodeURIComponent(p.sid) + '/rewind', { turn_id: turnId });
      toast(`Rewound. ${r.messages_reverted} message(s) marked reverted.`);
      await openSession(p.sid, i);
    } catch (e) { toast(errText(e), 'error'); }
  }

  // ---- right panel: the model and the session ------------------------------------------------------------------------
  function modelDetails(row) {
    if (!row) return '<p class="lc-note">No model chosen yet.</p>';
    const md0 = row.metadata || {};
    const pl = row.plan || {};
    const spd = row.speed || {};
    const facts = [
      md0.size_label && ['Size', md0.size_label + (md0.size_gb ? ` (${md0.size_gb} GB)` : ''), 'metadata'],
      md0.quant && ['Quantization', md0.quant, 'metadata'],
      md0.experts && ['Experts', `${md0.experts_used || '?'} of ${md0.experts} active`, 'metadata'],
      md0.context_max && ['Context', `${md0.context_max.toLocaleString()} max` + (pl.n_ctx_per_slot ? ` · ${pl.n_ctx_per_slot.toLocaleString()} served per request` : ''), pl.n_ctx_per_slot ? 'planned' : 'metadata'],
      ['Runs on', (RUNS_ON[row.runs_on] || row.runs_on) + (pl.vram_mib ? ` · needs ${gb(pl.vram_mib)} of ${gb(pl.free_mib)} free` : ''), row.plan ? 'planned' : 'unknown'],
      ['Speed', spd.tokens_per_s ? fmtTps(spd.tokens_per_s) : 'not known', spd.source || 'unknown'],
    ].filter(Boolean);
    return `<h4>${E(row.name)}</h4><div class="lc-kv">${facts.map(([k, v, src]) => `<div><span>${E(k)}</span><b>${E(v)}</b>${badge(src)}</div>`).join('')}</div>
${(row.strengths || []).length ? `<p class="lc-note">Good for</p><ul class="lc-good">${row.strengths.map((s) => `<li>${E(s.text)} ${badge(s.source)}</li>`).join('')}</ul>` : ''}
${(row.warnings || []).map((w) => `<p class="lc-warn">${E(w)}</p>`).join('')}`;
  }
  function renderRight(i) {
    const el = part(i, 'right');
    if (!el) return;
    const p = pane(i);
    const s = p.session;
    const model = (s && s.model) || S.defaults.model || (S.catalog && S.catalog.default_model) || '';
    const msgs = p.messages || [];
    const tokens = msgs.reduce((n, m) => n + (m.tokens || 0), 0);
    const c = p.ctx;
    const meter = c && c.meter ? (() => {
      const pct = c.meter.limit ? Math.min(100, Math.round(100 * c.meter.used / c.meter.limit)) : null;
      return `<div class="lc-meter" role="meter" aria-label="Context used" aria-valuemin="0" aria-valuemax="${E(c.meter.limit || 0)}" aria-valuenow="${E(c.meter.used)}"><span style="width:${pct == null ? 0 : pct}%" class="${pct != null && pct > 85 ? 'lc-meter-hot' : ''}"></span></div>
<div class="lc-kv"><div><span>Context</span><b>≈${E(c.meter.used.toLocaleString())}${c.meter.limit ? ' of ' + E(c.meter.limit.toLocaleString()) : ''} tokens</b>${badge('estimated')}</div>
<div><span>Tokens</span><b>${E(c.tokens.local.toLocaleString())} local · ${E(c.tokens.cloud.toLocaleString())} cloud</b>${badge('measured')}</div></div>
${pct != null && pct > 85 ? '<p class="lc-warn">Near the model\'s context: older turns are summarized before the next turn.</p>' : ''}
<button type="button" class="lc-mini" data-lc="compact" aria-label="Summarize the older turns now">Compact now</button>`;
    })() : '<p class="lc-note">A session starts with your first message.</p>';
    const agent = c ? `<div class="lc-kv"><div><span>Autonomy</span><b>${E(c.autonomy.level)} ${E(c.autonomy.label)}</b></div><div><span>Means</span><b>${E(c.autonomy.summary)}</b></div>
<div><span>Tools</span><b>${c.tools === 'all' ? 'the agent\'s full set' : c.tools === 'none' ? 'none (chat)' : E(c.tools.length) + ' read-only'}</b></div>
<div><span>Cloud</span><b>${E(c.cloud.assist)}${c.cloud.governor ? ' · governor ' + E(c.cloud.governor) : ''}</b></div></div>
<p class="lc-note">Destructive commands always ask; pushing only happens through the push queue.</p>` : '';
    const fb = p.factory;
    const board = fb && (fb.workers.length || fb.queue.length || fb.running) ? `<section class="lc-card"><h3>Factory</h3>${fb.running ? '<p class="lc-note">A run is going. <button type="button" class="lc-mini" data-lc="factory-stop">Stop it</button></p>' : ''}
${fb.workers.map((w) => `<div class="lc-kv"><div><button type="button" class="lc-mini" data-lc="open" data-sid="${E(w.id)}" aria-label="Open the worker session ${E(w.title)}"><span class="lc-dot lc-dot-${E(w.status || 'idle')}" aria-hidden="true"></span> ${E(w.title)}</button></div></div>`).join('')}
${fb.queue.map((q) => `<p class="lc-note">push queue ${E(q.id)}: <b>${E(q.status)}</b> (${E(q.branch)})</p>`).join('')}</section>` : '';
    const sd = S.side || {};
    const esc = sd.escalations && sd.escalations.count ? `<p class="lc-warn">${E(sd.escalations.count)} escalation(s) waiting for a decision.</p>` : '';
    const dg = sd.digest && sd.digest.text ? `<pre class="lc-pre lc-digest">${E(String(sd.digest.text).slice(0, 1200))}</pre>` : '';
    const wd = sd.watchdog ? `<p class="lc-note">WatchDog: ${E(sd.watchdog.total || 0)} incident(s)${sd.watchdog.escapes ? `, <b class="lc-warn">${E(sd.watchdog.escapes)} escape(s)</b>` : ''}. Containment is only proposed, never run.</p>` : '';
    el.innerHTML = `<section class="lc-card"><h3>Context</h3>${meter}</section>${c ? `<section class="lc-card"><h3>Agent</h3>${agent}</section>` : ''}${board}
<section class="lc-card"><h3>Model</h3>${modelDetails(modelRow(model))}</section>
<section class="lc-card"><h3>Session</h3>${s ? `<div class="lc-kv"><div><span>Title</span><b>${E(s.title || 'untitled')}</b></div>
<div><span>Messages</span><b>${msgs.length}</b></div><div><span>Tokens</span><b>${tokens.toLocaleString()}</b></div>
<div><span>Workspace</span><b>${E(s.workspace || 'none')}</b></div></div>` : '<p class="lc-note">A session starts with your first message.</p>'}</section>
${esc || dg || wd ? `<section class="lc-card"><h3>Manager &amp; WatchDog</h3>${esc}${dg}${wd}</section>` : ''}`;
  }

  // ---- the sidebar ---------------------------------------------------------------------------------------------------
  function sectionOf(s) {
    if (s.parent_id && S.sessions.some((r) => r.id === s.parent_id)) return 'child';      // drawn under its parent
    if (S.showArchived) return 'archived';
    if (s.pinned) return 'pinned';
    if (s.status === 'needs_you') return 'needs';
    if (s.status === 'working') return 'working';
    if (s.group_id && S.groups.some((g) => g.id === s.group_id)) return 'g:' + s.group_id;
    return new Date(s.updated * 1000).toDateString() === new Date().toDateString() ? 'today' : 'earlier';
  }
  function sessionRows(s, open, depth) {
    const kids = depth > 3 ? [] : S.sessions.filter((r) => r.parent_id === s.id);
    return sessionRow(s, open, depth) + kids.map((k) => sessionRows(k, open, depth + 1)).join('');
  }
  function sessionRow(s, open, depth) {
    const wt = s.worktree && s.worktree.branch ? `<span class="lc-chip" title="worktree branch">⎇ ${E(s.worktree.branch.split('/').pop())}</span>` : '';
    const ws = s.workspace ? `<span class="lc-chip" title="${E(s.workspace)}">${E(s.workspace.split(/[\\/]/).filter(Boolean).pop() || s.workspace)}</span>` : '';
    const link = s.link ? `<span class="lc-chip" title="linked session">${E({ fork: 'fork', task: 'task', handoff: 'hand-off', worker: 'worker' }[s.link] || s.link)}</span>` : '';
    return `<div class="lc-row${open.has(s.id) ? ' on' : ''}${s.unread ? ' lc-unread' : ''}${depth ? ' lc-child' : ''}" role="listitem" data-sid="${E(s.id)}"${depth ? ` style="margin-left:${Math.min(depth, 3) * 14}px"` : ''}>
<button type="button" class="lc-row-main" data-lc="open" data-sid="${E(s.id)}" aria-label="Open session ${E(s.title || 'untitled')}, ${E(STATUS_LABEL[s.status] || 'idle')}${s.unread ? ', unread' : ''}">
<span class="lc-row-title"><span class="lc-dot lc-dot-${E(s.status || 'idle')}" aria-hidden="true"></span>${s.pinned ? '📌 ' : ''}${E(s.title || 'Untitled session')}</span>
<span class="lc-row-sub">${link}<span class="lc-chip">${E(shortModel(s.model) || 'no model')}</span>${ws}${wt}<span>${E(when(s.updated))}</span></span></button>
<button type="button" class="lc-icon lc-row-menu" data-lc="menu" data-sid="${E(s.id)}" aria-label="More for ${E(s.title || 'this session')}" title="More">⋯</button></div>`;
  }
  function renderFilters() {
    const el = $('lc-filters');
    if (!el) return;
    const uniq = (k) => [...new Set(S.sessions.map((s) => s[k]).filter(Boolean))].sort();
    const sel = (key, label, opts, fmt) => `<select class="lc-sel" data-lcf="${key}" aria-label="Filter by ${label}"><option value="">${E(label)}: all</option>${opts.map((o) =>
      `<option value="${E(o)}"${S.filter[key] === o ? ' selected' : ''}>${E(fmt ? fmt(o) : o)}</option>`).join('')}</select>`;
    el.innerHTML = sel('status', 'status', Object.keys(STATUS_LABEL), (o) => STATUS_LABEL[o])
      + sel('model', 'model', uniq('model'), shortModel)
      + sel('workspace', 'workspace', uniq('workspace'), (o) => o.split(/[\\/]/).filter(Boolean).pop() || o);
  }
  function renderSide() {
    const el = $('lc-list');
    if (!el) return;
    renderFilters();
    const open = new Set(S.panes.map((p) => p.sid));
    const f = S.filter;
    const rows = S.sessions.filter((s) => (!f.status || s.status === f.status) && (!f.model || s.model === f.model) && (!f.workspace || s.workspace === f.workspace));
    if (!rows.length) { el.innerHTML = `<p class="lc-note lc-pad">${S.query || f.status || f.model || f.workspace ? 'Nothing matches.' : S.showArchived ? 'No archived sessions.' : 'No sessions yet.'}</p>`; return; }
    const sections = [['pinned', 'Pinned'], ['needs', 'Needs you'], ['working', 'Working now'],
      ...S.groups.map((g) => ['g:' + g.id, g.name, g.id]), ['today', 'Today'], ['earlier', 'Earlier'], ['archived', 'Archived']];
    el.innerHTML = sections.map(([key, label, gid]) => {
      const inS = rows.filter((s) => sectionOf(s) === key);
      if (!inS.length && !gid) return '';
      const closed = !!S.collapsed[key];
      const unread = inS.filter((s) => s.unread).length;
      return `<div class="lc-sec"><div class="lc-sec-head"><button type="button" class="lc-sec-btn" data-lc="collapse" data-key="${E(key)}" aria-expanded="${!closed}">${closed ? '▸' : '▾'} ${E(label)} <span class="lc-note">${inS.length}${unread ? ' · ' + unread + ' unread' : ''}</span></button>
${gid ? `<button type="button" class="lc-icon" data-lc="group-menu" data-gid="${E(gid)}" aria-label="Rename or remove the group ${E(label)}" title="Group">⋯</button>` : ''}</div>
${closed ? '' : inS.map((s) => sessionRows(s, open, 0)).join('') || '<p class="lc-note lc-pad">Empty: move a session here from its menu.</p>'}</div>`;
    }).join('');
  }
  function sessionMenu(sid, x, y) {
    const s = S.sessions.find((r) => r.id === sid);
    if (!s) return;
    closeMenu();
    const m = document.createElement('div');
    m.className = 'lc-menu';
    m.id = 'lc-menu';
    m.setAttribute('role', 'menu');
    const items = [['rename', 'Rename'], ['pin', s.pinned ? 'Unpin' : 'Pin'], ['split', 'Open in split view'], ['fork', 'Fork (its own worktree)'],
      ['task', 'New task from here…'], ['handoff', 'Hand off the rest…'], ['export-md', 'Export as Markdown'],
      ['export-json', 'Export as JSON'], ['archive', s.archived ? 'Restore from the archive' : 'Archive'], ['delete', 'Delete…']];
    const groups = S.groups.filter((g) => g.id !== s.group_id).map((g) => [`group:${g.id}`, `Move to ${g.name}`]);
    if (s.group_id) groups.push(['group:', 'Remove from its group']);
    groups.push(['group-new', 'Move to a new group…']);
    m.innerHTML = [...items.slice(0, 6), ...groups, ...items.slice(6)].map(([a, label]) => `<button type="button" role="menuitem" data-lcm="${E(a)}" data-sid="${E(sid)}">${E(label)}</button>`).join('');
    m.style.left = Math.min(x, window.innerWidth - 220) + 'px';
    m.style.top = Math.min(y, window.innerHeight - 260) + 'px';
    document.body.appendChild(m);
    const first = m.querySelector('button');
    if (first) first.focus();
  }
  function closeMenu() { const m = $('lc-menu'); if (m) m.remove(); }
  async function menuAction(action, sid) {
    closeMenu();
    const s = S.sessions.find((r) => r.id === sid) || {};
    const url = '/api/local-chat/sessions/' + encodeURIComponent(sid);
    try {
      if (action === 'rename') {
        const t = window.prompt('Rename the session', s.title || '');
        if (t != null) { await patch(url, { title: t }); }
      } else if (action === 'pin') await patch(url, { pinned: !s.pinned });
      else if (action === 'archive') await patch(url, { archived: !s.archived });
      else if (action === 'split') { openSplit(sid); return; }
      else if (action.startsWith('group:')) await patch(url, { group_id: action.slice(6) });
      else if (action === 'fork' || action === 'task' || action === 'handoff') {
        let body = {};
        if (action === 'task') { const brief = window.prompt('The task for the new session (its brief)'); if (!brief) return; body = { brief }; }
        if (action === 'handoff') {
          const plan = window.prompt('What is left to do? The new session continues from here with this plan.');
          if (!plan) return;
          body = { plan, stop: window.confirm('Stop this session\'s running turn first? (Cancel keeps it running.)') };
        }
        const r = await post(url + '/' + action, body);
        toast(action === 'fork' ? 'Forked.' : action === 'task' ? 'Task started.' : 'Handed off.');
        if (r.not_carried && r.not_carried.length) toast(r.not_carried.length + ' uncommitted change(s) were not carried into the fork: commit them first to include them.');
        await loadSessions();
        openSplit(r.session.id);
        return;
      }
      else if (action === 'group-new') {
        const name = window.prompt('Name of the new group');
        if (!name) return;
        const g = await post('/api/local-chat/groups', { name });
        await patch(url, { group_id: g.id });
      }
      else if (action === 'export-md' || action === 'export-json') {
        const d = await api(url);
        const name = (d.session.title || 'session').replace(/[^\w.-]+/g, '_');
        if (action === 'export-json') download(name + '.json', JSON.stringify(d, null, 1), 'application/json');
        else download(name + '.md', asMarkdown({ session: d.session, messages: d.messages || [] }), 'text/markdown');
        return;
      } else if (action === 'delete') {
        if (!window.confirm('Delete "' + (s.title || 'this session') + '" and all its messages? This cannot be undone. Archive keeps it instead.')) return;
        try { await api(url, { method: 'DELETE' }); }
        catch (e) {
          let d = null;
          try { d = JSON.parse(e.message).detail; } catch (_) { /* not the 409 */ }
          if (!d || !d.would_lose) throw e;
          const lost = d.would_lose.uncommitted.length + ' uncommitted change(s) and ' + d.would_lose.commits.length + ' commit(s)';
          if (!window.confirm('Its worktree holds unmerged work: ' + lost + ' would be lost. Delete anyway?')) return;
          await api(url + '?confirm=true', { method: 'DELETE' });
        }
        S.panes.forEach((p, i) => { if (p.sid === sid) openSession('', i); });
      }
    } catch (e) { toast(errText(e), 'error'); }
    S.panes.forEach((p, i) => { if (p.sid === sid && action !== 'delete') openSession(sid, i); });
    loadSessions();
  }
  async function groupMenu(gid) {
    const g = S.groups.find((x) => x.id === gid);
    if (!g) return;
    const name = window.prompt(`Rename the group "${g.name}" (leave it empty to remove the group; its sessions stay)`, g.name);
    if (name == null) return;
    try {
      if (name.trim()) await patch('/api/local-chat/groups/' + encodeURIComponent(gid), { name });
      else await api('/api/local-chat/groups/' + encodeURIComponent(gid), { method: 'DELETE' });
    } catch (e) { toast(errText(e), 'error'); }
    loadSessions();
  }

  // ---- split view ----------------------------------------------------------------------------------------------------
  function newPane() { return { sid: '', session: null, messages: [], running: false, draft: null, approvals: [], err: '', el: null }; }
  function openSplit(sid) {
    if (S.panes.length < 2) S.panes.push(newPane());
    S.active = S.panes.length - 1;
    render();
    openSession(sid, S.active);
  }
  function closePane(i) {
    if (S.panes.length < 2) return;
    S.panes.splice(i, 1);
    S.active = 0;
    const host = $('lc-panes');
    if (host) host.innerHTML = '';
    render();
  }

  // ---- the model picker ----------------------------------------------------------------------------------------------
  function openPicker(i) {
    S.pickerFor = i;
    renderPicker();
    if (!S.catalog) loadCatalog(false);
  }
  function closePicker() { S.pickerFor = -1; const el = $('lc-picker'); if (el) el.classList.add('hidden'); }
  function pickerRow(r, current) {
    const md0 = r.metadata || {};
    const pl = r.plan || {};
    const spd = r.speed || {};
    const bits = [md0.size_label, md0.quant, md0.experts ? `MoE ${md0.experts_used || '?'}/${md0.experts}` : '',
      md0.context_max ? `${Math.round(md0.context_max / 1024)}k ctx` + (pl.n_ctx_per_slot ? ` (${Math.round(pl.n_ctx_per_slot / 1024)}k served)` : '') : ''].filter(Boolean);
    const runs = `${E(RUNS_ON[r.runs_on] || r.runs_on)}${pl.vram_mib ? ` · ${E(gb(pl.vram_mib))} of ${E(gb(pl.free_mib))} free` : ''} ${badge(r.plan ? 'planned' : 'unknown')}`;
    const speedTxt = spd.tokens_per_s ? `${E(fmtTps(spd.tokens_per_s))} ${badge(spd.source)}` : `speed not known ${badge('unknown')}`;
    const good = (r.strengths || []).slice(0, 3).map((s) => `${E(s.text)} ${badge(s.source)}`).join(' · ');
    return `<div class="lc-prow${r.id === current ? ' on' : ''}${r.kind !== 'chat' ? ' lc-dim' : ''}">
<div class="lc-prow-main"><b>${E(r.name)}</b> <span class="lc-note">${E(bits.join(' · '))}</span>
<div class="lc-prow-line">Runs on: ${runs} · ${speedTxt}</div>${good ? `<div class="lc-prow-line">Good for: ${good}</div>` : ''}
${(r.warnings || []).map((w) => `<div class="lc-warn">${E(w)}</div>`).join('')}</div>
<div class="lc-prow-act">${r.kind === 'chat' && r.runs_on !== 'none' ? `<button type="button" class="btn primary" data-lcp="use" data-id="${E(r.id)}">${r.id === current ? 'In use' : 'Use'}</button>` : ''}
${r.loaded ? `<button type="button" class="btn" data-lcp="unload" data-id="${E(r.id)}">Unload</button>` : (r.runs_on === 'gpu' || r.runs_on === 'cpu') && r.group !== 'other' ? `<button type="button" class="btn" data-lcp="load" data-id="${E(r.id)}">Load</button>` : ''}
${r.group !== 'other' && r.runs_on !== 'none' ? `<button type="button" class="btn" data-lcp="bench" data-id="${E(r.id)}" title="Measure its speed on this machine">Benchmark</button>` : ''}</div></div>`;
  }
  function renderPicker() {
    const el = $('lc-picker');
    if (!el) return;
    if (S.pickerFor < 0) { el.classList.add('hidden'); return; }
    const p = pane(S.pickerFor);
    const current = (p && p.session && p.session.model) || S.defaults.model || (S.catalog && S.catalog.default_model) || '';
    const c = S.catalog;
    const filter = (S.pickerQuery || '').toLowerCase();
    let body;
    if (!c) body = `<p class="lc-note lc-pad">${S.catalogErr ? E(S.catalogErr) : 'Asking ModelMistress…'}</p>`;
    else {
      const rows = c.models.filter((r) => !filter || (r.id + ' ' + r.name).toLowerCase().includes(filter));
      body = (c.groups || []).map((g) => {
        const inG = rows.filter((r) => r.group === g);
        return inG.length ? `<div class="lc-pgroup"><h4>${E(GROUP_LABEL[g] || g)}</h4>${inG.map((r) => pickerRow(r, current)).join('')}</div>` : '';
      }).join('') || '<p class="lc-note lc-pad">No model matches.</p>';
    }
    el.innerHTML = `<div class="lc-picker-head"><b>Models on ModelMistress</b>
<input type="search" id="lc-picker-q" aria-label="Filter models" placeholder="Filter" value="${E(S.pickerQuery || '')}">
<button type="button" class="btn" data-lcp="refresh">Ask the hub again</button><a class="btn" href="#modules" data-lcp="more">Get more models</a>
<button type="button" class="lc-icon" data-lcp="close" aria-label="Close the model picker">✕</button></div>
${c ? `<p class="lc-note lc-pad">Device: ${E(c.device)} · ${E(c.ram_free_gb)} GB RAM free · hub ${E((c.hub || {}).version || '?')}. Every figure says where it came from.</p>` : ''}
<div class="lc-picker-body">${body}</div>`;
    el.classList.remove('hidden');
  }
  async function pickerAction(action, id) {
    const i = S.pickerFor;
    try {
      if (action === 'use') { S.defaults.model = id; if (pane(i) && pane(i).sid) await setField(i, 'model', id); else renderTop(i); closePicker(); renderRight(i); return; }
      if (action === 'refresh') { S.catalog = null; renderPicker(); await loadCatalog(true); return; }
      if (action === 'close' || action === 'more') { closePicker(); return; }
      if (action === 'load' || action === 'unload') {
        toast(`${action === 'load' ? 'Loading' : 'Unloading'} ${shortModel(id)}…`);
        await post('/api/modelmistress/' + action, { model: id });
      }
      if (action === 'bench') {
        toast(`Measuring ${shortModel(id)} on this machine. This can take a few minutes.`);
        const r = await post('/api/local-chat/models/benchmark', { model: id });
        toast(r.tokens_per_s ? `${shortModel(id)}: ${fmtTps(r.tokens_per_s)} measured` : 'The benchmark gave no speed.');
      }
      await loadCatalog(true);
    } catch (e) { toast(errText(e), 'error'); }
  }

  // ---- hint and status strip -----------------------------------------------------------------------------------------
  function renderHint() {
    const el = $('lc-hint');
    if (!el) return;
    const show = S.setup && S.setup.ready === false && store.get('hint-dismissed') !== '1';
    el.classList.toggle('hidden', !show);
    if (show) {
      el.innerHTML = `<span>No cloud backend is connected. That is optional: Local Chat runs on local models without one.</span>
<button type="button" class="btn" data-lc="setup">Connect a cloud backend</button><button type="button" class="lc-icon" data-lc="hint-close" aria-label="Dismiss this hint">✕</button>`;
    }
  }
  function renderStatus() {
    const el = $('lc-status');
    if (!el) return;
    const st = S.status || {};
    const hub = st.hub || {};
    const loaded = ((S.catalog && S.catalog.models) || []).filter((r) => r.loaded);
    const p = pane();
    const last = p && p.messages ? [...p.messages].reverse().find((m) => m.role === 'assistant' && m.tokens_per_s) : null;
    const dot = st.error ? 'bad' : !hub.up ? 'bad' : hub.degraded ? 'warn' : 'ok';
    const hubTxt = st.error ? 'ABP did not answer' : !st.hub ? 'Checking ModelMistress…' : !hub.up ? 'ModelMistress is not running' : 'ModelMistress ' + (hub.version || '') + (hub.degraded ? ' (degraded: ' + hub.degraded + ')' : '');
    el.innerHTML = `<span class="lc-dot lc-dot-${dot}" aria-hidden="true"></span><span>${E(hubTxt)}</span>
<span>Loaded: ${loaded.length ? loaded.map((r) => E(shortModel(r.id))).join(', ') : 'none'}</span>
${S.catalog ? `<span>${E(S.catalog.device)}</span>` : ''}
${last ? `<span>Last reply ${E(fmtTps(last.tokens_per_s))} ${badge('measured')}</span>` : ''}
${(st.running || []).length ? `<span>${st.running.length} answering</span>` : ''}`;
  }

  // ---- events --------------------------------------------------------------------------------------------------------
  function paneOf(el) { const p = el && el.closest ? el.closest('.lc-pane') : null; return p ? Number(p.dataset.pane) : S.active; }
  let capTimer = null;
  function showSlash(i) {
    const p = pane(i);
    const box = p.el.querySelector('.lc-input');
    const el = part(i, 'slash');
    const v = box.value;
    if (!v.startsWith('/') || v.includes('\n')) { el.classList.add('hidden'); return; }
    const hits = SLASH.filter(([c]) => c.startsWith(v.split(' ')[0]));
    const draw = (caps) => {
      el.innerHTML = hits.map(([c, d]) => `<button type="button" role="option" data-lc="slash" data-v="${E(c.split(' ')[0])}"><b>${E(c)}</b> <span class="lc-note">${E(d)}</span></button>`).join('')
        + (caps || []).map((c) => `<button type="button" role="option" data-lc="cap" data-where="${E(c.where)}"><span class="lc-chip">${E(c.kind)}</span> <b>${E(c.label)}</b> <span class="lc-note">${E(c.desc)}${c.network ? ' · needs the internet' : ''}${c.cloud ? ' · cloud' : ''}</span></button>`).join('');
      el.classList.toggle('hidden', !el.innerHTML);
    };
    draw(null);
    clearTimeout(capTimer);
    const q = v.slice(1).trim();
    if (q.length < 2 || hits.some(([c]) => c.split(' ')[0] === v.split(' ')[0])) return;
    capTimer = setTimeout(() => {
      api('/api/capabilities?limit=8&kind=page&kind=setting&kind=action&kind=command&kind=tool&kind=module&q=' + encodeURIComponent(q))
        .then((d) => { if (box.value === v) draw(d.results); }, () => {});
    }, 200);
  }
  function goCapability(i, where) {
    hideSlash(i);
    const box = pane(i).el.querySelector('.lc-input');
    box.value = '';
    if (where.startsWith('#')) { const a = document.querySelector(`nav.sidenav a[href="${where}"]`); if (a) a.click(); else location.hash = where; return; }
    if (where.startsWith('action:')) { if (window.abpActions) window.abpActions.run(where.slice(7)); return; }
    if (where.startsWith('settings/')) { location.hash = '#' + where; return; }
    if (where.startsWith('tool:')) { box.value = `Use the ${where.slice(5)} tool to `; box.focus(); return; }
    box.value = where; box.focus();
  }
  function hideSlash(i) { const el = part(i, 'slash'); if (el) el.classList.add('hidden'); }

  function wire() {
    const root = $('lc-root');
    if (!root || root.__lcWired) return;
    root.__lcWired = true;
    root.addEventListener('click', (ev) => {
      const t = ev.target.closest('[data-lc],[data-lcp]');
      if (!t) return;
      const i = paneOf(t);
      if (t.closest('.lc-pane')) S.active = i;
      if (t.dataset.lcp) { ev.preventDefault(); pickerAction(t.dataset.lcp, t.dataset.id); return; }
      if (t.dataset.lc && t.dataset.lc.startsWith('wt-') && t.dataset.lc !== 'wt-keep') { worktreeAction(i, t.dataset.lc, t); return; }
      const a = t.dataset.lc;
      const p = pane(i);
      if (a === 'new') newSession(S.active);
      else if (a === 'open') openSession(t.dataset.sid, S.active);
      else if (a === 'menu') { const r = t.getBoundingClientRect(); sessionMenu(t.dataset.sid, r.left, r.bottom); }
      else if (a === 'send') send(i);
      else if (a === 'queue') steer(i, null, 'queue');
      else if (a === 'cloud-send') send(i, null, true);
      else if (a === 'factory-run') factoryRun(i, t.dataset.plan, false);
      else if (a === 'factory-stop') post('/api/local-chat/sessions/' + encodeURIComponent(p.sid) + '/factory/stop').then(() => loadWorktree(i), (e) => toast(errText(e), 'error'));
      else if (a === 'queue-approve' || a === 'queue-reject') queueDecide(i, t.dataset.id, a === 'queue-approve');
      else if (a === 'rewind') rewind(i, t.dataset.turn);
      else if (a === 'collapse') { S.collapsed[t.dataset.key] = !S.collapsed[t.dataset.key]; store.set('collapsed', JSON.stringify(S.collapsed)); renderSide(); }
      else if (a === 'group-menu') groupMenu(t.dataset.gid);
      else if (a === 'new-group') { const name = window.prompt('Name of the new group'); if (name) post('/api/local-chat/groups', { name }).then(loadSessions, (e) => toast(errText(e), 'error')); }
      else if (a === 'notify') {
        if (typeof Notification === 'undefined') { toast('This browser cannot show notifications.', 'error'); return; }
        const on = store.get('notify') === '1';
        if (on) { store.set('notify', '0'); toast('Desktop notifications off.'); return; }
        Notification.requestPermission().then((perm) => { store.set('notify', perm === 'granted' ? '1' : '0'); toast(perm === 'granted' ? 'You will be notified when a session needs you or finishes.' : 'Notifications were not allowed.'); });
      } else if (a === 'prune') {
        post('/api/local-chat/worktrees/prune', {}).then((r) => toast(`Removed ${r.removed.length} orphan worktree(s); kept ${r.kept.length}.`), (e) => toast(errText(e), 'error'));
      }
      else if (a === 'cancel') cancel(i);
      else if (a === 'picker') openPicker(i);
      else if (a === 'mode') setField(i, 'mode', t.dataset.v);
      else if (a === 'right') {
        const lc = root.querySelector('.lc');
        if (window.innerWidth <= 1250) lc.classList.toggle('lc-showright');      // narrow: hidden unless asked for
        else { S.rightOpen = !S.rightOpen; store.set('right', S.rightOpen ? '1' : '0'); lc.classList.toggle('lc-noright', !S.rightOpen); }
        S.panes.forEach((_, n) => renderTop(n));
      }
      else if ((a === 'export' || a === 'export-json') && p.sid) serverExport(p, a === 'export' ? 'md' : 'json');
      else if (a === 'answer') send(i, t.dataset.v);
      else if (a === 'compact') post('/api/local-chat/sessions/' + encodeURIComponent(p.sid) + '/compact').then((r) => { toast(r.compacted ? `Compacted ${r.messages} message(s).` : r.why); refreshMessages(i); loadWorktree(i); }, (e) => toast(errText(e), 'error'));
      else if (a === 'close-pane') closePane(i);
      else if (a === 'approve') decide(i, t.dataset.id, t.dataset.v);
      else if (a === 'copy-code') { const code = t.closest('.lc-code').querySelector('code').textContent; copyOut(code); }
      else if (a === 'copy-msg') { const m = p.messages.find((x) => String(x.id) === t.dataset.id); if (m) copyOut(m.text); }
      else if (a === 'retry') { const m = p.messages.find((x) => String(x.id) === t.dataset.id); if (m) send(i, m.text); }
      else if (a === 'edit') { const m = p.messages.find((x) => String(x.id) === t.dataset.id); if (m) { const box = p.el.querySelector('.lc-input'); box.value = m.text; box.focus(); } }
      else if (a === 'cap') goCapability(i, t.dataset.where);
      else if (a === 'slash') { const box = p.el.querySelector('.lc-input'); box.value = t.dataset.v + ' '; box.focus(); showSlash(i); }
      else if (a === 'hint-close') { store.set('hint-dismissed', '1'); renderHint(); }
      else if (a === 'setup') { const b = $('btn-open-wizard'); if (b) b.click(); }
    });
    root.addEventListener('change', (ev) => {
      const t = ev.target;
      const i = paneOf(t);
      if (t.id === 'lc-archived') { S.showArchived = t.checked; loadSessions(); return; }
      if (t.dataset.lcf) { S.filter[t.dataset.lcf] = t.value; renderSide(); return; }
      if (t.dataset.lc === 'wt-keep') { worktreeAction(i, 'wt-keep', t); return; }
      if (t.dataset.lc === 'chapter-jump') { chapterJump(i, t.value); t.value = ''; return; }
      if (t.dataset.lc === 'autonomy') setField(i, 'autonomy', Number(t.value));
      else if (t.dataset.lc === 'cloud') setField(i, 'cloud_assist', t.value);
      else if (t.dataset.lc === 'workspace') setField(i, 'workspace', t.value.trim());
    });
    let qTimer = null;
    root.addEventListener('input', (ev) => {
      const t = ev.target;
      if (t.id === 'lc-search') { clearTimeout(qTimer); qTimer = setTimeout(() => { S.query = t.value.trim(); loadSessions(); }, 250); return; }
      if (t.id === 'lc-picker-q') { S.pickerQuery = t.value; const pos = t.selectionStart; renderPicker(); const n = $('lc-picker-q'); if (n) { n.focus(); n.setSelectionRange(pos, pos); } return; }
      if (t.classList.contains('lc-input')) showSlash(paneOf(t));
    });
    root.addEventListener('keydown', (ev) => {
      const t = ev.target;
      if (ev.key === 'Escape') { if (S.pickerFor >= 0) { closePicker(); ev.preventDefault(); } closeModal(); closeMenu(); return; }
      if (!t.classList || !t.classList.contains('lc-input')) return;
      const i = paneOf(t);
      S.active = i;
      const mod = ev.ctrlKey || ev.metaKey;
      if (ev.key === 'Enter' && mod) { ev.preventDefault(); ev.stopPropagation(); send(i); return; }
      if (ev.key === 'Tab' && !ev.shiftKey && !mod && !ev.altKey) {
        ev.preventDefault();
        const cur = (pane(i).session || S.defaults).mode;
        const n = MODES.findIndex((m) => m[0] === cur);
        setField(i, 'mode', MODES[(n + 1) % MODES.length][0]);
        return;
      }
      if ((ev.key === 'c' || ev.key === 'C') && ev.ctrlKey && pane(i).running && t.selectionStart === t.selectionEnd) { ev.preventDefault(); cancel(i); }
    });
    root.addEventListener('contextmenu', (ev) => {
      const row = ev.target.closest('.lc-row');
      if (!row) return;
      ev.preventDefault();
      sessionMenu(row.dataset.sid, ev.clientX, ev.clientY);
    });
    root.addEventListener('dragover', (ev) => { if (ev.target.closest && ev.target.closest('.lc-composer')) ev.preventDefault(); });
    root.addEventListener('drop', (ev) => {
      const comp = ev.target.closest && ev.target.closest('.lc-composer');
      if (!comp || !ev.dataTransfer || !ev.dataTransfer.files.length) return;
      ev.preventDefault();
      addFiles(paneOf(comp), ev.dataTransfer.files);
    });
    root.addEventListener('paste', (ev) => {
      const t = ev.target;
      if (!t.classList || !t.classList.contains('lc-input') || !ev.clipboardData || !ev.clipboardData.files.length) return;
      ev.preventDefault();
      addFiles(paneOf(t), ev.clipboardData.files);
    });
    root.addEventListener('click', (ev) => {
      const x = ev.target.closest && ev.target.closest('[data-lcx]');
      if (!x) return;
      const modal = $('lc-modal');
      if (x.dataset.lcx === 'close') { closeModal(); return; }
      const act = (modal.__actions || [])[Number(x.dataset.lcx)];
      if (act && act.run) act.run();
    });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) S.panes.forEach((_, n) => markRead(n)); });
    document.addEventListener('click', (ev) => {
      const m = ev.target.closest && ev.target.closest('[data-lcm]');
      if (m) { menuAction(m.dataset.lcm, m.dataset.sid); return; }
      if ($('lc-menu') && !ev.target.closest('.lc-menu')) closeMenu();
    });
  }
  function copyOut(text) {
    if (typeof window.copyText === 'function') { window.copyText(text, 'Text'); return; }
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast('Copied.'), () => toast('Could not copy.', 'error'));
  }
  function addFiles(i, files) {
    const box = pane(i).el.querySelector('.lc-input');
    Array.from(files).forEach((f) => {
      if (f.type.startsWith('image/')) { toast(`${f.name}: images are not sent yet (text only in this build).`, 'error'); return; }
      if (f.size > 200 * 1024) { toast(`${f.name} is over 200 KB; attach a smaller file or point the agent at it in the workspace.`, 'error'); return; }
      const r = new FileReader();
      r.onload = () => { box.value += `${box.value ? '\n\n' : ''}${f.name}:\n\`\`\`\n${String(r.result).replace(/```/g, '`​``')}\n\`\`\`\n`; box.focus(); };
      r.readAsText(f);
    });
  }

  // ---- lifecycle: poll only while the screen is visible --------------------------------------------------------------
  function tick() {
    if (!S.visible) return;
    loadStatus();
    S.panes.forEach((p, i) => { if (p.running) refreshMessages(i); });
  }
  function setVisible(on) {
    if (on === S.visible) return;
    S.visible = on;
    if (on) {
      tick();
      if (!S.catalog) loadCatalog(false);
      loadSessions();
      if (!S.timer) S.timer = setInterval(tick, 5000);
    } else if (S.timer) { clearInterval(S.timer); S.timer = null; }
  }
  function init() {
    const root = $('lc-root');
    if (!root) return;
    S.panes = [newPane()];
    render();
    wire();
    loadSetup();
    const last = store.get('last');
    loadSessions().then(() => { if (last && S.sessions.some((s) => s.id === last)) openSession(last, 0); });
    const sec = root.closest('section') || root;
    if ('IntersectionObserver' in window) new IntersectionObserver((es) => setVisible(es.some((e) => e.isIntersecting))).observe(sec);
    else setVisible(true);
  }
  ABP.localChat = { open: (sid) => openSession(sid, S.active), newSession: () => newSession(S.active), split: openSplit, state: S, sectionOf };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
