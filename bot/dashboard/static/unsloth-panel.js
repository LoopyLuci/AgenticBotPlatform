// Unsloth panel: Unsloth Studio from ABP. Load and serve models to ABP's bots, download from the Hugging Face hub,
// fine-tune, export, and reach every other Studio operation (forms built from Studio's own API description).
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_unsloth_panel.py fails if the two differ. It uses the page's own api(), esc() and showToast(),
// and draws into #us-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const post = (path, body, method) => api(path, { method: method || 'POST', body: JSON.stringify(body || {}) });
  const gb = (b) => (b == null ? '—' : (b / 1e9).toFixed(1) + ' GB');
  const TABS = [['overview', 'Overview'], ['models', 'Models'], ['train', 'Train'], ['export', 'Export'], ['all', 'All features']];
  const TRAIN_FIRST = ['model_name', 'training_type', 'hf_dataset', 'format_type', 'num_epochs', 'max_steps', 'learning_rate', 'batch_size', 'max_seq_length', 'use_lora', 'lora_r', 'lora_alpha', 'load_in_4bit', 'project_name'];
  const st = { tab: 'overview', ops: null, op: null, group: '', polls: {} };
  try { st.tab = localStorage.getItem('us-tab') || 'overview'; } catch (_) {}
  let loading = false;

  function css() {
    if ($('us-css')) return;
    const s = document.createElement('style');
    s.id = 'us-css';
    s.textContent = `.us-tabs{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0 12px;border-bottom:1px solid var(--line)}
.us-tabs button{background:none;border:0;border-bottom:2px solid transparent;color:var(--ink-soft);padding:8px 12px;font:inherit;font-weight:600;cursor:pointer}
.us-tabs button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--accent)}
.us-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:12px}
.us-card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--surface);min-width:0}
.us-card h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.us-muted{color:var(--muted);font-size:12px}.us-mono{font-family:var(--font-mono,monospace);font-size:12px;word-break:break-all}
.us-bar{height:10px;border-radius:5px;background:var(--surface-2);overflow:hidden;margin:4px 0}.us-bar i{display:block;height:100%;background:var(--accent)}
.us-table{width:100%;border-collapse:collapse;font-size:12.5px}.us-table th{text-align:left;color:var(--muted);font-weight:600;padding:6px;border-bottom:1px solid var(--line)}
.us-table td{padding:6px;border-bottom:1px solid var(--line);vertical-align:top}.us-scroll{overflow:auto;max-width:100%}.us-scroll.tall{max-height:60vh}
.us-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.us-form{display:grid;gap:8px}.us-form label{display:grid;gap:3px;font-size:12px;color:var(--ink-soft)}.us-form label.chk{display:flex;gap:6px;align-items:center}
#us-root input,#us-root select,#us-root textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%}
.us-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:8px}
.us-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.us-chip.on{background:var(--good-soft);color:var(--good,#1f9d55)}.us-chip.warn{background:var(--warning-soft);color:#8a5c00}
.us-pre{white-space:pre-wrap;max-height:50vh;overflow:auto;background:var(--surface-2);border-radius:6px;padding:8px;font-family:var(--font-mono,monospace);font-size:11.5px}
.us-ops li{padding:4px 0;border-bottom:1px solid var(--line);cursor:pointer;list-style:none}.us-ops{margin:0;padding:0;max-height:60vh;overflow:auto}
.us-ops li.sel{background:var(--accent-soft)}.us-split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.4fr);gap:12px}@media (max-width:1000px){.us-split{grid-template-columns:1fr}}
.us-svg{width:100%;height:auto}.us-svg text{fill:var(--muted);font-size:10px}`;
    document.head.appendChild(s);
  }

  // ---- a form from a JSON schema (Studio's own request descriptions) ----------------------------------------------------
  function typeOf(s) {
    if (!s) return 'string';
    if (s.enum) return 'enum';
    if (s.type) return s.type;
    const alt = (s.anyOf || s.oneOf || []).find((x) => x.type && x.type !== 'null');
    return alt ? (alt.enum ? 'enum' : alt.type) : 'string';
  }
  function enumOf(s) { return s.enum || ((s.anyOf || []).find((x) => x.enum) || {}).enum || []; }
  function field(name, s, required, prefix) {
    const t = typeOf(s);
    const d = s.default;
    const id = `${prefix}-${name}`;
    const label = `${E(name)}${required ? ' *' : ''}`;
    const title = E((s.description || s.title || '').slice(0, 300));
    const attrs = `data-f="${E(name)}" data-t="${t}" ${required ? 'data-req="1"' : ''} data-d="${E(JSON.stringify(d === undefined ? null : d))}" id="${id}" title="${title}"`;
    if (t === 'boolean') return `<label class="chk" title="${title}"><input type="checkbox" ${attrs} ${d ? 'checked' : ''}> ${label}</label>`;
    if (t === 'enum') return `<label title="${title}">${label}<select ${attrs}>${required ? '' : '<option value="">(default)</option>'}${enumOf(s).map((v) => `<option ${v === d ? 'selected' : ''}>${E(v)}</option>`).join('')}</select></label>`;
    if (t === 'object' || t === 'array') return `<label title="${title}">${label} <small class="us-muted">${t} (JSON)</small><textarea rows="2" ${attrs}>${d == null ? '' : E(JSON.stringify(d))}</textarea></label>`;
    const type = t === 'integer' || t === 'number' ? 'number' : 'text';
    return `<label title="${title}">${label}<input type="${type}" ${t === 'number' ? 'step="any"' : ''} ${attrs} value="${d == null ? '' : E(d)}"></label>`;
  }
  function schemaForm(schema, prefix, first) {
    const props = (schema && schema.properties) || {};
    const req = new Set((schema && schema.required) || []);
    const names = Object.keys(props);
    const top = names.filter((n) => req.has(n) || (first || []).includes(n)).sort((a, b) => (first || []).indexOf(a) - (first || []).indexOf(b));
    const rest = names.filter((n) => !top.includes(n));
    return `<div class="us-fields">${top.map((n) => field(n, props[n], req.has(n), prefix)).join('')}</div>
      ${rest.length ? `<details style="margin-top:8px"><summary class="us-muted">${rest.length} more setting(s)</summary><div class="us-fields" style="margin-top:8px">${rest.map((n) => field(n, props[n], req.has(n), prefix)).join('')}</div></details>` : ''}`;
  }
  function readForm(root) {
    // Only what differs from Studio's default is sent (plus required fields), so its own defaults keep applying.
    const out = {};
    root.querySelectorAll('[data-f]').forEach((el) => {
      const d = JSON.parse(el.dataset.d || 'null');
      let v;
      if (el.dataset.t === 'boolean') v = el.checked;
      else if (el.value === '') return;
      else if (el.dataset.t === 'integer') v = parseInt(el.value, 10);
      else if (el.dataset.t === 'number') v = Number(el.value);
      else if (el.dataset.t === 'object' || el.dataset.t === 'array') v = JSON.parse(el.value);
      else v = el.value;
      if (JSON.stringify(v) !== JSON.stringify(d) || el.dataset.req) out[el.dataset.f] = v;
    });
    return out;
  }

  // ---- tabs ---------------------------------------------------------------------------------------------------------------
  async function renderOverview(body) {
    const s = await api('/api/unsloth/status');
    const sm = s.summary;
    if (!sm.running) {
      body.innerHTML = `<div class="us-card"><h4>Unsloth Studio is not running</h4><p>${E(sm.hint || '')}. Start Unsloth Studio; ABP finds it at a configured provider's address (Models page) or http://127.0.0.1:8888.</p></div>`;
      return;
    }
    const gpus = (sm.gpus || []).map((g) => `<div><b>${E(g.name)}</b><div class="us-bar"><i style="width:${g.vram_total_gb ? (g.vram_used_gb / g.vram_total_gb * 100).toFixed(1) : 0}%"></i></div><span class="us-muted">${g.vram_used_gb} of ${g.vram_total_gb} GB VRAM in use</span></div>`).join('') || '<span class="us-muted">No GPU reported</span>';
    const tr = sm.training || {};
    const stg = s.storage || {};
    body.innerHTML = `<div class="us-grid">
      <div class="us-card"><h4>Studio</h4><div class="us-mono">${E(sm.url)}</div><div class="us-muted">provider “${E(sm.provider || '—')}” · backend ${E(sm.backend || '—')}${(sm.auth || {}).requires_password_change ? ' · <span class="us-chip warn">Studio asks for a password change</span>' : ''}</div>
        <div class="us-row" style="margin-top:8px"><a class="btn" href="${E(sm.url)}" target="_blank" rel="noopener">Open Unsloth Studio</a></div></div>
      <div class="us-card"><h4>GPU</h4>${gpus}</div>
      <div class="us-card"><h4>Loaded models</h4>${(sm.loaded || []).length ? sm.loaded.map((m) => `<div class="us-row"><span class="us-chip on">loaded</span><span class="us-mono" style="flex:1">${E(m)}</span><button class="btn" data-us="unload" data-model="${E(m)}">Unload</button></div>`).join('') : '<p class="us-muted">None. Load one on the Models tab; a bot that asks for an unloaded model gets it loaded on demand.</p>'}
        <p class="us-muted">Bots use a loaded model as <span class="us-mono">${E(sm.provider || 'unsloth')}/&lt;model&gt;</span>; the model router offers loaded ones to bots on auto.</p></div>
      <div class="us-card"><h4>Training</h4><b>${E(tr.phase || 'idle')}</b>${tr.running ? ` · step ${tr.step}/${tr.total_steps} · loss ${tr.loss == null ? '—' : Number(tr.loss).toFixed(4)}` : ''}<div class="us-muted">${E(tr.message || '')}</div></div>
      <div class="us-card"><h4>Where models go</h4><div class="us-mono">${E(stg.models_dir || '(Studio default)')}</div><div class="us-muted">${gb(stg.free_bytes)} free${stg.writable === false ? ' · not writable' : ''}</div>
        <form class="us-row" data-us-form="storage" style="margin-top:8px"><input name="models_dir" placeholder="e.g. E:\\AIModels\\Unsloth" value="${E(stg.models_dir || '')}" style="flex:1;min-width:180px"><button class="btn" type="submit">Use this folder</button></form></div>
    </div>`;
  }

  async function renderModels(body) {
    const [m, rec, dl] = await Promise.all([api('/api/unsloth/models'), api('/api/unsloth/recommended?limit=60').catch(() => ({ models: [] })), api('/api/unsloth/downloads').catch(() => ({ downloads: [] }))]);
    const avail = (m.available || []).map((x) => `<tr><td class="us-mono">${E(x.id)}</td><td>${x.loaded ? `<span class="us-chip on">loaded</span> <span class="us-muted">${x.context_length ? x.context_length.toLocaleString() + ' ctx' : ''}</span>` : ''}</td>
      <td class="us-row">${x.loaded ? `<button class="btn" data-us="unload" data-model="${E(x.id)}">Unload</button>` : `<input type="number" min="0" step="1024" placeholder="context" style="width:96px" data-ctx-for="${E(x.id)}" aria-label="Context length"><button class="btn primary" data-us="load" data-model="${E(x.id)}">Load</button>`}</td></tr>`).join('');
    const downloads = (dl.downloads || []).map((d) => `<div class="us-row"><span class="us-mono" style="flex:1">${E(d.repo_id)} ${E(d.variant || '')}</span><span class="us-chip">${E(d.state)}</span><span id="us-dlp-${E((d.repo_id + (d.variant || '')).replace(/[^a-z0-9]/gi, '_'))}" class="us-muted"></span></div>`).join('');
    body.innerHTML = `<div class="us-grid">
      <div class="us-card" style="grid-column:1/-1"><h4>What Studio can serve</h4><p class="us-muted">Loading a model puts it on the GPU (a large GGUF can take minutes). Leave context empty for ABP's default (32,768).</p>
        ${avail ? `<div class="us-scroll tall"><table class="us-table"><thead><tr><th>model</th><th>state</th><th></th></tr></thead><tbody>${avail}</tbody></table></div>` : '<p class="us-muted">Nothing yet. Download a model below.</p>'}</div>
      <div class="us-card"><h4>Download from the Hugging Face hub</h4>
        <form class="us-form" data-us-form="variants"><label>Repo <input name="repo_id" list="us-rec" placeholder="unsloth/Qwen3.5-9B-MTP-GGUF" required></label>
          <datalist id="us-rec">${(rec.models || []).map((x) => `<option value="${E(x.id)}">`).join('')}</datalist><div><button class="btn" type="submit">Show its files</button></div></form>
        <div id="us-variants" style="margin-top:8px"></div>
        ${downloads ? `<h4 style="margin-top:12px">Downloading</h4>${downloads}` : ''}</div>
      <div class="us-card"><h4>Recommended by Unsloth</h4><div class="us-scroll tall"><table class="us-table"><tbody>${(rec.models || []).map((x) => `<tr><td class="us-mono">${E(x.id)}</td><td>${x.is_gguf ? '<span class="us-chip">GGUF</span>' : ''}${x.is_vision ? ' <span class="us-chip">vision</span>' : ''}</td><td><button class="btn" data-us="pick" data-repo="${E(x.id)}">Files</button></td></tr>`).join('')}</tbody></table></div></div>
    </div>`;
    if ((dl.downloads || []).length) pollDownloads(dl.downloads);
  }

  async function showVariants(repo) {
    const box = $('us-variants');
    box.innerHTML = '<p class="us-muted">Asking Studio…</p>';
    try {
      const r = await api('/api/unsloth/variants?repo_id=' + encodeURIComponent(repo));
      box.innerHTML = r.variants.length ? `<div class="us-scroll"><table class="us-table"><thead><tr><th>quant</th><th>download</th><th></th></tr></thead><tbody>${r.variants.map((v) => `<tr><td>${E(v.quant)}</td><td>${gb(v.download_size_bytes || v.size_bytes)}${v.downloaded ? ' <span class="us-chip on">downloaded</span>' : v.partial ? ' <span class="us-chip warn">partial</span>' : ''}</td>
        <td class="us-row"><button class="btn" data-us="estimate" data-repo="${E(repo)}" data-variant="${E(v.quant)}">Fits?</button>${v.downloaded ? `<button class="btn primary" data-us="load" data-model="${E(repo)}" data-variant="${E(v.quant)}">Load</button>` : `<button class="btn primary" data-us="download" data-repo="${E(repo)}" data-variant="${E(v.quant)}">Download</button>`}</td></tr>`).join('')}</tbody></table></div>`
        : `<p class="us-muted">No GGUF files; this repo downloads whole.</p><button class="btn primary" data-us="download" data-repo="${E(repo)}">Download ${E(repo)}</button>`;
    } catch (e) { box.innerHTML = `<p class="us-muted">${E(errText(e))}</p>`; }
  }

  function pollDownloads(list) {
    list.forEach((d) => {
      const key = d.repo_id + (d.variant || '');
      if (st.polls[key]) return;
      st.polls[key] = setInterval(async () => {
        try {
          const s = await api(`/api/unsloth/download-status?repo_id=${encodeURIComponent(d.repo_id)}${d.variant ? '&variant=' + encodeURIComponent(d.variant) : ''}`);
          const p = s.progress || {};
          const el = $('us-dlp-' + key.replace(/[^a-z0-9]/gi, '_'));
          if (el) el.textContent = p.progress != null ? `${Math.round(p.progress * 100)}% · ${gb(p.completed_bytes)} of ${gb(p.expected_bytes)}` : s.state;
          if (!/running|queued|starting|pending/.test(String(s.state))) { clearInterval(st.polls[key]); delete st.polls[key]; toast(`${d.repo_id}: ${s.state}${s.error ? ' — ' + s.error : ''}`, s.error ? 'error' : 'good'); if (st.tab === 'models') renderModels($('us-body')); }
        } catch (_) { /* keep polling */ }
      }, 3000);
    });
  }

  function lossChart(m) {
    const ys = m.loss_history || [];
    if (ys.length < 2) return '<p class="us-muted">No loss recorded yet.</p>';
    const xs = m.step_history && m.step_history.length === ys.length ? m.step_history : ys.map((_, i) => i + 1);
    const W = 600, H = 160, p = 24;
    const minY = Math.min(...ys), maxY = Math.max(...ys), span = maxY - minY || 1, maxX = Math.max(...xs), minX = Math.min(...xs), sx = maxX - minX || 1;
    const pts = ys.map((y, i) => `${p + (xs[i] - minX) / sx * (W - p - 4)},${H - p - (y - minY) / span * (H - p - 8)}`).join(' ');
    return `<svg class="us-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Training loss"><text x="0" y="12">${maxY.toFixed(3)}</text><text x="0" y="${H - p}">${minY.toFixed(3)}</text><text x="${W - 60}" y="${H - 6}">step ${maxX}</text>
      <polyline fill="none" stroke="var(--accent)" stroke-width="2" points="${pts}"/></svg>`;
  }

  async function renderTrain(body) {
    const [schema, status, metrics, runs] = await Promise.all([api('/api/unsloth/train/schema'), api('/api/unsloth/train/status'), api('/api/unsloth/train/metrics'), api('/api/unsloth/train/runs').catch(() => ({ runs: [] }))]);
    const d = status.details || {};
    body.innerHTML = `<div class="us-grid"><div class="us-card" style="grid-column:1/-1"><h4>Now</h4><b>${E(status.phase || 'idle')}</b> ${status.is_training_running ? `· step ${d.step}/${d.total_steps} · epoch ${d.epoch} · loss ${d.loss == null ? '—' : Number(d.loss).toFixed(4)} <button class="btn danger" data-us="train-stop">Stop</button>` : ''}
        <div class="us-muted">${E(status.message || '')}${status.error ? ' · ' + E(status.error) : ''}</div>${lossChart(metrics)}</div>
      <div class="us-card" style="grid-column:1/-1"><h4>Start fine-tuning</h4><p class="us-muted">Every setting Studio offers, with its defaults; only what you change is sent. Required: model_name, training_type (e.g. lora), format_type (e.g. chatml) and a dataset (hf_dataset, or local_datasets).</p>
        <form data-us-form="train">${schemaForm(schema.schema, 'tr', TRAIN_FIRST)}<div style="margin-top:10px"><button class="btn primary" type="submit">Start training</button></div></form></div>
      <div class="us-card" style="grid-column:1/-1"><h4>Runs</h4>${(runs.runs || []).length ? `<div class="us-scroll"><table class="us-table"><tbody>${runs.runs.map((r) => `<tr><td class="us-mono">${E(r.id || r.run_id || r.job_id || '')}</td><td>${E(r.model_name || '')}</td><td>${E(r.status || r.phase || '')}</td><td class="us-muted">${E(r.created_at || r.started_at || '')}</td></tr>`).join('')}</tbody></table></div>` : '<p class="us-muted">No runs yet.</p>'}</div></div>`;
    if (status.is_training_running) setTimeout(() => { if (st.tab === 'train') renderTrain($('us-body')).catch(() => {}); }, 10000);
  }

  async function renderExport(body) {
    const s = await api('/api/unsloth/export/status');
    body.innerHTML = `<div class="us-grid"><div class="us-card"><h4>Export the trained model</h4>
        <form class="us-form" data-us-form="export"><label>Kind <select name="kind"><option>gguf</option><option>lora</option><option>merged</option><option>base</option></select></label>
        <label>Save to folder <input name="save_directory" placeholder="E:\\AIModels\\Unsloth\\exports\\my-model"></label>
        <label>GGUF quantization <input name="quantization_method" value="Q4_K_M"></label>
        <label class="chk"><input type="checkbox" name="push_to_hub"> push to the Hugging Face hub</label><label>Hub repo <input name="repo_id" placeholder="you/my-model"></label>
        <div><button class="btn primary" type="submit">Export</button></div></form></div>
      <div class="us-card"><h4>Status</h4><div>${s.is_export_active ? `<b>${E(s.active_op_kind)}</b> running` : 'idle'}</div><div class="us-muted">checkpoint: ${E(s.current_checkpoint || 'none loaded')}</div>
        ${s.last_op_kind ? `<div style="margin-top:6px">last: ${E(s.last_op_kind)} · ${E(s.last_op_status)}${s.last_op_output_path ? ` → <span class="us-mono">${E(s.last_op_output_path)}</span>` : ''}${s.last_op_error ? `<div class="us-muted">${E(s.last_op_error)}</div>` : ''}</div>` : ''}</div></div>`;
  }

  async function renderAll(body) {
    if (!st.ops) st.ops = (await api('/api/unsloth/operations')).operations;
    const groups = {};
    st.ops.forEach((o) => { groups[o.group] = (groups[o.group] || 0) + 1; });
    const list = st.ops.filter((o) => !st.group || o.group === st.group);
    body.innerHTML = `<p class="us-muted">All ${st.ops.length} operations Unsloth Studio describes, straight from its API: anything its own UI can do. Reads run at once; anything that changes Studio needs the desktop dashboard token and is recorded in the audit log.</p>
      <div class="us-split"><div class="us-card"><div class="us-row" style="margin-bottom:8px"><select data-us-group aria-label="Group"><option value="">every group (${st.ops.length})</option>${Object.keys(groups).sort().map((g) => `<option value="${E(g)}" ${st.group === g ? 'selected' : ''}>${E(g)} (${groups[g]})</option>`).join('')}</select>
        <input data-us-filter placeholder="filter" style="flex:1;min-width:120px" aria-label="Filter operations"></div>
        <ul class="us-ops">${list.map((o) => `<li data-us="op" data-id="${E(o.id)}" class="${st.op && st.op.id === o.id ? 'sel' : ''}"><span class="us-chip">${o.method}</span> <span class="us-mono">${E(o.path)}</span><br><span class="us-muted">${E(o.summary)}</span></li>`).join('')}</ul></div>
        <div class="us-card" id="us-op"><p class="us-muted">Pick an operation.</p></div></div>`;
    if (st.op) showOp(st.op.id);
  }

  function showOp(id) {
    const o = st.ops.find((x) => x.id === id);
    st.op = o;
    document.querySelectorAll('#us-root .us-ops li').forEach((li) => li.classList.toggle('sel', li.dataset.id === id));
    const params = o.params.length ? `<h4 style="margin-top:8px">Parameters</h4>${schemaForm({ properties: Object.fromEntries(o.params.map((p) => [p.name, p.schema])), required: o.params.filter((p) => p.required).map((p) => p.name) }, 'p')}` : '';
    const body = o.body ? `<h4 style="margin-top:8px">Body</h4>${o.body.properties ? schemaForm(o.body, 'b') : `<textarea data-raw-body rows="6" style="width:100%" class="us-mono">{}</textarea>`}` : '';
    const fileNames = (o.file_fields || []).map((x) => x.name);
    const formRest = o.form && o.form.properties ? { properties: Object.fromEntries(Object.entries(o.form.properties).filter(([k]) => !fileNames.includes(k))), required: (o.form.required || []).filter((k) => !fileNames.includes(k)) } : null;
    const upload = o.multipart ? `<h4 style="margin-top:8px">Files</h4>${(o.file_fields || []).map((x) => `<label>${E(x.name)} <input type="file" data-file="${E(x.name)}" ${x.many ? 'multiple' : ''}></label>`).join('')}
      ${formRest && Object.keys(formRest.properties).length ? `<h4 style="margin-top:8px">Fields</h4>${schemaForm(formRest, 'm')}` : ''}` : '';
    $('us-op').innerHTML = `<h4><span class="us-chip">${o.method}</span> ${E(o.path)}</h4><div>${E(o.summary)}</div>${o.description ? `<details><summary class="us-muted">about</summary><div class="us-muted">${E(o.description)}</div></details>` : ''}
      <form data-us-form="call"><div data-part="params">${params}</div><div data-part="body">${body}</div><div data-part="upload">${upload}</div>
      <div style="margin-top:10px"><button class="btn ${o.mutating ? 'danger' : 'primary'}" type="submit">${o.multipart ? 'Upload' : o.mutating ? 'Run (changes Studio)' : 'Run'}</button></div></form><div id="us-op-out" style="margin-top:10px"></div>`;
  }

  const RENDER = { overview: renderOverview, models: renderModels, train: renderTrain, export: renderExport, all: renderAll };

  async function load() {
    const root = $('us-root');
    if (!root || loading) return;
    loading = true;
    try {
      root.innerHTML = `<div class="us-tabs" role="tablist">${TABS.map(([k, l]) => `<button role="tab" aria-selected="${st.tab === k}" data-us="tab" data-tab="${k}">${l}</button>`).join('')}
        <span style="flex:1"></span><button class="btn" data-us="refresh" aria-label="Refresh" title="Refresh">↻</button></div><div id="us-body" role="tabpanel"><p class="us-muted">Loading…</p></div>`;
      await (RENDER[st.tab] || renderOverview)($('us-body'));
    } catch (e) {
      const m = errText(e);
      $('us-body') ? ($('us-body').innerHTML = `<p class="cardnote">${/not running|could not reach/.test(m) ? 'Unsloth Studio is not running. Start it, then refresh.' : 'Unsloth data unavailable: ' + E(m)}</p>`) : null;
    } finally { loading = false; }
  }
  async function show(tab) { st.tab = tab; try { localStorage.setItem('us-tab', tab); } catch (_) {} await load(); }

  async function busy(el, label, fn) {
    const old = el.textContent;
    el.disabled = true; el.textContent = label;
    try { return await fn(); } finally { el.disabled = false; el.textContent = old; }
  }

  async function act(el, ev) {
    const a = el.dataset.us;
    try {
      if (a === 'tab') return show(el.dataset.tab);
      if (a === 'refresh') { st.ops = null; return load(); }
      if (a === 'op') return showOp(el.dataset.id);
      if (a === 'pick') { const f = document.querySelector('#us-root form[data-us-form=variants] input'); f.value = el.dataset.repo; return showVariants(el.dataset.repo); }
      if (a === 'load') {
        const ctxEl = document.querySelector(`#us-root [data-ctx-for="${CSS.escape(el.dataset.model)}"]`);
        const r = await busy(el, 'Loading… (can take minutes)', () => post('/api/unsloth/load', { model: el.dataset.model, variant: el.dataset.variant || null, context: ctxEl && ctxEl.value ? Number(ctxEl.value) : 0 }));
        toast(`${el.dataset.model} loaded${r.context_length ? ' · ' + r.context_length.toLocaleString() + ' ctx' : ''}`, 'good'); return load();
      }
      if (a === 'unload') { await busy(el, 'Unloading…', () => post('/api/unsloth/unload', { model: el.dataset.model })); toast(el.dataset.model + ' unloaded', 'good'); return load(); }
      if (a === 'download') {
        await post('/api/unsloth/download', { repo_id: el.dataset.repo, variant: el.dataset.variant || null });
        toast(`Downloading ${el.dataset.repo} ${el.dataset.variant || ''}`, 'good');
        return renderModels($('us-body'));
      }
      if (a === 'estimate') {
        const r = await busy(el, '…', () => post('/api/unsloth/estimate', { model: el.dataset.repo, variant: el.dataset.variant }));
        const fits = r.fits != null ? r.fits : r.fits_in_vram != null ? r.fits_in_vram : null;
        toast(`${el.dataset.variant}: ${fits === true ? 'fits' : fits === false ? 'does not fit' : 'estimate'} — ${JSON.stringify(r).slice(0, 180)}`, fits === false ? 'error' : 'good'); return;
      }
      if (a === 'train-stop') { if (!window.confirm('Stop the training run?')) return; await post('/api/unsloth/train/stop', {}); toast('Stopping', 'good'); return renderTrain($('us-body')); }
    } catch (e) { toast(errText(e), 'error'); }
  }

  async function submit(form, ev) {
    ev.preventDefault();
    const kind = form.dataset.usForm;
    const data = Object.fromEntries(new FormData(form).entries());
    const btn = form.querySelector('button[type=submit]');
    try {
      if (kind === 'variants') return showVariants(data.repo_id.trim());
      if (kind === 'storage') { const r = await post('/api/unsloth/storage', { models_dir: data.models_dir }, 'PUT'); toast('Models now go to ' + r.models_dir, 'good'); return load(); }
      if (kind === 'train') {
        const fields = readForm(form);
        await busy(btn, 'Starting…', () => post('/api/unsloth/train/start', { fields }));
        toast('Training started', 'good'); return renderTrain($('us-body'));
      }
      if (kind === 'export') {
        const fields = { save_directory: data.save_directory || null, push_to_hub: !!data.push_to_hub, repo_id: data.repo_id || null };
        if (data.kind === 'gguf') fields.quantization_method = data.quantization_method || 'Q4_K_M';
        await busy(btn, 'Exporting…', () => post('/api/unsloth/export', { kind: data.kind, fields }));
        toast('Export done', 'good'); return renderExport($('us-body'));
      }
      if (kind === 'call') {
        const o = st.op;
        if (o.mutating && !window.confirm(`${o.method} ${o.path} changes Unsloth Studio. Run it?`)) return;
        const args = readForm(form.querySelector('[data-part=params]'));
        const bodyPart = form.querySelector('[data-part=body]');
        const raw = bodyPart.querySelector('[data-raw-body]');
        if (o.body) args.body = raw ? JSON.parse(raw.value || '{}') : readForm(bodyPart);
        const out = $('us-op-out');
        out.innerHTML = '<p class="us-muted">Running…</p>';
        let r;
        if (o.multipart) {
          const files = {};
          for (const inp of form.querySelectorAll('[data-file]')) {
            for (const file of inp.files) {
              const data = await new Promise((ok, bad) => { const rd = new FileReader(); rd.onload = () => ok(String(rd.result).split(',')[1] || ''); rd.onerror = bad; rd.readAsDataURL(file); });
              (files[inp.dataset.file] = files[inp.dataset.file] || []).push({ name: file.name, data });
            }
          }
          if (!Object.keys(files).length) { out.innerHTML = '<p class="us-muted">Choose a file first.</p>'; return; }
          Object.assign(args, readForm(form.querySelector('[data-part=upload]')));
          r = await post('/api/unsloth/upload', { operation: o.id, args, files });
        } else {
          r = await post('/api/unsloth/call', { operation: o.id, args });
        }
        out.innerHTML = `<div class="us-pre">${E(JSON.stringify(r.result, null, 2))}</div>`;
      }
    } catch (e) { toast(errText(e), 'error'); const out = $('us-op-out'); if (kind === 'call' && out) out.innerHTML = `<p class="us-muted">${E(errText(e))}</p>`; }
  }

  function init() {
    const root = $('us-root');
    if (!root) return;
    css();
    root.addEventListener('click', (ev) => { const el = ev.target.closest('[data-us]'); if (el && root.contains(el)) act(el, ev); });
    root.addEventListener('submit', (ev) => { const f = ev.target.closest('form[data-us-form]'); if (f) submit(f, ev); });
    root.addEventListener('change', (ev) => { if (ev.target.matches('[data-us-group]')) { st.group = ev.target.value; renderAll($('us-body')); } });
    root.addEventListener('input', (ev) => {
      if (!ev.target.matches('[data-us-filter]')) return;
      const q = ev.target.value.toLowerCase();
      root.querySelectorAll('.us-ops li').forEach((li) => { li.hidden = q && !li.textContent.toLowerCase().includes(q); });
    });
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'unsloth') load(); };
    window.addEventListener('hashchange', onHash);
    onHash();
    if ('IntersectionObserver' in window) {
      let seen = false;
      new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting && !seen) { seen = true; load(); } })).observe(root);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
