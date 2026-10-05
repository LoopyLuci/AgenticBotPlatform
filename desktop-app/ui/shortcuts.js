// The keyboard layer: chord resolution, the help overlay, the command palette, right-click menus,
// server-persisted custom bindings, and the bridge to the desktop app's OS-level global shortcuts.
//
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app
// (desktop-app/ui/); tests/test_shortcuts.py fails if the two differ. It reads its actions from
// action-registry.js and uses the page's own api(), esc() and showToast(). All the styling is
// injected at runtime, like every other panel, because each page's CSP forbids inline script.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;

  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s)
    : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));

  const IS_MAC = /Mac|iPhone|iPad/.test((navigator.platform || '') + (navigator.userAgent || ''));
  const MOD_LABEL = IS_MAC ? '\u2318' : 'Ctrl';
  const ALT_LABEL = IS_MAC ? '\u2325' : 'Alt';
  const SHIFT_LABEL = IS_MAC ? '\u21e7' : 'Shift';

  // Chords the browser, the OS or ABP itself already owns. Nothing here is ever bound, and a
  // custom binding that lands on one is refused rather than silently breaking the browser.
  const RESERVED = [
    'mod+t', 'mod+n', 'mod+w', 'mod+q', 'mod+l', 'mod+r', 'mod+s', 'mod+o', 'mod+p', 'mod+f',
    'mod+shift+n', 'mod+shift+t', 'mod+shift+w', 'mod+shift+q', 'mod+shift+s', 'mod+shift+p',
    'mod+alt+l', 'mod+alt+d', 'mod+alt+f4', 'mod+tab', 'mod+space', 'f5', 'f6', 'f11', 'f12',
    'alt+tab', 'alt+f4', 'ctrl+shift+i', 'ctrl+shift+j', 'ctrl+shift+c', 'escape',
  ];
  // Token by token: a sequence only needs one step to be somebody else's (`g` then Ctrl+T is
  // still Ctrl+T). Mirrored by shortcuts_api.is_reserved.
  const isReserved = (chord) => String(chord || '').split(' ').some((t) => RESERVED.includes(t));

  // ---- chords -----------------------------------------------------------------------------------
  // A chord is space-separated tokens; each token is modifiers joined by + and one key. `mod` is
  // Ctrl everywhere except macOS, where it is Cmd. Stored and compared in the canonical lowercase
  // form so a saved binding means the same thing on every machine.
  const KEY_ALIASES = {
    esc: 'escape', return: 'enter', spacebar: 'space', up: 'arrowup', down: 'arrowdown',
    left: 'arrowleft', right: 'arrowright', del: 'delete', ins: 'insert', '`': '`',
  };
  const keyName = (ev) => {
    let k = ev.key;
    if (!k) return '';
    if (k === ' ') k = 'space';
    k = k.length === 1 ? k.toLowerCase() : k.toLowerCase();
    return KEY_ALIASES[k] || k;
  };
  const tokenFor = (ev) => {
    const mods = [];
    if (IS_MAC ? ev.metaKey : ev.ctrlKey) mods.push('mod');
    if (!IS_MAC && ev.metaKey) mods.push('meta');
    if (IS_MAC && ev.ctrlKey) mods.push('ctrl');
    if (ev.altKey) mods.push('alt');
    if (ev.shiftKey) mods.push('shift');
    const key = keyName(ev);
    if (!key || ['control', 'meta', 'alt', 'shift'].includes(key)) return '';
    return [...mods, key].join('+');
  };
  const prettyKey = (key) => {
    if (key === 'space') return 'Space';
    if (key === 'arrowup') return '\u2191';
    if (key === 'arrowdown') return '\u2193';
    if (key === 'arrowleft') return '\u2190';
    if (key === 'arrowright') return '\u2192';
    if (key === 'enter') return 'Enter';
    if (key === 'escape') return 'Esc';
    if (key === 'delete') return 'Del';
    if (key === '`') return '`';
    return key.length === 1 ? key.toUpperCase() : key.charAt(0).toUpperCase() + key.slice(1);
  };
  const prettyChord = (chord) => chord.split(' ').map((tok) => tok.split('+').map((part) => {
    if (part === 'mod') return MOD_LABEL;
    if (part === 'alt') return ALT_LABEL;
    if (part === 'shift') return SHIFT_LABEL;
    return prettyKey(part);
  }).join(IS_MAC ? '' : '+')).join(' then ');

  // ---- bindings: defaults, then whatever the owner saved server-side --------------------------
  const registry = () => window.abpActions;
  const bindings = {};         // action id -> chord (canonical) or '' for none
  let loading = false;

  function canonical(chord) {
    return String(chord || '').trim().toLowerCase().split(/\s+/)
      .map((tok) => tok.split('+')
        .map((p) => (p === 'cmd' || p === 'ctrl' ? 'mod' : p === 'opt' || p === 'option' ? 'alt' : p === 'esc' ? 'escape' : p))
        .filter(Boolean).join('+')).filter(Boolean).join(' ');
  }
  function chordFor(action) {
    const saved = bindings[action.id];
    return saved === undefined ? ((action.keys || [])[0] || '') : saved;
  }
  function extraChords(action) {
    const saved = bindings[action.id];
    if (saved !== undefined) return saved ? [saved] : [];
    return (action.keys || []).slice(1).map(canonical);
  }

  // chord -> the actions on it, rebuilt whenever a binding changes. Two actions may share a chord
  // when they can never both be live: Mod+Enter sends from the Chat box, the Server Chat box and
  // the Support Bot box alike, which is the convention people expect from all three. When the
  // chord fires, the action whose context is the screen you are actually looking at wins; a
  // genuine clash (same context, or one of them global) is reported by conflicts() instead.
  let table = new Map();
  function rebuild() {
    table = new Map();
    for (const action of registry().list) {
      for (const chord of [chordFor(action), ...extraChords(action)]) {
        if (!chord) continue;
        if (!table.has(chord)) table.set(chord, []);
        table.get(chord).push(action);
      }
    }
  }
  // True when the two actions could both be live at once: two screen-scoped actions on the same
  // chord are fine only if they are on different screens.
  const overlaps = (a, b) => a.context === b.context || a.context === 'global' || b.context === 'global';
  const COMPOSER_OF = {
    'chat.send': ['chat-input'],
    'serverchat.send': ['serverchat-input'],
    'support.send': ['support-input', 'support-bot-input'],
  };
  function resolve(chord, target) {
    const candidates = table.get(chord) || [];
    if (candidates.length <= 1) return candidates[0] || null;
    const here = currentSection();
    const local = candidates.filter((a) => a.context === here);
    const pool = local.length ? local : candidates.filter((a) => a.context === 'global');
    // Still ambiguous: the composer the keystroke came from decides.
    if (target && target.id) {
      const owner = pool.find((a) => (COMPOSER_OF[a.id] || []).includes(target.id));
      if (owner) return owner;
    }
    return pool[0] || candidates[0];
  }
  // A real conflict is two actions that could both be live on one chord: two actions scoped to the
  // same screen, or either one global. Two screen-scoped actions on different screens sharing a
  // chord (the three Mod+Enter composers) is not a conflict - the engine picks by screen.
  const conflicts = () => {
    const byChord = new Map();
    for (const action of registry().list) {
      for (const chord of [chordFor(action), ...extraChords(action)]) {
        if (!chord) continue;
        if (!byChord.has(chord)) byChord.set(chord, []);
        byChord.get(chord).push(action);
      }
    }
    const out = [];
    byChord.forEach((actions, chord) => {
      for (let i = 0; i < actions.length; i++) {
        for (let j = i + 1; j < actions.length; j++) {
          if (overlaps(actions[i], actions[j])) out.push({ chord, actions: [actions[i].id, actions[j].id] });
        }
      }
    });
    return out;
  };

  // ---- where am I on this one long page? -------------------------------------------------------
  function currentSection() {
    const hash = (location.hash || '').replace('#', '');
    if (hash && document.getElementById(hash)) return hash;
    const main = document.querySelector('main');
    if (main) {
      const y = (main.getBoundingClientRect ? main.getBoundingClientRect().top : 0) + 120;
      let best = null;
      document.querySelectorAll('section[id]').forEach((s) => {
        const top = s.getBoundingClientRect().top;
        if (top <= y && (!best || top > best.getBoundingClientRect().top)) best = s;
      });
      if (best) return best.id;
    }
    return 'overview';
  }
  const typingSomewhere = (target) => {
    const el = target || document.activeElement;
    if (!el) return false;
    const tag = (el.tagName || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable === true;
  };

  // ---- the overlay shell ------------------------------------------------------------------------
  function css() {
    if ($('sc-css')) return;
    const s = document.createElement('style');
    s.id = 'sc-css';
    s.textContent = `.sc-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:flex-start;justify-content:center;z-index:8000;padding:8vh 12px}
.sc-panel{background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:0 18px 50px rgba(0,0,0,.35);width:min(640px,100%);max-height:82vh;display:flex;flex-direction:column;overflow:hidden}
.sc-panel.wide{width:min(880px,100%)}
.sc-input{width:100%;border:none;border-bottom:1px solid var(--line);background:transparent;color:var(--ink);font:inherit;font-size:15px;padding:14px 16px;outline:none}
.sc-list{overflow-y:auto;padding:6px;margin:0;list-style:none}
.sc-opt{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;cursor:pointer;font-size:13px}
.sc-opt[aria-selected="true"]{background:var(--accent-soft,var(--surface-2));outline:1px solid var(--accent)}
.sc-opt .sc-label{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sc-opt .sc-group{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.sc-opt.recent .sc-label::after{content:" · recent";color:var(--muted);font-size:11px}
.sc-keys{font-family:var(--font-mono,monospace);font-size:11px;color:var(--muted);white-space:nowrap}
.sc-empty{padding:18px;color:var(--muted);font-size:13px;text-align:center}
.sc-head{display:flex;align-items:baseline;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line)}
.sc-head h2{margin:0;font-size:14px}
.sc-head p{margin:0;color:var(--muted);font-size:12px}
.sc-sec{padding:10px 16px 4px;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.sc-row{display:flex;align-items:center;gap:10px;padding:6px 16px;font-size:12.5px;border-bottom:1px solid var(--line)}
.sc-row .sc-label{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sc-row .sc-id{color:var(--muted);font-family:var(--font-mono,monospace);font-size:11px}
.sc-row button{background:none;border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:2px 8px;font:inherit;font-size:11.5px;cursor:pointer}
.sc-row button:hover{border-color:var(--accent)}
.sc-row button.recording{border-color:var(--critical);color:var(--critical)}
.sc-note{padding:8px 16px;color:var(--muted);font-size:11.5px}
.sc-warn{color:var(--critical)}
.sc-foot{display:flex;gap:8px;align-items:center;padding:10px 16px;border-top:1px solid var(--line)}
.sc-foot .spacer{flex:1}
.sc-btn{background:var(--surface-2);border:1px solid var(--line);color:var(--ink);border-radius:8px;padding:5px 11px;font:inherit;font-size:12.5px;cursor:pointer}
.sc-btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.sc-btn:disabled{opacity:.5;cursor:not-allowed}
#sc-menu{position:fixed;z-index:8100;background:var(--surface);border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.3);padding:4px;min-width:190px}
#sc-menu[hidden]{display:none}
.sc-mi{display:flex;align-items:center;gap:10px;width:100%;text-align:left;background:none;border:none;color:var(--ink);font:inherit;font-size:12.5px;padding:6px 9px;border-radius:6px;cursor:pointer}
.sc-mi:hover,.sc-mi:focus-visible{background:var(--accent-soft,var(--surface-2));outline:none}
.sc-mi .sc-keys{margin-left:auto}`;
    document.head.appendChild(s);
  }

  let overlay = null;
  function closeOverlay() {
    if (!overlay) return;
    const { backdrop, onClose } = overlay;
    overlay = null;
    document.removeEventListener('keydown', overlayKey, true);
    if (onClose) onClose();
    backdrop.remove();
    if (overlayRestoreFocus && overlayRestoreFocus.isConnected) overlayRestoreFocus.focus();
    overlayRestoreFocus = null;
  }
  let overlayRestoreFocus = null;

  function openOverlay(build) {
    css();
    closeOverlay();
    const backdrop = document.createElement('div');
    backdrop.className = 'sc-backdrop';
    const panel = document.createElement('div');
    panel.className = 'sc-panel';
    backdrop.appendChild(panel);
    backdrop.addEventListener('mousedown', (ev) => { if (ev.target === backdrop) closeOverlay(); });
    document.body.appendChild(backdrop);
    overlayRestoreFocus = document.activeElement;
    overlay = { backdrop, panel };
    document.addEventListener('keydown', overlayKey, true);
    build(panel);
    return panel;
  }
  function overlayKey(ev) {
    if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); closeOverlay(); return; }
    if (overlay && overlay.onKey) overlay.onKey(ev);
  }

  // ---- the command palette ------------------------------------------------------------------------
  // Fuzzy: every character of the query appears in order, so "rln" finds "Run routine now".
  const RECENTS_KEY = 'abp-recent-actions';
  const readRecents = () => {
    try { return JSON.parse(localStorage.getItem(RECENTS_KEY) || '[]').filter((id) => registry().has(id)); }
    catch (_e) { return []; }
  };
  const noteRecent = (id) => {
    try {
      const next = [id, ...readRecents().filter((r) => r !== id)].slice(0, 8);
      localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
    } catch (_e) { /* private mode: recents are a nicety */ }
  };
  const fuzzy = (needle, hay) => {
    const n = needle.toLowerCase().replace(/\s+/g, '');
    const h = hay.toLowerCase();
    let i = 0;
    let score = 0;
    let streak = 0;
    for (let j = 0; j < h.length && i < n.length; j++) {
      if (h[j] === n[i]) { i++; streak++; score += 2 + streak; }
      else streak = 0;
    }
    return i === n.length ? score : -1;
  };

  function openPalette() {
    const panel = openOverlay((p) => {
      p.classList.add('wide');
      const input = document.createElement('input');
      input.className = 'sc-input';
      input.type = 'text';
      input.placeholder = 'Type a command, a screen or an action id…';
      input.setAttribute('role', 'combobox');
      input.setAttribute('aria-expanded', 'true');
      input.setAttribute('aria-controls', 'sc-palette-list');
      input.setAttribute('aria-label', 'Search every action');
      const list = document.createElement('ul');
      list.className = 'sc-list';
      list.id = 'sc-palette-list';
      list.setAttribute('role', 'listbox');
      list.setAttribute('aria-label', 'Actions');
      const hint = document.createElement('div');
      hint.className = 'sc-note';
      p.append(input, list, hint);

      let options = [];
      let active = 0;

      const render = () => {
        const q = input.value.trim();
        const all = registry().list.filter((a) => a.context === 'global' || a.context === currentSection() || isReachable(a));
        const recents = readRecents();
        const scored = all.map((a) => ({
          a,
          s: Math.max(
            q ? fuzzy(q, a.label) : -1,
            q ? fuzzy(q, a.id) : -1,
            q ? fuzzy(q, a.group) : -1,
          ),
        }));
        const hits = scored.filter((x) => x.s >= 0).sort((x, y) => y.s - x.s);
        const recent = recents
          .map((id) => scored.find((x) => x.a.id === id))
          .filter((x) => x && (!q || x.s >= 0));
        options = [...recent.filter((r) => !hits.some((h) => h.a === r.a)), ...hits];
        if (!q) {
          // With no query, the palette is a menu of everything, recents first.
          const recentIds = recents.map((id) => registry().get(id)).filter(Boolean);
          const rest = scored.map((x) => x.a).filter((a) => !recents.includes(a.id));
          options = [...recentIds, ...rest];
        }
        active = 0;
        list.innerHTML = options.length ? options.map((o, i) => {
          const chord = prettyChord(chordFor(o.a));
          return `<li class="sc-opt${recents.includes(o.a.id) ? ' recent' : ''}" role="option" id="sc-opt-${i}" aria-selected="${i === active}" data-i="${i}">
            <span class="sc-group">${E(o.a.group)}</span><span class="sc-label">${E(o.a.label)}</span>
            <span class="sc-keys">${chord ? E(chord) : ''}</span></li>`;
        }).join('') : '<li class="sc-empty">Nothing matches that.</li>';
        input.setAttribute('aria-activedescendant', options.length ? 'sc-opt-0' : '');
        hint.textContent = options.length
          ? `\u2191\u2193 to move, Enter to run, Esc to close. ${options.length} action(s)${recents.length ? `, ${recents.length} recently used.` : '.'}`
          : '';
      };

      const move = (delta) => {
        if (!options.length) return;
        active = (active + delta + options.length) % options.length;
        Array.from(list.children).forEach((li, i) => li.setAttribute('aria-selected', String(i === active)));
        const el = list.children[active];
        if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
        input.setAttribute('aria-activedescendant', `sc-opt-${active}`);
      };
      const fire = () => {
        const chosen = options[active];
        closeOverlay();
        if (!chosen) return;
        noteRecent(chosen.a.id);
        registry().run(chosen.a.id);
      };

      overlay.onKey = (ev) => {
        if (ev.key === 'ArrowDown') { ev.preventDefault(); ev.stopPropagation(); move(1); }
        else if (ev.key === 'ArrowUp') { ev.preventDefault(); ev.stopPropagation(); move(-1); }
        else if (ev.key === 'Home') { ev.preventDefault(); move(-options.length); }
        else if (ev.key === 'End') { ev.preventDefault(); move(options.length); }
        else if (ev.key === 'Enter') { ev.preventDefault(); ev.stopPropagation(); fire(); }
        else if (ev.key === 'Tab') { ev.preventDefault(); ev.stopPropagation(); move(ev.shiftKey ? -1 : 1); }
      };
      list.addEventListener('click', (ev) => {
        const li = ev.target.closest('[data-i]');
        if (!li) return;
        active = Number(li.dataset.i);
        fire();
      });
      list.addEventListener('mousemove', (ev) => {
        const li = ev.target.closest('[data-i]');
        if (!li || Number(li.dataset.i) === active) return;
        active = Number(li.dataset.i);
        Array.from(list.children).forEach((c, i) => c.setAttribute('aria-selected', String(i === active)));
        input.setAttribute('aria-activedescendant', `sc-opt-${active}`);
      });
      input.addEventListener('input', render);
      render();
      input.focus();
    });
    return panel;
  }

  // An action whose screen is not on this build still belongs in the palette (so a Tauri global
  // shortcut always has something to run) - it just says so when it runs.
  const isReachable = (action) => !action.context || action.context === 'global' || !!document.getElementById(action.context);

  // ---- the help overlay: every shortcut for this screen, plus the customiser ------------------
  function openHelp() {
    openOverlay((p) => {
      p.classList.add('wide');
      const here = currentSection();
      const head = document.createElement('div');
      head.className = 'sc-head';
      head.innerHTML = `<h2>Keyboard shortcuts</h2><p>for ${E(here)} \u00b7 ${E(IS_MAC ? 'macOS' : 'Windows/Linux')} \u00b7 saved on the server</p>`;
      p.appendChild(head);

      const mine = registry().list.filter((a) => a.context === 'global' || a.context === here);
      let editing = false;
      let recording = null;

      const rowsFor = (list) => list.map((a) => {
        const chord = chordFor(a);
        return `<div class="sc-row" data-id="${E(a.id)}">
          <span class="sc-label">${E(a.label)}</span>
          <span class="sc-id">${E(a.id)}</span>
          <span class="sc-keys">${chord ? E(prettyChord(chord)) : '\u2014'}</span>
          ${editing ? `<button type="button" data-rec="${E(a.id)}" class="${recording === a.id ? 'recording' : ''}" aria-label="Change the shortcut for ${E(a.label)}">${recording === a.id ? 'press keys\u2026' : 'change'}</button><button type="button" data-clr="${E(a.id)}" aria-label="Remove the shortcut for ${E(a.label)}">clear</button>` : ''}
        </div>`;
      }).join('');

      const body = document.createElement('div');
      body.style.overflow = 'auto';
      const conflictNote = document.createElement('div');
      conflictNote.className = 'sc-note';

      const draw = () => {
        const clashes = conflicts();
        conflictNote.innerHTML = clashes.length
          ? `<span class="sc-warn">${clashes.length} binding conflict(s): ${clashes.map((c) => `${E(c.chord)} (${E(c.actions.join(' and '))})`).join('; ')}. Fix one and both work again.</span>`
          : 'Bindings are stored with ABP\'s config, so they follow you to any browser or device signed in to this ABP.';
        const groups = {};
        mine.forEach((a) => { if (!groups[a.group]) groups[a.group] = []; groups[a.group].push(a); });
        body.innerHTML = Object.entries(groups).map(([group, list]) =>
          `<div class="sc-sec">${E(group)}</div>${rowsFor(list)}`).join('')
          + (editing ? rowsFor(registry().list.filter((a) => !mine.includes(a))) : '');
      };
      draw();
      p.append(body, conflictNote);

      const foot = document.createElement('div');
      foot.className = 'sc-foot';
      const editBtn = document.createElement('button');
      editBtn.className = 'sc-btn primary';
      editBtn.textContent = 'Customise';
      editBtn.addEventListener('click', () => { editing = !editing; recording = null; editBtn.textContent = editing ? 'Done' : 'Customise'; draw(); });
      const resetBtn = document.createElement('button');
      resetBtn.className = 'sc-btn';
      resetBtn.textContent = 'Reset all to defaults';
      resetBtn.addEventListener('click', async () => {
        resetBtn.disabled = true;
        await save({});
        draw();
        resetBtn.disabled = false;
        toast('Shortcuts reset to their defaults.', 'success');
      });
      const close = document.createElement('span');
      close.className = 'spacer';
      const done = document.createElement('button');
      done.className = 'sc-btn';
      done.textContent = 'Close';
      done.addEventListener('click', closeOverlay);
      foot.append(editBtn, resetBtn, close, done);
      p.appendChild(foot);

      // Recording: one keypress writes the binding. Validated here so the conflict is caught
      // while the person is looking at it, and again by the API (which is the real gate).
      body.addEventListener('click', async (ev) => {
        const rec = ev.target.closest('[data-rec]');
        if (rec) { recording = recording === rec.dataset.rec ? null : rec.dataset.rec; draw(); return; }
        const clr = ev.target.closest('[data-clr]');
        if (clr) { await save({ ...bindings, [clr.dataset.clr]: '' }); rebuild(); draw(); }
      });
      const onRecord = async (ev) => {
        if (!editing || !recording) return;
        const id = recording;
        ev.preventDefault();
        ev.stopPropagation();
        const chord = tokenFor(ev);
        if (!chord) return;
        if (isReserved(chord)) { toast(`${prettyChord(chord)} belongs to the browser or the OS.`, 'error'); return; }
        // The server has the final say (it is the gate that protects the stored set); this asks it
        // first so a conflict is answered while the person is looking at it. A failed ask is not
        // fatal: the PUT below would refuse it anyway, with the same message.
        try {
          const verdict = await api('/api/shortcuts/check', { method: 'POST', body: JSON.stringify({ chord, bindings }) });
          if (verdict && verdict.ok === false) { toast(verdict.reason || 'That shortcut is taken.', 'error'); return; }
        } catch (_e) { /* offline or no permission: fall through to the client-side checks */ }
        const clash = registry().list.find((a) => a.id !== id
          && [chordFor(a), ...extraChords(a)].includes(chord)
          && overlaps(a, registry().get(id)));
        if (clash) { toast(`${prettyChord(chord)} is already ${clash.label}.`, 'error'); return; }
        recording = null;
        await save({ ...bindings, [id]: chord });
        rebuild();
        draw();
      };
      document.addEventListener('keydown', onRecord, true);
      overlay.onClose = () => document.removeEventListener('keydown', onRecord, true);
    });
  }

  // ---- persistence ------------------------------------------------------------------------------
  async function load() {
    if (loading) return;
    loading = true;
    try {
      const data = await api('/api/shortcuts');
      const saved = (data && data.bindings) || {};
      Object.keys(bindings).forEach((k) => delete bindings[k]);
      Object.keys(saved).forEach((k) => {
        if (registry().has(k)) bindings[k] = canonical(saved[k]);
      });
    } catch (e) {
      toast(`Shortcuts could not be loaded, using the defaults: ${(e && e.message) || e}`, 'error');
    } finally {
      loading = false;
      rebuild();
    }
  }
  async function save(next) {
    try {
      const saved = await api('/api/shortcuts', { method: 'PUT', body: JSON.stringify({ bindings: next }) });
      // Take the server's version, not the one we sent: it normalises the chords (cmd -> mod,
      // Esc -> escape) and rejects the ones it will not take, so what we hold is what was stored.
      const stored = (saved && saved.bindings) || next;
      Object.keys(bindings).forEach((k) => delete bindings[k]);
      Object.keys(stored).forEach((k) => { bindings[k] = canonical(stored[k]); });
      rebuild();
      return stored;
    } catch (e) {
      toast(`That shortcut was not saved: ${(e && e.message) || e}`, 'error');
      return null;
    }
  }

  // ---- the keyboard itself ------------------------------------------------------------------------
  let pending = '';          // a `g`-style chord prefix waiting for its second key
  let pendingTimer = null;
  const clearPending = () => { pending = ''; if (pendingTimer) { clearTimeout(pendingTimer); pendingTimer = null; } };
  const isPrefix = (chord) => registry().list.some((a) => [chordFor(a), ...extraChords(a)].some((c) => c && c !== chord && c.startsWith(chord + ' ')));

  function onKeyDown(ev) {
    if (!registry() || overlay) return;         // the overlay owns the keyboard while it is up
    if (ev.defaultPrevented) return;
    if (ev.ctrlKey && ['c', 'v', 'x', 'a'].includes(String(ev.key).toLowerCase()) && !ev.altKey && !ev.shiftKey) return;
    const target = ev.target;
    const token = tokenFor(ev);
    if (!token) return;
    const typing = typingSomewhere(target);

    if (pending) {
      const chord = `${pending} ${token}`;
      clearPending();
      const next = resolve(chord, target);
      if (next) {
        ev.preventDefault();
        run(next, target);
        return;
      }
      // Not a chord after all: fall through so the key does its normal job.
    }

    const action = resolve(token, target);
    if (!action) return;
    if (typing && !action.whileTyping) return;
    // A send-on-Mod+Enter means "send" only for the composer it belongs to, so it never hijacks
    // somebody else's Enter inside some other text field.
    if (typing && (COMPOSER_OF[action.id] || []).length && !(COMPOSER_OF[action.id] || []).includes(target && target.id)) return;

    ev.preventDefault();
    ev.stopPropagation();
    if (isPrefix(token)) {
      pending = token;
      if (pendingTimer) clearTimeout(pendingTimer);
      pendingTimer = setTimeout(clearPending, 1500);
      return;
    }
    run(action, target);
  }
