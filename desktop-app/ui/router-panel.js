// Router panel: how the model router thinks and acts, what it learned, and how to teach and change it.
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_router_panel.py fails if the two differ. It uses the page's own api(), esc() and showToast(),
// and draws into #rt-root. Charts are plain SVG, so nothing is loaded from anywhere.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const post = (path, body, method) => api(path, { method: method || 'POST', body: JSON.stringify(body || {}) });
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : '—');
  const ago = (ts) => {
    if (!ts) return 'never';
    const s = Date.now() / 1000 - ts;
    if (s < 0) { const l = -s; return l > 86400 ? `in ${(l / 86400).toFixed(1)} d` : l > 3600 ? `in ${(l / 3600).toFixed(1)} h` : `in ${Math.max(1, Math.round(l / 60))} min`; }
    return s < 60 ? 'just now' : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${(s / 3600).toFixed(1)} h ago` : `${(s / 86400).toFixed(1)} d ago`;
  };
  const pct = (v) => (v == null ? '—' : Math.round(v * 100) + '%');
  const COMPS = ['quality', 'economy', 'headroom', 'reliability', 'speed'];
  const COLORS = { quality: 'var(--rt-q)', economy: 'var(--rt-e)', headroom: 'var(--rt-h)', reliability: 'var(--rt-r)', speed: 'var(--rt-s)' };
  const TABS = [['overview', 'Overview'], ['decisions', 'Decisions'], ['models', 'Models'], ['log', 'Learning log'], ['train', 'Training'], ['policy', 'Policy'], ['try', 'Try it']];
  const KIND_ICON = { cooldown: '⏸', recovered: '✅', feedback: '🗳', example: '🎓', policy: '📜', reset: '♻', cooldown_cleared: '▶', failure: '⚠' };
  const st = { tab: 'overview', hours: 24, meta: null, policy: null, draft: null, candidates: [], filter: {}, selected: null, logKind: '' };
  try { st.tab = localStorage.getItem('rt-tab') || 'overview'; } catch (_) {}
  let loading = false;

  function css() {
    if ($('rt-css')) return;
    const s = document.createElement('style');
    s.id = 'rt-css';
    s.textContent = `#rt-root{--rt-q:#3987e5;--rt-e:#1f9d55;--rt-h:#e0a800;--rt-r:#8e5bd6;--rt-s:#e0703a;--rt-ok:#1f9d55;--rt-bad:#d03b3b;--rt-wait:#8a94a0}
.rt-tabs{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0 12px;border-bottom:1px solid var(--line)}
.rt-tabs button{background:none;border:0;border-bottom:2px solid transparent;color:var(--ink-soft);padding:8px 12px;font:inherit;font-weight:600;cursor:pointer}
.rt-tabs button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--accent)}
.rt-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:8px}
.rt-kpi{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:var(--surface)}
.rt-kpi b{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.rt-kpi span{font-size:20px;font-weight:700;display:block;margin-top:2px}.rt-kpi small{color:var(--muted)}
.rt-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(320px,100%),1fr));gap:12px;margin-top:12px}
.rt-card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--surface);min-width:0}
.rt-card h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.rt-svg{width:100%;height:auto;display:block}.rt-svg text{fill:var(--muted);font-size:10px}
.rt-table{width:100%;border-collapse:collapse;font-size:12.5px}.rt-table th{text-align:left;color:var(--muted);font-weight:600;padding:6px;border-bottom:1px solid var(--line);white-space:nowrap}
.rt-table td{padding:6px;border-bottom:1px solid var(--line);vertical-align:top}.rt-table tr.rt-click{cursor:pointer}.rt-table tr.rt-click:hover td{background:var(--surface-2)}
.rt-table tr.rt-sel td{background:var(--accent-soft)}
.rt-scroll{overflow:auto;max-width:100%}.rt-scroll.rt-tall{max-height:70vh}.rt-table td:first-child{white-space:nowrap}
.rt-bar{display:flex;height:12px;border-radius:3px;overflow:hidden;background:var(--surface-2);min-width:120px}
.rt-bar i{display:block;height:100%}
.rt-ci{position:relative;height:10px;background:var(--surface-2);border-radius:5px;min-width:90px}
.rt-ci i{position:absolute;top:0;height:100%;background:color-mix(in srgb,var(--rt-r) 45%,transparent);border-radius:5px}
.rt-ci b{position:absolute;top:-2px;width:2px;height:14px;background:var(--rt-r)}
.rt-legend{display:flex;flex-wrap:wrap;gap:10px;font-size:11.5px;color:var(--ink-soft);margin:6px 0}.rt-legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.rt-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.rt-chip.ok{background:var(--good-soft);color:var(--rt-ok)}.rt-chip.failed{background:var(--critical-soft);color:var(--rt-bad)}.rt-chip.pending{background:var(--surface-3)}
.rt-chip.explore{background:var(--accent-soft);color:var(--accent-ink)}.rt-chip.rest{background:var(--warning-soft);color:#8a5c00}
.rt-split{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:12px}@media (max-width:1100px){.rt-split{grid-template-columns:1fr}}
.rt-filters{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}.rt-filters select,.rt-filters input,.rt-form input,.rt-form select,.rt-form textarea,.rt-pol input,.rt-pol select,.rt-pol textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%}
.rt-form{display:grid;gap:8px}.rt-form label{display:grid;gap:3px;font-size:12px;color:var(--ink-soft)}
.rt-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.rt-step{border-left:3px solid var(--line);padding:4px 0 4px 10px;margin:10px 0}.rt-step h5{margin:0 0 4px;font-size:12px;color:var(--ink)}
.rt-step.chosen{border-left-color:var(--accent)}
.rt-muted{color:var(--muted);font-size:12px}.rt-mono{font-family:var(--font-mono,monospace);font-size:12px;word-break:break-all}
.rt-list{list-style:none;margin:0;padding:0;font-size:12.5px}.rt-list li{padding:6px 0;border-bottom:1px solid var(--line);display:flex;gap:8px}
.rt-heat td.c{text-align:center;font-size:11px;min-width:64px;border-radius:4px}
.rt-pol table input[type=number]{width:64px}.rt-pol .rt-wbar{min-width:140px}
details.rt-more summary{cursor:pointer;color:var(--ink-soft);font-size:12px}`;
    document.head.appendChild(s);
  }

  // ---- small chart helpers ---------------------------------------------------------------------------------------------
  function stackBar(contrib, adjustments, max) {
    const total = Math.max(max || 1, 0.0001);
    const parts = COMPS.map((c) => `<i style="width:${Math.max(0, (contrib[c] || 0) / total * 100).toFixed(2)}%;background:${COLORS[c]}" title="${c}: ${(contrib[c] || 0).toFixed(3)}"></i>`).join('');
    const adj = (adjustments || []).reduce((a, x) => a + x.delta, 0);
    const adjPart = adj > 0 ? `<i style="width:${(adj / total * 100).toFixed(2)}%;background:repeating-linear-gradient(45deg,var(--accent),var(--accent) 3px,transparent 3px,transparent 6px)" title="rules and examples: +${adj.toFixed(3)}"></i>` : '';
    return `<div class="rt-bar" role="img" aria-label="score parts">${parts}${adjPart}</div>`;
  }
  function legend() {
    return `<div class="rt-legend">${COMPS.map((c) => `<span><i style="background:${COLORS[c]}"></i>${c}</span>`).join('')}<span><i style="background:repeating-linear-gradient(45deg,var(--accent),var(--accent) 3px,transparent 3px,transparent 6px)"></i>rules &amp; examples</span></div>`;
  }
  function timeline(points, bucket) {
    if (!points.length) return '<p class="rt-muted">No decisions in this period.</p>';
    const W = 640, H = 150, pad = 22;
    const max = Math.max(1, ...points.map((p) => p.ok + p.failed + p.pending));
    const bw = (W - pad) / points.length;
    let bars = '';
    points.forEach((p, i) => {
      let y = H - pad;
      const x = pad + i * bw;
      [['ok', 'var(--rt-ok)'], ['failed', 'var(--rt-bad)'], ['pending', 'var(--rt-wait)']].forEach(([k, col]) => {
        const h = (p[k] / max) * (H - pad - 8);
        if (h > 0) { y -= h; bars += `<rect x="${x + 1}" y="${y}" width="${Math.max(1, bw - 2)}" height="${h}" fill="${col}"><title>${when(p.t)}: ${p[k]} ${k}</title></rect>`; }
      });
    });
    const label = (i) => { const d = new Date(points[i].t * 1000); return bucket >= 86400 ? `${d.getMonth() + 1}/${d.getDate()}` : `${d.getHours()}:00`; };
    const ticks = [0, Math.floor(points.length / 2), points.length - 1].map((i) => `<text x="${pad + i * bw}" y="${H - 6}">${label(i)}</text>`).join('');
    return `<svg class="rt-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Decisions over time"><text x="0" y="12">${max}</text><line x1="${pad}" y1="${H - pad}" x2="${W}" y2="${H - pad}" stroke="var(--line)"/>${bars}${ticks}</svg>
      <div class="rt-legend"><span><i style="background:var(--rt-ok)"></i>answered</span><span><i style="background:var(--rt-bad)"></i>failed</span><span><i style="background:var(--rt-wait)"></i>pending</span></div>`;
  }
  function hbars(map) {
    const entries = Object.entries(map || {}).filter(([, v]) => v).sort((a, b) => b[1] - a[1]);
    if (!entries.length) return '<p class="rt-muted">Nothing yet.</p>';
    const max = Math.max(...entries.map((e) => e[1]));
    return entries.map(([k, v]) => `<div class="rt-row" style="margin:4px 0"><span style="width:110px" class="rt-muted">${E(k)}</span><div class="rt-bar" style="flex:1"><i style="width:${(v / max * 100).toFixed(1)}%;background:var(--accent)"></i></div><b style="width:36px;text-align:right">${v}</b></div>`).join('');
  }
  function ci(m) {
    return `<div class="rt-ci" title="reliability ${pct(m.reliability)} (likely ${pct(m.low)}–${pct(m.high)})"><i style="left:${m.low * 100}%;width:${Math.max(1, (m.high - m.low) * 100)}%"></i><b style="left:calc(${m.reliability * 100}% - 1px)"></b></div>`;
  }
  function heat(h, classes) {
    const models = Object.keys(h || {});
    if (!models.length) return '<p class="rt-muted">No automatic decisions in the last 7 days.</p>';
    const cell = (c) => {
      if (!c) return '<td class="c"></td>';
      const done = c.ok + c.failed, rate = done ? c.ok / done : null;
      const bg = rate == null ? 'var(--surface-2)' : `color-mix(in srgb, var(--rt-ok) ${Math.round(rate * 100)}%, var(--rt-bad))`;
      return `<td class="c" style="background:${bg};color:#fff" title="${c.ok} answered, ${c.failed} failed, ${c.pending} pending">${rate == null ? '…' : pct(rate)}<br><small>${c.ok + c.failed + c.pending}</small></td>`;
    };
    return `<div class="rt-scroll"><table class="rt-table rt-heat"><thead><tr><th>model</th>${classes.map((c) => `<th>${E(c)}</th>`).join('')}</tr></thead><tbody>
      ${models.map((m) => `<tr><td class="rt-mono">${E(m)}</td>${classes.map((c) => cell(h[m][c])).join('')}</tr>`).join('')}</tbody></table></div>`;
  }

  // ---- how it thought: shared by a decision and the simulator ----------------------------------------------------------
  function thinking(d) {
    const cls = d.classification || {};
    const cands = d.candidates || [];
    const max = Math.max(0.0001, ...cands.map((c) => Math.max(c.score, c.sampled_score || 0)));
    const scores = cls.scores && Object.keys(cls.scores).length
      ? `<div class="rt-muted" style="margin-top:6px">What the training examples say:</div>${Object.entries(cls.scores).map(([k, v]) => `<div class="rt-row"><span style="width:110px" class="rt-muted">${E(k)}</span><div class="rt-bar" style="flex:1"><i style="width:${v * 100}%;background:var(--accent)"></i></div><span style="width:40px">${pct(v)}</span></div>`).join('')}`
      : '';
    const w = d.weights ? `<div class="rt-muted" style="margin-top:4px">Weights for ${E(cls.task_class)}: ${COMPS.map((c) => `${c} ${d.weights[c]}`).join(' · ')}</div>` : '';
    const rows = cands.map((c, i) => `<tr class="${c.model === d.chosen ? 'rt-sel' : ''}"><td>${i + 1}</td><td class="rt-mono">${E(c.model)}${c.model === d.chosen ? ' <span class="rt-chip ok">chosen</span>' : ''}${c.model === d.greedy && d.explored ? ' <span class="rt-chip">usual pick</span>' : ''}</td>
      <td style="min-width:160px">${stackBar(c.contributions || {}, c.adjustments, max)}</td><td><b>${c.score.toFixed(3)}</b>${c.sampled_score != null ? `<br><small class="rt-muted">draw ${c.sampled_score.toFixed(3)}</small>` : ''}</td>
      <td><details class="rt-more"><summary>why</summary><ul class="rt-list">${(c.reasons || []).map((r) => `<li>${E(r)}</li>`).join('')}
      <li class="rt-muted">${COMPS.map((k) => `${k} ${(c.components || {})[k] != null ? c.components[k] : '—'}`).join(' · ')}</li></ul></details></td></tr>`).join('');
    return `<div class="rt-step"><h5>1 · What kind of task</h5><b>${E(cls.task_class || '—')}</b> <span class="rt-chip">${E(cls.source || '')}</span>
        <div>${(cls.reasons || []).map((r) => E(r)).join('; ')}</div>${cls.keyword_class && cls.keyword_class !== cls.task_class ? `<div class="rt-muted">keywords alone said ${E(cls.keyword_class)}</div>` : ''}${scores}${w}</div>
      <div class="rt-step"><h5>2 · Who was left out</h5>${(d.skipped || []).length ? `<ul class="rt-list">${d.skipped.map((s) => `<li>⛔ ${E(s)}</li>`).join('')}</ul>` : '<span class="rt-muted">Nobody; every candidate could do it.</span>'}
        ${(d.excluded || []).length ? `<div class="rt-muted">Already tried this turn: ${d.excluded.map(E).join(', ')}</div>` : ''}</div>
      <div class="rt-step chosen"><h5>3 · How the rest scored</h5>${legend()}${cands.length ? `<div class="rt-scroll"><table class="rt-table"><thead><tr><th>#</th><th>model</th><th>score parts</th><th>score</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : '<span class="rt-muted">No candidate fits.</span>'}</div>
      ${(d.notes || []).length ? `<div class="rt-step"><h5>4 · Notes</h5><ul class="rt-list">${d.notes.map((n) => `<li>${E(n)}</li>`).join('')}</ul></div>` : ''}`;
  }

  // ---- tabs -------------------------------------------------------------------------------------------------------------
  async function renderOverview(body) {
    const [o, m] = await Promise.all([api('/api/router/overview?hours=' + st.hours), api('/api/router/models')]);
    st.candidates = m.candidates || [];
    const kpi = (label, value, sub) => `<div class="rt-kpi"><b>${label}</b><span>${value}</span><small>${sub || ''}</small></div>`;
    const classes = st.meta ? st.meta.task_classes : ['trivial', 'coding', 'hard_reasoning', 'long_context', 'vision', 'bulk'];
    body.innerHTML = `<div class="rt-row" style="justify-content:space-between;margin-bottom:10px"><span class="rt-muted">Automatic choices by bots on <span class="rt-mono">model: auto</span>, their failovers and every turn after.</span>
        <select data-rt-hours aria-label="Period">${[[24, 'Last 24 hours'], [168, 'Last 7 days'], [720, 'Last 30 days']].map(([h, l]) => `<option value="${h}" ${st.hours === h ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
      <div class="rt-kpis">${kpi('Decisions', o.decisions, `${o.ok} answered · ${o.failed} failed`)}${kpi('Success rate', pct(o.success_rate), 'of decisions with an outcome')}
        ${kpi('Explored', o.explored, 'picks that tried a less proven model')}${kpi('Resting now', o.resting.length, 'models held back after failures')}
        ${kpi('Policy', 'v' + o.policy_version, o.learning ? 'learning on' : 'learning OFF')}${kpi('Training examples', o.examples, 'taught by hand or feedback')}
        ${kpi('Models known', (m.models || []).length, `${(m.never_used || []).length} candidate(s) never tried`)}</div>
      <div class="rt-grid"><div class="rt-card" style="grid-column:1/-1"><h4>Decisions over time</h4>${timeline(o.timeline, o.bucket_s)}</div>
        <div class="rt-card"><h4>Task classes</h4>${hbars(o.classes)}</div>
        <div class="rt-card"><h4>Resting models</h4>${o.resting.length ? `<ul class="rt-list">${o.resting.map((r) => `<li><span class="rt-chip rest">${E(r.kind || '')}</span><div style="flex:1"><span class="rt-mono">${E(r.key)}</span><br><span class="rt-muted">until ${when(r.until)} · strike ${r.strikes}${r.reason ? ' · ' + E(r.reason.slice(0, 140)) : ''}</span></div><button class="btn" data-rt="release" data-model="${E(r.key)}">Release</button></li>`).join('')}</ul>` : '<p class="rt-muted">None. Every candidate is available.</p>'}</div>
        <div class="rt-card" style="grid-column:1/-1"><h4>Outcomes by model and task class (7 days)</h4>${heat(o.heatmap, classes)}</div></div>`;
  }

  async function renderDecisions(body) {
    const f = st.filter;
    const q = new URLSearchParams({ limit: '150' });
    Object.entries(f).forEach(([k, v]) => { if (v) q.set(k, v); });
    const r = await api('/api/router/decisions?' + q.toString());
    const opt = (name, values, label) => `<select data-rt-filter="${name}" aria-label="${label}"><option value="">${label}: any</option>${values.map((v) => `<option ${f[name] === v ? 'selected' : ''}>${v}</option>`).join('')}</select>`;
    const rows = r.decisions.map((d) => `<tr class="rt-click ${st.selected === d.id ? 'rt-sel' : ''}" data-rt="open" data-id="${d.id}" tabindex="0">
      <td>#${d.id}</td><td title="${when(d.ts)}">${ago(d.ts)}</td><td><span class="rt-chip">${E(d.mode)}</span>${d.explored ? ' <span class="rt-chip explore">explored</span>' : ''}</td>
      <td>${E(d.task_class || '')}</td><td class="rt-mono">${E(d.chosen || '—')}</td><td><span class="rt-chip ${E(d.status)}">${E(d.status)}</span>${d.error_kind ? `<br><small class="rt-muted">${E(d.error_kind)}</small>` : ''}</td>
      <td>${d.latency_ms != null ? (d.latency_ms / 1000).toFixed(1) + 's' : ''}</td><td>${d.rating === 1 ? '👍' : d.rating === -1 ? '👎' : ''}</td></tr>`).join('');
    body.innerHTML = `<div class="rt-split"><div class="rt-card"><div class="rt-filters">${opt('mode', ['auto', 'sticky', 'reroute', 'failover', 'advise'], 'mode')}${opt('status', ['ok', 'failed', 'pending', 'advice'], 'status')}
        ${opt('task_class', st.meta ? st.meta.task_classes : [], 'class')}<input data-rt-filter="model" placeholder="model" value="${E(f.model || '')}" aria-label="Filter by model"></div>
        ${r.decisions.length ? `<div class="rt-scroll rt-tall"><table class="rt-table"><thead><tr><th>#</th><th>when</th><th>mode</th><th>class</th><th>chosen</th><th>outcome</th><th>time</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="rt-muted">No decisions yet. They appear when a bot on model: auto answers, or when you ask /route.</p>'}</div>
      <div class="rt-card" id="rt-detail"><p class="rt-muted">Pick a decision to see how the router thought, what happened, and to teach it.</p></div></div>`;
    if (st.selected) openDecision(st.selected);
  }

  async function openDecision(id) {
    st.selected = id;
    const pane = $('rt-detail');
    if (!pane) return;
    document.querySelectorAll('#rt-root tr[data-id]').forEach((tr) => tr.classList.toggle('rt-sel', Number(tr.dataset.id) === id));
    pane.innerHTML = '<p class="rt-muted">Loading…</p>';
    // In the one-column layout the detail sits under the list: bring it into view.
    if (pane.getBoundingClientRect().top > window.innerHeight) pane.scrollIntoView({ behavior: 'smooth', block: 'start' });
    try {
      const d = await api('/api/router/decisions/' + id);
      const det = Object.assign({}, d.detail || {}, { chosen: d.chosen });
      if (!det.weights && d.detail) det.weights = d.detail.weights;
      const classes = st.meta ? st.meta.task_classes : [];
      const models = Array.from(new Set([...(det.candidates || []).map((c) => c.model), ...st.candidates]));
      const link = (x) => `<a href="#router" data-rt="open" data-id="${x.id}">#${x.id}</a> ${E(x.mode)} → <span class="rt-mono">${E(x.chosen || '—')}</span> <span class="rt-chip ${E(x.status)}">${E(x.status)}</span>`;
      pane.innerHTML = `<h4>Decision #${d.id} · ${E(d.mode)} · ${when(d.ts)}${d.instance_id ? ' · bot #' + d.instance_id : ''} · policy v${d.policy_version}</h4>
        ${d.task_excerpt ? `<blockquote class="rt-mono" style="margin:6px 0;padding:6px 10px;border-left:3px solid var(--line)">${E(d.task_excerpt)}</blockquote>` : '<p class="rt-muted">(task text not recorded)</p>'}
        ${d.mode === 'sticky' ? `<div class="rt-step chosen"><h5>Kept the earlier pick</h5>${(det.notes || []).map(E).join('<br>')}</div>` : thinking(det)}
        <div class="rt-step"><h5>What happened</h5><span class="rt-chip ${E(d.status)}">${E(d.status)}</span> ${d.error_kind ? `<b>${E(d.error_kind)}</b> ` : ''}${d.latency_ms != null ? ` · first answer in ${(d.latency_ms / 1000).toFixed(1)}s` : ''} · ${d.calls} call(s) · ${d.tokens} tokens
          ${d.error ? `<div class="rt-mono rt-muted" style="margin-top:4px">${E(d.error)}</div>` : ''}
          ${d.parent ? `<div style="margin-top:6px">Follows ${link(d.parent)}</div>` : ''}${(d.children || []).length ? `<div style="margin-top:6px">Then: ${d.children.map(link).join('<br>')}</div>` : ''}
          ${(d.events || []).length ? `<ul class="rt-list" style="margin-top:6px">${d.events.map((e) => `<li>${KIND_ICON[e.kind] || '•'} ${E(e.message)}</li>`).join('')}</ul>` : ''}</div>
        ${d.mode === 'advise' ? '' : `<div class="rt-step"><h5>Teach it</h5><form class="rt-form" data-rt-form="feedback" data-id="${d.id}">
          <div class="rt-row"><button type="button" class="btn ${d.rating === 1 ? 'primary' : ''}" data-rt="rate" data-v="1" data-id="${d.id}">👍 Good choice</button><button type="button" class="btn ${d.rating === -1 ? 'primary' : ''}" data-rt="rate" data-v="-1" data-id="${d.id}">👎 Poor choice</button></div>
          <label>It was really a… <select name="correct_class"><option value="">(the class was right)</option>${classes.map((c) => `<option ${d.corrected_class === c ? 'selected' : ''}>${c}</option>`).join('')}</select></label>
          <label>It should have used… <input name="preferred_model" list="rt-models" value="${E(d.preferred_model || '')}" placeholder="provider/model"></label>
          <datalist id="rt-models">${models.map((m) => `<option value="${E(m)}">`).join('')}</datalist>
          <label>Note <input name="note" value="${E(d.feedback_note || '')}" maxlength="500"></label>
          <div><button class="btn primary" type="submit">Teach</button> <span class="rt-muted">A corrected class or model becomes a training example.</span></div></form></div>`}`;
    } catch (e) { pane.innerHTML = `<p class="rt-muted">${E(errText(e))}</p>`; }
  }

  async function renderModels(body) {
    const m = await api('/api/router/models');
    st.candidates = m.candidates || [];
    const rows = m.models.map((x) => {
      const rest = x.cooldown;
      const classes = Object.entries(x.classes || {}).map(([c, v]) => `${c} ${pct(v.reliability)} (${v.calls})`).join(' · ');
      return `<tr><td class="rt-mono">${E(x.model)}${st.candidates.includes(x.model) ? '' : ' <span class="rt-chip" title="not in the current candidate list">not a candidate</span>'}</td>
        <td><div class="rt-bar" style="min-width:70px"><i style="width:${x.share_7d * 100}%;background:var(--accent)"></i></div><small class="rt-muted">${x.picks_7d} pick(s)</small></td>
        <td>${ci(x)}<small class="rt-muted">${pct(x.reliability)} · ${x.ok} ok / ${x.fail} failed</small></td>
        <td>${x.latency_ms != null ? (x.latency_ms / 1000).toFixed(1) + 's' : '—'}</td><td>${x.up ? '👍' + x.up : ''} ${x.down ? '👎' + x.down : ''}</td>
        <td>${rest ? `<span class="rt-chip rest">${E(rest.kind)}</span><br><small class="rt-muted">until ${when(rest.until)}</small>` : x.streak > 0 ? `<span class="rt-chip failed">${x.streak} failed in a row</span>` : '<span class="rt-chip ok">available</span>'}
          ${x.last_error_kind ? `<br><small class="rt-muted" title="${E(x.last_error || '')}">last failure: ${E(x.last_error_kind)} ${ago(x.last_fail)}</small>` : ''}</td>
        <td><div class="rt-row">${rest ? `<button class="btn" data-rt="release" data-model="${E(x.model)}">Release</button>` : `<button class="btn" data-rt="rest" data-model="${E(x.model)}">Rest 1 h</button>`}
          <button class="btn" data-rt="rule" data-kind="prefer" data-model="${E(x.model)}">Prefer</button><button class="btn" data-rt="rule" data-kind="block" data-model="${E(x.model)}">Block</button>
          <button class="btn danger" data-rt="forget" data-model="${E(x.model)}">Forget</button></div>${classes ? `<small class="rt-muted">${E(classes)}</small>` : ''}</td></tr>`;
    }).join('');
    body.innerHTML = `<div class="rt-card"><h4>What it has learned about each model</h4><p class="rt-muted">Reliability is learned from every call ABP makes (older outcomes fade); the shaded band is where the true rate probably lies, so a narrow band means it is sure. A model rests after failures: a 429 for minutes (doubling on repeats), a 403/404 for days once it repeats, a bad key rests its whole provider.</p>
      ${m.models.length ? `<div class="rt-scroll"><table class="rt-table"><thead><tr><th>model</th><th>share (7 d)</th><th>reliability</th><th>speed</th><th>feedback</th><th>state</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="rt-muted">Nothing learned yet.</p>'}
      ${(m.never_used || []).length ? `<details class="rt-more" style="margin-top:8px"><summary>${m.never_used.length} candidate(s) never tried</summary><div class="rt-mono">${m.never_used.map(E).join('<br>')}</div></details>` : ''}
      <div class="rt-row" style="margin-top:10px"><input id="rt-rest-model" placeholder="provider/model or provider/*" list="rt-cands" aria-label="Model to rest"><datalist id="rt-cands">${st.candidates.map((c) => `<option value="${E(c)}">`).join('')}</datalist>
        <select id="rt-rest-secs" aria-label="How long">${[[3600, '1 hour'], [86400, '1 day'], [604800, '1 week']].map(([s, l]) => `<option value="${s}">${l}</option>`).join('')}</select><button class="btn" data-rt="rest-custom">Rest it</button>
        <span style="flex:1"></span><button class="btn danger" data-rt="reset-all">Reset all learning…</button></div></div>`;
  }

  async function renderLog(body) {
    const q = new URLSearchParams({ limit: '300' });
    if (st.logKind) q.set('kind', st.logKind);
    const r = await api('/api/router/events?' + q.toString());
    const kinds = ['', 'cooldown', 'recovered', 'failure', 'feedback', 'example', 'policy', 'reset', 'cooldown_cleared'];
    body.innerHTML = `<div class="rt-card"><h4>Everything it learned or changed, and why</h4><div class="rt-filters">${kinds.map((k) => `<button class="btn ${st.logKind === k ? 'primary' : ''}" data-rt="logkind" data-kind="${k}">${k ? (KIND_ICON[k] || '') + ' ' + k.replace('_', ' ') : 'all'}</button>`).join('')}</div>
      ${r.events.length ? `<ul class="rt-list">${r.events.map((e) => `<li><span title="${e.kind}">${KIND_ICON[e.kind] || '•'}</span><div style="flex:1">${E(e.message)}${e.data && e.data.changes ? `<details class="rt-more"><summary>${e.data.changes.length} change(s)</summary><div class="rt-mono">${e.data.changes.map(E).join('<br>')}</div></details>` : ''}
        <br><small class="rt-muted">${when(e.ts)}${e.decision_id ? ` · <a href="#router" data-rt="goto" data-id="${e.decision_id}">decision #${e.decision_id}</a>` : ''}</small></div></li>`).join('')}</ul>` : '<p class="rt-muted">Nothing yet.</p>'}</div>`;
  }

  async function renderTrain(body) {
    const [r, m] = await Promise.all([api('/api/router/examples'), api('/api/router/models')]);
    st.candidates = m.candidates || [];
    const classes = st.meta ? st.meta.task_classes : [];
    const counts = {};
    r.examples.forEach((x) => { if (x.task_class) counts[x.task_class] = (counts[x.task_class] || 0) + 1; });
    const pol = st.policy || {};
    const need = pol.learning ? pol.learning.classifier_min_examples : 3;
    body.innerHTML = `<div class="rt-split"><div class="rt-card"><h4>Teach it</h4><p class="rt-muted">Give it a task in your own words and what kind of task it is, which model should take tasks like it, or both. Once there are ${need}+ examples, a small classifier trained on them overrides the keywords when it is sure (${pol.learning ? pct(pol.learning.classifier_confidence) : '60%'} or more). A model preference boosts that model for similar tasks.</p>
        <form class="rt-form" data-rt-form="example"><label>Task <textarea name="text" rows="3" required placeholder="e.g. write the release notes for this sprint"></textarea></label>
        <div class="rt-row"><label>Task class <select name="task_class"><option value="">(no class)</option>${classes.map((c) => `<option>${c}</option>`).join('')}</select></label>
        <label>Preferred model <input name="preferred_model" list="rt-train-models" placeholder="provider/model"></label><datalist id="rt-train-models">${st.candidates.map((c) => `<option value="${E(c)}">`).join('')}</datalist></div>
        <div><button class="btn primary" type="submit">Add example</button></div></form>
        <h4 style="margin-top:14px">Examples per class</h4>${hbars(counts)}</div>
      <div class="rt-card"><h4>${r.examples.length} example(s)</h4>${r.examples.length ? `<div class="rt-scroll"><table class="rt-table"><thead><tr><th>task</th><th>teaches</th><th>from</th><th></th></tr></thead><tbody>
        ${r.examples.map((x) => `<tr><td>${E(x.text.slice(0, 200))}</td><td>${x.task_class ? `<span class="rt-chip">${E(x.task_class)}</span> ` : ''}${x.preferred_model ? `<span class="rt-mono">→ ${E(x.preferred_model)}</span>` : ''}</td>
          <td><small class="rt-muted">${E(x.source)}${x.decision_id ? ` #${x.decision_id}` : ''}<br>${ago(x.ts)}</small></td><td><button class="btn danger" data-rt="del-example" data-id="${x.id}" aria-label="Delete example">✕</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="rt-muted">None yet.</p>'}</div></div>`;
  }

  // ---- the policy editor ------------------------------------------------------------------------------------------------
  const LEARN_FIELDS = [
    ['enabled', 'bool', 'Learn from outcomes and feedback'], ['explore', 'num', 'Share of picks that explore (0–1)', 0, 1, 0.01],
    ['half_life_days', 'num', 'Half-life of what it learned (days)', 0.1, 365, 0.5], ['prior_strength', 'num', 'Weight of a catalog guess (outcomes)', 0, 100, 1],
    ['feedback_weight', 'num', 'One piece of feedback counts as (outcomes)', 0, 50, 0.5], ['classifier', 'bool', 'Let training examples override the keywords'],
    ['classifier_min_examples', 'num', 'Examples needed before the classifier speaks', 1, 1000, 1], ['classifier_confidence', 'num', 'Confidence it needs to override (0.34–1)', 0.34, 1, 0.01],
    ['similar_example_boost', 'num', 'Most a similar example can add (0–1)', 0, 1, 0.01], ['failover_for_auto', 'bool', 'Bots on auto try the next pick when their model fails'],
    ['block_after', 'num', 'Failures in a row before a 403/404 earns the long rest', 1, 100, 1],
  ];
  function policyForm() {
    const p = st.draft;
    const m = st.meta;
    const weights = m.task_classes.map((cls) => {
      const w = p.weights[cls];
      const sum = COMPS.reduce((a, c) => a + Number(w[c] || 0), 0) || 1;
      return `<tr><td>${cls}</td>${COMPS.map((c) => `<td><input type="number" min="0" max="1" step="0.05" value="${w[c]}" data-w="${cls}.${c}" aria-label="${cls} ${c}"></td>`).join('')}
        <td class="rt-wbar"><div class="rt-bar">${COMPS.map((c) => `<i style="width:${(w[c] / sum * 100).toFixed(1)}%;background:${COLORS[c]}" title="${c} ${pct(w[c] / sum)}"></i>`).join('')}</div></td></tr>`;
    }).join('');
    const rules = p.rules.map((r, i) => `<tr><td><select data-rule="${i}.kind">${m.rule_kinds.map((k) => `<option ${r.kind === k ? 'selected' : ''}>${k}</option>`).join('')}</select></td>
      <td><input data-rule="${i}.model" value="${E(r.model)}" list="rt-pol-models" placeholder="provider/model or provider/*"></td><td><input data-rule="${i}.classes" value="${E((r.classes || []).join(', '))}" placeholder="all classes"></td>
      <td><input type="number" min="0" max="1" step="0.05" data-rule="${i}.boost" value="${r.boost}" ${r.kind === 'prefer' || r.kind === 'avoid' ? '' : 'disabled'}></td>
      <td><input type="date" data-rule="${i}.until" value="${r.until ? new Date(r.until * 1000).toISOString().slice(0, 10) : ''}"></td><td><input data-rule="${i}.note" value="${E(r.note || '')}"></td>
      <td><button class="btn danger" data-rt="rule-del" data-i="${i}" aria-label="Remove rule">✕</button></td></tr>`).join('');
    const L = p.learning;
    const learn = LEARN_FIELDS.map(([k, t, label, lo, hi, step]) => t === 'bool'
      ? `<label class="rt-row"><input type="checkbox" data-l="${k}" ${L[k] ? 'checked' : ''}> ${label}</label>`
      : `<label>${label} <input type="number" data-l="${k}" value="${L[k]}" min="${lo}" max="${hi}" step="${step}"></label>`).join('');
    const cds = Object.keys(L.cooldown_s).map((k) => `<label>${k.replace(/_/g, ' ')} <input type="number" min="0" data-cd="${k}" value="${L.cooldown_s[k]}"> <small class="rt-muted">s</small></label>`).join('');
    return `<div class="rt-pol"><div class="rt-card"><h4>Weights: how much each part counts, per task class</h4><p class="rt-muted">Each row is scaled to add up to 1 when scoring; the bar shows the resulting shares.</p>
        <div class="rt-scroll"><table class="rt-table"><thead><tr><th>class</th>${COMPS.map((c) => `<th><i style="display:inline-block;width:9px;height:9px;background:${COLORS[c]};border-radius:2px"></i> ${c}</th>`).join('')}<th>shares</th></tr></thead><tbody>${weights}</tbody></table></div></div>
      <div class="rt-grid"><div class="rt-card"><h4>Extra classification keywords</h4><p class="rt-muted">Comma-separated words or phrases, added to the built-in ones.</p><div class="rt-form">
        ${['coding', 'hard_reasoning', 'bulk', 'trivial'].map((c) => `<label>${c} <input data-kw="${c}" value="${E((p.keywords[c] || []).join(', '))}"></label>`).join('')}</div></div>
        <div class="rt-card"><h4>Behaviour</h4><div class="rt-form"><label class="rt-row"><input type="checkbox" data-top="sticky" ${p.sticky ? 'checked' : ''}> A bot on auto keeps its pick between turns (re-routes when it starts failing)</label>
          <label class="rt-row"><input type="checkbox" data-top="record_task_text" ${p.record_task_text ? 'checked' : ''}> Record the first 240 characters of each task (secrets removed)</label>
          <label>Keep decisions and the log for (days) <input type="number" min="1" max="3650" data-top="retention_days" value="${p.retention_days}"></label></div></div></div>
      <div class="rt-card" style="margin-top:12px"><h4>Rules</h4><p class="rt-muted"><b>pin</b> puts a model first when it can do the task · <b>prefer</b>/<b>avoid</b> add or subtract the boost · <b>block</b> never picks it. Leave classes empty for every class, and the date empty for no end.</p>
        <datalist id="rt-pol-models">${st.candidates.map((c) => `<option value="${E(c)}">`).join('')}</datalist>
        ${p.rules.length ? `<div class="rt-scroll"><table class="rt-table"><thead><tr><th>kind</th><th>model</th><th>classes</th><th>boost</th><th>until</th><th>note</th><th></th></tr></thead><tbody>${rules}</tbody></table></div>` : '<p class="rt-muted">No rules.</p>'}
        <button class="btn" data-rt="rule-add">Add a rule</button></div>
      <div class="rt-grid"><div class="rt-card"><h4>Learning</h4><div class="rt-form">${learn}</div></div>
        <div class="rt-card"><h4>How long a model rests after each kind of failure</h4><p class="rt-muted">A 429 starts at "rate limited" and doubles on each repeat up to "rate limited max". 0 means no rest.</p><div class="rt-form">${cds}</div></div></div>
      <div class="rt-card" style="margin-top:12px"><div class="rt-row"><input id="rt-pol-note" placeholder="What is this change for? (kept in the history)" style="flex:1;min-width:220px">
        <button class="btn primary" data-rt="pol-save">Save as a new version</button><button class="btn" data-rt="pol-revert">Discard edits</button><button class="btn" data-rt="pol-defaults">Load the defaults</button><button class="btn" data-rt="pol-json">Edit as JSON</button></div>
        <div id="rt-pol-json" hidden><textarea id="rt-pol-text" rows="16" style="width:100%;margin-top:8px" class="rt-mono"></textarea><button class="btn" data-rt="pol-json-apply">Use this JSON</button></div></div>
      <div class="rt-card" style="margin-top:12px"><h4>History</h4><div id="rt-pol-history"><p class="rt-muted">Loading…</p></div></div></div>`;
  }
  async function renderPolicy(body) {
    const [p, m] = await Promise.all([api('/api/router/policy'), api('/api/router/models')]);
    st.meta = p; st.policy = p.policy; st.candidates = m.candidates || [];
    if (!st.draft) st.draft = JSON.parse(JSON.stringify(p.policy));
    body.innerHTML = `<p class="rt-muted">Policy v${p.version} is in force. Edits here take effect when saved, as a new version; any version can be restored.</p>` + policyForm();
    const h = await api('/api/router/policy/history');
    $('rt-pol-history').innerHTML = h.versions.length ? `<ul class="rt-list">${h.versions.map((v) => `<li><b>v${v.version}</b><div style="flex:1">${E(v.note || '(no note)')} <span class="rt-muted">· ${E(v.actor)} · ${when(v.ts)}</span>
        ${v.changes.length ? `<details class="rt-more"><summary>${v.changes.length} change(s)</summary><div class="rt-mono">${v.changes.map(E).join('<br>')}</div></details>` : ''}</div>
        ${v.version === h.current ? '<span class="rt-chip ok">in force</span>' : `<button class="btn" data-rt="pol-restore" data-v="${v.version}">Restore</button>`}</li>`).join('')}
        <li><b>v0</b><div style="flex:1">The built-in defaults</div>${h.current ? '<button class="btn" data-rt="pol-restore" data-v="0">Restore</button>' : '<span class="rt-chip ok">in force</span>'}</li></ul>` : '<p class="rt-muted">Only the built-in defaults so far.</p>';
  }
  function readPolicyForm() {
    const root = $('rt-root');
    const p = st.draft;
    root.querySelectorAll('[data-w]').forEach((el) => { const [c, k] = el.dataset.w.split('.'); p.weights[c][k] = Number(el.value); });
    root.querySelectorAll('[data-kw]').forEach((el) => { p.keywords[el.dataset.kw] = el.value.split(',').map((s) => s.trim()).filter(Boolean); });
    root.querySelectorAll('[data-rule]').forEach((el) => {
      const [i, k] = el.dataset.rule.split('.');
      const r = p.rules[Number(i)];
      if (k === 'classes') r.classes = el.value.split(',').map((s) => s.trim()).filter(Boolean);
      else if (k === 'boost') r.boost = Number(el.value);
      else if (k === 'until') r.until = el.value ? Math.floor(new Date(el.value + 'T23:59:59').getTime() / 1000) : null;
      else r[k] = el.value;
    });
    root.querySelectorAll('[data-l]').forEach((el) => { p.learning[el.dataset.l] = el.type === 'checkbox' ? el.checked : Number(el.value); });
    root.querySelectorAll('[data-cd]').forEach((el) => { p.learning.cooldown_s[el.dataset.cd] = Number(el.value); });
    root.querySelectorAll('[data-top]').forEach((el) => { p[el.dataset.top] = el.type === 'checkbox' ? el.checked : Number(el.value); });
    return p;
  }

  async function renderTry(body) {
    body.innerHTML = `<div class="rt-card"><h4>What would it do?</h4><p class="rt-muted">Routes a task the way a bot on auto would right now, without running it or recording anything (and without the exploration draw).</p>
      <form class="rt-form" data-rt-form="simulate"><textarea name="task" rows="4" required placeholder="Describe a task, e.g. refactor the auth module and add tests">${E(st.lastTry || '')}</textarea>
      <div class="rt-row"><label class="rt-row"><input type="checkbox" name="images"> includes an image</label><label>material (tokens) <input type="number" name="context_tokens" min="0" value="0" style="width:110px"></label>
      <button class="btn primary" type="submit">Route it</button></div></form><div id="rt-try-out" style="margin-top:10px"></div></div>`;
  }

  const RENDER = { overview: renderOverview, decisions: renderDecisions, models: renderModels, log: renderLog, train: renderTrain, policy: renderPolicy, try: renderTry };

  async function load() {
    const root = $('rt-root');
    if (!root || loading) return;
    loading = true;
    try {
      if (!st.meta) { const p = await api('/api/router/policy'); st.meta = p; st.policy = p.policy; }
      root.innerHTML = `<div class="rt-tabs" role="tablist">${TABS.map(([k, l]) => `<button role="tab" aria-selected="${st.tab === k}" data-rt="tab" data-tab="${k}">${l}</button>`).join('')}
        <span style="flex:1"></span><button class="btn" data-rt="refresh" title="Refresh" aria-label="Refresh">↻</button></div><div id="rt-body" role="tabpanel"><p class="rt-muted">Loading…</p></div>`;
      await (RENDER[st.tab] || renderOverview)($('rt-body'));
    } catch (e) {
      root.innerHTML = `<p class="cardnote">Router data unavailable: ${E(errText(e))}</p>`;
    } finally { loading = false; }
  }
  async function show(tab) { st.tab = tab; try { localStorage.setItem('rt-tab', tab); } catch (_) {} await load(); }

  async function act(el, ev) {
    const a = el.dataset.rt;
    try {
      if (a === 'tab') return show(el.dataset.tab);
      if (a === 'refresh') { st.meta = null; return load(); }
      if (a === 'open') { ev.preventDefault(); return openDecision(Number(el.dataset.id)); }
      if (a === 'goto') { ev.preventDefault(); st.selected = Number(el.dataset.id); return show('decisions'); }
      if (a === 'logkind') { st.logKind = el.dataset.kind; return renderLog($('rt-body')); }
      if (a === 'rate') {
        const r = await post(`/api/router/decisions/${el.dataset.id}/feedback`, { rating: Number(el.dataset.v) });
        toast(r.learned.length ? 'Learned: ' + r.learned.join('; ') : 'Noted', 'good');
        return openDecision(Number(el.dataset.id));
      }
      if (a === 'release') { await post('/api/router/models/release', { model: el.dataset.model }); toast(el.dataset.model + ' may be used again', 'good'); return load(); }
      if (a === 'rest') { await post('/api/router/models/rest', { model: el.dataset.model, seconds: 3600 }); toast(el.dataset.model + ' rests for an hour', 'good'); return load(); }
      if (a === 'rest-custom') {
        const model = $('rt-rest-model').value.trim();
        await post('/api/router/models/rest', { model, seconds: Number($('rt-rest-secs').value) });
        toast(model + ' is resting', 'good'); return load();
      }
      if (a === 'forget') {
        if (!window.confirm(`Forget everything learned about ${el.dataset.model}? Its reliability, speed and feedback start over.`)) return;
        await post('/api/router/models/forget', { model: el.dataset.model }); toast('Forgotten', 'good'); return load();
      }
      if (a === 'reset-all') {
        if (!window.confirm('Reset all learning? Decisions, statistics, rests and the learning log are deleted. The policy and training examples are kept.')) return;
        await post('/api/router/reset', { keep_policy: true, keep_examples: true }); toast('Learning reset', 'good'); return load();
      }
      if (a === 'rule') {
        const p = (await api('/api/router/policy')).policy;
        p.rules.push({ kind: el.dataset.kind, model: el.dataset.model, classes: [], boost: 0.2, until: null, note: 'added from the Models tab' });
        const r = await post('/api/router/policy', { policy: p, note: `${el.dataset.kind} ${el.dataset.model}` }, 'PUT');
        st.draft = null; st.meta = null;
        toast(`Policy v${r.version}: ${el.dataset.kind} ${el.dataset.model}`, 'good'); return load();
      }
      if (a === 'del-example') { await api('/api/router/examples/' + el.dataset.id, { method: 'DELETE' }); return renderTrain($('rt-body')); }
      if (a === 'rule-add') { readPolicyForm(); st.draft.rules.push({ kind: 'prefer', model: '', classes: [], boost: 0.2, until: null, note: '' }); return renderPolicy($('rt-body')); }
      if (a === 'rule-del') { readPolicyForm(); st.draft.rules.splice(Number(el.dataset.i), 1); return renderPolicy($('rt-body')); }
      if (a === 'pol-revert') { st.draft = null; return renderPolicy($('rt-body')); }
      if (a === 'pol-defaults') { st.draft = JSON.parse(JSON.stringify(st.meta.defaults)); toast('Defaults loaded; save to use them', 'good'); return renderPolicy($('rt-body')); }
      if (a === 'pol-json') { readPolicyForm(); $('rt-pol-json').hidden = !$('rt-pol-json').hidden; $('rt-pol-text').value = JSON.stringify(st.draft, null, 2); return; }
      if (a === 'pol-json-apply') { st.draft = JSON.parse($('rt-pol-text').value); return renderPolicy($('rt-body')); }
      if (a === 'pol-save') {
        const note = $('rt-pol-note').value;
        const r = await post('/api/router/policy', { policy: readPolicyForm(), note }, 'PUT');
        toast(r.changes.length ? `Saved as v${r.version} (${r.changes.length} change(s))` : 'Nothing changed', 'good');
        st.draft = null; st.meta = null; return load();
      }
      if (a === 'pol-restore') {
        const v = Number(el.dataset.v);
        if (!window.confirm(v ? `Restore policy version ${v}? It becomes a new version; nothing is lost.` : 'Restore the built-in defaults? It becomes a new version; nothing is lost.')) return;
        const r = await post('/api/router/policy/rollback', { version: v });
        toast(`Restored as v${r.version}`, 'good'); st.draft = null; st.meta = null; return load();
      }
    } catch (e) { toast(errText(e), 'error'); }
  }

  async function submit(form, ev) {
    ev.preventDefault();
    const kind = form.dataset.rtForm;
    const data = Object.fromEntries(new FormData(form).entries());
    try {
      if (kind === 'feedback') {
        const r = await post(`/api/router/decisions/${form.dataset.id}/feedback`, { note: data.note, correct_class: data.correct_class || null, preferred_model: data.preferred_model || null });
        toast(r.learned.length ? 'Learned: ' + r.learned.join('; ') : 'Noted', 'good');
        return openDecision(Number(form.dataset.id));
      }
      if (kind === 'example') {
        await post('/api/router/examples', { text: data.text, task_class: data.task_class || null, preferred_model: data.preferred_model || null });
        toast('Example added', 'good'); return renderTrain($('rt-body'));
      }
      if (kind === 'simulate') {
        st.lastTry = data.task;
        const out = $('rt-try-out');
        out.innerHTML = '<p class="rt-muted">Thinking…</p>';
        const r = await post('/api/router/simulate', { task: data.task, images: !!data.images, context_tokens: Number(data.context_tokens || 0) });
        out.innerHTML = thinking({ classification: r.classification, candidates: r.candidates, skipped: r.skipped, weights: r.weights, chosen: r.candidates.length ? r.candidates[0].model : null })
          + `<details class="rt-more"><summary>As text</summary><pre class="rt-mono" style="white-space:pre-wrap">${E(r.text)}</pre></details>`;
      }
    } catch (e) { toast(errText(e), 'error'); }
  }

  function init() {
    const root = $('rt-root');
    if (!root) return;
    css();
    root.addEventListener('click', (ev) => { const el = ev.target.closest('[data-rt]'); if (el && root.contains(el)) act(el, ev); });
    root.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { const el = ev.target.closest('tr[data-rt]'); if (el) act(el, ev); } });
    root.addEventListener('submit', (ev) => { const f = ev.target.closest('form[data-rt-form]'); if (f) submit(f, ev); });
    root.addEventListener('change', (ev) => {
      const t = ev.target;
      if (t.matches('[data-rt-hours]')) { st.hours = Number(t.value); load(); }
      else if (t.matches('[data-rt-filter]')) { st.filter[t.dataset.rtFilter] = t.value.trim(); st.selected = null; renderDecisions($('rt-body')); }
      else if (t.matches('[data-rule$=".kind"]')) { readPolicyForm(); renderPolicy($('rt-body')); }
    });
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'router') load(); };
    window.addEventListener('hashchange', onHash);
    onHash();
    if ('IntersectionObserver' in window) {
      let seen = false;
      new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting && !seen) { seen = true; load(); } })).observe(root);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
