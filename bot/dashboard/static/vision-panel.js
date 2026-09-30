// Vision panel: computer vision on this machine (OpenCV and its model zoo; nothing leaves the machine). Tabs:
//   Analyze   an image (upload, a path or URL, the screen, a camera): objects, faces, text, codes, people, colours,
//             shapes; the picture with the results drawn on it, and the results as a table
//   Find      where a piece of text or a smaller image is, on the screen or an image (positions to click)
//   Compare   what changed between two images
//   Edit      resize, crop, rotate, blur, edges... step by step
//   Models    the device and the model zoo's models (downloaded on first use, verified)
// Identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/); tests/test_vision_page.py
// fails if the two differ. It uses the page's own api(), esc() and showToast(), and draws into #vsp-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { const d = JSON.parse(m).detail; m = d || m; } catch (_) {} return typeof m === 'string' ? m : JSON.stringify(m); };
  const send = (path, body) => api(path, { method: 'POST', body: JSON.stringify(Object.assign({ inline: true }, body || {})) });
  const TASKS = [['objects', 'Objects'], ['faces', 'Faces'], ['text', 'Text'], ['codes', 'QR / barcodes'], ['people', 'People'], ['colors', 'Colours'], ['shapes', 'Shapes'], ['info', 'Info']];
  const EDITS = ['resize', 'crop', 'rotate', 'flip', 'gray', 'blur', 'sharpen', 'edges', 'threshold', 'invert', 'brightness', 'denoise'];
  const st = { tab: 'analyze', sources: {}, busy: false, tasks: ['objects', 'faces', 'text', 'codes'], steps: [] };
  try { st.tab = localStorage.getItem('vsp.tab') || 'analyze'; st.tasks = JSON.parse(localStorage.getItem('vsp.tasks') || 'null') || st.tasks; } catch (_) {}
  const keep = (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} };

  function css() {
    if ($('vsp-css')) return;
    const s = document.createElement('style');
    s.id = 'vsp-css';
    s.textContent = `.vsp-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}.vsp-tabs button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.vsp-split{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:12px}@media (max-width:1000px){.vsp-split{grid-template-columns:1fr}}
.vsp-card{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:var(--surface);min-width:0;display:grid;gap:8px;align-content:start}
.vsp-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.vsp-muted{color:var(--muted);font-size:12px}
.vsp-img{max-width:100%;border:1px solid var(--line);border-radius:8px;background:repeating-conic-gradient(var(--surface-2) 0 25%,var(--surface) 0 50%) 0 0/16px 16px}
.vsp-drop{border:2px dashed var(--line);border-radius:10px;padding:14px;text-align:center;cursor:pointer;color:var(--muted);font-size:12.5px}.vsp-drop.hot{border-color:var(--accent);color:var(--ink)}
.vsp-table{width:100%;border-collapse:collapse;font-size:12.5px}.vsp-table td,.vsp-table th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.vsp-mono{font-family:var(--font-mono,monospace);font-size:12px;white-space:pre-wrap;word-break:break-word}
.vsp-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}.vsp-chip.on{background:var(--good-soft);color:var(--good,#1f9d55)}
#vsp-root input,#vsp-root select,#vsp-root textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%}
.vsp-swatch{display:inline-block;width:18px;height:18px;border-radius:4px;border:1px solid var(--line);vertical-align:middle;margin-right:4px}`;
    document.head.appendChild(s);
  }

  // A source picker: drop or choose a file (sent as a data: URL), or type a path/URL, or pick the screen / a camera.
  function picker(key, label) {
    const cur = st.sources[key];
    const shown = cur ? (cur.startsWith('data:') ? 'an uploaded image' : cur) : '';
    return `<div class="vsp-card"><b>${E(label)}</b>
<div class="vsp-drop" id="vsp-drop-${key}">Drop an image here or click to choose one${shown ? `<br><span class="vsp-mono">${E(shown.slice(0, 120))}</span>` : ''}</div>
<input type="file" accept="image/*" id="vsp-file-${key}" style="display:none">
<div class="vsp-row"><input id="vsp-path-${key}" placeholder="or a path, an https URL" style="flex:1;min-width:180px" value="${cur && !cur.startsWith('data:') && !/^(screen|camera)/.test(cur) ? E(cur) : ''}">
<button class="btn ghost" data-src="${key}" data-val="screen">Screen</button><button class="btn ghost" data-src="${key}" data-val="camera">Camera</button></div>
${cur && cur.startsWith('data:') ? `<img class="vsp-img" style="max-height:160px" src="${cur}">` : ''}</div>`;
  }

  function wirePicker(key) {
    const drop = $(`vsp-drop-${key}`), file = $(`vsp-file-${key}`), path = $(`vsp-path-${key}`);
    if (!drop) return;
    const take = (f) => { if (!f) return; const r = new FileReader(); r.onload = () => { st.sources[key] = r.result; render(); }; r.readAsDataURL(f); };
    drop.addEventListener('click', () => file.click());
    file.addEventListener('change', () => take(file.files[0]));
    drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('hot'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('hot'));
    drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('hot'); take(e.dataTransfer.files[0]); });
    path.addEventListener('change', () => { st.sources[key] = path.value.trim(); });
  }

  const box = (b) => (b ? `${b[0]}, ${b[1]} · ${b[2]}×${b[3]}` : '');
  function resultsTable(r) {
    const rows = [];
    for (const o of r.objects || []) rows.push(['object', `${E(o.label)} <span class="vsp-muted">${(o.score * 100).toFixed(0)}%</span>`, box(o.box), o.screen_center]);
    for (const f of r.faces || []) rows.push(['face', `<span class="vsp-muted">${(f.score * 100).toFixed(0)}%</span>`, box(f.box), f.screen_center]);
    for (const c of r.codes || []) rows.push([c.kind, `<span class="vsp-mono">${E(c.text)}</span>`, box(c.box), c.screen_center]);
    for (const t of r.text || []) rows.push(['text', `${E(t.text)} <span class="vsp-muted">${(t.confidence * 100).toFixed(0)}%</span>`, box(t.box), t.screen_center]);
    for (const p of ((r.people || {}).regions || [])) rows.push(['person', '', box(p.box), null]);
    for (const s of r.shapes || []) rows.push(['shape', E(s.shape), box(s.box), null]);
    let html = rows.length ? `<table class="vsp-table"><tr><th>What</th><th></th><th>Where (x, y · w×h)</th><th>Screen</th></tr>${rows.map((x) => `<tr><td>${x[0]}</td><td>${x[1]}</td><td class="vsp-mono">${x[2]}</td><td class="vsp-mono">${x[3] ? x[3].join(', ') : ''}</td></tr>`).join('')}</table>` : '<p class="vsp-muted">Nothing found.</p>';
    if (r.colors) html += `<div>${r.colors.map((c) => `<span title="${E(c.hex)} · ${(c.share * 100).toFixed(0)}%"><span class="vsp-swatch" style="background:${E(c.hex)}"></span></span>`).join('')}</div>`;
    if (r.info) html += `<div class="vsp-muted">${r.info.width}×${r.info.height} · brightness ${r.info.brightness} · contrast ${r.info.contrast} · sharpness ${r.info.sharpness}</div>`;
    if (r.text_note) html += `<div class="vsp-muted">${E(r.text_note)}</div>`;
    if (r.timings_ms) html += `<div class="vsp-muted">${Object.entries(r.timings_ms).map(([k, v]) => `${k} ${v} ms`).join(' · ')}</div>`;
    return html;
  }

  async function run(fn) {
    if (st.busy) return;
    st.busy = true; render();
    try { st.result = await fn(); } catch (e) { st.result = null; toast(errText(e), 'error'); }
    st.busy = false; render();
  }

  function analyzeTab() {
    const r = st.result && st.result._tab === 'analyze' ? st.result : null;
    return `<div class="vsp-split"><div class="vsp-card">${r && r.annotated_data ? `<img class="vsp-img" src="${r.annotated_data}">` : '<p class="vsp-muted">The picture with the results drawn on it appears here.</p>'}${r ? resultsTable(r) : ''}</div>
<div style="display:grid;gap:10px;align-content:start">${picker('a', 'Image')}
<div class="vsp-card"><b>Look for</b><div class="vsp-row">${TASKS.map(([k, l]) => `<label class="vsp-row"><input type="checkbox" data-task="${k}" ${st.tasks.includes(k) ? 'checked' : ''}> ${l}</label>`).join('')}</div>
<button class="btn" id="vsp-go" ${st.busy ? 'disabled' : ''}>${st.busy ? 'Working…' : 'Analyze'}</button></div></div></div>`;
  }

  function findTab() {
    const r = st.result && st.result._tab === 'find' ? st.result : null;
    return `<div class="vsp-split"><div class="vsp-card">${r && r.annotated_data ? `<img class="vsp-img" src="${r.annotated_data}">` : '<p class="vsp-muted">Finds text or a smaller image; on the screen each match also has the position to click.</p>'}
${r ? `<p><b>${r.found}</b> found (by ${E(r.by)})</p><table class="vsp-table">${(r.matches || []).map((m) => `<tr><td>${E(m.text || 'match')}</td><td class="vsp-mono">${box(m.box)}</td><td class="vsp-mono">${m.screen_center ? 'click ' + m.screen_center.join(', ') : ''}</td></tr>`).join('')}</table>` : ''}</div>
<div style="display:grid;gap:10px;align-content:start">${picker('a', 'Where to look (default: the screen)')}
<div class="vsp-card"><label>Text <input id="vsp-text" placeholder="e.g. Save changes"></label><div class="vsp-muted">or a smaller image to find:</div></div>${picker('t', 'Image to find')}
<button class="btn" id="vsp-go" ${st.busy ? 'disabled' : ''}>${st.busy ? 'Working…' : 'Find'}</button></div></div>`;
  }

  function compareTab() {
    const r = st.result && st.result._tab === 'compare' ? st.result : null;
    return `<div class="vsp-split"><div class="vsp-card">${r && r.annotated_data ? `<img class="vsp-img" src="${r.annotated_data}">` : '<p class="vsp-muted">The second image with what changed outlined appears here.</p>'}
${r ? `<p>Similarity <b>${r.similarity}</b> · ${(r.changed_share * 100).toFixed(2)}% of pixels changed · ${r.regions.length} region(s)${r.identical ? ' · <span class="vsp-chip on">identical</span>' : ''}</p>` : ''}</div>
<div style="display:grid;gap:10px;align-content:start">${picker('a', 'Before')}${picker('b', 'After')}<button class="btn" id="vsp-go" ${st.busy ? 'disabled' : ''}>${st.busy ? 'Working…' : 'Compare'}</button></div></div>`;
  }

  function editTab() {
    const r = st.result && st.result._tab === 'edit' ? st.result : null;
    const steps = st.steps.map((s, i) => `<div class="vsp-row"><span class="vsp-mono">${i + 1}. ${E(JSON.stringify(s))}</span><button class="btn ghost" data-del="${i}">✕</button></div>`).join('');
    return `<div class="vsp-split"><div class="vsp-card">${r && r.path_data ? `<img class="vsp-img" src="${r.path_data}"><div class="vsp-muted">Saved: <span class="vsp-mono">${E(r.path)}</span></div>` : '<p class="vsp-muted">The edited image appears here.</p>'}</div>
<div style="display:grid;gap:10px;align-content:start">${picker('a', 'Image')}
<div class="vsp-card"><b>Steps</b>${steps || '<span class="vsp-muted">none yet</span>'}
<div class="vsp-row"><select id="vsp-op">${EDITS.map((o) => `<option>${o}</option>`).join('')}</select><input id="vsp-params" placeholder='e.g. {"width": 800}' style="flex:1"><button class="btn ghost" id="vsp-add">Add</button></div>
<button class="btn" id="vsp-go" ${st.busy ? 'disabled' : ''}>${st.busy ? 'Working…' : 'Apply'}</button></div></div></div>`;
  }

  async function modelsTab() {
    const s = await api('/api/vision');
    const d = s.device;
    return `<div class="vsp-card"><b>Device</b><div>Running on <b>${E(d.running_on)}</b>${d.gpu ? ` · GPU found: ${E(d.gpu)}` : ''} · OpenCV ${E(d.opencv)} · setting <span class="vsp-mono">vision.device = ${E(d.configured)}</span> (${d.choices.map(E).join(' | ')})</div>
<div class="vsp-muted">Results are saved in <span class="vsp-mono">${E(s.output_folder)}</span>.</div></div>
<table class="vsp-table" style="margin-top:10px"><tr><th>Model</th><th>Task</th><th>License</th><th>Size</th><th></th></tr>${s.models.map((m) => `<tr><td>${E(m.title)}</td><td>${E(m.task)}</td><td>${E(m.license)}</td><td>${m.size_mb} MB</td><td>${m.present ? '<span class="vsp-chip on">here</span>' : `<button class="btn ghost" data-fetch="${E(m.model)}">Download</button>`}</td></tr>`).join('')}</table>`;
  }

  async function go() {
    const a = st.sources.a || ($('vsp-path-a') && $('vsp-path-a').value.trim());
    if (st.tab === 'analyze') return run(async () => Object.assign(await send('/api/vision/analyze', { image: a, tasks: st.tasks }), { _tab: 'analyze' }));
    if (st.tab === 'find') {
      const text = ($('vsp-text') || {}).value || '';
      return run(async () => Object.assign(await send('/api/vision/find', { image: a || 'screen', text, template: text ? null : st.sources.t }), { _tab: 'find' }));
    }
    if (st.tab === 'compare') return run(async () => Object.assign(await send('/api/vision/compare', { before: a, after: st.sources.b }), { _tab: 'compare' }));
    if (st.tab === 'edit') return run(async () => Object.assign(await send('/api/vision/edit', { image: a, steps: st.steps }), { _tab: 'edit' }));
  }

  function wire() {
    const root = $('vsp-root');
    root.querySelectorAll('[data-tab]').forEach((b) => b.addEventListener('click', () => { st.tab = b.dataset.tab; keep('vsp.tab', st.tab); render(); }));
    root.querySelectorAll('[data-src]').forEach((b) => b.addEventListener('click', () => { st.sources[b.dataset.src] = b.dataset.val; render(); }));
    root.querySelectorAll('[data-task]').forEach((c) => c.addEventListener('change', () => {
      st.tasks = Array.from(root.querySelectorAll('[data-task]')).filter((x) => x.checked).map((x) => x.dataset.task);
      keep('vsp.tasks', JSON.stringify(st.tasks));
    }));
    root.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', () => { st.steps.splice(+b.dataset.del, 1); render(); }));
    root.querySelectorAll('[data-fetch]').forEach((b) => b.addEventListener('click', async () => {
      b.disabled = true; b.textContent = 'Downloading…';
      try { await api(`/api/vision/models/${encodeURIComponent(b.dataset.fetch)}/fetch`, { method: 'POST', body: '{}' }); toast('Downloaded and verified'); } catch (e) { toast(errText(e), 'error'); }
      render();
    }));
    const add = $('vsp-add');
    if (add) add.addEventListener('click', () => {
      let p = {};
      try { p = JSON.parse($('vsp-params').value || '{}'); } catch (_) { toast('The parameters must be JSON, e.g. {"width": 800}', 'error'); return; }
      st.steps.push(Object.assign({ op: $('vsp-op').value }, p)); render();
    });
    const goBtn = $('vsp-go');
    if (goBtn) goBtn.addEventListener('click', go);
    ['a', 'b', 't'].forEach(wirePicker);
  }

  async function render() {
    css();
    const root = $('vsp-root');
    if (!root) return;
    const tabs = [['analyze', 'Analyze'], ['find', 'Find'], ['compare', 'Compare'], ['edit', 'Edit'], ['models', 'Models & device']];
    const head = `<div class="vsp-tabs">${tabs.map(([k, l]) => `<button class="btn ghost ${st.tab === k ? 'on' : ''}" data-tab="${k}">${l}</button>`).join('')}</div>`;
    try {
      const html = st.tab === 'find' ? findTab() : st.tab === 'compare' ? compareTab() : st.tab === 'edit' ? editTab() : st.tab === 'models' ? await modelsTab() : analyzeTab();
      root.innerHTML = head + html;
    } catch (e) { root.innerHTML = head + `<p class="cardnote">${E(errText(e))}</p>`; }
    wire();
  }

  function init() {
    if (!$('vsp-root')) return;
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'vision') render(); };
    window.addEventListener('hashchange', onHash);
    onHash();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
