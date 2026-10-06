/* Extensions: what this install added on top of the public ABP.

   Both UIs load this (it is one of the /static/*.js the pages' CSP allows) and it asks the
   server what /api/extensions lists. Every panel an extension registered becomes a section, a
   nav entry and a <script> tag built here at runtime, so a private panel never has to be
   copied into dashboard.html or index.html — and when ABP_PRIVATE_DIR is unset the list is
   empty and this adds nothing.

   Sections are appended inside <main>, after the last one the HTML has, and the nav entry
   before </nav>; both UIs' nav is a plain <nav id="sidenav"> of <a href="#id"> links, and
   dashboard.js/main.js already scroll to whatever section a link names. */
(function () {
  'use strict';
  if (typeof fetch !== 'function') return;

  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'));

  async function load() {
    let data;
    try {
      const res = await fetch('/api/extensions', { headers: { 'X-Dashboard-Token': window.__ABP_TOKEN__ || '' } });
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
    link.innerHTML = '<span class="ic">' + E(panel.icon || '◆') + '</span><span class="nav-txt">' + E(panel.title) + '</span>';
    nav.appendChild(link);

    const section = document.createElement('section');
    section.id = panel.id;
    section.innerHTML = '<div class="sec-head"><h2>' + E(panel.title) + '</h2>' +
      (panel.description ? '<span class="desc">' + E(panel.description) + '</span>' : '') + '</div>' +
      '<div id="' + E(panel.id) + '-root"><p class="cardnote">Loading…</p></div>';
    main.appendChild(section);

    (panel.styles || []).forEach(function (href) {
      const link2 = document.createElement('link');
      link2.rel = 'stylesheet';
      link2.href = href;
      document.head.appendChild(link2);
    });
    // Loaded after the section exists, so the panel's own init finds its root div.
    const script = document.createElement('script');
    script.src = panel.script;
    document.body.appendChild(script);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', load);
  else load();
})();