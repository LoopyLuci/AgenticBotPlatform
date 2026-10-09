/* Module host: renders the page from the runtime's UI manifest (docs/runtime.md, "Events and the UI").

   Shared by the web dashboard and the desktop app (the two copies are identical). It
   - fetches GET /api/ui/manifest (through the page's API base and token, so it works from the desktop shell too),
   - shows an element carrying data-unit="<unit>" (nav links and sections) only while that unit has a live
     `builtin` panel, and creates / removes nav links + sections for `script` panels,
   - gives panels a lifecycle: ABP.panels.define(id, {mount(root, ctx), unmount()}), where ctx.api, ctx.every and
     ctx.on register timers and listeners the host removes on unmount; older panels (a script that finds its own
     `<id>-root`) are wrapped: unmount removes the section and the script / style tags,
   - re-renders when the socket says {"type":"runtime.changed","generation":N} (dashboard.js / main.js call
     ABP.moduleHost.onEvent), and polls GET /api/runtime/events?since=N every 5 s while the socket is down.
   A backend without /api/ui/manifest (404) leaves the page exactly as it is. */
(function () {
  'use strict';
  if (typeof fetch !== 'function') return;
  var ABP = window.ABP = window.ABP || {};

  var POLL_MS = 5000;
  var generation = -1;
  var available = null;          // null = not asked yet, false = backend has no manifest, true = rendering
  var socketIsUp = false;
  var pollTimer = null;
  var inflight = null;
  var queued = false;
  var records = {};              // script panel id -> record
  var defs = {};                 // panel id -> definition from ABP.panels.define
  var counts = { timers: 0, listeners: 0 };
  var readyResolve;
  var ready = new Promise(function (r) { readyResolve = r; });

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function base() { return (typeof API_BASE !== 'undefined' && API_BASE) ? String(API_BASE) : ''; }
  function token() {
    try { if (typeof getToken === 'function') return getToken() || ''; } catch (_e) { /* fall through */ }
    return window.__ABP_TOKEN__ || '';
  }
  function url(path) { return /^https?:/.test(path) ? path : base() + path; }
  function rawFetch(path) {
    var h = {};
    var t = token();
    if (t) h['X-Dashboard-Token'] = t;
    return fetch(url(path), { headers: h });
  }

  // ------------------------------------------------------------------ lifecycle
  function makeCtx(rec) {
    return {
      id: rec.id, unit: rec.panel.unit, version: rec.panel.version, root: rec.root,
      api: function (path, opts) {
        if (typeof api === 'function') return api(path, opts);
        return rawFetch(path).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
      },
      every: function (ms, fn) {
        var h = setInterval(function () { try { var p = fn(); if (p && p.catch) p.catch(function () {}); } catch (_e) { /* a panel's tick reports its own errors */ } }, ms);
        counts.timers++;
        rec.timers.push(h);
        return function () { var i = rec.timers.indexOf(h); if (i >= 0) { rec.timers.splice(i, 1); clearInterval(h); counts.timers--; } };
      },
      on: function (target, ev, fn, opts) {
        target.addEventListener(ev, fn, opts);
        counts.listeners++;
        var entry = { target: target, ev: ev, fn: fn, opts: opts };
        rec.listeners.push(entry);
        return function () { var i = rec.listeners.indexOf(entry); if (i >= 0) { rec.listeners.splice(i, 1); target.removeEventListener(ev, fn, opts); counts.listeners--; } };
      }
    };
  }

  function stopPanel(rec) {
    if (rec.mounted && rec.def && typeof rec.def.unmount === 'function') {
      try { rec.def.unmount(); } catch (e) { if (window.console) console.warn('panel ' + rec.id + ' unmount failed', e); }
    }
    rec.mounted = false;
    rec.timers.splice(0).forEach(function (h) { clearInterval(h); counts.timers--; });
    rec.listeners.splice(0).forEach(function (l) { try { l.target.removeEventListener(l.ev, l.fn, l.opts); } catch (_e) { /* gone */ } counts.listeners--; });
    delete defs[rec.id];
    rec.def = null;
    rec.assets.splice(0).forEach(function (el) { if (el.parentNode) el.parentNode.removeChild(el); });
  }

  function mountPanel(rec) {
    var def = defs[rec.id];
    if (!def || rec.mounted || rec.dead) return;
    rec.def = def;
    rec.mounted = true;
    rec.root.innerHTML = '';
    try {
      var r = typeof def.mount === 'function' ? def.mount(rec.root, makeCtx(rec)) : null;
      if (r && r.catch) r.catch(function (e) { showError(rec, e); });
    } catch (e) { showError(rec, e); }
  }

  function showError(rec, e) {
    rec.root.innerHTML = '<p class="cardnote">This panel failed: ' + esc(e && e.message ? e.message : e) + '</p>';
  }

  ABP.panels = ABP.panels || {};
  ABP.panels.define = function (id, def) {
    defs[id] = def;
    var rec = records[id];
    if (rec && rec.loading) mountPanel(rec);   // define() ran while its own script was executing
  };

  // ------------------------------------------------------------------ nav + sections
  function sidenav() { return document.getElementById('sidenav'); }

  function makeNavLink(p) {
    var a = document.createElement('a');
    a.href = '#' + p.id;
    a.title = p.title || p.id;
    a.setAttribute('data-ext-panel', p.id);
    a.setAttribute('data-kw', String(p.title || p.id).toLowerCase());
    var nav = sidenav();
    var desktop = nav && nav.querySelector('.nav-txt b');
    a.innerHTML = '<span class="ic">' + esc(p.icon || '◆') + '</span>' +
      (desktop ? '<span class="nav-txt"><b>' + esc(p.title || p.id) + '</b><small>' + esc(p.group || '') + '</small></span>'
               : '<span class="nav-txt">' + esc(p.title || p.id) + '</span>');
    return a;
  }

  function makeSection(p) {
    var s = document.createElement('section');
    s.id = p.id;
    s.setAttribute('data-ext-panel', p.id);
    s.innerHTML = '<div class="sec-head"><h2>' + esc(p.title || p.id) + '</h2>' +
      (p.description ? '<span class="desc">' + esc(p.description) + '</span>' : '') + '</div>' +
      '<div id="' + esc(p.id) + '-root"><p class="cardnote">Loading…</p></div>';
    return s;
  }

  function addShell(rec) {
    var nav = sidenav();
    var main = document.querySelector('main');
    if (!nav || !main) return false;
    var p = rec.panel;
    var existing = document.getElementById(p.id);
    if (existing && !existing.hasAttribute('data-ext-panel')) return false;   // a page element owns this id
    rec.link = makeNavLink(p);
    nav.appendChild(rec.link);
    if (typeof window.__abpWireNavLink === 'function') window.__abpWireNavLink(rec.link);
    rec.section = existing || makeSection(p);
    if (!existing) main.appendChild(rec.section);
    rec.root = document.getElementById(p.id + '-root');
    if (typeof window.__abpNavAdd === 'function') window.__abpNavAdd(rec.link, rec.section);
    if (typeof window.__abpObserveSection === 'function') window.__abpObserveSection(rec.section, true);
    return true;
  }

  function removeShell(rec) {
    if (typeof window.__abpNavRemove === 'function') window.__abpNavRemove(rec.link, rec.section);
    if (typeof window.__abpObserveSection === 'function' && rec.section) window.__abpObserveSection(rec.section, false);
    if (rec.link && rec.link.parentNode) rec.link.parentNode.removeChild(rec.link);
    if (rec.section && rec.section.parentNode) rec.section.parentNode.removeChild(rec.section);
  }

  function withVersion(src, version) {
    if (!version) return src;
    return src + (src.indexOf('?') >= 0 ? '&' : '?') + 'v=' + encodeURIComponent(version);
  }

  function loadPanel(rec) {
    var p = rec.panel;
    rec.loading = true;
    rec.root.innerHTML = '<p class="cardnote">Loading…</p>';
    (p.styles || []).forEach(function (href) {
      var l = document.createElement('link');
      l.rel = 'stylesheet';
      l.href = url(withVersion(href, p.version));
      document.head.appendChild(l);
      rec.assets.push(l);
    });
    if (!p.script) { rec.loading = false; showError(rec, new Error('the panel has no script')); return; }
    var s = document.createElement('script');
    s.src = url(withVersion(p.script, p.version));
    s.onload = function () {
      rec.loading = false;
      if (rec.dead) return;
      if (!rec.mounted) rec.legacy = true;   // never called define(): it found its -root on its own
    };
    s.onerror = function () {
      rec.loading = false;
      if (rec.dead) return;
      rec.root.innerHTML = '<p class="cardnote">This module\'s panel could not be loaded (' + esc(p.script) + ').</p>';
    };
    rec.assets.push(s);
    document.body.appendChild(s);
  }

  function addPanel(p) {
    var rec = { id: p.id, panel: p, timers: [], listeners: [], assets: [], mounted: false, legacy: false, loading: false, dead: false };
    records[p.id] = rec;
    if (!addShell(rec)) { delete records[p.id]; return; }
    loadPanel(rec);
  }

  function reloadPanel(rec, p) {
    stopPanel(rec);
    rec.panel = p;
    rec.legacy = false;
    var title = rec.section && rec.section.querySelector('h2');
    if (title) title.textContent = p.title || p.id;
    // a legacy panel owns its root's contents; start from a fresh root
    var fresh = document.createElement('div');
    fresh.id = p.id + '-root';
    if (rec.root && rec.root.parentNode) rec.root.parentNode.replaceChild(fresh, rec.root);
    rec.root = fresh;
    loadPanel(rec);
  }

  function removePanel(rec, keepNote) {
    rec.dead = true;
    stopPanel(rec);
    delete records[rec.id];
    var open = keepNote && location.hash === '#' + rec.id && rec.section;
    if (open) {
      if (rec.link && rec.link.parentNode) rec.link.parentNode.removeChild(rec.link);
      if (typeof window.__abpNavRemove === 'function') window.__abpNavRemove(rec.link, null);
      rec.section.setAttribute('data-unloaded', '1');
      rec.section.innerHTML = '<div class="sec-head"><h2>' + esc(rec.panel.title || rec.id) + '</h2></div>' +
        '<p class="cardnote" data-module-note="1">This module was unloaded.</p>';
      window.addEventListener('hashchange', function once() {
        window.removeEventListener('hashchange', once);
        if (rec.section.getAttribute('data-unloaded') && rec.section.parentNode) rec.section.parentNode.removeChild(rec.section);
      });
    } else {
      removeShell(rec);
    }
  }

  // ------------------------------------------------------------------ builtin visibility
  function applyBuiltin(live) {
    var els = document.querySelectorAll('[data-unit]');
    var hash = location.hash.replace(/^#/, '');
    Array.prototype.forEach.call(els, function (el) {
      var on = !!live[el.getAttribute('data-unit')];
      if (on) {
        if (el.hasAttribute('data-unit-hidden')) { el.style.display = ''; el.removeAttribute('data-unit-hidden'); }
      } else if (!el.hasAttribute('data-unit-hidden')) {
        el.style.display = 'none';
        el.setAttribute('data-unit-hidden', '1');
      }
    });
    // a builtin page that is open while its unit goes away says so instead of silently vanishing
    var old = document.querySelector('[data-module-unloaded-note]');
    if (old && old.parentNode) old.parentNode.removeChild(old);
    var openEl = hash && document.getElementById(hash);
    if (openEl && openEl.hasAttribute('data-unit-hidden')) {
      var note = document.createElement('p');
      note.className = 'cardnote';
      note.setAttribute('data-module-unloaded-note', '1');
      note.textContent = 'This module was unloaded.';
      var main = document.querySelector('main');
      if (main) main.insertBefore(note, main.firstChild);
    }
  }

  function render(manifest) {
    var panels = (manifest && manifest.panels) || [];
    var live = {};
    var scripts = {};
    panels.forEach(function (p) {
      if (p.kind === 'script') { if (p.id && !scripts[p.id]) scripts[p.id] = p; }
      else live[p.unit || p.section || p.id] = true;
    });
    applyBuiltin(live);
    Object.keys(records).forEach(function (id) {
      if (!scripts[id]) removePanel(records[id], true);
    });
    Object.keys(scripts).forEach(function (id) {
      var p = scripts[id];
      var rec = records[id];
      if (!rec) addPanel(p);
      else if (rec.panel.version !== p.version || rec.panel.script !== p.script) reloadPanel(rec, p);
    });
  }

  // ------------------------------------------------------------------ fetching
  function refresh() {
    if (inflight) { queued = true; return inflight; }
    inflight = rawFetch('/api/ui/manifest').then(function (res) {
      if (res.status === 404) { available = false; stopPolling(); return null; }
      if (!res.ok) return null;                      // 401 before the token arrives, 5xx: try again on the next tick
      return res.json();
    }).then(function (m) {
      if (m) {
        available = true;
        if (typeof m.generation === 'number') generation = Math.max(generation, m.generation);
        render(m);
      }
    }).catch(function () { /* server unreachable: keep what is shown */ }).then(function () {
      inflight = null;
      if (available !== null) readyResolve({ available: available });
      if (queued) { queued = false; refresh(); }
      else startPolling();
    });
    return inflight;
  }

  function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
  function startPolling() {
    if (pollTimer || available === false) return;
    pollTimer = setInterval(poll, POLL_MS);
  }

  function poll() {
    if (available === false || document.hidden) return;
    if (socketIsUp && available) return;   // the socket carries the events
    if (available !== true) { refresh(); return; }
    rawFetch('/api/runtime/events?since=' + generation).then(function (res) {
      if (!res.ok) return refresh();
      return res.json().then(function (d) {
        var g = d && typeof d.generation === 'number' ? d.generation : -1;
        (d && d.events || []).forEach(function (e) { if (e && typeof e.generation === 'number') g = Math.max(g, e.generation); });
        if (g > generation || (d && d.events && d.events.length)) return refresh();
      });
    }).catch(function () { /* unreachable: next tick */ });
  }

  function onEvent(msg) {
    if (!msg || msg.type !== 'runtime.changed' || available === false) return;
    if (typeof msg.generation === 'number' && msg.generation <= generation) return;
    try { window.dispatchEvent(new CustomEvent('abp:runtime-changed', { detail: msg })); } catch (_e) { /* old browser */ }
    refresh();
  }

  function socketUp(up) {
    socketIsUp = !!up;
    if (up && available !== false) refresh();   // events may have been missed while it was down
  }

  ABP.moduleHost = {
    onEvent: onEvent,
    socketUp: socketUp,
    refresh: refresh,
    ready: ready,
    get available() { return available; },
    get generation() { return generation; },
    // test / debugging aid: what is mounted and what the host would have to clean up
    debug: function () {
      return { generation: generation, available: available, timers: counts.timers, listeners: counts.listeners,
               panels: Object.keys(records).map(function (id) { return { id: id, mounted: records[id].mounted, legacy: records[id].legacy }; }) };
    }
  };

  function start() { refresh(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
