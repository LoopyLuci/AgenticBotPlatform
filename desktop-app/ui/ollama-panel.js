// Ollama panel: Ollama from ABP. Pull, load and serve models to ABP's bots, build models from a Modelfile's parts or a
// GGUF file, move old models into the models folder, and call every route Ollama serves.
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_ollama.py fails if the two differ. It uses the page's own api(), esc() and showToast(), and draws into #ol-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });
  const gb = (b) => (b == null ? '—' : (b / 1e9).toFixed(2) + ' GB');
  const TABS = [['overview', 'Overview'], ['models', 'Models'], ['get', 'Get models'], ['create', 'Create'], ['all', 'All features']];
  const st = { tab: 'overview', ops: null, op: null, group: '', poll: null };
  try { st.tab = localStorage.getItem('ol-tab') || 'overview'; } catch (_) {}
  let loading = false;

  function css() {
    if ($('ol-css')) return;
    const s = document.createElement('style');
    s.id = 'ol-css';
    s.textContent = `.ol-tabs{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0 12px;border-bottom:1px solid var(--line)}
.ol-tabs button{background:none;border:0;border-bottom:2px solid transparent;color:var(--ink-soft);padding:8px 12px;font:inherit;font-weight:600;cursor:pointer}
.ol-tabs button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--accent)}
.ol-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:12px}
.ol-card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--surface);min-width:0}
.ol-card h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.ol-muted{color:var(--muted);font-size:12px}.ol-mono{font-family:var(--font-mono,monospace);font-size:12px;word-break:break-all}
.ol-bar{height:10px;border-radius:5px;background:var(--surface-2);overflow:hidden;margin:4px 0}.ol-bar i{display:block;height:100%;background:var(--accent)}
.ol-table{width:100%;border-collapse:collapse;font-size:12.5px}.ol-table th{text-align:left;color:var(--muted);font-weight:600;padding:6px;border-bottom:1px solid var(--line)}
.ol-table td{padding:6px;border-bottom:1px solid var(--line);vertical-align:top}.ol-scroll{overflow:auto;max-width:100%}.ol-scroll.tall{max-height:60vh}
.ol-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.ol-form{display:grid;gap:8px}.ol-form label{display:grid;gap:3px;font-size:12px;color:var(--ink-soft)}.ol-form label.chk{display:flex;gap:6px;align-items:center}
#ol-root input,#ol-root select,#ol-root textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%}
.ol-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:8px}
.ol-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.ol-chip.on{background:var(--good-soft);color:var(--good,#1f9d55)}.ol-chip.warn{background:var(--warning-soft);color:#8a5c00}
.ol-pre{white-space:pre-wrap;max-height:50vh;overflow:auto;background:var(--surface-2);border-radius:6px;padding:8px;font-family:var(--font-mono,monospace);font-size:11.5px}
.ol-ops li{padding:4px 0;border-bottom:1px solid var(--line);cursor:pointer;list-style:none}.ol-ops{margin:0;padding:0;max-height:60vh;overflow:auto}
.ol-ops li.sel{background:var(--accent-soft)}.ol-split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.4fr);gap:12px}@media (max-width:1000px){.ol-split{grid-template-columns:1fr}}
.ol-warn{border-left:3px solid var(--warning,#fab219);padding:6px 10px;margin:6px 0;background:var(--warning-soft);border-radius:4px;font-size:12.5px}`;
    document.head.appendChild(s);
  }

  // ---- forms from a route's field list -----------------------------------------------------------------------------------
  function field(name, s, required) {
    const t = s.enum ? 'enum' : s.type || 'string';
    const title = E(s.description || '');
    const attrs = `data-f="${E(name)}" data-t="${t}" ${required ? 'data-req="1"' : ''} data-d="${E(JSON.stringify(s.default === undefined ? null : s.default))}" title="${title}"`;
    const label = `${E(name)}${required ? ' *' : ''}`;
    if (t === 'boolean') return `<label class="chk" title="${title}"><input type="checkbox" ${attrs} ${s.default ? 'checked' : ''}> ${label}</label>`;
    if (t === 'enum') return `<label title="${title}">${label}<select ${attrs}><option value="">(default)</option>${s.enum.map((v) => `<option>${E(v)}</option>`).join('')}</select></label>`;
    if (t === 'object' || t === 'array') return `<label title="${title}">${label} <small class="ol-muted">${t} (JSON)</small><textarea rows="2" ${attrs}></textarea></label>`;
    return `<label title="${title}">${label}<input type="${t === 'integer' || t === 'number' ? 'number' : 'text'}" ${t === 'number' ? 'step="any"' : ''} ${attrs} placeholder="${E((s.description || '').slice(0, 60))}"></label>`;
  }
  function schemaForm(schema) {
    const props = (schema && schema.properties) || {};
    const req = new Set((schema && schema.required) || []);
    return `<div class="ol-fields">${Object.keys(props).map((n) => field(n, props[n], req.has(n))).join('')}</div>`;
  }
  function readForm(root) {
    const out = {};
    root.querySelectorAll('[data-f]').forEach((el) => {
      const d = JSON.parse(el.dataset.d || 'null');
      let v;
      if (el.dataset.t === 'boolean') v = el.checked;
      else if (el.value === '') return;
      else if (el.dataset.t === 'integer') v = parseInt(el.value, 10);
      else if (el.dataset.t === 'number') v = Number(el.value);
      else if (el.dataset.t === 'object' || el.dataset.t === 'array') { try { v = JSON.parse(el.value); } catch (_) { v = el.value; } }
      else v = el.value;
      if (JSON.stringify(v) !== JSON.stringify(d) || el.dataset.req) out[el.dataset.f] = v;
    });
    return out;
  }

  // ---- jobs (pulls, pushes, creates, imports, moves) ------------------------------------------------------------------------
  function jobsHtml(jobs) {
    const list = (jobs || []).slice(0, 8);
    if (!list.length) return '<p class="ol-muted">Nothing running.</p>';
    return list.map((j) => `<div style="margin:6px 0"><div class="ol-row"><span class="ol-chip ${j.state === 'done' ? 'on' : j.state === 'failed' ? 'warn' : ''}">${E(j.kind)} · ${E(j.state)}</span><span class="ol-mono" style="flex:1">${E(j.model)}</span></div>
      ${j.state === 'running' ? `<div class="ol-bar"><i style="width:${j.total ? (j.completed / j.total * 100).toFixed(1) : 2}%"></i></div><span class="ol-muted">${E(j.status)}${j.total ? ` · ${gb(j.completed)} of ${gb(j.total)}` : ''}</span>` : ''}
      ${j.error ? `<div class="ol-muted">${E(j.error)}</div>` : ''}</div>`).join('');
  }
  function watchJobs() {
    if (st.poll) return;
    st.poll = setInterval(async () => {
      try {
        const r = await api('/api/ollama/jobs');
        const box = $('ol-jobs');
        if (box) box.innerHTML = jobsHtml(r.jobs);
        if (!r.jobs.some((j) => j.state === 'running')) { clearInterval(st.poll); st.poll = null; if (st.tab === 'models' || st.tab === 'get') load(); }
      } catch (_) { /* keep polling */ }
    }, 2500);
  }

  // ---- tabs -----------------------------------------------------------------------------------------------------------------
  async function renderOverview(body) {
    const s = await api('/api/ollama/status');
    const sm = s.summary;
    if (!sm.running) { body.innerHTML = `<div class="ol-card"><h4>Ollama is not running</h4><p>${E(sm.hint || '')}. Start Ollama; ABP finds it at a configured provider's address or http://127.0.0.1:11434.</p></div>`; return; }
    const full = s.full;
    const settings = Object.entries(full.settings || {}).map(([k, v]) => `<tr><td class="ol-mono">${E(k)}</td><td class="ol-mono">${E(v)}</td></tr>`).join('');
    body.innerHTML = `${(sm.warnings || []).map((w) => `<div class="ol-warn">⚠ ${E(w)}</div>`).join('')}<div class="ol-grid">
      <div class="ol-card"><h4>Ollama</h4><div class="ol-mono">${E(sm.url)}</div><div class="ol-muted">version ${E(sm.version)} · provider “${E(sm.provider || '—')}” · ${sm.installed} model(s) installed</div>
        <div style="margin-top:6px">${sm.account ? `Signed in as <b>${E(sm.account.name)}</b> (${E(sm.account.plan)} plan) · cloud models ${sm.cloud_enabled ? 'on' : 'off'}` : 'Not signed in to ollama.com (cloud models and web search need it)'}</div></div>
      <div class="ol-card"><h4>Loaded now</h4>${(sm.loaded || []).length ? sm.loaded.map((m) => `<div class="ol-row"><span class="ol-chip on">loaded</span><span class="ol-mono" style="flex:1">${E(m.model)}</span><span class="ol-muted" title="As Ollama reports it; for some models (gemma4) it undercounts. The GPU figure on the Unsloth page is measured.">${m.vram_gb} GB VRAM (reported) · ${m.context_length ? m.context_length.toLocaleString() + ' ctx' : ''}</span><button class="btn" data-ol="unload" data-model="${E(m.model)}">Unload</button></div>`).join('') : '<p class="ol-muted">Nothing loaded. Ollama loads a model on the first request; Models → Load does it now at a context that fits.</p>'}</div>
      <div class="ol-card"><h4>Where models live</h4><div class="ol-mono">${E(sm.storage.models_dir)}</div><div class="ol-muted">${sm.storage.free_gb} GB free</div>
        ${full.storage.old_models_dir ? `<p>There are older models in <span class="ol-mono">${E(full.storage.old_models_dir)}</span> that Ollama no longer sees.</p><button class="btn" data-ol="move">Move them here</button>` : ''}
        <p class="ol-muted">The folder is Ollama's own setting (OLLAMA_MODELS, or Ollama's Settings → Model location); changing it needs Ollama restarted.</p></div>
      <div class="ol-card"><h4>Running jobs</h4><div id="ol-jobs">${jobsHtml(full.jobs)}</div></div>
      <div class="ol-card" style="grid-column:1/-1"><h4>Server settings</h4>${settings ? `<div class="ol-scroll"><table class="ol-table"><tbody>${settings}</tbody></table></div>` : '<p class="ol-muted">Could not read them (a remote server, or no access).</p>'}</div></div>`;
    if ((full.jobs || []).some((j) => j.state === 'running')) watchJobs();
  }

  async function renderModels(body) {
    const r = await api('/api/ollama/models');
    body.innerHTML = `<div class="ol-card"><h4>Installed</h4><p class="ol-muted">Bots use these as <span class="ol-mono">&lt;provider&gt;/&lt;model&gt;</span> (e.g. ollama/qwen3.5:9b); the model router offers them to bots on auto. Leave context empty for ABP's default (32,768).</p>
      ${r.models.length ? `<div class="ol-scroll tall"><table class="ol-table"><thead><tr><th>model</th><th>size</th><th>kind</th><th></th></tr></thead><tbody>${r.models.map((m) => `<tr><td class="ol-mono">${E(m.model)}${m.loaded ? ' <span class="ol-chip on">loaded</span>' : ''}${m.cloud ? ' <span class="ol-chip">cloud</span>' : ''}</td>
        <td>${m.cloud ? '—' : m.size_gb + ' GB'}</td><td class="ol-muted">${E([m.family, m.parameters, m.quantization].filter(Boolean).join(' · '))}</td>
        <td><div class="ol-row">${m.cloud ? '' : m.loaded ? `<button class="btn" data-ol="unload" data-model="${E(m.model)}">Unload</button>` : `<input type="number" min="512" step="1024" placeholder="context" style="width:92px" data-ctx-for="${E(m.model)}" aria-label="Context length"><button class="btn primary" data-ol="load" data-model="${E(m.model)}">Load</button>`}
          <button class="btn" data-ol="show" data-model="${E(m.model)}">Details</button><button class="btn" data-ol="copy" data-model="${E(m.model)}">Copy</button><button class="btn danger" data-ol="delete" data-model="${E(m.model)}">Delete</button></div></td></tr>`).join('')}</tbody></table></div>` : '<p class="ol-muted">No models yet. Get one on the next tab.</p>'}
      <div id="ol-show" style="margin-top:10px"></div></div>`;
  }

  async function showModel(model) {
    const box = $('ol-show');
    box.innerHTML = '<p class="ol-muted">Asking Ollama…</p>';
    try {
      const d = await api('/api/ollama/show?model=' + encodeURIComponent(model));
      box.innerHTML = `<h4>${E(model)}</h4><div>${(d.capabilities || []).map((c) => `<span class="ol-chip on">${E(c)}</span>`).join(' ')} ${d.context_length ? `<span class="ol-muted">native context ${Number(d.context_length).toLocaleString()}</span>` : ''}</div>
        ${d.parameters ? `<h4 style="margin-top:8px">Parameters</h4><div class="ol-pre">${E(d.parameters)}</div>` : ''}${d.system ? `<h4 style="margin-top:8px">System prompt</h4><div class="ol-pre">${E(d.system)}</div>` : ''}
        <details style="margin-top:8px"><summary class="ol-muted">Modelfile</summary><div class="ol-pre">${E(d.modelfile || '')}</div></details>
        ${d.template ? `<details><summary class="ol-muted">Template</summary><div class="ol-pre">${E(d.template)}</div></details>` : ''}`;
    } catch (e) { box.innerHTML = `<p class="ol-muted">${E(errText(e))}</p>`; }
  }

  async function renderGet(body) {
    const [rec, jobs, files] = await Promise.all([api('/api/ollama/recommendations').catch(() => ({ recommendations: [] })), api('/api/ollama/jobs'), api('/api/ollama/gguf-files').catch(() => ({ files: [] }))]);
    body.innerHTML = `<div class="ol-grid">
      <div class="ol-card"><h4>Pull a model</h4><form class="ol-form" data-ol-form="pull"><label>Name <input name="model" placeholder="qwen3.5:9b, gemma4:26b, gemma4:31b-cloud…" required list="ol-rec"></label>
        <datalist id="ol-rec">${rec.recommendations.map((x) => `<option value="${E(x.model)}">`).join('')}</datalist><div><button class="btn primary" type="submit">Pull</button></div></form>
        <p class="ol-muted">Names come from ollama.com/library. A <span class="ol-mono">:cloud</span> model downloads nothing: it runs on ollama.com under your plan.</p>
        <h4 style="margin-top:10px">Running</h4><div id="ol-jobs">${jobsHtml(jobs.jobs)}</div></div>
      <div class="ol-card"><h4>Recommended by Ollama</h4>${rec.recommendations.map((x) => `<div class="ol-row" style="margin:6px 0"><div style="flex:1"><span class="ol-mono">${E(x.model)}</span> ${x.required_plan ? `<span class="ol-chip ${x.required_plan === 'free' ? 'on' : 'warn'}">${E(x.required_plan)} plan</span>` : '<span class="ol-chip">local</span>'}<br><span class="ol-muted">${E(x.description || '')}</span></div><button class="btn" data-ol="pull" data-model="${E(x.model)}">Pull</button></div>`).join('') || '<p class="ol-muted">None offered.</p>'}</div>
      <div class="ol-card" style="grid-column:1/-1"><h4>Import a GGUF file</h4><p class="ol-muted">Make a .gguf on this computer an Ollama model, for example one Unsloth Studio downloaded, without downloading it again. The file is uploaded to Ollama once; the model gets ABP's default context.</p>
        <form class="ol-form" data-ol-form="import"><label>File <select name="path" required>${files.files.map((f) => `<option value="${E(f.path)}">${E(f.name)} (${gb(f.size_bytes)})</option>`).join('')}<option value="">(type a path below)</option></select></label>
          <label>…or a path <input name="custom_path" placeholder="E:\\models\\my-model.gguf"></label><label>Model name <input name="model" placeholder="my-model:latest" required></label>
          <label>System prompt (optional) <input name="system"></label><div><button class="btn primary" type="submit">Import</button></div></form></div></div>`;
    if (jobs.jobs.some((j) => j.state === 'running')) watchJobs();
  }

  async function renderCreate(body) {
    const r = await api('/api/ollama/models');
    body.innerHTML = `<div class="ol-card"><h4>Create a model</h4><p class="ol-muted">A new model built on an installed one, with your own system prompt, template and default settings: what a Modelfile does. It shares the base model's files, so it takes no extra space.</p>
      <form class="ol-form" data-ol-form="create"><div class="ol-fields"><label>Name * <input name="model" required placeholder="my-assistant:latest"></label>
        <label>From * <select name="from" required>${r.models.map((m) => `<option>${E(m.model)}</option>`).join('')}</select></label>
        <label>Quantize (float16 bases only) <select name="quantize"><option value="">(no)</option><option>q4_K_M</option><option>q4_K_S</option><option>q8_0</option></select></label></div>
        <label>System prompt <textarea name="system" rows="3"></textarea></label>
        <label>Parameters (JSON) <textarea name="parameters" rows="3" class="ol-mono">{"num_ctx": 32768, "temperature": 0.7}</textarea></label>
        <details><summary class="ol-muted">Template, example messages, license</summary><div class="ol-form" style="margin-top:8px"><label>Template <textarea name="template" rows="4" class="ol-mono"></textarea></label>
          <label>Messages (JSON) <textarea name="messages" rows="3" class="ol-mono" placeholder='[{"role":"user","content":"hi"},{"role":"assistant","content":"Hello!"}]'></textarea></label><label>License <textarea name="license" rows="2"></textarea></label></div></details>
        <div><button class="btn primary" type="submit">Create</button></div></form><div id="ol-jobs" style="margin-top:10px"></div></div>`;
  }

  async function renderAll(body) {
    if (!st.ops) st.ops = (await api('/api/ollama/operations')).operations;
    const groups = {};
    st.ops.forEach((o) => { groups[o.group] = (groups[o.group] || 0) + 1; });
    const list = st.ops.filter((o) => !st.group || o.group === st.group);
    body.innerHTML = `<p class="ol-muted">Every route Ollama ${E((st.ops[0] || {}).version || '')} serves: models, running them (chat, generate, embeddings), the OpenAI- and Anthropic-compatible APIs, your ollama.com account and web search. Anything that changes Ollama needs the desktop dashboard token and is recorded in the audit log.</p>
      <div class="ol-split"><div class="ol-card"><div class="ol-row" style="margin-bottom:8px"><select data-ol-group aria-label="Group"><option value="">every group (${st.ops.length})</option>${Object.keys(groups).sort().map((g) => `<option value="${E(g)}" ${st.group === g ? 'selected' : ''}>${E(g)} (${groups[g]})</option>`).join('')}</select></div>
        <ul class="ol-ops">${list.map((o) => `<li data-ol="op" data-id="${E(o.id)}" class="${st.op && st.op.id === o.id ? 'sel' : ''}"><span class="ol-chip">${o.method}</span> <span class="ol-mono">${E(o.path)}</span><br><span class="ol-muted">${E(o.summary)}</span></li>`).join('')}</ul></div>
        <div class="ol-card" id="ol-op"><p class="ol-muted">Pick a route.</p></div></div>`;
    if (st.op) showOp(st.op.id);
  }

  function showOp(id) {
    const o = st.ops.find((x) => x.id === id);
    st.op = o;
    document.querySelectorAll('#ol-root .ol-ops li').forEach((li) => li.classList.toggle('sel', li.dataset.id === id));
    const params = o.params.length ? `<h4 style="margin-top:8px">Parameters</h4>${schemaForm({ properties: Object.fromEntries(o.params.map((p) => [p.name, p.schema])), required: o.params.filter((p) => p.required).map((p) => p.name) })}` : '';
    const body = o.body && o.body.properties && Object.keys(o.body.properties).length ? `<h4 style="margin-top:8px">Body</h4>${schemaForm(o.body)}` : '';
    $('ol-op').innerHTML = `<h4><span class="ol-chip">${o.method}</span> ${E(o.path)}</h4><div>${E(o.summary)}</div>
      ${o.multipart ? '<p class="ol-muted">This route takes an uploaded file; the agent can call it with ollama_call.</p>' : `<form data-ol-form="call"><div data-part="params">${params}</div><div data-part="body">${body}</div>
      <div style="margin-top:10px"><button class="btn ${o.mutating ? 'danger' : 'primary'}" type="submit">${o.mutating ? 'Run (changes Ollama)' : 'Run'}</button></div></form>`}<div id="ol-op-out" style="margin-top:10px"></div>`;
  }

  const RENDER = { overview: renderOverview, models: renderModels, get: renderGet, create: renderCreate, all: renderAll };

  async function load() {
    const root = $('ol-root');
    if (!root || loading) return;
    loading = true;
    try {
      root.innerHTML = `<div class="ol-tabs" role="tablist">${TABS.map(([k, l]) => `<button role="tab" aria-selected="${st.tab === k}" data-ol="tab" data-tab="${k}">${l}</button>`).join('')}
        <span style="flex:1"></span><button class="btn" data-ol="refresh" aria-label="Refresh" title="Refresh">↻</button></div><div id="ol-body" role="tabpanel"><p class="ol-muted">Loading…</p></div>`;
      await (RENDER[st.tab] || renderOverview)($('ol-body'));
    } catch (e) {
      const m = errText(e);
      if ($('ol-body')) $('ol-body').innerHTML = `<p class="cardnote">${/not running|could not reach/.test(m) ? 'Ollama is not running. Start it, then refresh.' : 'Ollama data unavailable: ' + E(m)}</p>`;
    } finally { loading = false; }
  }
  async function show(tab) { st.tab = tab; try { localStorage.setItem('ol-tab', tab); } catch (_) {} await load(); }
  async function busy(el, label, fn) { const old = el.textContent; el.disabled = true; el.textContent = label; try { return await fn(); } finally { el.disabled = false; el.textContent = old; } }

  async function act(el) {
    const a = el.dataset.ol;
    const model = el.dataset.model;
    try {
      if (a === 'tab') return show(el.dataset.tab);
      if (a === 'refresh') { st.ops = null; return load(); }
      if (a === 'op') return showOp(el.dataset.id);
      if (a === 'show') return showModel(model);
      if (a === 'load') {
        const ctxEl = document.querySelector(`#ol-root [data-ctx-for="${CSS.escape(model)}"]`);
        const r = await busy(el, 'Loading…', () => post('/api/ollama/load', { model, context: ctxEl && ctxEl.value ? Number(ctxEl.value) : 0 }));
        toast(`${model} loaded · ${Number(r.context_length).toLocaleString()} ctx · ${r.vram_gb} GB VRAM`, 'good'); return load();
      }
      if (a === 'unload') { await busy(el, 'Unloading…', () => post('/api/ollama/unload', { model })); toast(model + ' unloaded', 'good'); return load(); }
      if (a === 'pull') { await post('/api/ollama/pull', { model }); toast('Pulling ' + model, 'good'); if (st.tab !== 'get') return show('get'); return renderGet($('ol-body')).then(watchJobs); }
      if (a === 'copy') { const dest = window.prompt(`Copy ${model} as:`, model.split(':')[0] + '-copy:latest'); if (!dest) return; await post('/api/ollama/copy', { source: model, destination: dest }); toast('Copied to ' + dest, 'good'); return load(); }
      if (a === 'delete') { if (!window.confirm(`Delete ${model}? Its files are removed unless another model shares them.`)) return; await post('/api/ollama/delete', { model }); toast(model + ' deleted', 'good'); return load(); }
      if (a === 'move') { if (!window.confirm('Move the older models into the folder Ollama uses now? Each file is copied and checked before the old copy is removed.')) return; await post('/api/ollama/move-models', {}); toast('Moving models', 'good'); return watchJobs(); }
    } catch (e) { toast(errText(e), 'error'); }
  }

  async function submit(form, ev) {
    ev.preventDefault();
    const kind = form.dataset.olForm;
    const data = Object.fromEntries(new FormData(form).entries());
    try {
      if (kind === 'pull') { await post('/api/ollama/pull', { model: data.model.trim() }); toast('Pulling ' + data.model, 'good'); return renderGet($('ol-body')).then(watchJobs); }
      if (kind === 'import') {
        const path = (data.custom_path || '').trim() || data.path;
        if (!path) return toast('Choose a file or type a path', 'error');
        await post('/api/ollama/import-gguf', { path, model: data.model.trim(), system: data.system || null });
        toast('Importing ' + path, 'good'); return renderGet($('ol-body')).then(watchJobs);
      }
      if (kind === 'create') {
        const spec = { from: data.from, system: data.system || null, template: data.template || null, license: data.license || null, quantize: data.quantize || null };
        if (data.parameters.trim()) spec.parameters = JSON.parse(data.parameters);
        if (data.messages.trim()) spec.messages = JSON.parse(data.messages);
        await post('/api/ollama/create', { model: data.model.trim(), spec });
        toast('Creating ' + data.model, 'good'); return watchJobs();
      }
      if (kind === 'call') {
        const o = st.op;
        if (o.mutating && !window.confirm(`${o.method} ${o.path} changes Ollama. Run it?`)) return;
        const args = readForm(form.querySelector('[data-part=params]'));
        if (o.body) args.body = readForm(form.querySelector('[data-part=body]'));
        const out = $('ol-op-out');
        out.innerHTML = '<p class="ol-muted">Running…</p>';
        const r = await post('/api/ollama/call', { operation: o.id, args });
        out.innerHTML = `<div class="ol-pre">${E(typeof r.result === 'string' ? r.result : JSON.stringify(r.result, null, 2))}</div>`;
      }
    } catch (e) { toast(errText(e), 'error'); const out = $('ol-op-out'); if (kind === 'call' && out) out.innerHTML = `<p class="ol-muted">${E(errText(e))}</p>`; }
  }

  function init() {
    const root = $('ol-root');
    if (!root) return;
    css();
    root.addEventListener('click', (ev) => { const el = ev.target.closest('[data-ol]'); if (el && root.contains(el)) act(el); });
    root.addEventListener('submit', (ev) => { const f = ev.target.closest('form[data-ol-form]'); if (f) submit(f, ev); });
    root.addEventListener('change', (ev) => { if (ev.target.matches('[data-ol-group]')) { st.group = ev.target.value; renderAll($('ol-body')); } });
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'ollama') load(); };
    window.addEventListener('hashchange', onHash);
    onHash();
    if ('IntersectionObserver' in window) {
      let seen = false;
      new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting && !seen) { seen = true; load(); } })).observe(root);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
