// Cluster panel: this machine and every linked ABP server as nodes of one cluster. Each node's hardware and live
// load; what this machine shares (its owner's offer: off until turned on); submitting jobs (one, a gang across
// nodes, or an array of tasks); and following them (state, node, log, result files).
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_cluster.py fails if the two differ. It uses the page's own api(), esc() and showToast(), and draws
// into #clp-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { const d = JSON.parse(m).detail; m = d && d.error ? d.error + (d.reasons ? '\n' + Object.entries(d.reasons).map(([k, v]) => `${k}: ${v}`).join('\n') : '') : (d || m); } catch (_) {} return typeof m === 'string' ? m : JSON.stringify(m); };
  const post = (path, body, method) => api(path, { method: method || 'POST', body: JSON.stringify(body || {}) });
  const KINDS = ['command', 'python', 'module_op', 'module_build', 'inference'];
  const TEMPLATES = {
    command: { argv: ['python', '-c', "print('hello from', __import__('socket').gethostname())"] },
    python: { code: "import os, socket\nprint('hello from', socket.gethostname(), 'task', os.environ.get('CLUSTER_TASK_INDEX'), 'rank', os.environ.get('RANK'))\nopen(os.path.join(os.environ['CLUSTER_OUT_DIR'], 'result.txt'), 'w').write('done')" },
    module_op: { module: 'vm-harness', operation: 'audit.verify', args: {} },
    module_build: { module: 'modelmistress' },
    inference: { model: 'qwen3:8b', messages: [{ role: 'user', content: 'Say hello in five words.' }], max_tokens: 64 },
  };
  const st = { nodes: null, offer: null, jobs: null, open: '', timer: null, logOffset: 0, logText: '' };

  function css() {
    if ($('clp-css')) return;
    const s = document.createElement('style');
    s.id = 'clp-css';
    s.textContent = `.clp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(320px,100%),1fr));gap:12px}
.clp-card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--surface);min-width:0;display:flex;flex-direction:column;gap:6px}
.clp-card h3{margin:0;font-size:15px}.clp-card h4{margin:4px 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.clp-muted{color:var(--muted);font-size:12px}.clp-mono{font-family:var(--font-mono,monospace);font-size:12px;word-break:break-all}
.clp-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.clp-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.clp-chip.on{background:var(--good-soft);color:var(--good,#1f9d55)}.clp-chip.warn{background:var(--warning-soft);color:#8a5c00}.clp-chip.bad{background:var(--danger-soft,#fde8e8);color:var(--danger,#c0392b)}
.clp-bar{display:grid;grid-template-columns:52px 1fr auto;gap:6px;align-items:center;font-size:11.5px}.clp-bar>div{height:7px;border-radius:4px;background:var(--surface-2);overflow:hidden}.clp-bar i{display:block;height:100%;background:var(--accent)}.clp-bar i.hot{background:#d9822b}
.clp-pre{white-space:pre-wrap;max-height:45vh;overflow:auto;background:var(--surface-2);border-radius:6px;padding:8px;font-family:var(--font-mono,monospace);font-size:11.5px}
.clp-form{display:grid;gap:8px}.clp-form label{display:grid;gap:3px;font-size:12px;color:var(--ink-soft)}.clp-form label.chk{display:flex;gap:6px;align-items:center}
.clp-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(150px,100%),1fr));gap:8px}
#clp-root input,#clp-root select,#clp-root textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%}
#clp-root textarea{font-family:var(--font-mono,monospace);font-size:12px}
.clp-table{width:100%;border-collapse:collapse;font-size:12.5px}.clp-table td,.clp-table th{padding:5px 6px;border-bottom:1px solid var(--line);text-align:left}.clp-table tr[data-job]{cursor:pointer}.clp-table tr.sel{background:var(--accent-soft)}
.clp-split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.2fr);gap:12px}@media (max-width:1100px){.clp-split{grid-template-columns:1fr}}`;
    document.head.appendChild(s);
  }

  const chip = (t, k, title) => `<span class="clp-chip ${k || ''}"${title ? ` title="${E(title)}"` : ''}>${E(t)}</span>`;
  const bar = (label, used, total, unit) => {
    const pct = total ? Math.max(0, Math.min(100, (used / total) * 100)) : 0;
    return `<div class="clp-bar"><span class="clp-muted">${E(label)}</span><div><i class="${pct > 85 ? 'hot' : ''}" style="width:${pct.toFixed(0)}%"></i></div><span class="clp-muted">${total ? `${(+used).toFixed(1)} / ${(+total).toFixed(1)} ${unit}` : `${pct.toFixed(0)}%`}</span></div>`;
  };
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : '');
  const stateChip = (s) => chip(s || '?', s === 'done' ? 'on' : ['failed', 'lost', 'refused'].includes(s) ? 'bad' : ['cancelled'].includes(s) ? '' : 'warn');

  function nodeCard(n) {
    const i = n.info || {}, s = i.static || {}, l = i.live || {}, o = i.offer || {}, f = i.free || {}, cap = i.capacity || {};
    const health = n.health === 'ok' ? chip(n.peer ? 'online' : 'this machine', 'on') : chip(n.health === 'unsupported' ? 'needs ABP update' : n.health, n.health === 'stale' ? 'warn' : 'bad', n.error || '');
    if (!n.info) return `<div class="clp-card"><div class="clp-row" style="justify-content:space-between"><h3>${E(n.name)}</h3>${health}</div><div class="clp-muted">${E(n.error || 'No report yet.')}</div></div>`;
    const gpus = (s.gpus || []).map((g) => `${E(g.name)}${g.vram_gb ? ` · ${g.vram_gb} GB` : ''}`).join('<br>') || '<span class="clp-muted">no GPU</span>';
    const vramTotal = (s.gpus || []).reduce((a, g) => a + (g.vram_gb || 0), 0);
    const vramUsed = l.gpu_mem_used_gb != null ? l.gpu_mem_used_gb : (l.gpus || []).reduce((a, g) => a + (g.vram_used_gb || 0), 0);
    const sharing = i.available ? chip('sharing', 'on') : chip('not sharing', '', i.unavailable_reason);
    return `<div class="clp-card"><div class="clp-row" style="justify-content:space-between"><h3>${E(n.name)}</h3><span class="clp-row">${health}${sharing}</span></div>
<div class="clp-muted">${E((s.cpu || {}).model || '')} · ${E((s.cpu || {}).threads || '?')} threads · ${E(s.ram_gb)} GB · ${E(s.os)}${n.latency_ms ? ` · ${n.latency_ms} ms` : ''}</div>
<div class="clp-muted">${gpus}</div>
${bar('CPU', l.cpu_pct || 0, 0, '')}${bar('RAM', l.ram_used_gb || 0, s.ram_gb || 0, 'GB')}${vramTotal ? bar('VRAM', vramUsed || 0, vramTotal, 'GB') : ''}
${i.available ? `<h4>Offered · free</h4><div class="clp-muted">CPU ${E(f.cpu)} / ${E(cap.cpu)} threads · RAM ${E(f.ram_gb)} / ${E(cap.ram_gb)} GB · GPUs ${E((f.gpus || []).length)} / ${E((cap.gpus || []).length)} · slots ${E(f.slots)} / ${E(cap.max_jobs)}</div><div class="clp-muted">${E((o.kinds || []).join(', '))}${o.when === 'idle' ? ` · only when idle ${o.idle_minutes} min` : ''}</div>` : `<div class="clp-muted">${E(i.unavailable_reason || '')}</div>`}
<div class="clp-muted">jobs: ${E((i.jobs || {}).running || 0)} running, ${E((i.jobs || {}).waiting || 0)} waiting${((i.software || {}).models || []).length ? ` · ${(i.software.models || []).length} local models` : ''}${(s.hypervisors || []).length ? ` · ${E(s.hypervisors.join(', '))}` : ''}</div></div>`;
  }

  function offerCard() {
    const o = st.offer.offer, cap = st.offer.capacity, gpus = ((st.nodes || []).find((n) => !n.peer) || {}).info?.static?.gpus || [];
    const gpuSel = o.gpus === 'all' ? gpus.map((g) => g.index) : (o.gpus || []);
    return `<div class="clp-card"><h3>What this machine shares</h3><div class="clp-muted">Nothing is shared until you turn it on. Linked servers can see this and send jobs within it; only you can change it.</div>
<div class="clp-form" id="clp-offer">
<label class="chk"><input type="checkbox" data-o="enabled" ${o.enabled ? 'checked' : ''}> <b>Share this machine with the cluster</b></label>
<div class="clp-fields">
<label>CPU share (%) <input type="number" min="0" max="100" data-o="cpu_percent" value="${E(o.cpu_percent)}"></label>
<label>RAM (GB) <input type="number" min="0" step="0.5" data-o="ram_gb" value="${E(o.ram_gb)}"></label>
<label>Disk (GB) <input type="number" min="0" data-o="disk_gb" value="${E(o.disk_gb)}"></label>
<label>Jobs at once <input type="number" min="0" data-o="max_jobs" value="${E(o.max_jobs)}"></label>
<label>When <select data-o="when"><option value="always" ${o.when === 'always' ? 'selected' : ''}>always</option><option value="idle" ${o.when === 'idle' ? 'selected' : ''}>only when idle</option></select></label>
<label>Idle minutes <input type="number" min="0" data-o="idle_minutes" value="${E(o.idle_minutes)}"></label>
</div>
${gpus.length ? `<div class="clp-row"><span class="clp-muted">GPUs:</span>${gpus.map((g) => `<label class="chk"><input type="checkbox" data-gpu="${g.index}" ${gpuSel.includes(g.index) ? 'checked' : ''}> ${E(g.name)}</label>`).join('')}</div>` : ''}
<div class="clp-row"><span class="clp-muted">Job kinds:</span>${KINDS.map((k) => `<label class="chk"><input type="checkbox" data-kind="${k}" ${(o.kinds || []).includes(k) ? 'checked' : ''}> ${k}</label>`).join('')}</div>
<label>Linked servers allowed (comma-separated names, * for all) <input data-o="peers" value="${E((o.peers || []).join(', '))}"></label>
<label>Work folder <input data-o="work_dir" value="${E(o.work_dir || '')}" placeholder="${E(st.offer.work_dir)}"></label>
<div class="clp-row"><button class="btn" id="clp-offer-save">Save</button><span class="clp-muted">Now: ${E(cap.cpu)} threads, ${E(cap.ram_gb)} GB, ${E((cap.gpus || []).length)} GPU(s)</span></div>
</div></div>`;
  }

  function readOffer() {
    const root = $('clp-offer'), out = {};
    root.querySelectorAll('[data-o]').forEach((el) => {
      const k = el.dataset.o;
      if (el.type === 'checkbox') out[k] = el.checked;
      else if (el.type === 'number') out[k] = el.value === '' ? undefined : Number(el.value);
      else if (k === 'peers') out[k] = el.value.split(',').map((x) => x.trim()).filter(Boolean);
      else out[k] = el.value;
    });
    out.gpus = [...root.querySelectorAll('[data-gpu]')].filter((el) => el.checked).map((el) => Number(el.dataset.gpu));
    out.kinds = [...root.querySelectorAll('[data-kind]')].filter((el) => el.checked).map((el) => el.dataset.kind);
    Object.keys(out).forEach((k) => out[k] === undefined && delete out[k]);
    return out;
  }

  function submitCard() {
    const names = (st.nodes || []).filter((n) => n.health === 'ok').map((n) => n.name);
    return `<div class="clp-card"><h3>Run on the cluster</h3><div class="clp-form" id="clp-submit">
<div class="clp-fields">
<label>Kind <select id="clp-kind">${KINDS.map((k) => `<option>${k}</option>`).join('')}</select></label>
<label>Mode <select id="clp-mode"><option value="one">one job</option><option value="gang">gang (N nodes)</option><option value="array">array (N tasks)</option></select></label>
<label>N <input type="number" id="clp-n" min="1" value="2"></label>
<label>Node <select id="clp-node"><option value="">best fit</option>${names.map((n) => `<option>${E(n)}</option>`).join('')}</select></label>
<label>CPU threads <input type="number" id="clp-cpu" min="0.1" step="0.5" value="1"></label>
<label>RAM (GB) <input type="number" id="clp-ram" min="0.1" step="0.5" value="1"></label>
<label>GPUs <input type="number" id="clp-gpus" min="0" value="0"></label>
<label>VRAM each (GB) <input type="number" id="clp-vram" min="0" value="0"></label>
<label>Timeout (s) <input type="number" id="clp-timeout" min="1" value="3600"></label>
<label>Retries <input type="number" id="clp-retries" min="0" value="0"></label>
</div>
<label>Spec (JSON) <textarea id="clp-spec" rows="7"></textarea></label>
<div class="clp-row"><button class="btn" id="clp-run">Run</button><button class="btn ghost" id="clp-check">Which nodes fit?</button></div><div id="clp-submit-out"></div></div></div>`;
  }

  function jobsCard() {
    const rows = (st.jobs || []).map((p) => `<tr data-job="${E(p.id)}" class="${st.open === p.id ? 'sel' : ''}"><td class="clp-mono">${E(p.id)}</td><td>${E(p.name || p.kind)}</td><td>${E(p.node || '-')}</td><td>${stateChip(p.state)}</td><td class="clp-muted">${E(when(p.created))}</td></tr>`).join('');
    return `<div class="clp-card"><h3>Jobs from this machine</h3><table class="clp-table"><thead><tr><th>id</th><th>name</th><th>node</th><th>state</th><th>submitted</th></tr></thead><tbody>${rows || '<tr><td colspan="5" class="clp-muted">None yet.</td></tr>'}</tbody></table><div id="clp-job"></div></div>`;
  }

  async function jobDetail() {
    const box = $('clp-job');
    if (!box || !st.open) return;
    try {
      const p = await api(`/api/cluster/jobs/${encodeURIComponent(st.open)}`);
      const lg = await api(`/api/cluster/jobs/${encodeURIComponent(st.open)}/logs?offset=${st.logOffset}`).catch(() => null);
      if (lg) { st.logText = (st.logText + (lg.text || '')).slice(-200000); st.logOffset = lg.offset || st.logOffset; }
      const run = p.run || {};
      box.innerHTML = `<h4>${E(p.name || p.kind)} · ${E(p.node || '')}</h4><div class="clp-row">${stateChip(p.state)}${run.exit_code != null ? chip('exit ' + run.exit_code) : ''}${p.state && !['done', 'failed', 'cancelled', 'lost', 'refused'].includes(p.state) ? '<button class="btn ghost" id="clp-cancel">Cancel</button>' : ''}</div>
${run.error || p.error ? `<p class="cardnote">${E(run.error || p.error)}</p>` : ''}${p.reasons ? `<div class="clp-pre">${E(Object.entries(p.reasons).map(([k, v]) => `${k}: ${v}`).join('\n'))}</div>` : ''}
${run.result != null ? `<h4>Result</h4><div class="clp-pre">${E(JSON.stringify(run.result, null, 1))}</div>` : ''}
${(run.files || []).length ? `<h4>Files</h4>${run.files.map((f) => p.peer ? `<div class="clp-mono">${E(f.name)} <span class="clp-muted">${f.size} B, on ${E(p.node)}</span></div>` : `<div><a class="clp-mono" href="/api/cluster/runs/${encodeURIComponent(p.run_id)}/files/${encodeURIComponent(f.name)}" target="_blank">${E(f.name)}</a> <span class="clp-muted">${f.size} B</span></div>`).join('')}` : ''}
<h4>Log</h4><div class="clp-pre" id="clp-log">${E(st.logText)}</div>`;
      const c = $('clp-cancel');
      if (c) c.addEventListener('click', async () => { try { await post(`/api/cluster/jobs/${encodeURIComponent(st.open)}/cancel`); render(); } catch (e) { toast(errText(e), 'error'); } });
      const lb = $('clp-log'); if (lb) lb.scrollTop = lb.scrollHeight;
    } catch (e) { box.innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; }
  }

  function wire() {
    const save = $('clp-offer-save');
    if (save) save.addEventListener('click', async () => {
      try { await post('/api/cluster/offer', readOffer(), 'PUT'); toast('Saved: what this machine shares'); render(); } catch (e) { toast(errText(e), 'error'); }
    });
    const kind = $('clp-kind'), spec = $('clp-spec');
    const setTpl = () => { spec.value = JSON.stringify(TEMPLATES[kind.value], null, 2); };
    kind.addEventListener('change', setTpl); setTpl();
    const body = () => {
      const b = { kind: kind.value, spec: JSON.parse(spec.value), req: { cpu: +$('clp-cpu').value, ram_gb: +$('clp-ram').value, gpus: +$('clp-gpus').value, vram_gb: +$('clp-vram').value }, timeout_s: +$('clp-timeout').value, retries: +$('clp-retries').value };
      if ($('clp-node').value) b.node = $('clp-node').value;
      return b;
    };
    $('clp-check').addEventListener('click', async () => {
      try { const r = await post('/api/cluster/candidates', body()); $('clp-submit-out').innerHTML = `<div class="clp-pre">fits: ${E(r.fits.join(', ') || 'none')}\n${E(Object.entries(r.why_not).map(([k, v]) => `${k}: ${v}`).join('\n'))}</div>`; } catch (e) { toast(errText(e), 'error'); }
    });
    $('clp-run').addEventListener('click', async () => {
      let b;
      try { b = body(); } catch (_) { toast('The spec is not valid JSON', 'error'); return; }
      const mode = $('clp-mode').value, n = +$('clp-n').value;
      try {
        if (mode === 'one') { const p = await post('/api/cluster/jobs', b); st.open = p.id; st.logOffset = 0; st.logText = ''; toast(`Running on ${p.node}`); }
        else { const g = await post('/api/cluster/groups', mode === 'gang' ? { ...b, replicas: n } : { ...b, count: n }); toast(`${g.kind} ${g.id}: ${g.state}`); }
        render();
      } catch (e) { $('clp-submit-out').innerHTML = `<div class="clp-pre">${E(errText(e))}</div>`; }
    });
    document.querySelectorAll('#clp-root tr[data-job]').forEach((tr) => tr.addEventListener('click', () => { st.open = tr.dataset.job; st.logOffset = 0; st.logText = ''; render(); }));
  }

  async function render(refresh) {
    css();
    const root = $('clp-root');
    if (!root) return;
    try {
      const [cl, of, jobs] = await Promise.all([api(refresh ? '/api/cluster/refresh' : '/api/cluster', refresh ? { method: 'POST', body: '{}' } : undefined), api('/api/cluster/offer'), api('/api/cluster/jobs?limit=30')]);
      st.nodes = cl.nodes; st.offer = of; st.jobs = jobs;
      const keep = $('clp-spec') ? { spec: $('clp-spec').value, kind: $('clp-kind').value } : null;
      root.innerHTML = `<div class="clp-row" style="margin-bottom:8px"><button class="btn ghost" id="clp-refresh">Refresh now</button><span class="clp-muted">${st.nodes.length} node(s): this machine and every linked server (Peers page).</span></div>
<div class="clp-grid">${st.nodes.map(nodeCard).join('')}</div>
<div class="clp-split" style="margin-top:12px"><div style="display:grid;gap:12px">${offerCard()}${submitCard()}</div>${jobsCard()}</div>`;
      $('clp-refresh').addEventListener('click', () => render(true));
      wire();
      if (keep) { $('clp-kind').value = keep.kind; $('clp-spec').value = keep.spec; }
      await jobDetail();
    } catch (e) { root.innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; }
    clearTimeout(st.timer);
    if ((location.hash || '').replace('#', '') === 'cluster') st.timer = setTimeout(() => { if (!document.activeElement || !document.activeElement.closest || !document.activeElement.closest('#clp-root form, #clp-offer, #clp-submit')) render(); else st.timer = setTimeout(() => render(), 5000); }, 5000);
  }

  function init() {
    if (!$('clp-root')) return;
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'cluster') render(); else clearTimeout(st.timer); };
    window.addEventListener('hashchange', onHash);
    onHash();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
