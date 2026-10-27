// Video Studio (V1 engine, bot/video): the job queue, the library with provenance, workflows, models and settings.
// The Workspace's right column shows a card (ABPVideo.card()); "Open Video Studio" opens this file's own dialog with a
// new job, the jobs (progress, cancel), the library (play, lineage, provenance), the models and every setting.
// Shared, identical, by the dashboard and the desktop app.
(function () {
  'use strict';
  const E = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const V = { data: null, at: 0, busy: false, poll: null };
  const base = () => (typeof API_BASE !== 'undefined' ? API_BASE : '');
  const note = (msg, kind) => { if (typeof window.toast === 'function') window.toast(msg, kind); else { const n = document.getElementById('vs-msg'); if (n) n.textContent = msg; } };
  const errText = (e) => { const t = String((e && e.message) || e); try { return JSON.parse(t).detail || t; } catch (_) { return t; } };
  const url = (p) => `${base()}${p}`;
  const ACTIVE = ['queued', 'waiting_gpu', 'rendering'];
  const SWITCHES = [
    ['enabled', 'Video Studio on', 'Queue and render video jobs on this machine. Renders take the GPU for minutes to hours.'],
    ['unload_mm', 'Make room in VRAM', 'When free VRAM is short, unload the ModelMistress models for the render and load them again afterwards.'],
    ['visible_label', 'Visible AI label', 'Burn "AI-GENERATED | <id>" into every frame (provenance is embedded and in a sidecar either way).'],
    ['resume', 'Resume after a restart', 'Queue jobs a restart interrupted again (at most 3 starts); off fails them with a plain reason.'],
  ];
  const TEXTS = [
    ['sdcpp_bin', 'sd-cli path', 'stable-diffusion.cpp, Vulkan build (GGML_VK_DISABLE_COOPMAT=1 is always set).'],
    ['models_dir', 'Models folder', 'Where the model files live; empty = data/video/models. A fast disk loads models far quicker.'],
    ['ffmpeg', 'ffmpeg path', 'Empty = $ABP_FFMPEG, then PATH.'],
    ['ffmpeg_sha256', 'ffmpeg SHA-256 pin', 'When set, ffmpeg must hash to this or nothing runs.'],
  ];

  async function load(force) {
    if (!force && V.data && Date.now() - V.at < 15000) return V.data;
    try { V.data = await api('/api/video'); V.at = Date.now(); } catch (_) { V.data = null; }
    paintCards();
    if (document.getElementById('vs-dialog')) paintDialog();
    return V.data;
  }

  // ---- the Workspace card ------------------------------------------------------------------------------------------
  function cardBody() {
    const d = V.data;
    if (!d) return '<p class="lc-note">Render clips on this machine, with a library and provenance.</p><button type="button" class="btn" data-vs="open">Open Video Studio</button>';
    const s = d.settings || {};
    const q = d.queue || {};
    return `<div class="lc-kv"><div><label title="${E(SWITCHES[0][2])}"><input type="checkbox" data-vs="set-bool" data-key="enabled"${s.enabled ? ' checked' : ''}> Video Studio on</label></div>
<div><span>Jobs waiting or rendering</span><b>${E(q.active || 0)}</b></div><div><span>Clips in the library</span><b>${E((d.assets || []).length)}</b></div></div>
<button type="button" class="btn" data-vs="open">Open Video Studio</button>`;
  }
  function paintCards() { document.querySelectorAll('.vs-card-body').forEach((el) => { el.innerHTML = cardBody(); }); }
  function card() {
    // no fetch from here: the card is drawn even while the video unit is not loaded (its routes then 404)
    return `<section class="lc-card"><h3>Video Studio</h3><div class="vs-card-body">${cardBody()}</div></section>`;
  }

  async function setSettings(change) {
    try {
      await api('/api/video/settings', { method: 'PUT', body: JSON.stringify(change) });
      note(`Saved: ${Object.keys(change).join(', ')}.`);
    } catch (e) { note(errText(e), 'error'); }
    load(true);
  }

  // ---- the dialog --------------------------------------------------------------------------------------------------
  function settingsHtml(d) {
    const s = d.settings || {};
    const wfIds = [...new Set((d.workflows || []).map((w) => w.id))];
    return `<div class="lc-kv">${SWITCHES.map(([k, l, tip]) => `<div><label title="${E(tip)}"><input type="checkbox" data-vs="set-bool" data-key="${k}"${s[k] ? ' checked' : ''}> ${E(l)}</label></div>`).join('')}
<div><label title="The workflow a job uses when it names none.">Default workflow <select class="lc-sel" data-vs="set-text" data-key="default_workflow">${wfIds.map((id) => `<option value="${E(id)}"${s.default_workflow === id ? ' selected' : ''}>${E(id)}</option>`).join('')}</select></label></div>
<div><label title="The longest a job may render (its budget).">Job timeout (s) <input type="number" min="60" max="86400" step="60" data-vs="set-int" data-key="job_timeout_s" value="${E(s.job_timeout_s)}"></label></div>
<div><label title="The longest clip a job may ask for.">Clip cap (s) <input type="number" min="1" max="120" data-vs="set-int" data-key="max_seconds" value="${E(s.max_seconds)}"></label></div></div>
<div class="vs-form">${TEXTS.map(([k, l, tip]) => `<label title="${E(tip)}">${E(l)} <input data-vs="set-text" data-key="${k}" value="${E(s[k] || '')}" placeholder="${E(tip)}"></label>`).join('')}</div>`;
  }
  function jobRows(d) {
    return (d.jobs || []).map((j) => {
      const pct = Math.round((j.progress || 0) * 100);
      const clip = j.asset ? `<video controls preload="metadata" src="${E(url(j.asset.url))}" class="vs-video"></video>` : '';
      return `<div class="lc-card"><b>${E(j.id)}</b> ${E(j.workflow)} · <b>${E(j.status)}</b>${j.status === 'rendering' ? ` ${pct}%` : ''} — ${E(j.message || '')}
${j.status === 'rendering' ? `<progress max="100" value="${pct}" aria-label="progress of ${E(j.id)}"></progress>` : ''}
${ACTIVE.includes(j.status) ? ` <button type="button" class="btn danger" data-vs="cancel" data-id="${E(j.id)}">Cancel</button>` : ''}
<div class="lc-note">${E((j.params || {}).prompt || '')}</div>${j.error ? `<p class="lc-warn">${E(j.error)}</p>` : ''}${clip}</div>`;
    }).join('') || '<p class="lc-note">No video jobs yet.</p>';
  }
  function assetRows(d) {
    return (d.assets || []).map((a) => `<figure class="vs-asset"><video controls preload="metadata" src="${E(url(a.url))}" class="vs-video"></video>
<figcaption><b>${E(a.id)}</b> ${E(a.workflow)}${a.parent_id ? ` · from ${E(a.parent_id)}` : ''}<br>${E(a.prompt)}<br>seed ${E(a.seed)} · ${E(a.duration)} s ·
<a href="${E(url(a.provenance))}" target="_blank" rel="noopener">provenance</a></figcaption></figure>`).join('') || '<p class="lc-note">The library is empty.</p>';
  }
  function modelRows(d) {
    return (d.models || []).map((m) => {
      const dl = (d.downloads || {})[m.id];
      return `<tr><td>${E(m.title)}</td><td>${E((m.bytes / 1e9).toFixed(1))} GB</td><td>${E(m.vram_gb)} GB</td><td><b>${E(m.state)}</b>${dl ? ` (download ${E(dl.state)}${dl.of ? ` ${Math.round(100 * dl.bytes / dl.of)}% of ${E(dl.file)}` : ''}${dl.error ? `: ${E(dl.error)}` : ''})` : ''}</td>
<td><button type="button" class="btn" data-vs="verify" data-id="${E(m.id)}">Verify</button> ${m.state === 'missing' ? `<button type="button" class="btn" data-vs="download" data-id="${E(m.id)}">Download</button>` : ''}</td></tr>`;
    }).join('');
  }
  function paintDialog() {
    const el = document.getElementById('vs-dialog');
    const d = V.data;
    if (!el || !d) return;
    const s = d.settings || {};
    const ff = d.ffmpeg || {};
    const be = (d.backends || []).map((b) => `${E(b.title)}: ${b.available ? 'ready' : E(b.why)}`).join(' · ');
    const wfOpts = (d.workflows || []).map((w) => `<option value="${E(w.ref)}"${w.id === s.default_workflow ? ' selected' : ''}>${E(w.title)} (${E(w.ref)})</option>`).join('');
    el.querySelector('.vs-body').innerHTML = `
<p class="lc-note">Jobs run one at a time and take the GPU lock. Every clip is labelled AI-generated, with an embedded manifest and a sidecar (lineage included). Backends: ${be}. ffmpeg: ${ff.ffmpeg ? E(ff.ffmpeg) + (ff.pinned ? (ff.verified ? ' (pin verified)' : ' (pin MISMATCH)') : '') : 'not found (OpenCV fallback for thumbnails)'}.</p>
<h4>Settings</h4>${settingsHtml(d)}
<h4>New job</h4><form data-vs="job-form" class="vs-form">
<label>Workflow <select name="workflow">${wfOpts}</select></label>
<label>Prompt <textarea name="prompt" rows="2" required maxlength="2000" placeholder="a red ball rolls across a wooden table"></textarea></label>
<label>Seconds <input type="number" name="seconds" min="0.5" max="${E(s.max_seconds)}" step="0.5" value="2"></label>
<button type="submit" class="btn primary"${s.enabled ? '' : ' disabled title="Switch Video Studio on first"'}>Queue</button></form>
<h4>Jobs</h4>${jobRows(d)}
<h4>Library</h4><div class="vs-grid">${assetRows(d)}</div>
<h4>Models</h4><table class="vs-table"><thead><tr><th>Model</th><th>Size</th><th>VRAM</th><th>State</th><th></th></tr></thead><tbody>${modelRows(d)}</tbody></table>`;
  }

  function open() {
    let el = document.getElementById('vs-dialog');
    if (!el) {
      el = document.createElement('div');
      el.id = 'vs-dialog';
      el.className = 'vs-dialog';
      el.setAttribute('role', 'dialog');
      el.setAttribute('aria-modal', 'true');
      el.setAttribute('aria-label', 'Video Studio');
      el.innerHTML = `<div class="vs-panel"><div class="vs-head"><h3>Video Studio</h3><span id="vs-msg" class="lc-note" aria-live="polite"></span><button type="button" class="btn" data-vs="close" aria-label="Close">✕</button></div><div class="vs-body"></div></div>`;
      document.body.appendChild(el);
    }
    paintDialog();
    load(true);
    clearInterval(V.poll);
    V.poll = setInterval(() => {
      if (!document.getElementById('vs-dialog')) clearInterval(V.poll);
      else if (V.data && ((V.data.jobs || []).some((j) => ACTIVE.includes(j.status)) || Object.values(V.data.downloads || {}).some((x) => x.state === 'running'))) load(true);
    }, 3000);
  }
  function close() { const el = document.getElementById('vs-dialog'); if (el) el.remove(); clearInterval(V.poll); }

  async function submit(form) {
    if (V.busy) return;
    V.busy = true;
    const f = form.elements;
    try {
      const j = await api('/api/video/jobs', { method: 'POST', body: JSON.stringify({ workflow: f.workflow.value, params: { prompt: f.prompt.value, seconds: Number(f.seconds.value) } }) });
      note(`Job ${j.id} queued.`);
    } catch (e) { note(errText(e), 'error'); }
    V.busy = false;
    load(true);
  }

  async function post(path, done) {
    try { await api(path, { method: 'POST', body: '{}' }); note(done); } catch (e) { note(errText(e), 'error'); }
    load(true);
  }

  document.addEventListener('click', (ev) => {
    const t = ev.target.closest('[data-vs]');
    if (!t) return;
    const a = t.dataset.vs;
    const id = encodeURIComponent(t.dataset.id || '');
    if (a === 'open') open();
    else if (a === 'close') close();
    else if (a === 'cancel') post(`/api/video/jobs/${id}/cancel`, 'Cancelled.');
    else if (a === 'verify') { note('Verifying (hashing the files can take a minute)…'); post(`/api/video/models/${id}/verify`, 'Verified.'); }
    else if (a === 'download') post(`/api/video/models/${id}/download`, 'Download started.');
  });
  document.addEventListener('change', (ev) => {
    const t = ev.target;
    if (!t.dataset || !t.dataset.key) return;
    if (t.dataset.vs === 'set-bool') setSettings({ [t.dataset.key]: t.checked });
    else if (t.dataset.vs === 'set-int') setSettings({ [t.dataset.key]: Math.round(Number(t.value)) });
    else if (t.dataset.vs === 'set-text') setSettings({ [t.dataset.key]: t.value });
  });
  document.addEventListener('submit', (ev) => {
    const form = ev.target.closest('form[data-vs]');
    if (!form) return;
    ev.preventDefault();
    submit(form);
  });
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && document.getElementById('vs-dialog')) close(); });

  const css = document.createElement('style');
  css.textContent = `.vs-dialog{position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.45);display:flex;align-items:flex-start;justify-content:center;padding:24px 16px;overflow:auto}
.vs-panel{background:var(--surface,#1e1e1e);color:var(--text,inherit);border-radius:10px;max-width:960px;width:100%;padding:12px 16px;box-shadow:0 10px 40px rgba(0,0,0,.4)}
.vs-head{display:flex;align-items:center;gap:12px}.vs-head h3{margin:0;flex:1}
.vs-form{display:grid;gap:6px;margin:6px 0 12px}.vs-form label{display:grid;gap:2px;font-size:13px}
.vs-table{width:100%;border-collapse:collapse;font-size:13px}.vs-table td,.vs-table th{padding:4px 6px;text-align:left;border-bottom:1px solid var(--border,#333)}
.vs-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}.vs-asset{margin:0;font-size:12px}
.vs-video{width:100%;max-height:240px;background:#000;border-radius:6px}`;
  document.head.appendChild(css);

  window.ABPVideo = { card, open, load };
})();