const run = (action, target) => {
    noteRecent(action.id);
    registry().run(action.id, target);
  };

  // ---- right-click menus ---------------------------------------------------------------------------
  // Every one of these is a place on the page where a menu is more useful than nothing. Each maps
  // a selector to the actions offered there; nothing fires inside a text field, so the browser's
  // own copy/paste menu is never replaced.
  const MENUS = [
    { sel: '.botcard', items: ['bot.chat', 'bot.toggle', 'bot.restart', 'bot.agent', 'bot.edit', 'bot.delete'] },
    { sel: '#chat-panels .chat-row', items: ['chat.message.copy', 'chat.message.quote', 'chat.message.retry', 'chat.message.delete'] },
    { sel: '#serverchat-window .chat-row', items: ['serverchat.message.copy', 'serverchat.message.delete'] },
    { sel: '#files-tbody tr', items: ['file.download', 'file.copyPath', 'files.refresh'] },
    { sel: '#mdp-root .mdp-card', items: ['module.act', 'module.open', 'modules.refresh'] },
    { sel: '#diag-cells-tbody tr', items: ['process.kill', 'processes.open'] },
    { sel: '#log-lines', items: ['logs.copyLine', 'logs.copyAll'] },
    { sel: '#rt-root tbody tr', items: ['routine.run', 'routine.refresh'] },
    { sel: '#sessions-list .session-row', items: ['sessions.export'] },
    { sel: '#activity-list .activity-row', items: ['activity.copy', 'activity.clear'] },
    { sel: '#kanban-columns .card', items: ['kanban.card.copy', 'kanban.card.delete', 'kanban.refresh'] },
  ];

  let menu = null;
  function closeMenu(restoreFocus) {
    if (!menu) return;
    const { el, opener } = menu;
    menu = null;
    el.remove();
    document.removeEventListener('keydown', menuKey, true);
    if (restoreFocus && opener && opener.isConnected) opener.focus();
  }
  const itemsFor = (el) => {
    for (const m of MENUS) {
      if (el.closest(m.sel)) return { def: m, items: m.items.map((id) => registry().get(id)).filter(Boolean) };
    }
    return null;
  };
  const openMenu = (el, x, y, opener) => {
    const found = itemsFor(el);
    if (!found || !found.items.length) return false;
    closeMenu(false);
    css();
    const box = document.createElement('div');
    box.id = 'sc-menu';
    box.setAttribute('role', 'menu');
    box.setAttribute('aria-label', `${found.def.items[0] ? registry().get(found.def.items[0]).group : 'Row'} actions`);
    box.innerHTML = found.items.map((a, i) => {
      const chord = prettyChord(chordFor(a));
      return `<button type="button" class="sc-mi" role="menuitem" tabindex="${i === 0 ? 0 : -1}" data-id="${E(a.id)}"
        aria-label="${E(a.label)}${chord ? ` (${E(chord)})` : ''}"><span>${E(a.label)}</span>${chord ? `<span class="sc-keys">${E(chord)}</span>` : ''}</button>`;
    }).join('');
    document.body.appendChild(box);
    // Keep the menu on screen: flip it back inside rather than letting it run off the edge.
    const rect = box.getBoundingClientRect();
    const left = Math.max(4, Math.min(x, window.innerWidth - rect.width - 4));
    const top = Math.max(4, Math.min(y, window.innerHeight - rect.height - 4));
    box.style.left = `${left}px`;
    box.style.top = `${top}px`;
    const buttons = () => Array.from(box.querySelectorAll('.sc-mi'));
    buttons().forEach((b, i) => {
      b.addEventListener('click', () => { closeMenu(false); registry().run(b.dataset.id, el); });
      b.addEventListener('keydown', (ev) => {
        const all = buttons();
        const at = all.indexOf(b);
        if (ev.key === 'ArrowDown') { ev.preventDefault(); all[(at + 1) % all.length].focus(); }
        else if (ev.key === 'ArrowUp') { ev.preventDefault(); all[(at - 1 + all.length) % all.length].focus(); }
        else if (ev.key === 'Home') { ev.preventDefault(); all[0].focus(); }
        else if (ev.key === 'End') { ev.preventDefault(); all[all.length - 1].focus(); }
      });
    });
    box.addEventListener('keydown', (ev) => {
      if (ev.key === 'Tab' || ev.key === 'Escape' && document.activeElement === ev.target) {
        ev.preventDefault();
        closeMenu(true);
      }
    });
    menu = { el: box, opener };
    document.addEventListener('keydown', menuKey, true);
    return true;
  };
  // Escape anywhere closes; Enter/Space on a button is the browser's own click.
  const menuKey = (ev) => {
    if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); closeMenu(true); }
  };

  function onContextMenu(ev) {
    // Never take the native menu away from someone editing text.
    if (typingSomewhere(ev.target)) return;
    const found = itemsFor(ev.target);
    if (!found) return;
    ev.preventDefault();
    openMenu(ev.target, ev.clientX, ev.clientY, document.activeElement);
  }
  function onContextKey(ev) {
    // Shift+F10 and the Menu key open the same menu on whatever has focus - the keyboard-only path.
    if (!(ev.key === 'F10' && ev.shiftKey) && ev.key !== 'ContextMenu') return;
    const el = document.activeElement;
    if (!el || !itemsFor(el)) return;
    ev.preventDefault();
    const rect = el.getBoundingClientRect();
    openMenu(el, rect.left, rect.bottom, el);
  }
  function onDismiss(ev) {
    if (menu && ev.target === menu.el) return;
    if (menu && ev.type === 'mousedown' && menu.el.contains(ev.target)) return;
    closeMenu(false);
  }

  // ---- the desktop app's OS-level global shortcuts -------------------------------------------------
  // The Rust side registers GLOBAL shortcuts and emits {"action": "<id>"} on `abp://action`.
  // Running it through the registry means the same conflict checks, the same confirmation
  // dialogs and the same "row I meant" logic as a key pressed in the window.
  async function listenForTauriActions() {
    if (!window.__TAURI__ || !window.__TAURI__.event || typeof window.__TAURI__.event.listen !== 'function') return;
    try {
      await window.__TAURI__.event.listen('abp://action', (event) => {
        const id = event && event.payload && event.payload.action;
        if (!id) return;
        if (!registry().has(id)) { toast(`The desktop app asked for an action ABP does not have: ${id}`, 'error'); return; }
        run(registry().get(id), document.activeElement);
      });
    } catch (e) {
      console.warn('ABP: could not subscribe to abp://action', e);
    }
  }

  // ---- boot ------------------------------------------------------------------------------------------
  function init() {
    if (!window.abpActions) return;
    css();
    rebuild();
    document.addEventListener('keydown', onKeyDown, true);
    document.addEventListener('contextmenu', onContextMenu, true);
    document.addEventListener('keydown', onContextKey, true);
    document.addEventListener('mousedown', onDismiss, true);
    document.addEventListener('scroll', () => closeMenu(false), true);
    window.addEventListener('hashchange', () => closeMenu(false));
    window.addEventListener('blur', () => closeMenu(false));
    const btn = $('btn-shortcuts');
    if (btn) btn.addEventListener('click', () => (overlay ? closeOverlay() : openHelp()));
    const search = $('btn-command-palette');
    if (search) search.addEventListener('click', () => openPalette());
    load();
    listenForTauriActions();
  }

  window.abpShortcuts = {
    openPalette,
    openHelp,
    closeOverlay,
    closeMenu,
    currentSection,
    bindings: () => ({ ...bindings }),
    // Exposed so the tests (and a curious console) can ask the same questions the customiser asks.
    chords: () => {
      const out = {};
      registry().list.forEach((a) => { out[a.id] = chordFor(a); });
      return out;
    },
    conflicts,
    isReserved,
    canonical,
    prettyChord,
    tokenFor,
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);