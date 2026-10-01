// Storage panel: the ABP File Server (bot/fileserver). Tabs:
//   Overview   the file server (start/stop, its web and WebDAV addresses), the machine (CPU, memory), alerts, events
//   Array      data disks and parity: set up, status (protected, pending, warnings, errors), sync / scrub / rebuild
//   Shares     user shares and folder shares: cache, allocation, split level, access, users; SMB/NFS commands; pools
//   Users      file-server users; share links
//   Disks      every drive: health, SMART, temperature, failure risk with reasons; volumes
//   Search     the content index: search by words / meaning, duplicates, index now
//   Jobs       remotes, transfer jobs (copy / mirror / two-way), backups (encrypted, deduplicated) with restore
//   Apps       one-click containers on the shares (Jellyfin, Nextcloud, Immich, Syncthing...)
// Identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/); tests/test_fileserver_page.py
// fails if the two differ. It uses the page's own api(), esc() and showToast(), and draws into #stp-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { const d = JSON.parse(m).detail; m = d || m; } catch (_) {} return typeof m === 'string' ? m : JSON.stringify(m); };
  const send = (path, method, body) => api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
  const A = '/api/fileserver';
  const human = (n) => { n = +n || 0; const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']; let i = 0; while (n >= 1024 && i < 5) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + ' ' + u[i]; };
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : 'never');
  const st = { tab: 'overview', busy: false, o: null, form: null };
  try { st.tab = localStorage.getItem('stp.tab') || 'overview'; } catch (_) {}
  const keep = (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} };

  function css() {
    if ($('stp-css')) return;
    const s = document.createElement('style');
    s.id = 'stp-css';
    s.textContent = `.stp-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}.stp-tabs button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.stp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
.stp-card{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:var(--surface);min-width:0;display:grid;gap:8px;align-content:start}
.stp-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.stp-muted{color:var(--muted);font-size:12px}
.stp-table{width:100%;border-collapse:collapse;font-size:12.5px}.stp-table td,.stp-table th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.stp-mono{font-family:var(--font-mono,monospace);font-size:12px;white-space:pre-wrap;word-break:break-word}
.stp-log{font-family:var(--font-mono,monospace);font-size:12px;white-space:pre-wrap;background:var(--surface-2);border-radius:8px;padding:8px;max-height:260px;overflow:auto}
.stp-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.stp-chip.ok{background:var(--good-soft);color:var(--good,#1f9d55)}.stp-chip.warn{background:#fff4d6;color:#8a6100}.stp-chip.bad{background:var(--bad-soft,#fde8e8);color:var(--bad,#c53030)}
.stp-form{display:grid;grid-template-columns:minmax(120px,170px) 1fr;gap:6px 10px;align-items:center}@media (max-width:640px){.stp-form{grid-template-columns:1fr}}
#stp-root input,#stp-root select,#stp-root textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%;min-width:0}
.stp-meter{height:7px;background:var(--surface-2);border-radius:4px;overflow:hidden}.stp-meter i{display:block;height:100%;background:var(--accent)}`;
    document.head.appendChild(s);
  }
  const chip = (cls, text) => `<span class="stp-chip ${cls}">${E(text)}</span>`;
  const meter = (used, total) => `<div class="stp-meter"><i style="width:${total ? Math.min(100, used / total * 100).toFixed(1) : 0}%"></i></div>`;

  async function follow(run, logId) {
    const el = $(logId);
    if (el) { el.style.display = ''; el.textContent = ''; }
    let seen = 0;
    for (;;) {
      const r = await api(`${A}/runs/${run.run}?since=${seen}`);
      seen = r.log_total;
      if (el && r.log.length) { el.textContent += (el.textContent ? '\n' : '') + r.log.join('\n'); el.scrollTop = el.scrollHeight; }
      if (r.done) {
        if (el) el.textContent += '\n' + (r.error ? `failed: ${r.error}` : `done: ${JSON.stringify(r.result).slice(0, 600)}`);
        if (r.error) toast(r.error, 'error'); else toast(`${run.title}: done`);
        return r;
      }
      await new Promise((ok) => setTimeout(ok, 1200));
    }
  }
  const runInto = async (p, logId) => { try { await follow(await p, logId); } catch (e) { toast(errText(e), 'error'); } st.o = null; };

  // ---- tabs ----
  async function overviewTab() {
    const o = st.o, s = o.server;
    const stats = await api(`${A}/stats`).catch(() => null);
    return `<div class="stp-grid"><div class="stp-card"><b>File server</b>
<div>${s.running ? chip('ok', 'running') : chip('', 'stopped')} port ${E(s.port)}</div>
${s.running ? `<div>Web: <a href="${E(s.urls.web)}" target="_blank" rel="noopener">${E(s.urls.web)}</a><br>WebDAV: <span class="stp-mono">${E(s.urls.webdav)}&lt;share&gt;</span></div>` : ''}
<div class="stp-row"><button class="btn" id="stp-srv">${s.running ? 'Stop' : 'Start'}</button><label class="stp-row"><input type="checkbox" id="stp-auto" ${o.settings.autostart ? 'checked' : ''}> start with ABP</label></div>
<div class="stp-muted">To reach it from the internet with HTTPS, add a site on the Hosting page that proxies to http://127.0.0.1:${E(s.port)}.</div></div>
${stats ? `<div class="stp-card"><b>This machine</b><div>CPU ${E(stats.cpu_percent)}% of ${E(stats.cpus)}</div>${meter(stats.cpu_percent, 100)}
<div>Memory ${human(stats.memory.used)} of ${human(stats.memory.total)}</div>${meter(stats.memory.used, stats.memory.total)}
<div class="stp-muted">Up ${Math.round(stats.uptime_s / 3600)} h · network ${human(stats.net.recv)} in, ${human(stats.net.sent)} out since boot</div></div>` : ''}
<div class="stp-card"><b>Alerts</b>${o.alerts.length ? o.alerts.map((a) => `<div>${chip('bad', 'ransomware?')} <b>${E(a.share)}</b> score ${E(a.score)} ${a.frozen ? '(frozen)' : ''}<div class="stp-muted">${E(a.detail.entropy_jumps)} files became random, ${E(a.detail.renamed_to_new_extensions)} renamed, notes: ${E((a.detail.ransom_notes || []).join(', ') || 'none')}</div></div>`).join('') : '<div class="stp-muted">none</div>'}
${o.frozen.length ? `<div>Frozen (read-only): ${E(o.frozen.join(', '))} <button class="btn ghost" id="stp-unfreeze">Unfreeze</button></div>` : ''}</div>
<div class="stp-card"><b>Recent events</b><table class="stp-table">${o.events.map((e) => `<tr><td class="stp-muted">${when(e.at)}</td><td>${chip(e.level === 'info' ? '' : e.level === 'alert' ? 'bad' : 'warn', e.kind)}</td><td>${E(e.text)}</td></tr>`).join('') || '<tr><td class="stp-muted">nothing yet</td></tr>'}</table></div></div>`;
  }

  function arrayTab() {
    const a = st.o.array;
    const cfg = st.form && st.form.array;
    return `<div class="stp-grid"><div class="stp-card"><b>Array</b>${a.configured ? `
<div>${a.dual_parity ? 'Dual' : 'Single'} parity · ${E(a.block_kib)} KiB blocks · last sync ${when(a.last_sync)} · last scrub ${when(a.last_scrub)}</div>
${a.warnings.map((w) => `<div>${chip('warn', 'note')} ${E(w)}</div>`).join('')}
<table class="stp-table"><tr><th>Disk</th><th>State</th><th>Files</th><th>Protected</th><th>Space</th></tr>
${a.disks.map((d) => `<tr><td><b>${E(d.name)}</b><div class="stp-muted stp-mono">${E(d.path)}</div></td><td>${d.present ? chip('ok', 'ok') : chip('bad', 'missing')}</td><td>${E(d.files)}${d.unsynced ? ` <span class="stp-muted">(${E(d.unsynced)} unsynced)</span>` : ''}</td><td>${human(d.protected_bytes)}</td><td>${d.size ? `${human(d.size - d.free)} / ${human(d.size)}${meter(d.size - d.free, d.size)}` : ''}</td></tr>`).join('')}
${a.parity.map((p) => `<tr><td><b>${E(p.name)}</b> (${E(p.kind)})<div class="stp-muted stp-mono">${E(p.path)}</div></td><td>${p.present ? chip('ok', 'ok') : chip('warn', 'not built')}</td><td></td><td>${human(p.size)}</td><td>${p.free !== undefined ? human(p.free) + ' free' : ''}</td></tr>`).join('')}</table>
<div class="stp-row"><button class="btn" data-arr="sync">Sync parity</button><button class="btn ghost" data-arr="scrub">Scrub 10%</button><button class="btn ghost" data-arr="scrub100">Scrub all</button><button class="btn ghost" data-arr="fix">Repair damaged / missing files</button><button class="btn ghost" id="stp-a-changes">Count unsynced</button></div>
${a.errors.length ? `<div>${chip('bad', `${a.errors.length} corrupted block(s)`)} ${E(a.errors.slice(0, 5).map((x) => `${x.disk}/${x.path}`).join(', '))}</div>` : ''}
<div class="stp-row"><select id="stp-fix-disk"><option value="">Rebuild a disk onto a new drive…</option>${a.disks.map((d) => `<option>${E(d.name)}</option>`).join('')}</select><input id="stp-fix-target" placeholder="the new drive's folder"><button class="btn ghost" id="stp-fix-go">Rebuild</button></div>
<div class="stp-log" id="stp-a-log" style="display:none"></div>` : '<p class="cardnote">Not set up. Each data disk is a folder on its own drive (any size); parity goes on a drive at least as large as the largest data disk.</p>'}</div>
<div class="stp-card"><b>${a.configured ? 'Change the array' : 'Set up the array'}</b><div class="stp-form">
<label>Data disks</label><textarea id="stp-a-disks" rows="4" placeholder="d1=E:\\nas\\disk1&#10;d2=F:\\nas\\disk2">${E(cfg ? cfg.disks : (a.disks || []).map((d) => `${d.name}=${d.path}`).join('\n'))}</textarea>
<label>Parity</label><input id="stp-a-p1" value="${E((a.parity[0] || {}).path || '')}" placeholder="G:\\nas\\parity">
<label>Second parity</label><input id="stp-a-p2" value="${E((a.parity[1] || {}).path || '')}" placeholder="optional: survives two failed disks">
<label>Block size (KiB)</label><select id="stp-a-bs">${[64, 128, 256, 512, 1024].map((b) => `<option ${b === (a.block_kib || 256) ? 'selected' : ''}>${b}</option>`).join('')}</select>
<label>Same drive</label><span><input type="checkbox" id="stp-a-same"> allow parity on a data disk's drive (testing only: no protection)</span></div>
<div class="stp-row"><button class="btn" id="stp-a-save">Save</button></div></div></div>`;
  }

  function shareForm(s) {
    s = s || { cache: 'no', allocation: 'highwater', split_level: 0, access: 'private', users: {}, recycle_bin: true, min_free_gb: 2 };
    const pools = Object.keys(st.o.pools);
    return `<div class="stp-card"><b>${s.name ? `Edit ${E(s.name)}` : 'New share'}</b><div class="stp-form">
<label>Name</label><input id="stp-s-name" value="${E(s.name || '')}" ${s.name ? 'disabled' : ''}>
<label>Comment</label><input id="stp-s-comment" value="${E(s.comment || '')}">
<label>Kind</label><select id="stp-s-kind"><option value="user" ${s.path ? '' : 'selected'}>User share (across the array)</option><option value="folder" ${s.path ? 'selected' : ''}>A folder</option></select>
<label class="k-folder">Folder</label><input class="k-folder" id="stp-s-path" value="${E(s.path || '')}">
<label class="k-user">Cache</label><select class="k-user" id="stp-s-cache">${['no', 'yes', 'only', 'prefer'].map((c) => `<option ${s.cache === c ? 'selected' : ''}>${c}</option>`).join('')}</select>
<label class="k-user">Cache pool</label><select class="k-user" id="stp-s-pool">${pools.map((p) => `<option ${s.cache_pool === p ? 'selected' : ''}>${E(p)}</option>`).join('') || '<option value="">(add a pool below)</option>'}</select>
<label class="k-user">Allocation</label><select class="k-user" id="stp-s-alloc">${[['highwater', 'High-water'], ['mostfree', 'Most free'], ['fillup', 'Fill-up']].map(([k, l]) => `<option value="${k}" ${s.allocation === k ? 'selected' : ''}>${l}</option>`).join('')}</select>
<label class="k-user">Split level</label><input class="k-user" id="stp-s-split" type="number" min="0" value="${E(s.split_level || 0)}">
<label class="k-user">Minimum free (GB)</label><input class="k-user" id="stp-s-minfree" value="${E(s.min_free_gb)}">
<label>Access</label><select id="stp-s-access">${[['public', 'Public: everyone reads and writes'], ['secure', 'Secure: everyone reads, listed users write'], ['private', 'Private: listed users only']].map(([k, l]) => `<option value="${k}" ${s.access === k ? 'selected' : ''}>${l}</option>`).join('')}</select>
<label>Users</label><input id="stp-s-users" value="${E(Object.entries(s.users || {}).map(([u, m]) => `${u}:${m}`).join(', '))}" placeholder="ann:rw, bob:r">
<label>Recycle bin</label><span><input type="checkbox" id="stp-s-bin" ${s.recycle_bin ? 'checked' : ''}> deleted files kept ${E(s.recycle_days || 30)} days</span></div>
<div class="stp-row"><button class="btn" id="stp-s-save" data-name="${E(s.name || '')}">${s.name ? 'Save' : 'Create'}</button><button class="btn ghost" id="stp-s-cancel">Cancel</button></div></div>`;
  }

  async function sharesTab() {
    const sh = await api(`${A}/shares`);
    const edit = st.form && st.form.share;
    return `<div class="stp-row" style="margin-bottom:8px"><button class="btn" id="stp-s-new">New share</button></div>
${edit ? shareForm(edit === 'new' ? null : sh.find((s) => s.name === edit)) : ''}
<div class="stp-grid">${sh.map((s) => `<div class="stp-card"><div class="stp-row"><b>${E(s.name)}</b>${chip('', s.access)}${s.path ? chip('', 'folder') : chip('', `cache ${s.cache}`)}${st.o.frozen.includes(s.name) ? chip('bad', 'frozen') : ''}</div>
<div class="stp-muted">${E(s.comment || '')} ${s.path ? E(s.path) : `${E(s.allocation)} · split ${E(s.split_level)}`}</div>
<div class="stp-muted">${E(Object.entries(s.users || {}).map(([u, m]) => `${u} (${m})`).join(', ') || 'no users listed')}</div>
<div class="stp-row"><button class="btn ghost" data-sedit="${E(s.name)}">Edit</button><button class="btn ghost" data-smb="${E(s.name)}">SMB</button><button class="btn ghost" data-nfs="${E(s.name)}">NFS</button><button class="btn ghost" data-srm="${E(s.name)}">Remove</button></div>
<div class="stp-mono" id="stp-exp-${E(s.name)}"></div></div>`).join('') || '<p class="cardnote">No shares yet.</p>'}</div>
<div class="stp-card" style="margin-top:12px"><b>Cache pools</b><table class="stp-table">${Object.entries(st.o.pools).map(([n, p]) => `<tr><td>${E(n)}</td><td class="stp-mono">${E(p.path)}</td><td><button class="btn ghost" data-poolrm="${E(n)}">Remove</button></td></tr>`).join('')}</table>
<div class="stp-row"><input id="stp-p-name" placeholder="cache" size="10"><input id="stp-p-path" placeholder="a folder on a fast drive (SSD/NVMe)" style="flex:1"><button class="btn ghost" id="stp-p-add">Add pool</button></div></div>`;
  }

  async function usersTab() {
    const [users, links] = await Promise.all([api(`${A}/users`), api(`${A}/links`)]);
    return `<div class="stp-grid"><div class="stp-card"><b>Users</b><table class="stp-table">${users.map((u) => `<tr><td>${E(u.name)} ${u.admin ? chip('', 'admin') : ''}</td><td><button class="btn ghost" data-urm="${E(u.name)}">Remove</button></td></tr>`).join('')}</table>
<div class="stp-row"><input id="stp-u-name" placeholder="name" size="12"><input id="stp-u-pw" type="password" autocomplete="new-password" placeholder="password (8+)" size="14"><label><input type="checkbox" id="stp-u-admin"> admin</label><button class="btn" id="stp-u-add">Add / reset</button></div>
<div class="stp-muted">File-server users sign in to the web file manager and WebDAV; a share's access decides what they see.</div></div>
<div class="stp-card"><b>Share links</b><table class="stp-table">${links.map((l) => `<tr><td class="stp-mono">${E(l.share)}/${E(l.path)}</td><td>${l.has_password ? chip('', 'password') : ''} ${l.allow_upload ? chip('', 'uploads') : ''}<div class="stp-muted">${l.downloads} download(s) · ${l.expires ? 'expires ' + when(l.expires) : 'no expiry'}</div></td><td><button class="btn ghost" data-lrm="${E(l.token)}">Revoke</button></td></tr>`).join('') || '<tr><td class="stp-muted">none (create them in the file manager)</td></tr>'}</table></div></div>`;
  }

  async function disksTab() {
    const inv = await api(`${A}/disks`);
    const band = (b) => chip(b === 'ok' ? 'ok' : b === 'watch' ? 'warn' : b === 'unknown' ? '' : 'bad', b);
    return `<div class="stp-grid">${inv.drives.map((d) => `<div class="stp-card"><div class="stp-row"><b>${E(d.model || d.device)}</b>${band(d.risk.band)}</div>
<div class="stp-muted">${E(d.device)} · ${E(d.media || (d.rotational ? 'HDD' : 'SSD'))} · ${E(d.bus || '')} · ${human(d.size)}${d.volumes && d.volumes.length ? ' · ' + E(d.volumes.join(' ')) : ''}</div>
<div>${d.os_health ? `OS: ${E(d.os_health)} · ` : ''}${d.smart_passed !== undefined && d.smart_passed !== null ? `SMART ${d.smart_passed ? 'passed' : 'FAILED'} · ` : ''}${d.temperature_c ? `${E(d.temperature_c)} °C · ` : ''}${d.power_on_hours ? `${E(d.power_on_hours)} h` : ''}</div>
<div class="stp-muted">failure risk ≈ ${(d.risk.probability * 100).toFixed(1)}%: ${E(d.risk.reasons.join('; '))}</div></div>`).join('')}</div>
${inv.smartctl ? '' : '<p class="stp-muted">Install smartmontools (smartctl) for full SMART data, or run ABP elevated for Windows\' reliability counters.</p>'}
<div class="stp-card" style="margin-top:12px"><b>Volumes</b><table class="stp-table">${inv.volumes.map((v) => `<tr><td class="stp-mono">${E(v.mount)}</td><td>${E(v.fs)}</td><td style="min-width:160px">${human(v.used)} / ${human(v.size)}${meter(v.used, v.size)}</td></tr>`).join('')}</table></div>`;
  }

  async function searchTab() {
    const stats = await api(`${A}/index`).catch(() => ({ shares: {} }));
    return `<div class="stp-card"><div class="stp-row"><input id="stp-q" placeholder="words, or describe what you are looking for" style="flex:1;min-width:220px" value="${E(st.q || '')}"><select id="stp-qm"><option value="auto">Best</option><option value="words">Words</option><option value="meaning">Meaning</option></select><button class="btn" id="stp-q-go">Search</button><button class="btn ghost" id="stp-dups">Find duplicates</button><button class="btn ghost" id="stp-idx">Index now</button></div>
<div class="stp-muted">Indexed: ${E(Object.entries(stats.shares).map(([s, v]) => `${s} ${v.files} files`).join(', ') || 'nothing yet')} · meaning search uses ${E(stats.embedder || '?')} (on this machine)</div>
<div id="stp-results"></div><div class="stp-log" id="stp-i-log" style="display:none"></div></div>`;
  }

  async function jobsTab() {
    const [remotes, tr, bk] = await Promise.all([api(`${A}/remotes`), api(`${A}/transfers`), api(`${A}/backups`)]);
    return `<div class="stp-grid"><div class="stp-card"><b>Remotes</b><table class="stp-table">${remotes.map((r) => `<tr><td><b>${E(r.name)}</b></td><td>${E(r.kind)}</td><td class="stp-mono">${E(r.path || r.share || r.url || r.endpoint || '')}</td><td><button class="btn ghost" data-rrm="${E(r.name)}">Remove</button></td></tr>`).join('') || '<tr><td class="stp-muted">none</td></tr>'}</table>
<div class="stp-row"><input id="stp-r-name" placeholder="name" size="10"><select id="stp-r-kind">${['local', 'share', 'abp', 'webdav', 's3'].map((k) => `<option>${k}</option>`).join('')}</select><input id="stp-r-fields" placeholder='url=https://nas2:8790 user=ann (or path=… / share=… / endpoint= bucket= region= access_key=)' style="flex:1;min-width:200px"><input id="stp-r-secret" type="password" placeholder="password / secret key" size="14"><button class="btn ghost" id="stp-r-add">Save</button></div></div>
<div class="stp-card"><b>Transfers</b><table class="stp-table">${Object.entries(tr.jobs).map(([n, j]) => `<tr><td><b>${E(n)}</b> ${chip('', j.mode)}<div class="stp-muted">${E(j.source.remote ? j.source.remote + ':' + (j.source.path || '') : j.source.path)} → ${E(j.dest.remote ? j.dest.remote + ':' + (j.dest.path || '') : j.dest.path)}${j.every_minutes ? ` · every ${E(j.every_minutes)} min` : ''}</div><div class="stp-muted">${j.last_run ? `last: ${when(j.last_run.at)}, ${E(j.last_run.done)} done, ${E(j.last_run.failed)} failed` : ''}</div></td><td><button class="btn ghost" data-trun="${E(n)}">Run</button> <button class="btn ghost" data-tdry="${E(n)}">Preview</button> <button class="btn ghost" data-trm="${E(n)}">Remove</button></td></tr>`).join('') || '<tr><td class="stp-muted">none</td></tr>'}</table>
<div class="stp-row"><input id="stp-t-name" placeholder="name" size="10"><input id="stp-t-from" placeholder="from: a folder or remote:name/sub" style="flex:1"><input id="stp-t-to" placeholder="to" style="flex:1"><select id="stp-t-mode"><option>copy</option><option>mirror</option><option>two-way</option></select><input id="stp-t-every" placeholder="every min" size="8"><button class="btn ghost" id="stp-t-add">Save</button></div><div class="stp-log" id="stp-t-log" style="display:none"></div></div>
<div class="stp-card"><b>Backups</b><table class="stp-table">${Object.entries(bk).map(([n, j]) => `<tr><td><b>${E(n)}</b><div class="stp-muted">${E(j.sources.join(', '))} → ${E(j.repo)} · every ${E(j.every_hours)} h</div><div class="stp-muted">${j.last_run ? `last: ${when(j.last_run.at)} (${E(j.last_run.files)} files)` : 'not run yet'}</div></td><td><button class="btn ghost" data-brun="${E(n)}">Back up now</button> <button class="btn ghost" data-bchk="${E(n)}">Check</button> <button class="btn ghost" data-bsnap="${E(n)}">Restore…</button> <button class="btn ghost" data-brm="${E(n)}">Remove</button></td></tr>`).join('') || '<tr><td class="stp-muted">none</td></tr>'}</table>
<div class="stp-row"><input id="stp-b-name" placeholder="name" size="10"><input id="stp-b-src" placeholder="folders to back up, ; separated" style="flex:1"><input id="stp-b-repo" placeholder="repository folder (another drive)" style="flex:1"><input id="stp-b-pw" type="password" placeholder="repository password" size="14"><input id="stp-b-every" value="24" size="4" title="hours"><button class="btn ghost" id="stp-b-add">Save</button></div>
<div class="stp-muted">Encrypted with the password (keep it: it cannot be recovered). Deduplicated: unchanged data is never stored twice.</div><div class="stp-log" id="stp-b-log" style="display:none"></div></div></div>`;
  }

  async function appsTab() {
    const cat = await api(`${A}/apps`);
    const shareNames = st.o.shares.map((s) => s.name);
    return `<p class="stp-muted">Containers run through Docker on this machine; installing downloads the image from its registry. Map each of the app's folders to a share (or type a folder).</p>
<div class="stp-grid">${cat.map((a) => `<div class="stp-card"><div class="stp-row"><b>${E(a.title)}</b>${chip('', a.category)}</div><div class="stp-muted">${E(a.about)}</div><div class="stp-muted stp-mono">${E(a.image)} · ports ${E(Object.values(a.ports).join(', '))}</div>
<div class="stp-form">${Object.keys(a.mounts).map((k) => `<label>${E(k)}</label><input list="stp-sharelist" data-mount="${E(a.id)}:${E(k)}" placeholder="share:${E(shareNames[0] || 'media')}/${E(a.id)}-${E(k)}">`).join('')}</div>
<div class="stp-row"><button class="btn ghost" data-app="${E(a.id)}">Install</button></div><div class="stp-log" id="stp-app-${E(a.id)}" style="display:none"></div></div>`).join('')}</div>
<datalist id="stp-sharelist">${shareNames.map((n) => `<option value="share:${E(n)}">`).join('')}</datalist>`;
  }

  // ---- wiring ----
  function wire() {
    const root = $('stp-root');
    const on = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };
    const each = (sel, fn) => root.querySelectorAll(sel).forEach((el) => el.addEventListener('click', () => fn(el)));
    const act = async (fn, ok) => { try { await fn(); if (ok) toast(ok); st.o = null; render(); } catch (e) { toast(errText(e), 'error'); } };
    each('[data-tab]', (b) => { st.tab = b.dataset.tab; keep('stp.tab', st.tab); st.form = null; render(); });
    on('stp-srv', () => act(() => send(`${A}/server/${st.o.server.running ? 'stop' : 'start'}`, 'POST'), st.o.server.running ? 'Stopped' : 'Started'));
    const auto = $('stp-auto'); if (auto) auto.addEventListener('change', () => act(() => send(`${A}/settings`, 'PUT', { autostart: auto.checked })));
    on('stp-unfreeze', () => { if (confirm('Unfreeze the shares? Only do this once the cause is found and stopped.')) act(() => send(`${A}/guard/unfreeze`, 'POST', {}), 'Unfrozen'); });
    // array
    each('[data-arr]', (b) => { const a = b.dataset.arr; runInto(a === 'sync' ? send(`${A}/array/sync`, 'POST', {}) : a === 'fix' ? send(`${A}/array/fix`, 'POST', {}) : send(`${A}/array/scrub`, 'POST', { percent: a === 'scrub100' ? 100 : 10 }), 'stp-a-log'); });
    on('stp-a-changes', async () => { try { const r = await api(`${A}/array?changes=true`); toast(r.status.disks.map((d) => `${d.name}: ${d.unsynced ?? '?'} unsynced`).join(' · ')); } catch (e) { toast(errText(e), 'error'); } });
    on('stp-fix-go', () => { const d = $('stp-fix-disk').value, t = $('stp-fix-target').value.trim(); if (!d || !t) { toast('Choose the disk and the new drive\'s folder', 'error'); return; } runInto(send(`${A}/array/fix`, 'POST', { disk: d, target: t }), 'stp-a-log'); });
    on('stp-a-save', () => act(() => send(`${A}/array`, 'PUT', {
      disks: $('stp-a-disks').value.split(/\n+/).map((l) => l.trim()).filter(Boolean).map((l) => { const i = l.indexOf('='); return { name: l.slice(0, i).trim(), path: l.slice(i + 1).trim() }; }),
      parity: [$('stp-a-p1').value.trim(), $('stp-a-p2').value.trim()].filter(Boolean).map((p) => ({ path: p })),
      block_kib: +$('stp-a-bs').value, allow_same_drive: $('stp-a-same').checked }), 'Array saved: run a sync to protect it'));
    // shares
    on('stp-s-new', () => { st.form = { share: 'new' }; render(); });
    on('stp-s-cancel', () => { st.form = null; render(); });
    const kind = $('stp-s-kind');
    if (kind) { const sync = () => root.querySelectorAll('.k-user,.k-folder').forEach((el) => { el.style.display = el.classList.contains(`k-${kind.value}`) ? '' : 'none'; }); kind.addEventListener('change', sync); sync(); }
    on('stp-s-save', (ev) => {
      const name = ev.target.dataset.name || $('stp-s-name').value.trim();
      const users = {}; $('stp-s-users').value.split(',').map((x) => x.trim()).filter(Boolean).forEach((x) => { const [u, m] = x.split(':'); users[u.trim()] = (m || 'r').trim(); });
      const s = { comment: $('stp-s-comment').value, access: $('stp-s-access').value, users, recycle_bin: $('stp-s-bin').checked };
      if ($('stp-s-kind').value === 'folder') s.path = $('stp-s-path').value.trim();
      else Object.assign(s, { path: null, cache: $('stp-s-cache').value, cache_pool: $('stp-s-pool').value || 'cache', allocation: $('stp-s-alloc').value, split_level: +$('stp-s-split').value || 0, min_free_gb: +$('stp-s-minfree').value || 0 });
      act(async () => { if (ev.target.dataset.name) await send(`${A}/shares/${encodeURIComponent(name)}`, 'PATCH', s); else await send(`${A}/shares`, 'POST', { name, settings: s }); st.form = null; }, 'Saved');
    });
    each('[data-sedit]', (b) => { st.form = { share: b.dataset.sedit }; render(); });
    each('[data-srm]', (b) => { if (confirm(`Forget share ${b.dataset.srm}? Its files stay on the disks.`)) act(() => send(`${A}/shares/${encodeURIComponent(b.dataset.srm)}`, 'DELETE'), 'Removed'); });
    each('[data-smb],[data-nfs]', async (b) => { const n = b.dataset.smb || b.dataset.nfs; try { const r = await api(`${A}/shares/${encodeURIComponent(n)}/${b.dataset.smb ? 'smb' : 'nfs'}`); $(`stp-exp-${n}`).textContent = [...(r.commands || []), ...(r.notes || []).map((x) => '# ' + x)].join('\n'); } catch (e) { toast(errText(e), 'error'); } });
    on('stp-p-add', () => act(() => send(`${A}/pools/${encodeURIComponent($('stp-p-name').value.trim() || 'cache')}`, 'PUT', { path: $('stp-p-path').value.trim() }), 'Pool added'));
    each('[data-poolrm]', (b) => act(() => send(`${A}/pools/${encodeURIComponent(b.dataset.poolrm)}`, 'DELETE'), 'Removed'));
    // users, links
    on('stp-u-add', () => act(() => send(`${A}/users`, 'POST', { name: $('stp-u-name').value.trim(), password: $('stp-u-pw').value, admin: $('stp-u-admin').checked }), 'Saved'));
    each('[data-urm]', (b) => { if (confirm(`Remove user ${b.dataset.urm}?`)) act(() => send(`${A}/users/${encodeURIComponent(b.dataset.urm)}`, 'DELETE'), 'Removed'); });
    each('[data-lrm]', (b) => act(() => send(`${A}/links/${encodeURIComponent(b.dataset.lrm)}`, 'DELETE'), 'Revoked'));
    // search
    const doSearch = async () => {
      st.q = $('stp-q').value.trim(); if (!st.q) return;
      try { const res = await api(`${A}/search?q=${encodeURIComponent(st.q)}&mode=${$('stp-qm').value}`); $('stp-results').innerHTML = `<table class="stp-table">${res.map((r) => `<tr><td class="stp-mono">${E(r.share)}/${E(r.path)}</td><td>${E(r.kind)}</td><td>${human(r.size)}</td><td class="stp-muted">${E(r.snippet)} ${E(r.tags.join(', '))}</td></tr>`).join('') || '<tr><td class="stp-muted">nothing found</td></tr>'}</table>`; }
      catch (e) { toast(errText(e), 'error'); }
    };
    on('stp-q-go', doSearch);
    const qi = $('stp-q'); if (qi) qi.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });
    on('stp-dups', async () => { try { const d = await api(`${A}/duplicates`); $('stp-results').innerHTML = `<p>${human(d.wasted_bytes)} could be freed.</p>` + d.groups.slice(0, 50).map((g) => `<div class="stp-card" style="margin-bottom:6px"><div>${chip('', g.kind)} ${human(g.wasted)} wasted</div><div class="stp-mono">${g.files.map((f) => `${E(f.share)}/${E(f.path)} (${human(f.size)})`).join('\n')}</div></div>`).join(''); } catch (e) { toast(errText(e), 'error'); } });
    on('stp-idx', () => runInto(send(`${A}/index`, 'POST', {}), 'stp-i-log'));
    // jobs
    on('stp-r-add', () => { const fields = {}; $('stp-r-fields').value.split(/\s+/).filter(Boolean).forEach((kv) => { const i = kv.indexOf('='); if (i > 0) fields[kv.slice(0, i)] = kv.slice(i + 1); }); const sec = $('stp-r-secret').value; const k = $('stp-r-kind').value; if (sec) fields[k === 's3' ? 'secret_key' : 'password'] = sec; act(() => send(`${A}/remotes/${encodeURIComponent($('stp-r-name').value.trim())}`, 'PUT', { kind: k, fields }), 'Saved'); });
    each('[data-rrm]', (b) => act(() => send(`${A}/remotes/${encodeURIComponent(b.dataset.rrm)}`, 'DELETE'), 'Removed'));
    const spec = (v) => (v.startsWith('remote:') ? { remote: v.slice(7).split('/')[0], path: v.slice(7).split('/').slice(1).join('/') } : { path: v });
    on('stp-t-add', () => act(() => send(`${A}/transfers/${encodeURIComponent($('stp-t-name').value.trim())}`, 'PUT', { source: spec($('stp-t-from').value.trim()), dest: spec($('stp-t-to').value.trim()), mode: $('stp-t-mode').value, every_minutes: +$('stp-t-every').value || 0 }), 'Saved'));
    each('[data-trun]', (b) => runInto(send(`${A}/transfers/${encodeURIComponent(b.dataset.trun)}/run`, 'POST'), 'stp-t-log'));
    each('[data-tdry]', (b) => runInto(send(`${A}/transfers/${encodeURIComponent(b.dataset.tdry)}/run?dry=true`, 'POST'), 'stp-t-log'));
    each('[data-trm]', (b) => act(() => send(`${A}/transfers/${encodeURIComponent(b.dataset.trm)}`, 'DELETE'), 'Removed'));
    on('stp-b-add', () => act(() => send(`${A}/backups/${encodeURIComponent($('stp-b-name').value.trim())}`, 'PUT', { repo: $('stp-b-repo').value.trim(), sources: $('stp-b-src').value.split(';').map((x) => x.trim()).filter(Boolean), password: $('stp-b-pw').value, every_hours: +$('stp-b-every').value || 24 }), 'Backup saved'));
    each('[data-brun]', (b) => runInto(send(`${A}/backups/${encodeURIComponent(b.dataset.brun)}/run`, 'POST', {}), 'stp-b-log'));
    each('[data-bchk]', (b) => runInto(send(`${A}/backups/${encodeURIComponent(b.dataset.bchk)}/check`, 'POST', { read_percent: 10 }), 'stp-b-log'));
    each('[data-brm]', (b) => { if (confirm('Forget this backup job? The repository and its snapshots stay.')) act(() => send(`${A}/backups/${encodeURIComponent(b.dataset.brm)}`, 'DELETE'), 'Removed'); });
    each('[data-bsnap]', async (b) => {
      try {
        const snaps = await api(`${A}/backups/${encodeURIComponent(b.dataset.bsnap)}/snapshots`);
        const pick = prompt('Restore which snapshot? ' + snaps.slice(-10).map((s) => `${s.id} (${new Date(s.time * 1000).toLocaleString()}, ${s.stats.files} files)`).join('; '), (snaps[snaps.length - 1] || {}).id || '');
        if (!pick) return; const target = prompt('Restore into which folder? (an empty folder is safest)'); if (!target) return;
        runInto(send(`${A}/backups/${encodeURIComponent(b.dataset.bsnap)}/restore`, 'POST', { snapshot: pick, target }), 'stp-b-log');
      } catch (e) { toast(errText(e), 'error'); }
    });
    // apps
    each('[data-app]', (b) => {
      const id = b.dataset.app; const mounts = {};
      root.querySelectorAll(`[data-mount^="${id}:"]`).forEach((i) => { mounts[i.dataset.mount.split(':')[1]] = i.value.trim() || i.placeholder; });
      const name = prompt('Container name', id); if (!name) return;
      if (!confirm(`Install ${id}? Docker downloads its image from its registry.`)) return;
      runInto(send(`${A}/apps`, 'POST', { app: id, name, mounts }), `stp-app-${id}`);
    });
  }

  async function render() {
    css();
    const root = $('stp-root');
    if (!root || st.busy) return;
    st.busy = true;
    const tabs = [['overview', 'Overview'], ['array', 'Array & parity'], ['shares', 'Shares'], ['users', 'Users & links'], ['disks', 'Disks'], ['search', 'Search'], ['jobs', 'Transfers & backups'], ['apps', 'Apps']];
    const head = `<div class="stp-tabs">${tabs.map(([k, l]) => `<button class="btn ghost ${st.tab === k ? 'on' : ''}" data-tab="${k}">${l}</button>`).join('')}</div>`;
    try {
      if (!st.o) st.o = await api(A);
      const t = st.tab;
      const html = t === 'array' ? arrayTab() : t === 'shares' ? await sharesTab() : t === 'users' ? await usersTab() : t === 'disks' ? await disksTab()
        : t === 'search' ? await searchTab() : t === 'jobs' ? await jobsTab() : t === 'apps' ? await appsTab() : await overviewTab();
      root.innerHTML = head + html;
    } catch (e) { root.innerHTML = head + `<p class="cardnote">${E(errText(e))}</p>`; }
    st.busy = false;
    wire();
  }

  function init() {
    if (!$('stp-root')) return;
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'storage') { st.o = null; render(); } };
    window.addEventListener('hashchange', onHash);
    onHash();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
