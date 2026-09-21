// The bottom Terminal + Activity panel. Loaded after main.js (uses its
// getToken()/api()/esc()/IS_TAURI/API_BASE globals) and after xterm.js's
// vendor bundle. Two terminal front-ends share this one panel:
//   - Desktop (IS_TAURI): a real ConPTY-backed shell via xterm.js, wired
//     to desktop-app/src-tauri/src/terminal.rs's terminal_start/write/
//     resize/stop commands.
//   - Browser dashboard (this same file, loaded by dashboard.html too):
//     no pty exists there at all — a plain input+scrollback view that
//     only runs ABP's own slash commands through /api/terminal/exec.
// Either way, a line starting with "/" is always an ABP command (routed
// through the SAME /api/terminal/exec the browser view uses even on
// desktop) — see interceptSlashLine() below for why that has to be
// decided character-by-character rather than after the fact.

(function () {
  const panel = document.getElementById('term-panel');
  if (!panel) return; // this page doesn't have the panel (shouldn't happen, but never crash main.js's own boot over it)

  const header = document.getElementById('term-header');
  const resizeHandle = document.getElementById('term-resize-handle');
  const btnMinimize = document.getElementById('term-minimize');
  const btnMaximize = document.getElementById('term-maximize');
  const tabTerminal = document.getElementById('term-tab-terminal');
  const tabActivity = document.getElementById('term-tab-activity');
  const viewTerminal = document.getElementById('term-view-terminal');
  const viewActivity = document.getElementById('term-view-activity');
  const activityBadge = document.getElementById('term-activity-badge');
  const instanceSelect = document.getElementById('term-instance-select');

  const HAS_XTERM = typeof window.Terminal === 'function';
  const USE_REAL_SHELL = typeof IS_TAURI !== 'undefined' && IS_TAURI && HAS_XTERM;
  // dashboard.html has no API_BASE at all (every fetch is same-origin
  // already); the desktop shell's own copy of this file always defines it.
  const TERM_API_BASE = typeof API_BASE !== 'undefined' ? API_BASE : '';

  // ------------------------------------------------------- collapse/resize --
  let panelHeight = 320;
  let unseenActivity = 0;

  // One button toggles both directions — a separate floating "reopen"
  // button used to exist here, but it rendered UNDER the collapsed
  // panel's own header bar (a genuine z-index bug: the header stays
  // visible at z-index 400 even while collapsed, the floating button sat
  // at 399), making it genuinely invisible/unreachable behind that bar.
  // The header itself never goes away when collapsed, so its own
  // minimize/restore button is reachable either way — no second control
  // is needed at all.
  function updateMinimizeButton() {
    const collapsed = panel.classList.contains('collapsed');
    btnMinimize.textContent = collapsed ? '⌃' : '━';
    btnMinimize.title = collapsed ? 'Open Terminal' : 'Minimize';
  }
  // The panel is a real row at the bottom of the window (body is a flex
  // column), so the content above it already ends where it begins — nothing
  // to reserve space for. The one thing that still needs its height is the
  // fixed-position toast stack, which must sit ABOVE the bar, not on it:
  // --term-panel-h is kept equal to the panel's actual rendered height
  // (collapsed, drag-resized, maximized, window-resized alike).
  function syncPanelHeight() {
    document.documentElement.style.setProperty('--term-panel-h', panel.getBoundingClientRect().height + 'px');
  }
  function syncClearance() { syncPanelHeight(); }
  // The panel's max-height is a share of the window, so resizing the window
  // can change its height without any of the handlers below firing.
  window.addEventListener('resize', syncPanelHeight);
  function setCollapsed(collapsed) {
    panel.classList.toggle('collapsed', collapsed);
    updateMinimizeButton();
    syncClearance();
    if (!collapsed) {
      unseenActivity = 0;
      updateActivityBadge();
      if (USE_REAL_SHELL) ensureRealTerminalStarted();
      fitXterm();
    }
  }
  updateMinimizeButton();
  syncClearance();
  btnMinimize.onclick = () => setCollapsed(!panel.classList.contains('collapsed'));
  btnMaximize.onclick = () => {
    panel.classList.toggle('maximized');
    syncClearance();
    fitXterm();
  };

  (function wireDragResize() {
    let dragging = false, startY = 0, startHeight = 0;
    resizeHandle.addEventListener('mousedown', (e) => {
      if (panel.classList.contains('maximized')) return;
      dragging = true; startY = e.clientY; startHeight = panel.getBoundingClientRect().height;
      document.body.style.userSelect = 'none';
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const delta = startY - e.clientY;
      panelHeight = Math.max(120, Math.min(window.innerHeight * 0.85, startHeight + delta));
      panel.style.height = panelHeight + 'px';
      syncClearance();
      fitXterm();
    });
    window.addEventListener('mouseup', () => {
      if (dragging) { dragging = false; document.body.style.userSelect = ''; }
    });
  })();
  // Any change to the panel's real size (collapse, drag, maximize, window resize)
  // re-syncs the toast offset and re-fits the terminal.
  if (typeof ResizeObserver === 'function') {
    let queued = false;
    new ResizeObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; syncPanelHeight(); fitXterm(); });
    }).observe(panel);
  } else {
    window.addEventListener('resize', syncPanelHeight);
  }

  // ------------------------------------------------------------- tabs -----
  function selectTab(name) {
    const isTerminal = name === 'terminal';
    tabTerminal.classList.toggle('active', isTerminal);
    tabActivity.classList.toggle('active', !isTerminal);
    viewTerminal.classList.toggle('active', isTerminal);
    viewActivity.classList.toggle('active', !isTerminal);
    if (!isTerminal) {
      unseenActivity = 0;
      updateActivityBadge();
      // Self-heals a first load that raced the dashboard token not being
      // set yet (or any other transient /api/activity failure) — rather
      // than staying empty forever because the one-shot call at panel
      // init happened to fail, retry every time a human actually looks
      // at this tab and finds nothing there yet.
      if (!activityList.children.length) loadInitialActivity();
    }
    if (isTerminal) fitXterm();
  }
  tabTerminal.onclick = () => selectTab('terminal');
  tabActivity.onclick = () => selectTab('activity');

  function updateActivityBadge() {
    activityBadge.textContent = String(unseenActivity);
    activityBadge.classList.toggle('hidden', unseenActivity === 0);
  }

  // ---------------------------------------------------- instance select ---
  async function loadInstancesForSelect() {
    try {
      const bots = await api('/api/bots');
      for (const b of bots) {
        const opt = document.createElement('option');
        opt.value = b.id;
        opt.textContent = b.name;
        instanceSelect.appendChild(opt);
      }
    } catch (_e) { /* select just stays at "No instance context" */ }
  }

  // ----------------------------------------------------- scoped REPL ------
  // The browser dashboard's whole terminal experience, and also the
  // "/command" path on desktop (see interceptSlashLine()).
  async function runScopedCommand(text) {
    const instance_id = instanceSelect.value ? Number(instanceSelect.value) : null;
    try {
      const res = await fetch(`${TERM_API_BASE}/api/terminal/exec`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Dashboard-Token': getToken() },
        body: JSON.stringify({ text, instance_id }),
      });
      if (!res.ok) return `error: HTTP ${res.status} — check the dashboard token`;
      const data = await res.json();
      return data.output ?? '';
    } catch (e) {
      return 'error: ' + e;
    }
  }

  function buildScopedRepl() {
    const wrap = document.createElement('div');
    wrap.id = 'scoped-terminal';
    wrap.style.cssText = 'display:flex; flex-direction:column; height:100%;';
    wrap.innerHTML = `
      <div id="scoped-output" class="scoped-output"></div>
      <div class="scoped-inputline">
        <span class="scoped-prompt">&gt;</span>
        <input id="scoped-input" type="text" placeholder="/help" autocomplete="off" spellcheck="false">
      </div>`;
    viewTerminal.appendChild(wrap);
    const output = wrap.querySelector('#scoped-output');
    const input = wrap.querySelector('#scoped-input');
    function printLine(text, cls) {
      const div = document.createElement('div');
      if (cls) div.className = cls;
      div.textContent = text;
      output.appendChild(div);
      output.scrollTop = output.scrollHeight;
    }
    printLine('ABP scoped command console — type /help for the full command list.', 'scoped-line-cmd');
    input.addEventListener('keydown', async (e) => {
      if (e.key !== 'Enter') return;
      const text = input.value.trim();
      if (!text) return;
      printLine('> ' + text, 'scoped-line-cmd');
      input.value = '';
      input.disabled = true;
      const reply = await runScopedCommand(text);
      printLine(reply, reply.startsWith('error') ? 'scoped-line-err' : '');
      input.disabled = false;
      input.focus();
    });
    panel.addEventListener('transitionend', () => input.focus());
  }

  // ------------------------------------------------- real pty (desktop) ---
  let term = null, fitAddon = null, started = false;
  // Tracks the line currently being typed, ONLY so a "/"-prefixed line can
  // be recognized and diverted to runScopedCommand() instead of the real
  // shell — plain keystrokes still go straight to the pty character by
  // character exactly like a normal terminal (full readline/tab-complete/
  // Ctrl+C behavior intact), since real interactive programs need that.
  // Whether a line is a slash command can only be known once its first
  // character arrives, so nothing is forwarded to the pty until then.
  let lineBuf = '', inSlashLine = false, atLineStart = true;

  function buildXterm() {
    term = new window.Terminal({
      convertEol: true,
      fontFamily: 'Cascadia Code, Consolas, ui-monospace, monospace',
      fontSize: 13,
      theme: { background: getComputedStyle(document.documentElement).getPropertyValue('--surface').trim() || '#17212b' },
    });
    fitAddon = new window.FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(document.getElementById('xterm-container'));
    term.writeln('ABP terminal — real PowerShell session. Lines starting with / run an ABP command instead (try /help).');
    term.writeln('');

    term.onData(async (data) => {
      // Ctrl+C: always forward immediately (must interrupt whatever the
      // pty is doing, slash-line or not) and reset local line tracking.
      if (data === '\x03') {
        lineBuf = ''; inSlashLine = false; atLineStart = true;
        await window.__TAURI__.core.invoke('terminal_write', { data });
        return;
      }
      if (data === '\r' || data === '\n') {
        if (inSlashLine) {
          term.write('\r\n');
          const text = lineBuf;
          lineBuf = ''; inSlashLine = false; atLineStart = true;
          const reply = await runScopedCommand(text);
          term.write(reply.replace(/\n/g, '\r\n') + '\r\n');
        } else {
          atLineStart = true;
          await window.__TAURI__.core.invoke('terminal_write', { data: '\r' });
        }
        return;
      }
      if (data === '\x7f' || data === '\b') { // backspace
        if (inSlashLine) {
          if (lineBuf.length) {
            lineBuf = lineBuf.slice(0, -1);
            term.write('\b \b');
          }
          if (!lineBuf.length) { inSlashLine = false; atLineStart = true; } // backspaced the "/" itself away — back to a genuinely fresh line, nothing was ever sent to the pty for it
        } else {
          await window.__TAURI__.core.invoke('terminal_write', { data });
        }
        return;
      }
      if (atLineStart && data.length > 0) {
        atLineStart = false;
        if (data === '/') {
          inSlashLine = true;
          lineBuf = '/';
          term.write(data); // local echo — this keystroke never reaches the pty
          return;
        }
      }
      if (inSlashLine) {
        lineBuf += data;
        term.write(data);
        return;
      }
      await window.__TAURI__.core.invoke('terminal_write', { data });
    });
  }

  function fitXterm() {
    if (!term || !fitAddon || panel.classList.contains('collapsed')) return;
    requestAnimationFrame(() => {
      try {
        fitAddon.fit();
        if (started) {
          window.__TAURI__.core.invoke('terminal_resize', { cols: term.cols, rows: term.rows }).catch(() => {});
        }
      } catch (_e) { /* container not laid out yet */ }
    });
  }

  async function ensureRealTerminalStarted() {
    if (started) return;
    started = true;
    try {
      await window.__TAURI__.core.invoke('terminal_start');
      fitXterm();
    } catch (e) {
      term.writeln('\r\nfailed to start terminal: ' + e);
    }
  }

  if (USE_REAL_SHELL) {
    buildXterm();
    window.__TAURI__.event.listen('terminal-output', (evt) => {
      term.write(evt.payload.data.replace(/\n/g, '\r\n'));
    });
    window.addEventListener('resize', fitXterm);
  } else {
    buildScopedRepl();
  }

  // ---------------------------------------------------------- activity ----
  const activityList = document.getElementById('activity-list');
  const levelFilter = document.getElementById('activity-level-filter');
  const searchInput = document.getElementById('activity-search');
  const autoscrollCheckbox = document.getElementById('activity-autoscroll');
  document.getElementById('activity-clear').onclick = () => { activityList.innerHTML = ''; };
  let lastSeenId = 0;
  const MAX_ROWS = 1000;

  function fmtTs(ts) {
    const d = new Date(ts * 1000);
    return d.toTimeString().slice(0, 8);
  }

  function matchesFilters(row) {
    const level = levelFilter.value;
    const q = searchInput.value.trim().toLowerCase();
    if (level && row.level !== level) return false;
    if (q && !(row.message.toLowerCase().includes(q) || row.logger.toLowerCase().includes(q))) return false;
    return true;
  }

  function appendActivityRow(entry) {
    const row = document.createElement('div');
    row.className = 'activity-row';
    row.dataset.level = entry.level;
    row.dataset.logger = entry.logger;
    row.dataset.message = entry.message;
    row.innerHTML =
      `<span class="a-ts">${esc(fmtTs(entry.ts))}</span>` +
      `<span class="a-level ${esc(entry.level)}">${esc(entry.level)}</span>` +
      `<span class="a-logger" title="${esc(entry.logger)}">${esc(entry.logger)}</span>` +
      `<span class="a-msg">${esc(entry.message)}</span>`;
    if (!matchesFilters(entry)) row.classList.add('a-hidden');
    activityList.appendChild(row);
    while (activityList.children.length > MAX_ROWS) activityList.removeChild(activityList.firstChild);
    if (autoscrollCheckbox.checked) activityList.scrollTop = activityList.scrollHeight;
    if (entry.id > lastSeenId) lastSeenId = entry.id;
    if (viewActivity.classList.contains('active') === false || panel.classList.contains('collapsed')) {
      if (entry.level === 'ERROR' || entry.level === 'CRITICAL' || entry.level === 'WARNING') {
        unseenActivity += 1;
        updateActivityBadge();
      }
    }
  }

  function reapplyFilters() {
    for (const row of activityList.children) {
      const matches = matchesFilters({ level: row.dataset.level, message: row.dataset.message, logger: row.dataset.logger });
      row.classList.toggle('a-hidden', !matches);
    }
  }
  levelFilter.onchange = reapplyFilters;
  searchInput.oninput = reapplyFilters;

  async function loadInitialActivity() {
    try {
      const data = await api('/api/activity?limit=200');
      for (const entry of data.entries) appendActivityRow(entry);
    } catch (_e) { /* Activity tab just starts empty; live pushes still work */ }
  }

  window.onActivityEntry = function (entry) {
    if (entry.id <= lastSeenId) return; // already have it from the initial /api/activity load
    appendActivityRow(entry);
  };

  // ------------------------------------------------------ copy / save -----
  // Both buttons act on whichever tab is currently active, so there's one
  // pair of controls in the header instead of duplicating them per tab.
  function terminalTabText() {
    if (USE_REAL_SHELL && term) {
      // xterm keeps the whole scrollback in term.buffer.active, not just
      // what's currently painted on screen — walk every line, not just
      // the visible viewport, so Save/Copy captures the full session.
      const buf = term.buffer.active;
      const lines = [];
      for (let i = 0; i < buf.length; i++) {
        const line = buf.getLine(i);
        if (line) lines.push(line.translateToString(true));
      }
      // Trim trailing blank lines xterm pads the buffer with.
      while (lines.length && lines[lines.length - 1] === '') lines.pop();
      return lines.join('\n');
    }
    const output = document.getElementById('scoped-output');
    return output ? output.textContent : '';
  }

  function activityTabText() {
    const lines = [];
    for (const row of activityList.children) {
      if (row.classList.contains('a-hidden')) continue; // respect the active level/text filter
      const ts = row.querySelector('.a-ts');
      lines.push(`[${ts ? ts.textContent : '?'}] ${row.dataset.level} ${row.dataset.logger}: ${row.dataset.message}`);
    }
    return lines.join('\n');
  }

  function activeTabLabel() {
    return tabTerminal.classList.contains('active') ? 'Terminal' : 'Activity';
  }
  function activeTabText() {
    return tabTerminal.classList.contains('active') ? terminalTabText() : activityTabText();
  }

  document.getElementById('term-copy').onclick = () => {
    const text = activeTabText();
    if (!text) { showCopyToast('Nothing to copy yet.'); return; }
    copyText(text, activeTabLabel());
  };
  document.getElementById('term-save').onclick = () => {
    const text = activeTabText();
    if (!text) { showCopyToast('Nothing to save yet.'); return; }
    const label = activeTabLabel().toLowerCase();
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    const blob = new Blob([text], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `abp-${label}-${stamp}.txt`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  loadInstancesForSelect();
  loadInitialActivity();
})();
