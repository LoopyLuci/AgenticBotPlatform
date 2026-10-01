// Local AI and Neural Lab pages (bot/localai, bot/neurallab).
//   Local AI     Overview (the Ollama-compatible server, engine, GPUs, loaded models, settings) · Models (pull, import,
//                reference, show, delete) · Discover (other programs' models: use them in place or import) ·
//                Fine-tune (the GPU training environment, LoRA SFT/DPO runs with live loss, export, Amethyst modules)
//   Neural Lab   Runs · Designer (a spec: check shapes/parameters, train on the GPU) · Projects (BrainBuilder graphs,
//                KotMoE designs and checkpoints in; Amethyst, Kestrion) · System models (the models that tune this
//                machine: advice, CPU policy, measuring, retraining) · Telemetry (this machine, live)
// Identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/); tests/test_localai.py fails
// if the two differ. It uses the page's own api(), esc() and showToast(), and draws into #aip-root and #lab-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { const d = JSON.parse(m).detail; m = d || m; } catch (_) {} return typeof m === 'string' ? m : JSON.stringify(m); };
  const send = (path, method, body) => api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
  const A = '/api/localai', L = '/api/lab';
  const human = (n) => { n = +n || 0; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + ' ' + u[i]; };
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : '');
  const chip = (cls, text) => `<span class="aip-chip ${cls}">${E(text)}</span>`;
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (_) {} return null; };
  const ai = { tab: store('aip.tab') || 'overview', busy: false };
  const lab = { tab: store('lab.tab') || 'runs', busy: false };
  const ACTIVE = ['starting', 'loading', 'training', 'merging', 'stopping'];

  function css() {
    if ($('aip-css')) return;
    const s = document.createElement('style');
    s.id = 'aip-css';
    s.textContent = `.aip-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}.aip-tabs button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.aip-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
.aip-card{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:var(--surface);min-width:0;display:grid;gap:8px;align-content:start}
.aip-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.aip-muted{color:var(--muted);font-size:12px}
.aip-table{width:100%;border-collapse:collapse;font-size:12.5px}.aip-table td,.aip-table th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.aip-wrap{overflow-x:auto}.aip-mono{font-family:var(--font-mono,monospace);font-size:12px;white-space:pre-wrap;word-break:break-word}
.aip-log{font-family:var(--font-mono,monospace);font-size:12px;white-space:pre-wrap;background:var(--surface-2);border-radius:8px;padding:8px;max-height:260px;overflow:auto}
.aip-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.aip-chip.ok{background:var(--good-soft);color:var(--good,#1f9d55)}.aip-chip.warn{background:#fff4d6;color:#8a6100}.aip-chip.bad{background:var(--bad-soft,#fde8e8);color:var(--bad,#c53030)}
.aip-form{display:grid;grid-template-columns:minmax(110px,160px) 1fr;gap:6px 10px;align-items:center}@media (max-width:640px){.aip-form{grid-template-columns:1fr}}
#aip-root input,#aip-root select,#aip-root textarea,#lab-root input,#lab-root select,#lab-root textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%;min-width:0}
#lab-root textarea{width:100%;min-height:260px;font-family:var(--font-mono,monospace);font-size:12px}
.aip-spark{width:100%;height:60px}.aip-spark path{fill:none;stroke:var(--accent);stroke-width:1.5}.aip-spark path.b{stroke:var(--good,#1f9d55)}`;
    document.head.appendChild(s);
  }

  async function follow(run, logId) {
    const el = $(logId);
    if (el) { el.style.display = ''; el.textContent = ''; }
    let seen = 0;
    for (;;) {
      const r = await api(`${A}/runs/${run.run}?since=${seen}`);
      seen = r.log_total;
      if (el && r.log.length) { el.textContent += (el.textContent ? '\n' : '') + r.log.join('\n'); el.scrollTop = el.scrollHeight; }
      if (r.done) {
        if (el) el.textContent += '\n' + (r.error ? `failed: ${r.error}` : 'done');
        if (r.error) toast(r.error, 'error'); else toast(`${run.title}: done`);
        return r;
      }
      await new Promise((ok) => setTimeout(ok, 1200));
    }
  }
  const runInto = async (p, logId, after) => { try { await follow(await p, logId); } catch (e) { toast(errText(e), 'error'); } if (after) after(); };
  const tabsHtml = (tabs, cur, attr) => `<div class="aip-tabs">${tabs.map(([k, l]) => `<button class="btn ghost ${cur === k ? 'on' : ''}" ${attr}="${k}">${l}</button>`).join('')}</div>`;
  const metrics = (f) => Object.entries(f || {}).filter(([k]) => k.startsWith('val_')).map(([k, v]) => `${k.slice(4)} ${E(v)}`).join(' · ');

  // ================= Local AI =================
  async function aiOverview(o) {
    const s = o.server, e = o.engine;
    return `<div class="aip-grid"><div class="aip-card"><b>Model server</b>
<div>${s.running ? chip('ok', 'running') : chip('', 'stopped')} <span class="aip-mono">${E(s.url)}</span> ${s.version ? `· Ollama API ${E(s.version)}` : ''}</div>
<div class="aip-muted">Any Ollama app or library works against it: set OLLAMA_HOST=${E(s.url)}. OpenAI clients: ${E(s.url)}/v1</div>
<div class="aip-row"><button class="btn" id="aip-srv">${s.running ? 'Stop' : 'Start'}</button>
<label class="aip-row"><input type="checkbox" id="aip-auto" ${o.settings.autostart !== false ? 'checked' : ''}> start with ABP</label>
<label class="aip-row"><input type="checkbox" id="aip-tune" ${o.settings.auto_tune !== false ? 'checked' : ''}> tune options per model</label></div></div>
<div class="aip-card"><b>Engine</b><div>${e ? `llama.cpp ${E(e.build)} · ${E(e.backend)}` : chip('warn', 'not installed')}</div>
<div>${o.gpus.map((g) => `${E(g.name)} ${g.vram_gb ? E(g.vram_gb) + ' GB' : ''}`).join('<br>') || '<span class="aip-muted">no GPU found</span>'}</div>
<div class="aip-row"><button class="btn ghost" id="aip-eng">${e ? 'Update engine' : 'Install engine'}</button></div><div class="aip-log" id="aip-eng-log" style="display:none"></div></div>
<div class="aip-card"><b>Loaded now</b>${o.running.length ? `<table class="aip-table">${o.running.map((m) => `<tr><td>${E(m.name)}</td><td>${human(m.size_vram || m.size)}</td><td class="aip-muted">until ${E((m.expires_at || '').slice(11, 19))}</td></tr>`).join('')}</table>` : '<div class="aip-muted">nothing loaded (models load on first use)</div>'}</div>
<div class="aip-card"><b>Settings</b><div class="aip-form">
<span>Keep loaded (s)</span><input id="aip-ka" type="number" value="${E(o.settings.keep_alive_s)}">
<span>Default context</span><input id="aip-ctx" type="number" value="${E(o.settings.default_ctx)}">
<span>Models at once</span><input id="aip-max" type="number" value="${E(o.settings.max_loaded)}"></div>
<div class="aip-row"><button class="btn ghost" id="aip-save">Save</button><span class="aip-muted">Store: ${E(o.home)}</span></div></div></div>`;
  }

  function aiModels(o) {
    return `<div class="aip-card"><div class="aip-row"><input id="aip-pull" placeholder="qwen2.5:0.5b or hf.co/org/repo:Q4_K_M" style="flex:1"><button class="btn" id="aip-pull-go">Pull</button></div>
<div class="aip-row"><input id="aip-imp-path" placeholder="path to a .gguf" style="flex:1"><input id="aip-imp-name" placeholder="name, e.g. me/model:tag"><button class="btn ghost" data-imp="0">Import</button><button class="btn ghost" data-imp="1">Use in place</button></div>
<div class="aip-log" id="aip-pull-log" style="display:none"></div>
<div class="aip-wrap"><table class="aip-table"><tr><th>Name</th><th>Size</th><th>Quant</th><th>Family</th><th>Source</th><th></th></tr>
${o.models.map((m) => `<tr><td class="aip-mono">${E(m.name)}</td><td>${human(m.size)}</td><td>${E(m.details.quantization_level)}</td><td>${E(m.details.family)} ${E(m.details.parameter_size)}</td><td class="aip-muted">${E(m.source)}</td>
<td><button class="btn ghost" data-show="${E(m.name)}">Modelfile</button> <button class="btn ghost" data-rm="${E(m.name)}">Remove</button></td></tr>`).join('') || '<tr><td class="aip-muted" colspan="6">no models yet</td></tr>'}</table></div>
<div class="aip-mono" id="aip-show"></div></div>`;
  }

  async function aiDiscover() {
    const d = await api(`${A}/discover`);
    return `<div class="aip-card"><b>Models other programs keep here</b><div class="aip-muted">Ollama stores are read in place; GGUF files can be used where they are or imported; Hugging Face checkpoints are bases for fine-tuning.</div>
<table class="aip-table">${d.locations.filter((l) => l.exists).map((l) => `<tr><td>${E(l.app)}</td><td class="aip-mono">${E(l.path)}</td><td>${E(l.models)}</td></tr>`).join('')}</table>
<div class="aip-row"><button class="btn" id="aip-adopt-all">Use everything found</button></div>
<div class="aip-wrap"><table class="aip-table">${d.found.map((it, i) => `<tr><td>${chip(it.adopted ? 'ok' : '', it.adopted ? 'in use' : it.kind)}</td><td class="aip-mono">${E(it.name || it.path)}<div class="aip-muted">${E(it.app)} · ${E(it.path)}${it.size ? ' · ' + human(it.size) : ''}${it.kind === 'safetensors' && !it.tokenizer ? ' · tokenizer missing (fetched when training)' : ''}</div></td>
<td>${it.adopted ? '' : (it.actions || []).filter((a) => a !== 'train-base').map((a) => `<button class="btn ghost" data-adopt="${i}" data-act="${a}">${E(a)}</button>`).join(' ')}</td></tr>`).join('') || '<tr><td class="aip-muted">nothing found</td></tr>'}</table></div></div>`
      + `<script type="application/json" id="aip-found">${E(JSON.stringify(d.found))}</script>`;
  }

  async function aiTrain() {
    const t = await api(`${A}/train`);
    const env = t.env;
    const d = await api(`${A}/discover`).catch(() => ({ found: [] }));
    const bases = d.found.filter((x) => x.kind === 'safetensors');
    return `<div class="aip-grid"><div class="aip-card"><b>Training environment</b>
<div>${env.installed ? (env.gpu ? chip('ok', 'GPU ready') : chip('bad', 'no GPU')) : chip('warn', 'not set up')} ${env.installed ? `PyTorch ${E(env.torch)} · ${E(env.device)} ${env.vram_gb ? E(env.vram_gb) + ' GB' : ''}` : ''}</div>
${env.installed ? '' : '<div class="aip-row"><button class="btn" id="aip-setup">Set up (downloads PyTorch for this GPU)</button></div>'}<div class="aip-log" id="aip-setup-log" style="display:none"></div>
<div class="aip-muted">Training runs on the GPU only; conversion and quantizing use a few CPU threads.</div></div>
<div class="aip-card"><b>New fine-tune</b><div class="aip-form">
<span>Base model</span><select id="aip-base">${bases.map((b) => `<option value="${E(b.repo)}">${E(b.repo)} (${human(b.size)})</option>`).join('')}<option value="">a folder…</option></select>
<span>Folder</span><input id="aip-base-dir" placeholder="(or a Hugging Face checkpoint folder)">
<span>Data</span><select id="aip-data">${t.datasets.map((x) => `<option value="${E(x.path)}">${E(x.origin)}: ${E(x.name)} ${x.rows ? '(' + x.rows + ' rows)' : ''}</option>`).join('')}<option value="">a file…</option></select>
<span>Data file</span><input id="aip-data-file" placeholder="JSONL: messages, conversations, instruction/output, prompt/chosen/rejected">
<span>Method</span><select id="aip-method"><option value="sft">SFT (examples)</option><option value="dpo">DPO (preferences)</option></select>
<span>Steps</span><input id="aip-steps" type="number" value="100"><span>Learning rate</span><input id="aip-lr" type="number" step="0.00001" value="0.0002">
<span>LoRA rank</span><input id="aip-rank" type="number" value="16"><span>New model</span><input id="aip-name" placeholder="abp/my-model:q4_k_m">
<span>Export</span><select id="aip-export">${t.quants.map((q) => `<option ${q === 'q4_k_m' ? 'selected' : ''}>${E(q)}</option>`).join('')}</select></div>
<div class="aip-row"><button class="btn" id="aip-train-go" ${env.installed ? '' : 'disabled'}>Train on the GPU</button></div></div></div>
<div class="aip-card" style="margin-top:12px"><b>Runs</b><div class="aip-wrap"><table class="aip-table"><tr><th>Run</th><th>State</th><th>Base → model</th><th>Progress</th><th></th></tr>
${t.runs.map((r) => `<tr><td class="aip-mono">${E(r.id)}</td><td>${chip(r.state === 'done' ? 'ok' : r.state === 'failed' ? 'bad' : 'warn', r.state || '')}</td>
<td>${E(r.base)} → <span class="aip-mono">${E(r.name)}</span>${r.error ? `<div class="aip-muted">${E(r.error)}</div>` : ''}${r.exported ? `<div class="aip-muted">${r.exported.error ? 'export failed: ' + E(r.exported.error).slice(0, 200) : 'in the store as ' + E(r.exported.name)}</div>` : ''}</td>
<td>step ${E(r.step || 0)}/${E(r.total_steps || '?')} · loss ${E(r.loss ?? '-')}${r.gpu_mem_gb ? ' · ' + E(r.gpu_mem_gb) + ' GB' : ''}${r.eta ? ' · ' + Math.round(r.eta) + ' s left' : ''}</td>
<td>${ACTIVE.includes(r.state) ? `<button class="btn ghost" data-stop="${E(r.id)}">Stop</button>` : ''}${r.state === 'done' ? `<button class="btn ghost" data-export="${E(r.id)}">Export</button> <button class="btn ghost" data-ame="${E(r.id)}">Amethyst module</button>` : ''}</td></tr>`).join('') || '<tr><td class="aip-muted" colspan="5">no runs yet</td></tr>'}</table></div>
<div class="aip-log" id="aip-run-log" style="display:none"></div></div>`;
  }

  function wireAi(root, o) {
    const on = (sel, fn) => root.querySelectorAll(sel).forEach((b) => b.addEventListener('click', () => fn(b)));
    on('[data-aitab]', (b) => { ai.tab = b.dataset.aitab; store('aip.tab', ai.tab); renderAi(); });
    on('#aip-srv', async () => { try { await send(`${A}/server/${o.server.running ? 'stop' : 'start'}`, 'POST'); } catch (e) { toast(errText(e), 'error'); } setTimeout(renderAi, 1500); });
    root.querySelectorAll('#aip-auto,#aip-tune').forEach((c) => c.addEventListener('change', () => send(`${A}/settings`, 'PUT', { autostart: $('aip-auto').checked, auto_tune: $('aip-tune').checked }).then(() => toast('Saved')).catch((e) => toast(errText(e), 'error'))));
    on('#aip-save', () => send(`${A}/settings`, 'PUT', { keep_alive_s: +$('aip-ka').value, default_ctx: +$('aip-ctx').value, max_loaded: +$('aip-max').value }).then(() => toast('Saved')).catch((e) => toast(errText(e), 'error')));
    on('#aip-eng', () => runInto(send(`${A}/engine/install`, 'POST', {}), 'aip-eng-log', renderAi));
    on('#aip-pull-go', () => { const n = $('aip-pull').value.trim(); if (n) runInto(send(`${A}/models/pull`, 'POST', { name: n }), 'aip-pull-log', renderAi); });
    on('[data-imp]', (b) => { const path = $('aip-imp-path').value.trim(), name = $('aip-imp-name').value.trim(); if (!path || !name) return toast('a path and a name', 'error');
      send(`${A}/models/import`, 'POST', { name, path, reference: b.dataset.imp === '1' }).then(() => { toast('Added'); renderAi(); }).catch((e) => toast(errText(e), 'error')); });
    on('[data-show]', (b) => api(`${A}/models/show?name=${encodeURIComponent(b.dataset.show)}`).then((r) => { $('aip-show').textContent = r.modelfile; }).catch((e) => toast(errText(e), 'error')));
    on('[data-rm]', (b) => { if (confirm(`Remove ${b.dataset.rm}? (a referenced model's file stays where it is)`)) send(`${A}/models?name=${encodeURIComponent(b.dataset.rm)}`, 'DELETE').then(renderAi).catch((e) => toast(errText(e), 'error')); });
    on('#aip-adopt-all', () => send(`${A}/discover/adopt-all`, 'POST', { gguf: 'reference' }).then((r) => { toast(`${r.length} added`); renderAi(); }).catch((e) => toast(errText(e), 'error')));
    on('[data-adopt]', (b) => { const found = JSON.parse(($('aip-found') || {}).textContent || '[]'); const it = found[+b.dataset.adopt];
      send(`${A}/discover/adopt`, 'POST', { item: it, action: b.dataset.act }).then(() => { toast('Done'); renderAi(); }).catch((e) => toast(errText(e), 'error')); });
    on('#aip-setup', () => { if (confirm('Download PyTorch for this GPU and the training libraries (several GB) into ABP\'s local AI folder?')) runInto(send(`${A}/train/setup`, 'POST'), 'aip-setup-log', renderAi); });
    on('#aip-train-go', () => {
      const base = $('aip-base').value || $('aip-base-dir').value.trim(), data = $('aip-data').value || $('aip-data-file').value.trim();
      if (!base || !data) return toast('choose a base model and data', 'error');
      const body = { base, data: [data], method: $('aip-method').value, max_steps: +$('aip-steps').value, learning_rate: +$('aip-lr').value, rank: +$('aip-rank').value,
        alpha: +$('aip-rank').value, name: $('aip-name').value.trim(), export: $('aip-export').value };
      send(`${A}/train`, 'POST', body).then((r) => { toast(`Training ${r.id} on the GPU`); renderAi(); }).catch((e) => toast(errText(e), 'error'));
    });
    on('[data-stop]', (b) => send(`${A}/train/${b.dataset.stop}/stop`, 'POST').then(renderAi));
    on('[data-export]', (b) => runInto(send(`${A}/train/${b.dataset.export}/export`, 'POST', {}), 'aip-run-log', renderAi));
    on('[data-ame]', (b) => { const name = prompt('Amethyst module name', 'my-skill'); if (name) runInto(send(`${A}/train/${b.dataset.ame}/amethyst`, 'POST', { name, install: true }), 'aip-run-log', renderAi); });
  }

  let aiTimer = null;
  async function renderAi() {
    css();
    const root = $('aip-root');
    if (!root || ai.busy) return;
    ai.busy = true;
    const head = tabsHtml([['overview', 'Overview'], ['models', 'Models'], ['discover', 'Discover'], ['train', 'Fine-tune']], ai.tab, 'data-aitab');
    let o = null;
    try {
      o = await api(A);
      const html = ai.tab === 'models' ? aiModels(o) : ai.tab === 'discover' ? await aiDiscover() : ai.tab === 'train' ? await aiTrain() : await aiOverview(o);
      root.innerHTML = head + html;
    } catch (e) { root.innerHTML = head + `<p class="cardnote">${E(errText(e))}</p>`; }
    ai.busy = false;
    if (o) wireAi(root, o);
    clearTimeout(aiTimer);
    if (o && ai.tab === 'train' && (o.runs || []).some((r) => ACTIVE.includes(r.state)) && location.hash === '#localai') aiTimer = setTimeout(renderAi, 4000);
  }

  // ================= Neural Lab =================
  function labRuns(o) {
    return `<div class="aip-card"><b>Training runs</b><div class="aip-wrap"><table class="aip-table"><tr><th>Run</th><th>Design</th><th>State</th><th>Parameters</th><th>Result</th><th></th></tr>
${o.runs.map((r) => `<tr><td class="aip-mono">${E(r.id)}</td><td>${E(r.name)}<div class="aip-muted">${E((r.origin || {}).project || '')} ${E(r.label !== r.name ? r.label || '' : '')}</div></td>
<td>${chip(r.state === 'done' ? 'ok' : r.state === 'failed' ? 'bad' : 'warn', r.state || '')}</td><td>${(+r.params || 0).toLocaleString()}</td>
<td>${r.state === 'done' ? metrics(r.final) : `step ${E(r.step || 0)}/${E(r.total_steps || '?')} · loss ${E(r.loss ?? '-')} ${metrics(r)}`}${r.error ? `<div class="aip-muted">${E(r.error)}</div>` : ''}${(r.final || {}).sample ? `<div class="aip-mono">${E(r.final.sample)}</div>` : ''}</td>
<td>${ACTIVE.includes(r.state) ? `<button class="btn ghost" data-lstop="${E(r.id)}">Stop</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="6" class="aip-muted">no runs yet</td></tr>'}</table></div></div>`;
  }

  function labDesigner(o) {
    const draft = store('lab.draft') || JSON.stringify({ name: 'my-moe', task: 'classify', input: { kind: 'features', size: 784 },
      layers: [{ op: 'linear', out: 256 }, { op: 'gelu' }, { op: 'moe', experts: 8, top_k: 2, hidden: 256 }, { op: 'linear', out: 10 }], train: { lr: 0.001, epochs: 3 } }, null, 1);
    return `<div class="aip-grid"><div class="aip-card"><b>Design</b><textarea id="lab-spec">${E(draft)}</textarea>
<div class="aip-row"><button class="btn ghost" id="lab-check">Check</button><input id="lab-dname" placeholder="save as"><button class="btn ghost" id="lab-save">Save</button>
<select id="lab-load"><option value="">open a saved design…</option>${o.designs.map((d) => `<option>${E(d.name)}</option>`).join('')}</select></div>
<div class="aip-mono" id="lab-desc"></div></div>
<div class="aip-card"><b>Train on the GPU</b><div class="aip-form"><span>Data</span><input id="lab-data" placeholder="a .csv, .jsonl, .npz or text file">
<span>Target column</span><input id="lab-target" placeholder="(csv/jsonl)"><span>Tokenizer</span><input id="lab-tok" placeholder="byte, words, a KotMoE .bpe, an HF folder">
<span>Steps</span><input id="lab-steps" type="number" placeholder="(from epochs)"></div>
<div class="aip-row"><button class="btn" id="lab-train">Train</button></div>
<b>Ops</b><div class="aip-muted">${o.ops.map((x) => `<b>${E(x.op)}</b>${x.needs.length ? ' (' + E(x.needs.join(', ')) + ')' : ''}: ${E(x.doc)}`).join('<br>')}</div></div></div>`;
  }

  async function labProjects(o) {
    const bb = await api(`${L}/brainbuilder`).catch(() => []), km = await api(`${L}/kotmoe`).catch(() => ({ registry: [], checkpoints: [] }));
    return `<div class="aip-grid"><div class="aip-card"><b>Projects</b><table class="aip-table">${o.projects.map((p) => `<tr><td>${E(p.folder)}</td><td>${p.present ? chip('ok', 'found') : chip('warn', 'missing')}</td><td class="aip-mono">${E(p.path || '')}</td></tr>`).join('')}</table>
<div class="aip-muted">Kestrion reads ABP's model store when ABP starts it (KESTRION_MODEL_DIRS), and ABP finds Kestrion's models under Discover. Amethyst modules come from fine-tunes (Local AI → Fine-tune → Amethyst module).</div></div>
<div class="aip-card"><b>BrainBuilder graphs</b><table class="aip-table">${bb.map((g) => `<tr><td>${E(g.name || g.path)}<div class="aip-muted">${E(g.error || g.task + ' · ' + (+g.params).toLocaleString() + ' parameters')}</div></td><td>${g.error ? '' : `<button class="btn ghost" data-imp-bb="${E(g.path)}">Bring in</button>`}</td></tr>`).join('') || '<tr><td class="aip-muted">none</td></tr>'}</table></div>
<div class="aip-card"><b>KotMoE</b><table class="aip-table">${km.registry.map((r) => `<tr><td>${E(r.id)}<div class="aip-muted">${E(r.experts)} experts · top ${E(r.top_k)} · hidden ${E(r.hidden)} · ${E(r.status)}</div></td><td><button class="btn ghost" data-imp-km="${E(r.id)}">Bring in</button></td></tr>`).join('')}
${km.checkpoints.map((c) => `<tr><td class="aip-mono">${E(c.path)}<div class="aip-muted">kotmoe-gen · ${E(c.layers)} layers · ${E(c.experts)} experts · step ${E(c.step)}</div></td><td><button class="btn ghost" data-imp-kc="${E(c.path)}">Bring in</button></td></tr>`).join('') || ''}</table></div></div>
<div class="aip-mono" id="lab-imp-out"></div>`;
  }

  async function labSystune() {
    const s = await api(`${L}/systune`);
    const p = s.cpu_policy;
    const names = { transfer: 'Drive transfers', llm: 'Model runtime (llama.cpp)', memory: 'Memory forecast', stability: 'CPU stability' };
    return `<div class="aip-grid"><div class="aip-card"><b>CPU policy</b><div>${chip(p.level === 'normal' ? 'ok' : p.level === 'high' ? 'bad' : 'warn', p.level)} ${E(p.threads)} thread(s) for ABP's heavy work${p.avoid.length ? ` · keeping off CPU ${E(p.avoid.join(', '))}` : ''}</div>
${p.reasons.map((r) => `<div class="aip-muted">• ${E(r)}</div>`).join('')}<div class="aip-muted">${E(p.power_losses_30d)} unexpected power loss(es) in 30 days${p.anomaly != null ? ` · load pattern ${E(p.anomaly)}× normal` : ''}</div></div>
${Object.entries(s.models).map(([k, m]) => `<div class="aip-card"><b>${E(names[k] || k)}</b><div>${m.adopted ? chip('ok', 'in use') : chip('', 'learning')} ${E(m.measurements)}/${E(m.needed)} measurements</div>
${m.metrics ? `<div class="aip-muted">${metrics(m.metrics)}</div>` : ''}${m.last_error ? `<div class="aip-muted">${E(m.last_error)}</div>` : ''}
<div class="aip-row">${k === 'transfer' ? '<button class="btn ghost" data-bench="transfer">Measure drives</button>' : ''}${k === 'llm' ? '<button class="btn ghost" data-bench="llm">Measure the GPU</button>' : ''}<button class="btn ghost" data-retrain="${k}">Retrain</button></div></div>`).join('')}
<div class="aip-card"><b>Ask</b><div class="aip-row"><input id="lab-src" placeholder="from, e.g. E:\\" size="8"><input id="lab-dst" placeholder="to, e.g. D:\\" size="8"><input id="lab-gb" type="number" value="50" size="4">GB<button class="btn ghost" id="lab-adv-copy">Copy settings</button></div>
<div class="aip-row"><button class="btn ghost" id="lab-adv-mem">Memory in 5 min</button></div><div class="aip-mono" id="lab-adv"></div></div></div>
<div class="aip-log" id="lab-bench-log" style="display:none"></div>`;
  }

  async function labTelemetry() {
    const t = await api(`${L}/telemetry?seconds=900`);
    const s = t.samples;
    const spark = (key, cls) => { if (s.length < 2) return ''; const t0 = s[0].t, t1 = s[s.length - 1].t || t0 + 1;
      return `<path class="${cls}" d="${s.map((x, i) => `${i ? 'L' : 'M'}${((x.t - t0) / (t1 - t0) * 300).toFixed(1)},${(60 - (+x[key] || 0) * 0.58).toFixed(1)}`).join(' ')}"/>`; };
    const last = s[s.length - 1];
    return `<div class="aip-grid"><div class="aip-card"><b>Last 15 minutes</b><svg class="aip-spark" viewBox="0 0 300 60" preserveAspectRatio="none">${spark('cpu', '')}${spark('ram_used', 'b')}</svg>
<div class="aip-muted">CPU (accent) and memory (green), % · ${E(t.stats.samples)} samples recorded</div>
${last ? `<div>CPU ${E(last.cpu)}% (busiest core ${E(last.cpu_max)}%) · ${E(last.freq_mhz)} MHz · RAM ${E(last.ram_used)}% · GPU ${E(last.gpu_compute || last.gpu_3d || 0)}% ${E(last.gpu_mem_gb)} GB</div>
<table class="aip-table">${Object.entries(last.disks || {}).map(([d, v]) => `<tr><td>${E(d)}</td><td>read ${E(v.read_mb_s)} MB/s</td><td>write ${E(v.write_mb_s)} MB/s</td><td>busy ${E(v.busy)}%</td></tr>`).join('')}</table>` : '<div class="aip-muted">no samples yet (the recorder starts with ABP)</div>'}</div></div>`;
  }

  function wireLab(root) {
    const on = (sel, fn) => root.querySelectorAll(sel).forEach((b) => b.addEventListener('click', () => fn(b)));
    const spec = () => { const t = $('lab-spec').value; store('lab.draft', t); return JSON.parse(t); };
    on('[data-labtab]', (b) => { lab.tab = b.dataset.labtab; store('lab.tab', lab.tab); renderLab(); });
    on('[data-lstop]', (b) => send(`${L}/runs/${b.dataset.lstop}/stop`, 'POST').then(renderLab));
    on('#lab-check', () => { try { send(`${L}/validate`, 'POST', { spec: spec() }).then((r) => { $('lab-desc').textContent = r.text; }).catch((e) => { $('lab-desc').textContent = errText(e); }); } catch (e) { $('lab-desc').textContent = 'not valid JSON: ' + e.message; } });
    on('#lab-save', () => { try { const n = $('lab-dname').value.trim() || spec().name; send(`${L}/designs/${encodeURIComponent(n)}`, 'PUT', { spec: spec() }).then(() => { toast('Saved'); renderLab(); }).catch((e) => toast(errText(e), 'error')); } catch (e) { toast(e.message, 'error'); } });
    const load = $('lab-load'); if (load) load.addEventListener('change', () => { if (load.value) api(`${L}/designs/${encodeURIComponent(load.value)}`).then((s) => { $('lab-spec').value = JSON.stringify(s, null, 1); store('lab.draft', $('lab-spec').value); }); });
    on('#lab-train', () => { try { const data = { path: $('lab-data').value.trim() }; if (!data.path) return toast('a data file', 'error');
      if ($('lab-target').value.trim()) data.target = $('lab-target').value.trim(); if ($('lab-tok').value.trim()) data.tokenizer = $('lab-tok').value.trim();
      const steps = +$('lab-steps').value; send(`${L}/runs`, 'POST', { spec: spec(), data, train: steps ? { max_steps: steps } : {} })
        .then((r) => { toast(`Run ${r.id}: ${(+r.params).toLocaleString()} parameters on the GPU`); lab.tab = 'runs'; renderLab(); }).catch((e) => toast(errText(e), 'error')); } catch (e) { toast(e.message, 'error'); } });
    const imp = (body) => send(`${L}/import`, 'POST', body).then((r) => { $('lab-imp-out').textContent = r.text; store('lab.draft', JSON.stringify(r.spec, null, 1)); toast('Saved as a design (open the Designer)'); }).catch((e) => toast(errText(e), 'error'));
    on('[data-imp-bb]', (b) => imp({ kind: 'brainbuilder', path: b.dataset.impBb }));
    on('[data-imp-km]', (b) => imp({ kind: 'kotmoe-registry', id: b.dataset.impKm }));
    on('[data-imp-kc]', (b) => imp({ kind: 'kotmoe-checkpoint', path: b.dataset.impKc }));
    on('[data-bench]', (b) => { const k = b.dataset.bench; if (!confirm(k === 'transfer' ? 'Measure every drive with room: about 2.5 GB written and read on each (removed afterwards)?' : 'Measure each model with llama-bench on the GPU?')) return;
      runInto(send(`${L}/systune/bench`, 'POST', { kind: k }), 'lab-bench-log', renderLab); });
    on('[data-retrain]', (b) => send(`${L}/systune/train`, 'POST', { kind: b.dataset.retrain }).then((r) => toast(`Training run ${r.id}`)).catch((e) => toast(errText(e), 'error')));
    on('#lab-adv-copy', () => api(`${L}/systune/advice?kind=transfer&src=${encodeURIComponent($('lab-src').value)}&dst=${encodeURIComponent($('lab-dst').value)}&size_gb=${+$('lab-gb').value || 4}`)
      .then((r) => { $('lab-adv').textContent = JSON.stringify(r, null, 1); }).catch((e) => toast(errText(e), 'error')));
    on('#lab-adv-mem', () => api(`${L}/systune/advice?kind=memory`).then((r) => { $('lab-adv').textContent = Object.keys(r).length ? JSON.stringify(r, null, 1) : 'the memory model is still learning'; }));
  }

  let labTimer = null;
  async function renderLab() {
    css();
    const root = $('lab-root');
    if (!root || lab.busy) return;
    lab.busy = true;
    const head = tabsHtml([['runs', 'Runs'], ['designer', 'Designer'], ['projects', 'Projects'], ['systune', 'System models'], ['telemetry', 'Telemetry']], lab.tab, 'data-labtab');
    let o = null;
    try {
      o = await api(L);
      const t = lab.tab;
      const html = t === 'designer' ? labDesigner(o) : t === 'projects' ? await labProjects(o) : t === 'systune' ? await labSystune() : t === 'telemetry' ? await labTelemetry() : labRuns(o);
      root.innerHTML = head + html;
    } catch (e) { root.innerHTML = head + `<p class="cardnote">${E(errText(e))}</p>`; }
    lab.busy = false;
    wireLab(root);
    clearTimeout(labTimer);
    if (o && location.hash === '#lab' && ((lab.tab === 'runs' && o.runs.some((r) => ACTIVE.includes(r.state))) || lab.tab === 'telemetry')) labTimer = setTimeout(renderLab, 5000);
  }

  function init() {
    const onHash = () => { const h = (location.hash || '').replace('#', ''); if (h === 'localai') renderAi(); if (h === 'lab') renderLab(); };
    window.addEventListener('hashchange', onHash);
    onHash();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
