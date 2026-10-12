// Kanban: the work board's tasks as read-only cards, in their own panel under the hand-made cards. One block per board
// (e.g. abp/swarm-3, abp/hermes-5), one column per task state. Nothing here can change the board: no buttons, only a
// Refresh. This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_kanban_board_mirror.py fails if the two differ. It uses the page's own api() and esc(), draws into #kb-wb-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  let timer = null;

  function render(root, data, err) {
    if (err) { root.innerHTML = `<p class="cardnote">${E(err)}</p>`; return; }
    const boards = (data && data.boards) || [];
    if (!boards.length) { root.innerHTML = '<p class="cardnote">The work board has no tasks.</p>'; return; }
    root.innerHTML = boards.map((b) => {
      const cols = b.columns.filter((c) => c.cards.length || ['open', 'running', 'done', 'failed'].includes(c.name));
      return `<h4 style="margin:12px 0 6px">${E(b.board)} <span class="cardnote">(${E(b.total)})</span></h4>
<div style="display:grid;grid-template-columns:repeat(${cols.length},minmax(130px,1fr));gap:10px">${cols.map((c) =>
        `<div><div style="font-weight:700;font-size:12px;margin-bottom:4px">${E(c.name)} <span class="cardnote">${E(c.cards.length)}</span></div>${c.cards.map((t) =>
          `<div class="card" style="padding:6px 8px;margin:0 0 6px;font-size:12px"><div>${E(t.title)}</div><div class="cardnote">${t.owner ? E(t.owner) + ' - ' : ''}try ${E(t.attempts)}${t.note ? ' - ' + E(t.note) : ''}</div></div>`).join('')}</div>`).join('')}</div>`;
    }).join('');
  }

  async function load() {
    const root = $('kb-wb-root');
    if (!root) return;
    try { render(root, await api('/api/kanban/work-board')); }
    catch (e) { render(root, null, 'The work board is not available.'); }
  }

  function init() {
    const root = $('kb-wb-root');
    if (!root) return;
    const btn = $('btn-kb-wb-refresh');
    if (btn) btn.onclick = load;
    const onHash = () => {
      const on = (location.hash || '').replace('#', '') === 'kanban';
      if (on) { load(); if (!timer) timer = setInterval(load, 10000); }
      else if (timer) { clearInterval(timer); timer = null; }
    };
    window.addEventListener('hashchange', onHash);
    onHash();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
