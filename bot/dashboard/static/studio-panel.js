// Studio panel: generative code and GUI for ABP itself, with a real-time preview (bot/studio). Tabs:
//   Variants   every variant (an overlay of the UI: only the files it changes), preview, compare, rate, apply, discard
//   Preview    the dashboard as a variant has it, live (it reloads the moment a file changes); switch between the live
//              UI and any variant instantly, or show two side by side; a screenshot diff of what visibly changed
//   Edit       a variant's copy of any UI file; saving updates the preview at once
//   Theme      the theme tokens (colours, fonts) for light and dark, and the component gallery in the variant's styles
//   Generate   models propose several variants from one request (layer 2)
//   History    applied changes (one click to revert), the log, and the datasets built from it
// Identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/); tests/test_studio.py checks
// it. It uses the page's own api(), esc() and showToast(), and draws into #std-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { const d = JSON.parse(m).detail; m = d || m; } catch (_) {} return typeof m === 'string' ? m : JSON.stringify(m); };
  const send = (path, body, method) => api(path, { method: method || 'POST', body: JSON.stringify(body || {}) });
  // the preview is served by ABP itself (same origin), so it only works where the page comes from ABP's server
  const ORIGIN = (location.protocol === 'http:' || location.protocol === 'https:') ? '' : 'http://127.0.0.1:8787';
  const st = { tab: 'variants', data: null, cur: '', other: '', section: '', file: '', editor: null, mode: 'light', theme: 'light', gen: null, side: false };
  try { const s = JSON.parse(localStorage.getItem('std.state') || '{}'); Object.assign(st, { tab: s.tab || st.tab, cur: s.cur || '', section: s.section || '', file: s.file || '' }); } catch (_) {}
  const keep = () => { try { localStorage.setItem('std.state', JSON.stringify({ tab: st.tab, cur: st.cur, section: st.section, file: st.file })); } catch (_) {} };

  function css() {
    if ($('std-css')) return;
    const s = document.createElement('style');
    s.id = 'std-css';
    s.textContent = `.std-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}.std-tabs button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.std-card{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:var(--surface);min-width:0;display:grid;gap:8px;align-content:start}
.std-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.std-muted{color:var(--muted);font-size:12px}.std-mono{font-family:var(--font-mono,monospace);font-size:12px;word-break:break-all}
.std-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr));gap:10px}
.std-frame{width:100%;height:72vh;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.std-two{display:grid;grid-template-columns:1fr 1fr;gap:8px}@media (max-width:1100px){.std-two{grid-template-columns:1fr}}
.std-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}.std-chip.on{background:var(--good-soft);color:var(--good,#1f9d55)}.std-chip.bad{background:var(--critical-soft);color:var(--critical)}
#std-root input,#std-root select,#std-root textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%}
.std-editor{width:100%;min-height:60vh;font-family:var(--font-mono,monospace);font-size:12px;line-height:1.45;tab-size:2;white-space:pre}
.std-pre{white-space:pre-wrap;max-height:40vh;overflow:auto;background:var(--surface-2);border-radius:6px;padding:8px;font-family:var(--font-mono,monospace);font-size:11.5px}
.std-tok{display:grid;grid-template-columns:minmax(120px,1fr) 44px minmax(120px,1.2fr);gap:6px;align-items:center}`;
    document.head.appendChild(s);
  }

  const chip = (t, k) => `<span class="std-chip ${k || ''}">${E(t)}</span>`;
  const vName = (id) => { const v = (st.data && st.data.variants || []).find((x) => x.id === id); return v ? v.name : id; };
  const previewUrl = (vid, sec) => (vid ? `${ORIGIN}/studio/v/${encodeURIComponent(vid)}/` : `${ORIGIN}/`) + (sec ? '#' + encodeURIComponent(sec) : '');
  const variantOptions = (sel, withLive) => (withLive ? `<option value="" ${!sel ? 'selected' : ''}>Live (as it is now)</option>` : '') +
    (st.data.variants || []).filter((v) => v.state === 'open').map((v) => `<option value="${E(v.id)}" ${sel === v.id ? 'selected' : ''}>${E(v.name)}</option>`).join('');

  async function load() { st.data = await api('/api/studio'); if (st.cur && !(st.data.variants || []).some((v) => v.id === st.cur)) st.cur = ''; }

  function variantsTab() {
    const vs = st.data.variants || [];
    const groups = {};
    for (const v of vs) (groups[v.group] = groups[v.group] || []).push(v);
    const card = (v) => `<div class="std-card"><div class="std-row" style="justify-content:space-between"><b>${E(v.name)}</b>${v.state === 'applied' ? chip('applied', 'on') : chip(v.origin)}</div>
<div class="std-muted">${E(v.note || '')}</div><div class="std-mono">${(v.files || []).map(E).join('<br>') || '<span class="std-muted">no changes yet</span>'}</div>
<div class="std-row">${[1, 2, 3, 4, 5].map((n) => `<button class="btn ghost" data-rate="${E(v.id)}" data-n="${n}" title="rate ${n}">${v.rating >= n ? '★' : '☆'}</button>`).join('')}</div>
<div class="std-row"><button class="btn ghost" data-open="${E(v.id)}">Preview</button><button class="btn ghost" data-edit="${E(v.id)}">Edit</button><button class="btn ghost" data-fork="${E(v.id)}">Copy</button>${v.state === 'open' ? `<button class="btn primary" data-apply="${E(v.id)}">Apply</button>` : ''}<button class="btn danger" data-discard="${E(v.id)}">Discard</button></div></div>`;
    return `<div class="std-row" style="margin-bottom:10px"><input id="std-new-name" placeholder="A name for a new variant"><button class="btn" id="std-new">New variant</button><span class="std-muted">A variant holds only the files it changes; the rest is the live UI.</span></div>
${Object.values(groups).map((g) => `<div class="std-grid" style="margin-bottom:12px">${g.map(card).join('')}</div>`).join('') || '<p class="std-muted">No variants yet: make one, or let models propose some (Generate).</p>'}`;
  }

  function previewTab() {
    const sec = st.section;
    const secs = ['', 'overview', 'bots', 'chat', 'modules', 'vision', 'octopus', 'cluster', 'studio', 'settings'];
    const head = `<div class="std-row" style="margin-bottom:8px"><label>Show <select id="std-cur">${variantOptions(st.cur, true)}</select></label>
<label class="std-row"><input type="checkbox" id="std-side" ${st.side ? 'checked' : ''}> beside <select id="std-other">${variantOptions(st.other, true)}</select></label>
<label>at <select id="std-sec">${secs.map((s) => `<option value="${s}" ${s === sec ? 'selected' : ''}>${s ? '#' + s : 'the top'}</option>`).join('')}</select></label>
${st.cur ? '<button class="btn ghost" id="std-shot">What visibly changed?</button>' : ''}<a class="btn ghost" href="${E(previewUrl(st.cur, sec))}" target="_blank" rel="noopener">Open in a tab</a>
<span class="std-muted">Edits to the variant show here as soon as they are saved.</span></div><div id="std-shot-out"></div>`;
    const frame = (vid) => `<iframe class="std-frame" src="${E(previewUrl(vid, sec))}" title="preview"></iframe>`;
    return head + (st.side ? `<div class="std-two">${frame(st.cur)}${frame(st.other)}</div>` : frame(st.cur));
  }

  function editTab() {
    if (!st.cur) return `<p class="std-muted">Pick a variant first (Variants, Edit), or make one:</p><button class="btn" id="std-new">New variant</button>`;
    const files = st.data.ui_files || [];
    return `<div class="std-row" style="margin-bottom:8px"><b>${E(vName(st.cur))}</b><select id="std-file" style="min-width:320px"><option value="">Pick a file…</option>${files.map((f) => `<option ${f === st.file ? 'selected' : ''}>${E(f)}</option>`).join('')}</select>
<button class="btn primary" id="std-save" ${st.file ? '' : 'disabled'}>Save (Ctrl+S)</button><button class="btn ghost" id="std-resetfile" ${st.file ? '' : 'disabled'}>Back to live</button><button class="btn ghost" id="std-validate">Check</button></div>
<div class="std-two"><div><textarea id="std-editor" class="std-editor" spellcheck="false" placeholder="Pick a file">${E(st.editor == null ? '' : st.editor)}</textarea></div><iframe class="std-frame" src="${E(previewUrl(st.cur, st.section))}" title="preview"></iframe></div><div id="std-check-out"></div>`;
  }

  async function themeTab() {
    if (!st.cur) return `<p class="std-muted">Pick a variant first (Variants), or make one:</p><button class="btn" id="std-new">New variant</button>`;
    const t = await api(`/api/studio/variants/${encodeURIComponent(st.cur)}/tokens`);
    const vals = t[st.mode] || {};
    const isHex = (v) => /^#[0-9a-f]{6}$/i.test(v);
    const rows = Object.entries(vals).map(([k, v]) => `<div class="std-tok"><span class="std-mono">${E(k)}</span>${isHex(v) ? `<input type="color" data-color="${E(k)}" value="${E(v)}">` : '<span></span>'}<input data-tok="${E(k)}" value="${E(v)}"></div>`).join('');
    return `<div class="std-row" style="margin-bottom:8px"><b>${E(vName(st.cur))}</b><select id="std-mode"><option value="light" ${st.mode === 'light' ? 'selected' : ''}>Light theme</option><option value="dark" ${st.mode === 'dark' ? 'selected' : ''}>Dark theme</option></select>
<button class="btn primary" id="std-tok-save">Save to the variant</button><span class="std-muted">Both the dashboard and the desktop app; the gallery and the preview update at once.</span></div>
<div class="std-two"><div class="std-card" style="max-height:72vh;overflow:auto">${rows || '<span class="std-muted">No tokens found</span>'}</div>
<iframe class="std-frame" src="${E(ORIGIN + '/studio/v/' + encodeURIComponent(st.cur) + '/components?theme=' + st.mode)}" title="components"></iframe></div>`;
  }

  function generateTab() {
    const files = st.data.ui_files || [];
    const g = st.gen;
    const results = g && g.result ? `<div class="std-grid" style="margin-top:10px">${g.result.attempts.map((a) => `<div class="std-card"><div class="std-row" style="justify-content:space-between"><b class="std-mono">${E(a.model)}</b>${a.ok ? chip('valid', 'on') : chip(a.error ? 'failed' : 'problems', 'bad')}</div>
<div>${E(a.explanation || a.error || '')}</div>${(a.problems || []).concat(a.refused || []).map((p) => `<div class="std-muted">• ${E(p)}</div>`).join('')}<div class="std-muted">${a.edits || 0} edit(s) · ${a.seconds}s</div>
${a.variant && !a.error ? `<div class="std-row"><button class="btn ghost" data-open="${E(a.variant)}">Preview</button><button class="btn ghost" data-edit="${E(a.variant)}">Edit</button></div>` : ''}</div>`).join('')}</div>
<div class="std-row" style="margin-top:8px">${g.result.variants.length > 1 ? `<button class="btn ghost" id="std-cmp" data-a="${E(g.result.variants[0])}" data-b="${E(g.result.variants[1])}">Compare the first two side by side</button>` : ''}</div>` : g && g.state === 'running' ? '<p class="std-muted">The models are working… (each proposal becomes its own variant)</p>' : g && g.state === 'failed' ? `<p class="cardnote">${E(g.error)}</p>` : '';
    return `<div class="std-card"><label>What should change?<textarea id="std-instr" rows="3" style="width:100%" placeholder="e.g. On the Vision page, show the detected objects as coloured chips above the picture">${E(st.instr || '')}</textarea></label>
<label>Files it concerns (Ctrl/Shift to pick several)<select id="std-files" multiple size="6" style="width:100%">${files.map((f) => `<option ${(st.genFiles || []).includes(f) ? 'selected' : ''}>${E(f)}</option>`).join('')}</select></label>
<div class="std-row"><label>Focus <input id="std-focus" placeholder="a section id or a word, for long files" value="${E(st.focus || '')}"></label><label>Variants <input id="std-n" type="number" min="1" max="6" value="${st.n || 3}" style="width:60px"></label>
<label>Models <input id="std-models" placeholder="provider/model, … (default: free and local ones)" style="min-width:280px" value="${E(st.models || '')}"></label></div>
<div class="std-row"><button class="btn primary" id="std-gen" ${g && g.state === 'running' ? 'disabled' : ''}>Propose variants</button><span class="std-muted">Each proposal is checked (HTML, JavaScript) and logged; nothing reaches the live UI until you apply it.</span></div></div>${results}`;
  }

  async function historyTab() {
    const lg = await api('/api/studio/log?limit=150');
    const bk = st.data.backups || [];
    return `<div class="std-two"><div class="std-card"><b>Applied changes</b>${bk.map((b) => `<div class="std-row" style="justify-content:space-between"><span><b>${E(b.name)}</b> <span class="std-muted">${new Date(b.at * 1000).toLocaleString()}</span><br><span class="std-mono">${Object.keys(b.files).map(E).join(', ')}</span></span><button class="btn ghost" data-revert="${E(b.id)}">Revert</button></div>`).join('') || '<span class="std-muted">Nothing applied yet.</span>'}</div>
<div class="std-card"><div class="std-row" style="justify-content:space-between"><b>Log</b><button class="btn ghost" id="std-datasets">Build the datasets</button></div><div id="std-ds-out"></div>
<div class="std-pre">${lg.events.slice().reverse().map((e) => `${new Date(e.t * 1000).toLocaleTimeString()}  ${e.kind.padEnd(13)} ${e.vid || ''} ${e.file || e.model || e.name || ''}${e.ok === false ? '  ✗' : ''}`).map(E).join('\n')}</div></div></div>`;
  }

  async function render() {
    css();
    const root = $('std-root');
    if (!root) return;
    const tabs = [['variants', 'Variants'], ['preview', 'Preview'], ['edit', 'Edit'], ['theme', 'Theme'], ['generate', 'Generate'], ['history', 'History & data']];
    const head = `<div class="std-tabs">${tabs.map(([k, l]) => `<button class="btn ghost ${st.tab === k ? 'on' : ''}" data-tab="${k}">${l}</button>`).join('')}</div>`;
    try {
      await load();
      const html = st.tab === 'preview' ? previewTab() : st.tab === 'edit' ? editTab() : st.tab === 'theme' ? await themeTab() : st.tab === 'generate' ? generateTab() : st.tab === 'history' ? await historyTab() : variantsTab();
      root.innerHTML = head + html;
    } catch (e) { root.innerHTML = head + `<p class="cardnote">${E(errText(e))}</p>`; }
    wire();
    keep();
  }

  async function openFile(f) {
    st.file = f; st.editor = null;
    if (f && st.cur) { try { st.editor = (await api(`/api/studio/variants/${encodeURIComponent(st.cur)}/file?path=${encodeURIComponent(f)}`)).content; } catch (e) { toast(errText(e), 'error'); } }
    render();
  }

  async function save() {
    const ed = $('std-editor');
    if (!ed || !st.file || !st.cur) return;
    st.editor = ed.value;
    try { await send(`/api/studio/variants/${encodeURIComponent(st.cur)}/file`, { path: st.file, content: ed.value }, 'PUT'); toast('Saved: the preview updates now'); } catch (e) { toast(errText(e), 'error'); }
  }

  function wire() {
    const root = $('std-root');
    const on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };
    root.querySelectorAll('[data-tab]').forEach((b) => b.addEventListener('click', () => { st.tab = b.dataset.tab; render(); }));
    const act = async (fn, msg) => { try { await fn(); if (msg) toast(msg); } catch (e) { toast(errText(e), 'error'); } render(); };
    on('std-new', 'click', () => act(async () => { const v = await send('/api/studio/variants', { name: ($('std-new-name') || {}).value || '' }); st.cur = v.id; }, 'A new variant: edit it, or theme it'));
    root.querySelectorAll('[data-open]').forEach((b) => b.addEventListener('click', () => { st.cur = b.dataset.open; st.tab = 'preview'; render(); }));
    root.querySelectorAll('[data-edit]').forEach((b) => b.addEventListener('click', () => { st.cur = b.dataset.edit; st.tab = 'edit'; openFile(st.file); }));
    root.querySelectorAll('[data-fork]').forEach((b) => b.addEventListener('click', () => act(async () => { const v = await send('/api/studio/variants', { base: b.dataset.fork, name: vName(b.dataset.fork) + ' (copy)' }); st.cur = v.id; }, 'Copied')));
    root.querySelectorAll('[data-apply]').forEach((b) => b.addEventListener('click', () => { if (confirm('Apply this variant to the live UI? Each file is backed up first; one click reverts it (History).')) act(() => send(`/api/studio/variants/${encodeURIComponent(b.dataset.apply)}/apply`), 'Applied: open pages reload'); }));
    root.querySelectorAll('[data-discard]').forEach((b) => b.addEventListener('click', () => { if (confirm('Discard this variant?')) act(() => send(`/api/studio/variants/${encodeURIComponent(b.dataset.discard)}/discard`)); }));
    root.querySelectorAll('[data-rate]').forEach((b) => b.addEventListener('click', () => act(() => send(`/api/studio/variants/${encodeURIComponent(b.dataset.rate)}/rate`, { rating: +b.dataset.n }))));
    root.querySelectorAll('[data-revert]').forEach((b) => b.addEventListener('click', () => { if (confirm('Put the files back as they were before this change?')) act(() => send('/api/studio/revert', { backup: b.dataset.revert }), 'Reverted'); }));
    on('std-cur', 'change', (e) => { st.cur = e.target.value; render(); });
    on('std-other', 'change', (e) => { st.other = e.target.value; render(); });
    on('std-side', 'change', (e) => { st.side = e.target.checked; render(); });
    on('std-sec', 'change', (e) => { st.section = e.target.value; render(); });
    on('std-shot', 'click', async () => {
      const out = $('std-shot-out'); out.innerHTML = '<p class="std-muted">Taking both screenshots in a real browser…</p>';
      try {
        const r = await send(`/api/studio/variants/${encodeURIComponent(st.cur)}/shot`, { section: st.section });
        const pic = await send('/api/vision/edit', { image: r.annotated, steps: [{ op: 'resize', scale: 1 }], inline: true });
        out.innerHTML = `<div class="std-card"><div>Similarity <b>${r.similarity}</b> · ${(r.changed_share * 100).toFixed(2)}% of the page changed · ${r.regions.length} region(s)</div><img src="${pic.path_data}" style="max-width:100%;border:1px solid var(--line);border-radius:8px"></div>`;
      } catch (e) { out.innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; }
    });
    on('std-file', 'change', (e) => openFile(e.target.value));
    on('std-save', 'click', save);
    const ed = $('std-editor');
    if (ed) ed.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save(); } else if (e.key === 'Tab') { e.preventDefault(); const s = ed.selectionStart; ed.setRangeText('  ', s, ed.selectionEnd, 'end'); } });
    on('std-resetfile', 'click', () => act(async () => { await api(`/api/studio/variants/${encodeURIComponent(st.cur)}/file?path=${encodeURIComponent(st.file)}`, { method: 'DELETE' }); await openFile(st.file); }, 'Back to the live file'));
    on('std-validate', 'click', async () => { const r = await send(`/api/studio/variants/${encodeURIComponent(st.cur)}/validate`); $('std-check-out').innerHTML = r.problems.length ? r.problems.map((p) => `<div class="cardnote">${E(p)}</div>`).join('') : '<p class="std-muted">No problems found.</p>'; });
    on('std-mode', 'change', (e) => { st.mode = e.target.value; render(); });
    root.querySelectorAll('[data-color]').forEach((c) => c.addEventListener('input', () => { const t = root.querySelector(`[data-tok="${CSS.escape(c.dataset.color)}"]`); if (t) t.value = c.value; }));
    on('std-tok-save', 'click', () => act(async () => { const values = {}; root.querySelectorAll('[data-tok]').forEach((i) => { values[i.dataset.tok] = i.value; }); await send(`/api/studio/variants/${encodeURIComponent(st.cur)}/tokens`, { mode: st.mode, values }); }, 'Theme saved to the variant'));
    on('std-gen', 'click', async () => {
      st.instr = $('std-instr').value; st.genFiles = Array.from($('std-files').selectedOptions).map((o) => o.value);
      st.focus = $('std-focus').value; st.n = +$('std-n').value || 3; st.models = $('std-models').value;
      const models = st.models.split(',').map((s) => s.trim()).filter(Boolean);
      try {
        let job = await send('/api/studio/generate', { instruction: st.instr, files: st.genFiles, focus: st.focus, variants: st.n, models: models.length ? models : null });
        st.gen = job; render();
        while (job.state === 'running') { await new Promise((r) => setTimeout(r, 2000)); job = await api(`/api/studio/jobs/${job.id}`); }
        st.gen = job; render();
      } catch (e) { toast(errText(e), 'error'); }
    });
    on('std-cmp', 'click', (e) => { st.cur = e.target.dataset.a; st.other = e.target.dataset.b; st.side = true; st.tab = 'preview'; render(); });
    on('std-datasets', 'click', async () => { try { const r = await send('/api/studio/datasets'); $('std-ds-out').innerHTML = Object.entries(r).map(([k, v]) => `<div class="std-mono">${E(k)}: ${v.rows} row(s) · ${E(v.path)}</div>`).join(''); } catch (e) { toast(errText(e), 'error'); } });
  }

  function init() {
    if (!$('std-root')) return;
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'studio') render(); };
    window.addEventListener('hashchange', onHash);
    onHash();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
