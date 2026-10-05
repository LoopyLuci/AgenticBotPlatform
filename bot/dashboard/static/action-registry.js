// The one action registry: every user-visible thing either UI can do, as data.
//
// Both UIs load this exact file (bot/dashboard/static/ and desktop-app/ui/);
// tests/test_shortcuts.py fails if the two copies differ, if an id is duplicated, if two actions
// claim the same default chord, if a default is a key the browser or the OS needs, or if an
// action id the UI or the docs reference is missing from here. docs/shortcuts.md is the same
// table in prose: the desktop app's Rust side binds OS-level GLOBAL shortcuts to these ids and
// emits them as {"action": "<id>"}, and that agent reads that file.
//
// An action is { id, label, group, context, keys, whileTyping, run }:
//   id          stable, dotted, and the only thing anything else ever refers to
//   label       what the command palette and the help overlay show
//   group       palette/overlay grouping ("Go to", "Chat", ...)
//   context     'global' or the id of the screen it belongs to; the help overlay lists the
//               global ones plus the ones for the screen you are looking at
//   keys        default chords, first one primary. [] means "palette and right-click only": a
//               chord that acts on whatever row you happen to be pointing at is not something
//               to fire from across the page
//   whileTyping true only for the actions meant to fire inside a text field (Mod+Enter to send);
//               everything else is suppressed while an input has focus
//   run(target) does the thing. `target` is whatever the caller had - a row, a cell id, a
//               routine id - or undefined from the palette.
//
// Chord grammar: space-separated tokens, each a set of modifiers joined by + (mod+shift+k) and/or
// one key (g, vi, ?, /). `mod` is Ctrl on Windows/Linux and Cmd on macOS. shortcuts.js resolves
// chords, does the platform swap and the conflict check.
(function (api) {
  'use strict';

  // ---- the page globals this file drives, looked up late so a UI that lacks one (the desktop
  // ---- app's support bot has its own ids) no-ops instead of throwing.
  const $ = (id) => document.getElementById(id);
  const call = (name, ...args) => {
    const f = window[name];
    return typeof f === 'function' ? f(...args) : undefined;
  };
  const toast = (message, type) => {
    if (typeof window.showToast === 'function') window.showToast(message, type);
    else console.log(message);
  };
  const confirmThen = (question, fn) => {
    if (window.confirm(question)) fn();
  };
  const copy = (text, label) => (typeof window.copyText === 'function' ? window.copyText(text, label) : null);
  const post = async (path, body) => {
    if (typeof api !== 'function') { toast('The page is not connected to ABP yet.', 'error'); return null; }
    return api(path, { method: 'POST', body: JSON.stringify(body || {}) });
  };
  const del = (path) => (typeof api === 'function' ? api(path, { method: 'DELETE' }) : null);

  // Navigate: click the sidebar link when there is one (that is what scrolls, updates the hash
  // and marks the nav active), otherwise scroll the section itself. Several ids let one action
  // cover a screen the two UIs name differently.
  const navTo = (ids) => {
    for (const id of ids) {
      const link = document.querySelector(`nav.sidenav a[href="#${id}"]`);
      if (link) { link.click(); return true; }
    }
    for (const id of ids) {
      const section = $(id);
      if (section) { section.scrollIntoView({ behavior: 'smooth', block: 'start' }); return true; }
    }
    toast(`This build of ABP has no ${ids[0]} screen.`, 'error');
    return false;
  };

  // "The row the person meant": the last bot card or cell row they pointed at, falling back to the
  // bot the Chat tab is pointed at. These pages have no selection model, so a shortcut that acts
  // on a row has to work this way.
  const lastTouched = { bot: null, cell: null };
  const numAttr = (el, prefix) => {
    if (!el) return null;
    const key = Object.keys(el.dataset || {}).find((k) => k.startsWith(prefix) && k !== `${prefix}Model` && /^\d+$/.test(el.dataset[k] || ''));
    return key ? Number(el.dataset[key]) : null;
  };
  const currentBotId = (target) => {
    if (target && target.closest) {
      const btn = target.closest('[data-bot-edit],[data-bot-startstop],[data-bot-toggle],[data-bot-restart],[data-bot-delete],[data-bot-agent]');
      const direct = numAttr(btn, 'bot');
      if (direct) return direct;
    }
    if (lastTouched.bot) {
      const btn = lastTouched.bot.querySelector('[data-bot-edit],[data-bot-startstop],[data-bot-toggle],[data-bot-restart],[data-bot-delete]');
      const id = numAttr(btn, 'bot');
      if (id) return id;
    }
    const sel = $('chat-instance');
    return sel && sel.value ? Number(sel.value) : null;
  };

  function messageText(target) {
    const bubble = target && target.closest ? target.closest('.chat-bubble') : null;
    if (!bubble) return '';
    const parts = [];
    for (const child of bubble.children) {
      if (child.tagName === 'BUTTON' || child.classList.contains('chat-meta') || child.classList.contains('chat-actions')) continue;
      parts.push(child.textContent);
    }
    return parts.join('\n').trim();
  }

  // The log view is one element with newlines in it, so "the line under the pointer" has to come
  // from the caret position rather than from an element.
  function lineUnder(target) {
    const host = target && target.closest ? target.closest('#log-lines') : null;
    if (!host) return '';
    const text = host.textContent;
    try {
      let range = document.caretRangeFromPoint ? document.caretRangeFromPoint(target.clientX, target.clientY) : null;
      if (!range && document.caretPositionFromPoint) {
        const pos = document.caretPositionFromPoint(target.clientX, target.clientY);
        if (pos) range = pos;
      }
      if (!range) return text;
      const before = document.createRange();
      before.setStart(host, 0);
      before.setEnd(range.startContainer || range.offsetNode, range.startOffset || range.offset || 0);
      const at = before.toString().length;
      const start = text.lastIndexOf('\n', at - 1) + 1;
      const end = text.indexOf('\n', at);
      return text.slice(start, end === -1 ? text.length : end);
    } catch (_e) {
      return text;  // no caret API here: the whole view is still better than nothing
    }
  }

  function stepChatInstance(delta) {
    const tabs = Array.from(document.querySelectorAll('#chat-tabs .chat-tab'));
    if (!tabs.length) { toast('No bots yet.', 'error'); return; }
    const active = tabs.findIndex((t) => t.classList.contains('active'));
    tabs[((active === -1 ? 0 : active) + delta + tabs.length) % tabs.length].click();
  }

  // ---- navigation: `g` then a unique token, so not one of these steals a bare key -----------
  const SCREENS = [
    [['overview'], 'Overview', 'o'],
    [['jobs'], 'Jobs', 'j'],
    [['telemetry'], 'Connections & Telemetry', 'te'],
    [['database'], 'Database', 'db'],
    [['control'], 'Control Center', 'c'],
    [['resilience'], 'Resilience', 're'],
    [['diagnostics'], 'Diagnostics', 'dg'],
    [['logs'], 'Live Logs', 'l'],
    [['chat'], 'Chat', 'h'],
    [['server-chat'], 'Server Chat', 'sc'],
    [['support', 'support-bot'], 'Support Bot', 'sp'],
    [['sessions'], 'Sessions', 'se'],
    [['bots'], 'Bots', 'b'],
    [['models'], 'Models', 'm'],
    [['router'], 'Model Router', 'ro'],
    [['unsloth'], 'Unsloth', 'un'],
    [['cluster'], 'Cluster', 'cl'],
    [['octopus'], 'Octopus', 'oc'],
    [['vision'], 'Vision', 'vi'],
    [['hosting'], 'Hosting', 'ho'],
    [['storage'], 'Storage', 'st'],
    [['localai'], 'Local AI', 'lo'],
    [['lab'], 'Neural Lab', 'lb'],
    [['studio'], 'Studio', 'sd'],
    [['modules'], 'Modules', 'mo'],
    [['transferdaemon'], 'TransferDaemon', 'td'],
    [['power'], 'Power', 'pw'],
    [['vm-harness'], 'VM-Harness', 'vm'],
    [['hermes-manager'], 'Hermes Manager', 'hm'],
    [['ollama'], 'Ollama', 'ol'],
    [['kanban'], 'Kanban', 'kb'],
    [['agents'], 'ABP Agents', 'ag'],
    [['swarms'], 'Swarms', 'sw'],
    [['tailscale'], 'Tailscale', 'ts'],
    [['containers'], 'Containers', 'co'],
    [['vms'], 'Virtual Machines', 'vs'],
    [['infra-rules'], 'Infra Automation', 'ir'],
    [['browser'], 'Browser', 'br'],
    [['ssh-toolkit'], 'SSH Toolkit', 'sk'],
    [['automation'], 'Automation', 'au'],
    [['routines'], 'Routines', 'rt'],
    [['customize'], 'Customize UI', 'cu'],
    [['training'], 'Training', 'tr'],
    [['platforms'], 'Platforms', 'pf'],
    [['mobile'], 'Mobile', 'mb'],
    [['files', 'storage'], 'Files', 'fi'],
    [['servers'], 'Linked Servers', 'sv'],
    [['updates'], 'Updates', 'up'],
  ];

  const ACTIONS = [
    // ---- general ------------------------------------------------------------------------------
    { id: 'ui.palette', label: 'Command palette', group: 'General', context: 'global', keys: ['mod+k'],
      run: () => window.abpShortcuts.openPalette() },
    { id: 'ui.help', label: 'Keyboard shortcuts', group: 'General', context: 'global', keys: ['?', 'mod+/'],
      run: () => window.abpShortcuts.openHelp() },
    { id: 'ui.refresh', label: 'Refresh everything on screen', group: 'General', context: 'global', keys: ['mod+alt+r'],
      run: () => call('refreshAll') },
    { id: 'ui.theme', label: 'Switch between light and dark', group: 'General', context: 'global', keys: ['mod+alt+t'],
      run: () => {
        const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        try { localStorage.setItem('bs-ui-theme', next); } catch (_e) { /* private mode */ }
        if (typeof window.applyAppearanceTheme === 'function') window.applyAppearanceTheme(next);
        else document.documentElement.setAttribute('data-theme', next);
      } },
    { id: 'ui.sidebar', label: 'Collapse or expand the sidebar', group: 'General', context: 'global', keys: ['mod+alt+s'],
      run: () => { const b = $('btn-side-toggle'); if (b) b.click(); else toast('There is no sidebar here.', 'error'); } },
    { id: 'ui.terminal', label: 'Show or hide Terminal and Activity', group: 'General', context: 'global', keys: ['mod+alt+j'],
      run: () => { const b = $('term-minimize'); if (b) b.click(); } },
    { id: 'ui.search', label: 'Search this screen', group: 'General', context: 'global', keys: ['/'],
      run: () => {
        const section = window.abpShortcuts.currentSection();
        const scope = section && $(section);
        const input = (scope && scope.querySelector('input[type="search"],input[type="text"]')) || $('activity-search');
        if (!input) { toast('There is nothing to search on this screen.', 'error'); return; }
        input.focus();
        input.select();
      } },
    { id: 'copy.selection', label: 'Copy the selected text', group: 'General', context: 'global', keys: ['mod+alt+shift+c'],
      run: () => {
        const text = String(window.getSelection() || '');
        if (!text.trim()) { toast('Nothing is selected.', 'error'); return; }
        copy(text, 'Selection');
      } },
    { id: 'config.reload', label: 'Re-read the config from disk', group: 'General', context: 'global', keys: ['mod+alt+shift+r'],
      run: () => call('reloadConfig') },

    // ---- one per screen ------------------------------------------------------------------------
    ...SCREENS.map(([ids, label, token]) => ({
      id: `nav.${ids[0].replace(/[^a-z0-9]+/g, '-')}`,
      label: `Go to ${label}`,
      group: 'Go to',
      context: 'global',
      keys: [`g ${token}`],
      run: () => navTo(ids),
    })),

    // ---- chat -----------------------------------------------------------------------------------
    { id: 'chat.focus', label: 'Jump to the message box', group: 'Chat', context: 'chat', keys: ['mod+alt+i'],
      run: () => { const el = $('chat-input'); if (el) el.focus(); } },
    { id: 'chat.send', label: 'Send the message', group: 'Chat', context: 'chat', keys: ['mod+enter'], whileTyping: true,
      run: () => call('sendChatMessage') },
    { id: 'chat.new', label: 'Start a new session with this bot', group: 'Chat', context: 'chat', keys: ['mod+alt+n'],
      run: async () => {
        const sel = $('chat-instance');
        if (!sel || !sel.value) { toast('Pick a bot first.', 'error'); return; }
        try {
          await post(`/api/bots/${Number(sel.value)}/session/new`, {});
          call('refreshChat', Number(sel.value));
          toast('New session started.', 'success');
        } catch (e) { toast(String((e && e.message) || e), 'error'); }
      } },
    { id: 'chat.copy', label: 'Copy this conversation as text', group: 'Chat', context: 'chat', keys: ['mod+alt+c'],
      run: () => { const b = $('btn-chat-copy'); if (b) b.click(); } },
    { id: 'chat.clear', label: 'Delete this bot\'s chat history', group: 'Chat', context: 'chat', keys: ['mod+alt+delete'],
      run: () => confirmThen('Delete every message in this bot\'s chat history?', () => { const b = $('btn-chat-clear'); if (b) b.click(); }) },
    { id: 'chat.mode', label: 'Switch between Chat with Bot and Send from Server', group: 'Chat', context: 'chat', keys: ['mod+alt+m'],
      run: () => { const b = $('chat-mode-switch'); if (b) b.click(); } },
    { id: 'chat.instance.next', label: 'Next bot in this chat', group: 'Chat', context: 'chat', keys: ['mod+alt+]'],
      run: () => stepChatInstance(1) },
    { id: 'chat.instance.prev', label: 'Previous bot in this chat', group: 'Chat', context: 'chat', keys: ['mod+alt+['],
      run: () => stepChatInstance(-1) },
    { id: 'chat.message.copy', label: 'Copy this message', group: 'Chat', context: 'chat', keys: [],
      run: (t) => copy(messageText(t), 'Message') },
    { id: 'chat.message.quote', label: 'Quote this message into the box', group: 'Chat', context: 'chat', keys: [],
      run: (t) => {
        const el = $('chat-input');
        if (!el) { toast('There is no message box here.', 'error'); return; }
        const quoted = messageText(t).split('\n').map((l) => `> ${l}`).join('\n');
        el.value = (el.value.trim() ? `${el.value.trim()}\n` : '') + `${quoted}\n\n`;
        el.focus();
      } },
    { id: 'chat.message.retry', label: 'Ask the bot again with this message', group: 'Chat', context: 'chat', keys: [],
      run: (t) => {
        const el = $('chat-input');
        if (!el) { toast('There is no message box here.', 'error'); return; }
        el.value = messageText(t);
        el.focus();
        call('sendChatMessage');
      } },
    { id: 'chat.message.delete', label: 'Delete this message', group: 'Chat', context: 'chat', keys: [],
      run: async (t) => {
        const row = t && t.closest ? t.closest('[data-msg-id]') : null;
        if (!row) { toast('That message cannot be deleted from here.', 'error'); return; }
        confirmThen('Delete this message?', async () => {
          await del(`/api/chat/messages/${encodeURIComponent(row.dataset.msgId)}`);
          const inst = $('chat-instance');
          if (inst && inst.value) call('refreshChat', Number(inst.value));
        });
      } },
    { id: 'serverchat.send', label: 'Send from the server', group: 'Chat', context: 'server-chat', keys: ['mod+enter'], whileTyping: true,
      run: () => call('sendServerChatMessage') },
    { id: 'serverchat.message.copy', label: 'Copy this message', group: 'Chat', context: 'server-chat', keys: [],
      run: (t) => copy(messageText(t), 'Message') },
    { id: 'serverchat.message.delete', label: 'Delete this message', group: 'Chat', context: 'server-chat', keys: [],
      run: (t) => {
        const row = t && t.closest ? t.closest('#serverchat-window .chat-row') : null;
        const btn = row && row.querySelector('.chat-msg-delete');
        if (btn) btn.click();
        else toast('Only your own messages can be deleted from here.', 'error');
      } },
    { id: 'support.send', label: 'Send this to the Support Bot', group: 'Chat', context: 'support', keys: ['mod+enter'], whileTyping: true,
      run: () => {
        const b = $('btn-support-bot-send') || $('btn-support-send');
        if (b) b.click(); else call('sendSupportMessage');
      } },

    // ---- bots -----------------------------------------------------------------------------------
    { id: 'bot.toggle', label: 'Start or stop this bot', group: 'Bots', context: 'bots', keys: ['mod+alt+b'],
      run: (t) => {
        const id = currentBotId(t);
        const btn = id && document.querySelector(`[data-bot-startstop="${id}"]`);
        if (btn) btn.click(); else toast('Point at a bot card, or pick a bot, first.', 'error');
      } },
    { id: 'bot.restart', label: 'Restart this bot', group: 'Bots', context: 'bots', keys: ['mod+alt+shift+b'],
      run: (t) => {
        const id = currentBotId(t);
        const btn = id && document.querySelector(`[data-bot-restart="${id}"]`);
        if (btn) btn.click(); else toast('Point at a bot card first.', 'error');
      } },
    { id: 'bot.edit', label: 'Edit this bot', group: 'Bots', context: 'bots', keys: ['mod+alt+e'],
      run: (t) => {
        const id = currentBotId(t);
        const btn = id && document.querySelector(`[data-bot-edit="${id}"]`);
        if (btn) btn.click(); else toast('Point at a bot card first.', 'error');
      } },
    { id: 'bot.agent', label: 'Agent settings for this bot', group: 'Bots', context: 'bots', keys: [],
      run: (t) => {
        const id = currentBotId(t);
        const btn = id && document.querySelector(`[data-bot-agent="${id}"]`);
        if (btn) btn.click(); else toast('That bot has no agent of its own.', 'error');
      } },
    { id: 'bot.chat', label: 'Chat with this bot', group: 'Bots', context: 'bots', keys: [],
      run: (t) => {
        const id = currentBotId(t);
        if (!id) { toast('Point at a bot card first.', 'error'); return; }
        navTo(['chat']);
        const sel = $('chat-instance');
        if (sel) { sel.value = String(id); call('switchToInstance', id); }
      } },
    { id: 'bot.delete', label: 'Delete this bot', group: 'Bots', context: 'bots', keys: ['mod+alt+shift+e'],
      run: (t) => {
        const id = currentBotId(t);
        const btn = id && document.querySelector(`[data-bot-delete="${id}"]`);
        if (btn) confirmThen('Delete this bot and its stored credentials?', () => btn.click());
        else toast('Point at a bot card first.', 'error');
      } },

    // ---- processes and the emergency stop --------------------------------------------------------
    { id: 'processes.open', label: 'Open the Processes panel', group: 'Processes', context: 'global', keys: ['mod+alt+p'],
      run: () => {
        navTo(['diagnostics']);
        const btn = document.querySelector('[data-tab="processes"]');
        if (btn) btn.click();
      } },
    { id: 'process.kill', label: 'Kill this cell', group: 'Processes', context: 'diagnostics', keys: [],
      run: (t) => {
        const row = t && t.closest ? t.closest('#diag-cells-tbody tr') : (lastTouched.cell && CSS.escape(lastTouched.cell.id));
        const btn = t && t.closest ? t.closest('[data-kill-cell]')
          : (lastTouched.cell && lastTouched.cell.querySelector('[data-kill-cell]'));
        if (btn) confirmThen('Kill this cell and everything running in it?', () => btn.click());
        else { toast(String(row ? 'That cell has nothing to kill.' : 'Point at a cell row to kill that cell.'), 'error'); }
      } },
    { id: 'estop.emergency', label: 'Emergency stop: kill every cell', group: 'Processes', context: 'global', keys: ['mod+alt+shift+x'],
      run: () => confirmThen('Kill EVERY cell in this ABP process? Daemons are somebody\'s service and are left alone.', async () => {
        try {
          const r = await post('/api/sandbox/estop', { reason: 'the emergency stop, from a keyboard shortcut' });
          toast(`Stopped ${(r && r.count) || 0} cell(s).`, 'success');
          call('refreshDiagnostics');
        } catch (e) { toast(String((e && e.message) || e), 'error'); }
      }) },
    { id: 'estop.agent', label: 'Engage or release the agent emergency stop', group: 'Processes', context: 'global', keys: [],
      run: async () => {
        try {
          const now = typeof api === 'function' ? await api('/api/estop') : null;
          if (now && now.engaged) {
            await post('/api/estop', { engaged: false });
            toast('New agent work released.', 'success');
          } else {
            await post('/api/estop', { engaged: true, reason: 'from a dashboard keyboard shortcut' });
            toast('New agent work is blocked until you release it.', 'success');
          }
        } catch (e) { toast(String((e && e.message) || e), 'error'); }
      } },

    // ---- routines -------------------------------------------------------------------------------
    { id: 'routine.run', label: 'Run this routine now', group: 'Routines', context: 'routines', keys: ['mod+alt+g'],
      run: (t) => {
        if (t !== undefined && /^\d+$/.test(String(t))) {
          const exact = document.querySelector(`[data-rt="run"][data-id="${t}"]`);
          if (exact) { exact.click(); return; }
        }
        const open = document.querySelector('#rt-root [data-rt="run"]');
        if (open) { open.click(); return; }
        const all = document.querySelectorAll('#rt-root tbody tr [data-rt="run"]');
        if (all.length === 1) { all[0].click(); return; }
        toast('Open a routine first, or right-click one and choose Run now.', 'error');
      } },
    // The routines and modules panels redraw themselves when their section comes on screen (they
    // poll behind an IntersectionObserver), so "refresh" is just "go there" - there is no second
    // refresh entry point to call and inventing one would mean editing two other agents' panels.
    { id: 'routine.refresh', label: 'Go to Routines and reload the list', group: 'Routines', context: 'routines', keys: [],
      run: () => navTo(['routines']) },

    // ---- files ----------------------------------------------------------------------------------
    { id: 'files.refresh', label: 'Re-read this folder', group: 'Files', context: 'files', keys: ['mod+alt+f'],
      run: () => call('refreshFilesList') },
    { id: 'file.download', label: 'Download this file', group: 'Files', context: 'files', keys: [],
      run: (t) => {
        const btn = t && t.closest ? t.closest('[data-files-download]') : null;
        if (btn) btn.click(); else toast('Point at a file row first.', 'error');
      } },
    { id: 'file.copyPath', label: 'Copy this file\'s path', group: 'Files', context: 'files', keys: [],
      run: (t) => {
        const row = t && t.closest ? t.closest('#files-tbody tr') : null;
        if (!row) { toast('Point at a file row first.', 'error'); return; }
        const named = row.querySelector('[data-files-open]') || row.querySelector('td');
        const crumb = $('files-breadcrumb');
        const dir = ((crumb && crumb.textContent) || '').replace(/\/+$/, '');
        const name = (named && named.textContent || '').trim();
        copy(dir ? `${dir}/${name}` : name, 'Path');
      } },

    // ---- logs ------------------------------------------------------------------------------------
    { id: 'logs.copyLine', label: 'Copy this log line', group: 'Logs', context: 'logs', keys: [],
      run: (t) => copy(lineUnder(t), 'Log line') },
    { id: 'logs.copyAll', label: 'Copy the whole log view', group: 'Logs', context: 'logs', keys: ['mod+alt+shift+l'],
      run: () => { const el = $('log-lines'); if (el) copy(el.textContent, 'Logs'); } },

    // ---- activity, sessions, modules, kanban -------------------------------------------------------
    { id: 'activity.clear', label: 'Clear the Activity list (the log itself is untouched)', group: 'Activity', context: 'global', keys: ['mod+alt+shift+a'],
      run: () => { const b = $('activity-clear'); if (b) b.click(); } },
    { id: 'activity.copy', label: 'Copy this activity entry', group: 'Activity', context: 'global', keys: [],
      run: (t) => {
        const row = t && t.closest ? t.closest('.activity-row') : null;
        if (row) copy(row.textContent.trim(), 'Entry');
      } },
    { id: 'sessions.export', label: 'Export this session as JSON', group: 'Sessions', context: 'sessions', keys: ['mod+alt+shift+j'],
      run: (t) => {
        const row = t && t.closest ? t.closest('[data-session-id]') : document.querySelector('#sessions-list .session-row');
        const btn = row ? row.querySelector('[data-session-row-export]') : null;
        if (btn) btn.click();
        else { const all = $('btn-sessions-export-all'); if (all) all.click(); else toast('Point at a session first.', 'error'); }
      } },
    // Same as routine.refresh: the modules panel reloads when its section scrolls into view.
    { id: 'modules.refresh', label: 'Go to Modules and reload the list', group: 'Modules', context: 'modules', keys: ['mod+alt+o'],
      run: () => navTo(['modules']) },
    { id: 'module.open', label: 'Open this module\'s page', group: 'Modules', context: 'modules', keys: [],
      run: (t) => {
        const btn = t && t.closest ? t.closest('[data-open]') : null;
        if (btn) btn.click(); else toast('Point at a module card first.', 'error');
      } },
    { id: 'module.act', label: 'Run this module\'s action (install, update, hub)', group: 'Modules', context: 'modules', keys: [],
      run: (t) => {
        const btn = t && t.closest ? t.closest('[data-act]') : null;
        if (btn) btn.click(); else toast('Point at one of the module\'s buttons first.', 'error');
      } },
    { id: 'kanban.refresh', label: 'Refresh the board', group: 'Kanban', context: 'kanban', keys: ['mod+alt+shift+k'],
      run: () => call('refreshKanban') },
    { id: 'kanban.card.copy', label: 'Copy this card\'s text', group: 'Kanban', context: 'kanban', keys: [],
      run: (t) => {
        const card = t && t.closest ? t.closest('#kanban-columns .card') : null;
        if (!card) { toast('Point at a card first.', 'error'); return; }
        copy((card.firstElementChild && card.firstElementChild.textContent || '').trim(), 'Card');
      } },
    { id: 'kanban.card.delete', label: 'Delete this card', group: 'Kanban', context: 'kanban', keys: [],
      run: (t) => {
        const btn = t && t.closest ? t.closest('[data-kanban-delete]') : null;
        if (btn) confirmThen('Delete this card?', () => btn.click());
        else toast('Point at a card first.', 'error');
      } },
  ];

  const byId = {};
  ACTIONS.forEach((a) => { byId[a.id] = a; });

  window.abpActions = {
    list: ACTIONS,
    groups: ACTIONS.reduce((acc, a) => {
      if (!acc[a.group]) acc[a.group] = [];
      acc[a.group].push(a);
      return acc;
    }, {}),
    get: (id) => byId[id] || null,
    has: (id) => Object.prototype.hasOwnProperty.call(byId, id),
    run: (id, target) => {
      const action = byId[id];
      if (!action) { toast(`No action called ${id}.`, 'error'); return false; }
      try {
        const out = action.run(target);
        if (out && typeof out.catch === 'function') out.catch((e) => toast(String((e && e.message) || e), 'error'));
        return true;
      } catch (e) {
        toast(`${action.label} failed: ${(e && e.message) || e}`, 'error');
        return false;
      }
    },
  };

  // Rows remember themselves as the pointer moves over them, so a global shortcut knows which
  // bot card or cell row "this bot" meant. One delegated listener for the whole page.
  document.addEventListener('pointerover', (ev) => {
    const t = ev.target;
    if (!t || !t.closest) return;
    const bot = t.closest('.botcard');
    if (bot) lastTouched.bot = bot;
    const cell = t.closest('#diag-cells-tbody tr');
    if (cell) lastTouched.cell = cell;
  }, true);
})(typeof api === 'function' ? api : null);