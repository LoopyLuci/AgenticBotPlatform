// Likeness (Video Studio V3): face swap, recast and characters of real people, under consent (bot/likeness).
// The Workspace's right column shows a card (ABPLikeness.card()); "Open Likeness" opens this file's own dialog with the
// consent records, a new job, characters and the job log. Shared, identical, by the dashboard and the desktop app.
(function () {
  'use strict';
  const E = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const L = { data: null, at: 0, busy: false, poll: null };
  const base = () => (typeof API_BASE !== 'undefined' ? API_BASE : '');
  const note = (msg, kind) => { if (typeof window.toast === 'function') window.toast(msg, kind); else { const n = document.getElementById('lk-msg'); if (n) n.textContent = msg; } };
  const errText = (e) => { const t = String((e && e.message) || e); try { return JSON.parse(t).detail || t; } catch (_) { return t; } };
  const outUrl = (name) => `${base()}/api/likeness/out/${encodeURIComponent(name)}`;

  async function load(force) {
    if (!force && L.data && Date.now() - L.at < 15000) return L.data;
    try { L.data = await api('/api/likeness'); L.at = Date.now(); } catch (_) { L.data = null; }
    paintCards();
    if (document.getElementById('lk-dialog')) paintDialog();
    return L.data;
  }

  async function upload(file) {                // multipart, so not through api() (which sends JSON)
    const fd = new FormData();
    fd.append('file', file);
    const headers = {};
    const t = typeof getToken === 'function' ? getToken() : '';
    if (t) headers['X-Dashboard-Token'] = t;
    const r = await fetch(`${base()}/api/likeness/uploads`, { method: 'POST', body: fd, headers });
    if (!r.ok) throw new Error(await r.text());
    return (await r.json()).id;
  }
  async function uploads(input) { const ids = []; for (const f of (input && input.files) || []) ids.push(await upload(f)); return ids; }

  // ---- the Workspace card ------------------------------------------------------------------------------------------
  function cardBody() {
    const d = L.data;
    if (!d) return '<p class="lc-note">Face swap, recast and characters, under consent.</p><button type="button" class="btn" data-lk="open">Open Likeness</button>';
    const s = d.settings || {};
    const active = (d.consents || []).filter((c) => !c.revoked).length;
    const running = (d.jobs || []).filter((j) => j.status === 'running' || j.status === 'queued').length;
    return `<div class="lc-kv"><div><label title="Face swap, recast and characters of real people. Every use needs a consent record; outputs are labelled AI-generated."><input type="checkbox" data-lk="enabled"${s.enabled ? ' checked' : ''}> Likeness on</label></div>
<div><span>Consent records</span><b>${E(active)}</b></div><div><span>Jobs running</span><b>${E(running)}</b></div></div>
<button type="button" class="btn" data-lk="open">Open Likeness</button>`;
  }
  function paintCards() { document.querySelectorAll('.lk-card-body').forEach((el) => { el.innerHTML = cardBody(); }); }
  function card() {
    // no fetch from here: the card is drawn even while the likeness unit is not loaded (its routes then 404)
    return `<section class="lc-card"><h3>Likeness</h3><div class="lk-card-body">${cardBody()}</div></section>`;
  }

  async function setSettings(change) {
    try {
      const s = await api('/api/likeness/settings', { method: 'PUT', body: JSON.stringify(change) });
      note('enabled' in change ? `Likeness ${s.enabled ? 'on' : 'off'}.` : `Videos capped at ${s.max_seconds} s.`);
    } catch (e) { note(errText(e), 'error'); }
    load(true);
  }

  // ---- the dialog --------------------------------------------------------------------------------------------------
  const STATEMENT = 'I confirm this person (or I, if it is my own likeness) gave informed consent to this use of their face and likeness, and that they are 18 or older.';
  function consentRows(d) {
    return (d.consents || []).map((c) => `<tr><td>${E(c.name)}</td><td>${E(c.kind)}</td><td>${E(c.attested_by)}</td><td>${E(new Date(c.created * 1000).toLocaleDateString())}</td>
<td>${c.revoked ? '<span class="lc-warn">revoked</span>' : `<button type="button" class="btn danger" data-lk="revoke" data-id="${E(c.id)}">Revoke</button>`}</td></tr>`).join('') || '<tr><td colspan="5" class="lc-note">No consent records yet.</td></tr>';
  }
  function consentOpts(d) { return (d.consents || []).filter((c) => !c.revoked).map((c) => `<option value="${E(c.id)}">${E(c.name)} (${E(c.kind)})</option>`).join(''); }
  function jobRows(d) {
    return (d.jobs || []).map((j) => {
      const out = j.output ? (j.output.endsWith('.webm') ? `<video controls src="${E(outUrl(j.output))}" style="max-width:100%;max-height:240px"></video>` : `<img alt="output of ${E(j.id)}" src="${E(outUrl(j.output))}" style="max-width:100%;max-height:240px">`)
        + `<br><a href="${E(outUrl(j.output + '.provenance.json'))}" target="_blank" rel="noopener">provenance</a>` : '';
      return `<div class="lc-card"><b>${E(j.id)}</b> ${E(j.mode)} · ${E(j.person)} · <b>${E(j.status)}</b>${j.status === 'running' ? ` ${Math.round((j.progress || 0) * 100)}%` : ''}
${j.error ? `<p class="lc-warn">${E(j.error)}</p>` : ''}<div>${out}</div></div>`;
    }).join('') || '<p class="lc-note">No jobs in this run of ABP.</p>';
  }
  function charRows(d) {
    return (d.characters || []).map((c) => `<tr><td>${E(c.name)}</td><td><code>${E(c.trigger)}</code></td><td>${E(c.images.length)}</td>
<td>${c.dataset ? `${E(c.dataset.images)} images` : '—'}</td><td><button type="button" class="btn" data-lk="dataset" data-id="${E(c.id)}">Build LoRA dataset</button></td></tr>`).join('') || '<tr><td colspan="5" class="lc-note">No characters yet.</td></tr>';
  }
  function logRows(d) {
    return (d.log || []).slice(0, 15).map((e) => `<li>${E(new Date(e.at * 1000).toLocaleString())} — ${E(e.event)} ${E(e.job_id || e.id || e.consent_id || e.character_id || '')}${e.reason ? ` — <span class="lc-warn">${E(e.reason)}</span>` : ''}</li>`).join('');
  }

  function paintDialog() {
    const el = document.getElementById('lk-dialog');
    const d = L.data;
    if (!el || !d) return;
    const s = d.settings || {};
    const missing = (d.models || []).filter((m) => !m.present).map((m) => m.name);
    el.querySelector('.lk-body').innerHTML = `
<p class="lc-note">Every real person needs a consent record. Sexual content involving real people and anything involving minors is refused. Every output carries a visible <b>AI-GENERATED</b> label, embedded provenance and a sidecar manifest; every job and refusal is logged.</p>
<div class="lc-kv"><div><label><input type="checkbox" data-lk="enabled"${s.enabled ? ' checked' : ''}> Likeness on</label></div>
<div><label>Video cap <select class="lc-sel" data-lk="max_seconds">${[15, 30, 60, 120, 300].map((v) => `<option value="${v}"${Number(s.max_seconds) === v ? ' selected' : ''}>${v} s</option>`).join('')}</select></label></div></div>
${missing.length ? `<p class="lc-note">The first job downloads ${E(missing.join(', '))} (InsightFace, non-commercial research license; pinned SHA-256).</p>` : ''}
<h4>Consent records</h4><table class="lk-table"><thead><tr><th>Person</th><th>Kind</th><th>Attested by</th><th>Date</th><th></th></tr></thead><tbody>${consentRows(d)}</tbody></table>
<details><summary>Add a consent record</summary><form data-lk="consent-form" class="lk-form">
<label>Person's name <input name="name" required maxlength="120"></label>
<label>Kind <select name="kind"><option value="self">my own likeness</option><option value="consented">another adult who consented</option></select></label>
<label>Attested by <input name="attested_by" required maxlength="120"></label>
<label>Scope (what it may be used for) <input name="scope" maxlength="500"></label>
<label>Attestation <textarea name="statement" rows="3" required>${E(STATEMENT)}</textarea></label>
<label><input type="checkbox" name="adult" required> The person is 18 or older</label>
<label>Reference photos (1-8, one clear face each) <input type="file" name="images" accept="image/*" multiple required></label>
<button type="submit" class="btn primary">Record consent</button></form></details>
<h4>New job</h4><form data-lk="job-form" class="lk-form">
<label>Mode <select name="mode"><option value="faceswap">Face swap (image or video)</option><option value="recast">Recast (replace a person in a video)</option></select></label>
<label>Person (consent) <select name="consent_id" required>${consentOpts(d)}</select></label>
<label>Source face photo (that person) <input type="file" name="source" accept="image/*" required></label>
<label>Target image or video <input type="file" name="target" accept="image/*,video/*" required></label>
<label>Which face to replace <select name="target_face"><option value="largest">the largest face</option><option value="best">the person in the reference photo</option><option value="all">every face</option></select></label>
<label>Reference photo of the person to replace (for "the person in the reference photo") <input type="file" name="target_reference" accept="image/*"></label>
<label>Purpose <input name="purpose" required maxlength="500" placeholder="e.g. birthday video for my family"></label>
<button type="submit" class="btn primary"${s.enabled ? '' : ' disabled title="Switch Likeness on first"'}>Start</button></form>
<h4>Jobs</h4>${jobRows(d)}
<h4>Characters (reference sets and LoRA datasets)</h4><table class="lk-table"><thead><tr><th>Name</th><th>Trigger</th><th>Photos</th><th>Dataset</th><th></th></tr></thead><tbody>${charRows(d)}</tbody></table>
<details><summary>Add a character</summary><form data-lk="char-form" class="lk-form">
<label>Name <input name="name" required maxlength="120"></label>
<label>Person (consent) <select name="consent_id" required>${consentOpts(d)}</select></label>
<label>Reference photos (1-40, each must match the consent) <input type="file" name="images" accept="image/*" multiple required></label>
<button type="submit" class="btn primary">Add character</button></form></details>
<h4>Log</h4><ul class="lk-log">${logRows(d)}</ul>`;
  }

  function open() {
    let el = document.getElementById('lk-dialog');
    if (!el) {
      el = document.createElement('div');
      el.id = 'lk-dialog';
      el.className = 'lk-dialog';
      el.setAttribute('role', 'dialog');
      el.setAttribute('aria-modal', 'true');
      el.setAttribute('aria-label', 'Likeness');
      el.innerHTML = `<div class="lk-panel"><div class="lk-head"><h3>Likeness</h3><span id="lk-msg" class="lc-note" aria-live="polite"></span><button type="button" class="btn" data-lk="close" aria-label="Close">✕</button></div><div class="lk-body"></div></div>`;
      document.body.appendChild(el);
    }
    paintDialog();
    load(true);
    clearInterval(L.poll);
    L.poll = setInterval(() => { if (!document.getElementById('lk-dialog')) clearInterval(L.poll); else if ((L.data && (L.data.jobs || []).some((j) => j.status === 'running' || j.status === 'queued'))) load(true); }, 3000);
  }
  function close() { const el = document.getElementById('lk-dialog'); if (el) el.remove(); clearInterval(L.poll); }

  async function submit(form) {
    if (L.busy) return;
    L.busy = true;
    const f = form.elements;
    try {
      const kind = form.dataset.lk;
      if (kind === 'consent-form') {
        note('Uploading the reference photos…');
        const images = await uploads(f.images);
        await api('/api/likeness/consents', { method: 'POST', body: JSON.stringify({ name: f.name.value, kind: f.kind.value, attested_by: f.attested_by.value,
          scope: f.scope.value, statement: f.statement.value, adult: f.adult.checked, images }) });
        note('Consent recorded.');
      } else if (kind === 'job-form') {
        note('Uploading…');
        const [source] = await uploads(f.source);
        const [target] = await uploads(f.target);
        const [ref] = f.target_reference.files.length ? await uploads(f.target_reference) : [];
        const j = await api('/api/likeness/jobs', { method: 'POST', body: JSON.stringify({ mode: f.mode.value, consent_id: f.consent_id.value, source, target,
          target_face: f.target_face.value, target_reference: ref, purpose: f.purpose.value }) });
        note(`Job ${j.id} started.`);
      } else if (kind === 'char-form') {
        const images = await uploads(f.images);
        await api('/api/likeness/characters', { method: 'POST', body: JSON.stringify({ name: f.name.value, consent_id: f.consent_id.value, images }) });
        note('Character added.');
      }
    } catch (e) { note(errText(e), 'error'); }
    L.busy = false;
    load(true);
  }

  document.addEventListener('click', async (ev) => {
    const t = ev.target.closest('[data-lk]');
    if (!t) return;
    const a = t.dataset.lk;
    if (a === 'open') open();
    else if (a === 'close') close();
    else if (a === 'revoke') {
      if (!confirm('Revoke this consent? Past outputs stay logged; no new job can use it.')) return;
      try { await api(`/api/likeness/consents/${encodeURIComponent(t.dataset.id)}/revoke`, { method: 'POST', body: JSON.stringify({ reason: 'revoked from the Workspace' }) }); note('Consent revoked.'); } catch (e) { note(errText(e), 'error'); }
      load(true);
    } else if (a === 'dataset') {
      try { const r = await api(`/api/likeness/characters/${encodeURIComponent(t.dataset.id)}/dataset`, { method: 'POST', body: '{}' }); note(`Dataset: ${r.images} images.`); } catch (e) { note(errText(e), 'error'); }
      load(true);
    }
  });
  document.addEventListener('change', (ev) => {
    const t = ev.target;
    if (!t.dataset) return;
    if (t.dataset.lk === 'enabled') setSettings({ enabled: t.checked });
    else if (t.dataset.lk === 'max_seconds') setSettings({ max_seconds: Number(t.value) });
  });
  document.addEventListener('submit', (ev) => {
    const form = ev.target.closest('form[data-lk]');
    if (!form) return;
    ev.preventDefault();
    submit(form);
  });
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && document.getElementById('lk-dialog')) close(); });

  const css = document.createElement('style');
  css.textContent = `.lk-dialog{position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.45);display:flex;align-items:flex-start;justify-content:center;padding:24px 16px;overflow:auto}
.lk-panel{background:var(--surface,#1e1e1e);color:var(--text,inherit);border-radius:10px;max-width:860px;width:100%;padding:12px 16px;box-shadow:0 10px 40px rgba(0,0,0,.4)}
.lk-head{display:flex;align-items:center;gap:12px}.lk-head h3{margin:0;flex:1}
.lk-form{display:grid;gap:6px;margin:6px 0 12px}.lk-form label{display:grid;gap:2px;font-size:13px}
.lk-table{width:100%;border-collapse:collapse;font-size:13px}.lk-table td,.lk-table th{padding:4px 6px;text-align:left;border-bottom:1px solid var(--border,#333)}
.lk-log{font-size:12px;max-height:180px;overflow:auto}`;
  document.head.appendChild(css);

  window.ABPLikeness = { card, open, load };
})();
