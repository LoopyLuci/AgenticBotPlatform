// Module panel: one generic, hand-written renderer that shows ANY module - its status, its operations as forms built
// from their typed inputs (type, required, help, choices) and what they return. What differs per module is only a
// declarative layout spec (abp-panel.json beside the module's files: which operations to feature, sections, labels, how
// to show each result as a table, text or JSON), read from GET /api/modules/{id}/panel and, when there is none, a default
// grouping. The spec holds no HTML and no script: this file draws everything, and every value that comes from the
// module, its hub, a result or a proposal goes through E(). Operations run through the module operation API
// (POST /api/modules/{id}/call, write auth); a destructive one asks for confirmation first. A layout can be proposed by a
// model through the assisted-setup routes and is kept only when a person picks one.
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_module_panel.py fails if the two differ. modules-panel.js opens it with ABPModulePanel.mount(root, id, ctx),
// ctx = { api, post, back, toast }.
(function () {
  'use strict';
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const errText = (e) => { if (window.ABP && ABP.unitUnavailable && ABP.unitUnavailable(e)) return 'This module is not loaded.'; let m = e && e.message ? e.message : String(e); try { const d = JSON.parse(m).detail; m = (d && d.error) || d || m; } catch (_) {} return typeof m === 'string' ? m : JSON.stringify(m); };
  const KINDS = ['table', 'text', 'json'];
  const MAX_ROWS = 200;
  const MAX_COLS = 20;
  const enc = encodeURIComponent;

  function css() {
    if (document.getElementById('mp-css')) return;
    const s = document.createElement('style');
    s.id = 'mp-css';
    s.textContent = `.mp-card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--surface);min-width:0;display:flex;flex-direction:column;gap:8px}
.mp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(340px,100%),1fr));gap:12px;margin-bottom:14px}
.mp-sec h3{margin:12px 0 2px;font-size:15px}.mp-muted{color:var(--muted);font-size:12px}.mp-mono{font-family:var(--font-mono,monospace);font-size:12px;word-break:break-all}
.mp-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.mp-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.mp-chip.on{background:var(--good-soft);color:var(--good,#1f9d55)}.mp-chip.warn{background:var(--warning-soft);color:#8a5c00}.mp-chip.bad{background:var(--danger-soft,#fde8e8);color:var(--danger,#c0392b)}
.mp-pre{white-space:pre-wrap;max-height:45vh;overflow:auto;background:var(--surface-2);border-radius:6px;padding:8px;font-family:var(--font-mono,monospace);font-size:11.5px}
.mp-scroll{overflow:auto;max-height:45vh}.mp-table{border-collapse:collapse;font-size:12px;width:100%}.mp-table th{text-align:left;color:var(--muted);padding:4px 6px;border-bottom:1px solid var(--line);white-space:nowrap}.mp-table td{padding:4px 6px;border-bottom:1px solid var(--line);vertical-align:top;word-break:break-word}
.mp-form{display:grid;gap:8px}.mp-form label{display:grid;gap:3px;font-size:12px;color:var(--ink-soft)}.mp-form label.chk{display:flex;gap:6px;align-items:center}
.mp-form input,.mp-form select,.mp-form textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%}`;
    document.head.appendChild(s);
  }

  const chipHtml = (text, kind) => { const clsAttr = ['on', 'warn', 'bad'].includes(kind) ? kind : ''; return `<span class="mp-chip ${clsAttr}">${E(text)}</span>`; };

  // ---- results: table | text | json, never markup ------------------------------------------------------------------
  const cellHtml = (v) => E(v !== null && typeof v === 'object' ? JSON.stringify(v) : v);

  function rowsOf(r) {
    if (Array.isArray(r)) return r;
    if (r && typeof r === 'object') {
      const arrays = Object.keys(r).filter((k) => Array.isArray(r[k]));
      if (arrays.length === 1) return r[arrays[0]];
    }
    return null;
  }

  const rowHtml = (x, cols) => { const tdsHtml = cols.map((c) => `<td>${cellHtml(x[c])}</td>`).join(''); return `<tr>${tdsHtml}</tr>`; };

  function tableHtml(rows) {
    if (!rows.every((x) => x && typeof x === 'object' && !Array.isArray(x))) return null;
    const cols = [];
    rows.slice(0, MAX_ROWS).forEach((x) => Object.keys(x).forEach((k) => { if (!cols.includes(k) && cols.length < MAX_COLS) cols.push(k); }));
    if (!cols.length) return null;
    const headHtml = cols.map((c) => `<th>${E(c)}</th>`).join('');
    const bodyHtml = rows.slice(0, MAX_ROWS).map((x) => rowHtml(x, cols)).join('');
    const moreHtml = rows.length > MAX_ROWS ? `<p class="mp-muted">${E(rows.length - MAX_ROWS)} more rows not shown.</p>` : '';
    return `<div class="mp-scroll"><table class="mp-table"><tr>${headHtml}</tr>${bodyHtml}</table></div>${moreHtml}`;
  }

  function textOf(r) {
    if (typeof r === 'string') return r;
    if (r && typeof r === 'object') {
      for (const k of ['stdout', 'output', 'text', 'message']) if (typeof r[k] === 'string') return r[k] + (typeof r.stderr === 'string' && r.stderr ? '\n' + r.stderr : '');
    }
    return null;
  }

  function resultHtml(kind, r) {
    if (r && r.format === 'png' && typeof r.base64 === 'string' && /^[A-Za-z0-9+/=]+$/.test(r.base64)) return `<img alt="result" style="max-width:100%" src="data:image/png;base64,${E(r.base64)}">`;
    if (kind === 'table') { const rows = rowsOf(r); const t = rows && tableHtml(rows); if (t) return t; }
    if (kind === 'text') { const t = textOf(r); if (t !== null) return `<div class="mp-pre">${E(t)}</div>`; }
    return `<div class="mp-pre">${E(JSON.stringify(r, null, 1))}</div>`;
  }

  // ---- forms from the typed inputs ---------------------------------------------------------------------------------
  function fieldHtml(name, s, required) {
    s = s || {};
    const labelHtml = E(name) + (required ? ' *' : '') + (s.description ? ` <span class="mp-muted">${E(s.description)}</span>` : '');
    const reqAttr = required ? ' data-required="1"' : '';
    const attrsHtml = `data-name="${E(name)}"${reqAttr}`;
    const numKind = s.type === 'integer' || s.type === 'number' ? s.type : 'string';
    const kindAttr = numKind;
    const stepAttr = s.type === 'integer' ? ' step="1"' : ' step="any"';
    if (Array.isArray(s.enum) && s.enum.length) {
      const optHtml = (required ? '' : '<option value=""></option>') + s.enum.map((v) => `<option value="${E(v)}">${E(v)}</option>`).join('');
      return `<label>${labelHtml}<select ${attrsHtml} data-kind="${kindAttr}">${optHtml}</select></label>`;
    }
    if (s.type === 'boolean') return `<label class="chk"><input type="checkbox" ${attrsHtml} data-kind="bool">${labelHtml}</label>`;
    if (s.type === 'integer' || s.type === 'number') return `<label>${labelHtml}<input type="number" ${attrsHtml} data-kind="${kindAttr}"${stepAttr}></label>`;
    if (s.type === 'object' || s.type === 'array' || !s.type) return `<label>${labelHtml}<textarea rows="3" ${attrsHtml} data-kind="json" placeholder="JSON"></textarea></label>`;
    return `<label>${labelHtml}<input ${attrsHtml} data-kind="string"></label>`;
  }

  function readForm(card) {
    const args = {};
    card.querySelectorAll('[data-name]').forEach((el) => {
      const k = el.dataset.name, kind = el.dataset.kind;
      if (kind === 'bool') { if (el.checked) args[k] = true; else if (el.dataset.required) args[k] = false; return; }
      const v = el.value.trim();
      if (v === '') { if (el.dataset.required) throw new Error(k + ' is required'); return; }
      args[k] = kind === 'integer' ? parseInt(v, 10) : kind === 'number' ? parseFloat(v) : kind === 'json' ? JSON.parse(v) : v;
    });
    return args;
  }

  function opCardHtml(o, label, output) {
    const sch = o.input_schema || {}, props = sch.properties || {}, req = sch.required || [];
    const fieldsHtml = Object.entries(props).map(([k, s]) => fieldHtml(k, s, req.includes(k))).join('') || '<span class="mp-muted">No arguments.</span>';
    const flagHtml = o.mutating ? chipHtml(o.destructive ? 'destructive' : 'changes something', o.destructive ? 'bad' : 'warn') : '';
    return `<div class="mp-card" data-op="${E(o.id)}" data-output="${E(output)}"><div class="mp-row" style="justify-content:space-between"><b>${E(label || o.id)}</b>${flagHtml}</div>
<div class="mp-muted">${E(o.summary)}</div><div class="mp-mono mp-muted">${E(o.id)}</div>
<div class="mp-form">${fieldsHtml}<div class="mp-row"><button class="btn" data-run="1">Run</button></div></div><div data-out="1"></div></div>`;
  }

  // ---- layout ------------------------------------------------------------------------------------------------------
  function defaultLayout(ops) {
    const groups = {};
    ops.forEach((o) => { const g = o.id.includes('.') ? o.id.split('.')[0] : 'other'; (groups[g] = groups[g] || []).push({ id: o.id }); });
    return { sections: Object.keys(groups).map((g) => ({ title: g, operations: groups[g] })) };
  }

  function sectionsHtml(lay, byId, featured) {
    const used = new Set();
    const parts = (lay.sections || []).map((s) => {
      const cards = (s.operations || []).filter((e) => byId[e.id] && !used.has(e.id)).map((e) => { used.add(e.id); return opCardHtml(byId[e.id], e.label, KINDS.includes(e.output) ? e.output : 'json'); });
      if (!cards.length) return '';
      const noteHtml = s.note ? `<div class="mp-muted">${E(s.note)}</div>` : '';
      const gridHtml = cards.join('');
      return `<div class="mp-sec"><h3>${E(s.title)}</h3>${noteHtml}<div class="mp-grid">${gridHtml}</div></div>`;
    });
    const rest = Object.keys(byId).filter((id) => !used.has(id));
    if (featured && rest.length) {
      const restHtml = rest.map((id) => opCardHtml(byId[id], '', 'json')).join('');
      parts.push(`<details class="mp-sec"><summary><b>More operations</b> <span class="mp-muted">${E(rest.length)} not featured in this layout</span></summary><div class="mp-grid">${restHtml}</div></details>`);
    }
    return parts.join('');
  }

  // ---- proposals of a layout (assisted setup, panel mode) ----------------------------------------------------------
  function proposalsHtml(b) {
    const colsHtml = (b.proposals || []).map((p) => {
      const diffHtml = (p.diff || []).map((d) => `<li class="mp-muted">${E(d.to)}</li>`).join('');
      const probHtml = (p.problems || []).map((x) => `<li class="mp-muted">${E(x)}</li>`).join('');
      const rejHtml = p.ok ? '' : chipHtml('rejected', 'bad');
      return `<div class="mp-card"><div class="mp-row"><b>Proposal ${E(p.n)}</b> ${rejHtml} <span class="mp-muted">${E(p.model)}</span></div>`
        + (p.ok ? `<ul>${diffHtml}</ul><div class="mp-row"><button class="btn" data-keep="${E(p.n)}" data-batch="${E(b.batch)}">Use this layout</button></div>` : `<ul>${probHtml}</ul>`) + '</div>';
    }).join('');
    const errHtml = (b.errors || []).length ? `<p class="mp-muted">${E(b.errors.join(' | '))}</p>` : '';
    const gridHtml = colsHtml ? `<div class="mp-grid">${colsHtml}</div>` : '<p class="mp-muted">No proposal came back.</p>';
    return `<p class="mp-muted">Nothing is saved until you use one. A saved layout lives in abp-panel.json beside the module's files, so a refresh keeps it.</p>${gridHtml}${errHtml}`;
  }

  // ---- the panel ---------------------------------------------------------------------------------------------------
  async function mount(root, mid, ctx) {
    css();
    const api = ctx.api, post = ctx.post, toast = ctx.toast || (() => {});
    const token = (root._mpToken = (root._mpToken || 0) + 1);
    const live = () => root._mpToken === token;
    const base = '/api/modules/' + enc(mid);
    root.innerHTML = '<p class="mp-muted">Loading…</p>';
    let status, lay, ops = [], opsErr = '';
    try {
      [status, lay] = await Promise.all([api(base), api(base + '/panel')]);
    } catch (e) { root.innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; return; }
    const m = status.module || {}, inst = status.install || {}, hub = status.hub || {};
    if (m.has_hub && hub.running) {
      try { ops = await api(base + '/operations'); } catch (e) { opsErr = errText(e); }
    }
    if (!live()) return;
    const byId = {};
    ops.forEach((o) => { byId[o.id] = o; });
    const layout = lay.has_spec ? lay.layout : ((lay.layout.sections || []).length ? lay.layout : defaultLayout(ops));
    const chipsHtml = [inst.installed ? chipHtml(inst.ready ? 'built' : 'not built', inst.ready ? 'on' : 'warn') : chipHtml('not installed'),
      m.has_hub ? (hub.running ? chipHtml('hub running', 'on') : chipHtml('hub stopped')) : '',
      lay.has_spec ? chipHtml('custom layout', 'on') : chipHtml('default layout')].join(' ');
    const probHtml = (lay.problems || []).length ? `<p class="mp-muted">The saved layout was ignored: ${E(lay.problems.join('; '))}</p>` : '';
    const bodyHtml = !m.has_hub ? '<p class="mp-muted">This module has no hub, so it has no operations to run.</p>'
      : !hub.running ? '<p class="mp-muted">Start the hub (Details, Start hub) to use its operations.</p>'
      : opsErr ? `<p class="cardnote">${E(opsErr)}</p>`
      : (sectionsHtml(layout, byId, lay.has_spec) || '<p class="mp-muted">No operations.</p>');
    root.innerHTML = `<div class="mp-row" style="margin-bottom:8px"><button class="btn ghost" data-mp="back">← Module</button><h3 style="margin:0">${E(lay.layout.title || m.name || mid)}</h3>${chipsHtml}</div>
<div class="mp-muted">${E(m.description)}</div>${probHtml}
<div class="mp-row" style="margin:8px 0"><button class="btn ghost" data-mp="reload">Reload</button><button class="btn ghost" data-mp="propose">Propose a layout</button></div><div data-mp-out="1"></div>
${bodyHtml}`;
    root.querySelector('[data-mp="back"]').addEventListener('click', () => { if (ctx.back) ctx.back(); });
    root.querySelector('[data-mp="reload"]').addEventListener('click', () => mount(root, mid, ctx));
    root.querySelector('[data-mp="propose"]').addEventListener('click', async () => {
      const out = root.querySelector('[data-mp-out]');
      out.innerHTML = '<p class="mp-muted">Asking the models for layouts…</p>';
      try {
        const b = await post(base + '/assist', { n: 3, panel: true });
        out.innerHTML = proposalsHtml(b);
        out.querySelectorAll('[data-keep]').forEach((btn) => btn.addEventListener('click', async () => {
          try {
            await post(base + '/assist/keep', { batch: btn.dataset.batch, proposal: Number(btn.dataset.keep) });
            toast('Layout saved'); mount(root, mid, ctx);
          } catch (e) { toast(errText(e), 'error'); }
        }));
      } catch (e) { out.innerHTML = `<p class="mp-muted">${E(errText(e))}</p>`; }
    });
    root.querySelectorAll('[data-run]').forEach((btn) => btn.addEventListener('click', async () => {
      const card = btn.closest('[data-op]'), o = byId[card.dataset.op], out = card.querySelector('[data-out]');
      if (!o) return;
      let args;
      try { args = readForm(card); } catch (e) { toast(errText(e) || 'An argument is not valid', 'error'); return; }
      // A destructive operation asks first, and so does any that changes something; a read-only one runs at once.
      if (o.destructive && !confirm(o.id + ' cannot be undone. Run it?')) return;
      if (o.mutating && !o.destructive && !confirm(o.id + ' changes something. Run it?')) return;
      out.innerHTML = '<p class="mp-muted">Running…</p>';
      try {
        const r = (await post(base + '/call', { operation: o.id, args })).result;
        out.innerHTML = resultHtml(card.dataset.output, r);
      } catch (e) { out.innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; }
    }));
  }

  window.ABPModulePanel = { mount };
})();
