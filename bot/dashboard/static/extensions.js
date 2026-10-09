/* Extensions: what this install added on top of the public ABP.

   With a runtime that serves /api/ui/manifest, module-host.js builds every extension panel (they arrive in the
   manifest as `script` panels) and this file does nothing. It is the fallback for an older backend that has no
   manifest: it asks /api/extensions once and adds each panel as a section, a nav entry and a <script> tag, so a
   private panel never has to be copied into dashboard.html or index.html. Unset ABP_PRIVATE_DIR means an empty
   list and nothing is added. */
(function () {
  'use strict';
  if (typeof fetch !== 'function') return;

  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'));
  const base = () => (typeof API_BASE !== 'undefined' && API_BASE) ? String(API_BASE) : '';
  const token = () => (typeof getToken === 'function' ? getToken() : window.__ABP_TOKEN__) || '';

  async function load() {
    let data;
    try {
      const res = await fetch(base() + '/api/extensions', { headers: { 'X-Dashboard-Token': token() } });
      if (!res.ok) return;
      data = await res.json();
    } catch (e) {
      return; // no dashboard token on this page load, or nothing listening: nothing to add
    }
    (data.panels || []).forEach(add);
  }

  function add(panel) {
    const nav = document.getElementById('sidenav');
    const main = document.querySelector('main');
    if (!nav || !main) return;
    if (document.getElementById(panel.id)) return; // already there (a reload, or a duplicate id)

    const link = document.createElement('a');
    link.href = '#' + panel.id;
    link.title = panel.title;
    link.setAttribute('data-ext-panel', panel.id);
    link.innerHTML = '<span class="ic">' + E(panel.icon || '◆') + '</span><span class="nav-txt">' + E(panel.title) + '</span>';
    nav.appendChild(link);
    if (typeof window.__abpWireNavLink === 'function') window.__abpWireNavLink(link);

    const section = document.createElement('section');
    section.id = panel.id;
    section.setAttribute('data-ext-panel', panel.id);
    section.innerHTML = '<div class="sec-head"><h2>' + E(panel.title) + '</h2>' +
      (panel.description ? '<span class="desc">' + E(panel.description) + '</span>' : '') + '</div>' +
      '<div id="' + E(panel.id) + '-root"><p class="cardnote">Loading…</p></div>';
    main.appendChild(section);
    if (typeof window.__abpObserveSection === 'function') window.__abpObserveSection(section, true);

    (panel.styles || []).forEach(function (href) {
      const link2 = document.createElement('link');
      link2.rel = 'stylesheet';
      link2.href = base() + href;
      document.head.appendChild(link2);
    });
    // Loaded after the section exists, so the panel's own init finds its root div.
    const script = document.createElement('script');
    script.src = base() + panel.script;
    script.onerror = function () {
      const root = document.getElementById(panel.id + '-root');
      if (root) root.innerHTML = '<p class="cardnote">This panel could not be loaded.</p>';
    };
    document.body.appendChild(script);
  }

  function start() {
    const host = window.ABP && window.ABP.moduleHost;
    if (!host) { load(); return; }
    host.ready.then(function (r) { if (r && r.available === false) load(); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
