// The hello extension's panel. It is served from the extension's own folder
// (/extensions/hello/panel.js) and added to both UIs' navigation at runtime by
// bot/dashboard/static/extensions.js — no line of this file exists in the dashboard's HTML or in
// the repo. Same contract as a panel ABP ships: draw into #<id>-root, reload on hashchange.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s));

  async function render() {
    const root = $('hello-root');
    if (!root) return;
    try {
      const res = await api('/api/hello');
      root.innerHTML = `<p class="cardnote">${E(res.greeting)} from <b>${E(res.from)}</b>.</p>`;
    } catch (e) {
      root.innerHTML = `<p class="cardnote">${E(e && e.message ? e.message : e)}</p>`;
    }
  }

  function init() {
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'hello') render(); };
    window.addEventListener('hashchange', onHash);
    onHash();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);