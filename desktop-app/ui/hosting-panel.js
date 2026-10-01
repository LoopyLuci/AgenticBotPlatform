// Hosting panel: ABP Web Hosting (bot/hosting). Tabs:
//   Sites      every site (a folder, an app on a port, or a redirect) and its domains; New site; Go live (a mode, the
//              plan, run with a live log); Publish; Check from outside; Edit; Remove
//   Network    this machine's public / LAN addresses and NAT situation, the recommendation, the web server (start,
//              stop, log), the router's port forwards (UPnP), hosting settings
//   Accounts   providers to connect (Cloudflare, DigitalOcean, Hetzner, Netlify, Vercel, an SSH server, FTP...);
//              secrets are typed here once, sealed on the server and never shown again
//   DNS        zones and records of any connected account; set and delete record sets
//   Tunnels    Cloudflare Tunnel (cloudflared), Tailscale Funnel; certificates (ACME) and getting one
//   Servers    VPSs at Hetzner / DigitalOcean / Vultr / Linode: sizes with prices, create (the price is confirmed),
//              destroy (the name is typed); SSH servers: install Caddy
// Identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/); tests/test_hosting_page.py
// fails if the two differ. It uses the page's own api(), esc() and showToast(), and draws into #hsp-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { const d = JSON.parse(m).detail; m = d || m; } catch (_) {} return typeof m === 'string' ? m : JSON.stringify(m); };
  const send = (path, method, body) => api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
  const A = '/api/hosting';
  const MODES = [
    ['cloudflare-tunnel', 'Cloudflare Tunnel', 'No ports to open; works behind any router or carrier NAT. The domain must be on Cloudflare.', 'tunnel'],
    ['tailscale-funnel', 'Tailscale Funnel', 'A free https://<machine>.ts.net address; no domain needed.', ''],
    ['port-forward', 'Port forward (home router)', 'DNS points at your public IP; the router forwards 80/443 here; a Let\'s Encrypt certificate.', ''],
    ['direct', 'Direct (public IP)', 'This machine has a public address itself.', ''],
    ['server', 'My server / VPS (SSH)', 'The files are uploaded to a server and Caddy serves them with HTTPS.', 'server'],
    ['provider', 'Hosting provider', 'Netlify, Vercel, Cloudflare Pages, GitHub Pages, or FTP hosting.', 'deploy'],
    ['lan', 'Local network only', 'Reachable from devices at home; nothing exposed to the internet.', ''],
  ];
  const st = { tab: 'sites', data: null, providers: null, form: null, live: null, dns: { account: '', zone: '' }, srv: { account: '' }, busy: false };
  try { st.tab = localStorage.getItem('hsp.tab') || 'sites'; } catch (_) {}
  const keep = (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} };

  function css() {
    if ($('hsp-css')) return;
    const s = document.createElement('style');
    s.id = 'hsp-css';
    s.textContent = `.hsp-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}.hsp-tabs button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.hsp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.hsp-card{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:var(--surface);min-width:0;display:grid;gap:8px;align-content:start}
.hsp-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.hsp-muted{color:var(--muted);font-size:12px}
.hsp-table{width:100%;border-collapse:collapse;font-size:12.5px}.hsp-table td,.hsp-table th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.hsp-mono{font-family:var(--font-mono,monospace);font-size:12px;white-space:pre-wrap;word-break:break-word}
.hsp-log{font-family:var(--font-mono,monospace);font-size:12px;white-space:pre-wrap;background:var(--surface-2);border-radius:8px;padding:8px;max-height:280px;overflow:auto}
.hsp-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.hsp-chip.ok{background:var(--good-soft);color:var(--good,#1f9d55)}.hsp-chip.bad{background:var(--bad-soft,#fde8e8);color:var(--bad,#c53030)}
.hsp-form{display:grid;grid-template-columns:minmax(120px,170px) 1fr;gap:6px 10px;align-items:center}@media (max-width:640px){.hsp-form{grid-template-columns:1fr}}
.hsp-form label{font-size:12.5px;color:var(--ink-soft)}
#hsp-root input,#hsp-root select,#hsp-root textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%;min-width:0}
.hsp-modes{display:grid;gap:6px}.hsp-mode{border:1px solid var(--line);border-radius:8px;padding:6px 8px;cursor:pointer}.hsp-mode.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.hsp-step{display:flex;gap:8px;font-size:12.5px}.hsp-step b{min-width:16px}`;
    document.head.appendChild(s);
  }

  const chip = (ok, yes, no) => `<span class="hsp-chip ${ok ? 'ok' : 'bad'}">${E(ok ? yes : no)}</span>`;
  const accountsWith = (cap) => (st.data ? st.data.accounts : []).filter((a) => !cap || a.caps.includes(cap));
  const accOptions = (cap, sel) => accountsWith(cap).map((a) => `<option value="${E(a.id)}" ${a.id === sel ? 'selected' : ''}>${E(a.name)} (${E(a.label)})</option>`).join('');

  async function load() {
    st.data = await api(A);
    if (!st.providers) st.providers = await api(`${A}/providers`);
  }

  // ---- runs: a background job's log, polled ----
  async function follow(run, box) {
    let seen = 0;
    const el = $(box);
    for (;;) {
      let r;
      try { r = await api(`${A}/runs/${run.run}?since=${seen}`); } catch (e) { if (el) el.textContent += `\n${errText(e)}`; return null; }
      seen = r.log_total;
      if (el && r.log.length) { el.textContent += (el.textContent ? '\n' : '') + r.log.join('\n'); el.scrollTop = el.scrollHeight; }
      if (r.done) return r;
      await new Promise((ok) => setTimeout(ok, 1200));
    }
  }

  // ---- Sites ----
  function siteCard(s) {
    const exp = (s.exposure || {}).mode;
    const last = (s.history || []).slice(-1)[0];
    const src = s.kind === 'static' ? (s.build ? `build: ${s.build.command || ''} → ${s.build.output || ''}` : s.root) : s.kind === 'proxy' ? s.upstream : s.redirect_to;
    return `<div class="hsp-card"><div class="hsp-row"><b>${E(s.name)}</b><span class="hsp-chip">${E(s.kind)}</span>${exp ? `<span class="hsp-chip">${E(exp)}</span>` : '<span class="hsp-chip">not live</span>'}${s.auth ? '<span class="hsp-chip">password</span>' : ''}${s.enabled === false ? '<span class="hsp-chip bad">off</span>' : ''}</div>
<div class="hsp-mono">${E(src || '')}</div>
<div>${(s.domains || []).map((d) => `<a href="${d.endsWith('.localhost') ? 'http' : 'https'}://${E(d)}" target="_blank" rel="noopener">${E(d)}</a>`).join(', ') || '<span class="hsp-muted">no domains yet</span>'}</div>
${last ? `<div class="hsp-muted">${E(last.action)} ${last.ok ? 'succeeded' : 'had problems'} · ${new Date(last.at * 1000).toLocaleString()}</div>` : ''}
${(s.targets || []).length ? `<div class="hsp-muted">deploys to: ${(s.targets || []).map((t) => E((accountsWith('').find((a) => a.id === t.account) || {}).name || t.account)).join(', ')}</div>` : ''}
<div class="hsp-row"><button class="btn" data-live="${E(s.id)}">Go live</button><button class="btn ghost" data-publish="${E(s.id)}" ${(s.targets || []).length ? '' : 'disabled title="add a deploy target first (Edit)"'}>Publish</button><button class="btn ghost" data-check="${E(s.id)}">Check</button><button class="btn ghost" data-edit="${E(s.id)}">Edit</button><button class="btn ghost" data-rm="${E(s.id)}">Remove</button></div>
<div id="hsp-out-${E(s.id)}"></div></div>`;
  }

  function siteForm(s) {
    s = s || { kind: 'static', https: 'auto', force_https: true, serve: 'edge' };
    const b = s.build || {};
    return `<div class="hsp-card"><b>${s.id ? `Edit ${E(s.name)}` : 'New site'}</b><div class="hsp-form">
<label>Name</label><input id="hsp-f-name" value="${E(s.name || '')}" placeholder="My blog">
<label>What it serves</label><select id="hsp-f-kind">${[['static', 'A folder of files (HTML, a built app)'], ['proxy', 'An app running on a port'], ['redirect', 'A redirect to another address']].map(([k, l]) => `<option value="${k}" ${s.kind === k ? 'selected' : ''}>${l}</option>`).join('')}</select>
<label class="k-static">Folder</label><input class="k-static" id="hsp-f-root" value="${E(s.root || '')}" placeholder="E:\\sites\\blog (or the project, with a build below)">
<label class="k-static">Single-page app</label><span class="k-static"><input type="checkbox" id="hsp-f-spa" ${s.spa ? 'checked' : ''}> unknown paths load index.html</span>
<label class="k-static">Build command</label><input class="k-static" id="hsp-f-bcmd" value="${E(b.command || '')}" placeholder="optional: npm run build">
<label class="k-static">Build in / output</label><span class="k-static hsp-row"><input id="hsp-f-bcwd" value="${E(b.cwd || '')}" placeholder="project folder" style="flex:2"><input id="hsp-f-bout" value="${E(b.output || 'dist')}" style="flex:1"></span>
<label class="k-proxy">App address</label><input class="k-proxy" id="hsp-f-up" value="${E(s.upstream || '')}" placeholder="http://127.0.0.1:3000">
<label class="k-redirect">Send visitors to</label><input class="k-redirect" id="hsp-f-redir" value="${E(s.redirect_to || '')}" placeholder="https://example.com">
<label>Domains</label><input id="hsp-f-domains" value="${E((s.domains || []).join(', '))}" placeholder="example.com, www.example.com (or blog.localhost to try it)">
<label>HTTPS</label><select id="hsp-f-https">${[['auto', 'Automatic certificate'], ['self-signed', 'Self-signed (testing, LAN)'], ['off', 'Off (http only)']].map(([k, l]) => `<option value="${k}" ${s.https === k ? 'selected' : ''}>${l}</option>`).join('')}</select>
<label>Password</label><span class="hsp-row"><input id="hsp-f-user" value="${E((s.auth || {}).user || '')}" placeholder="user" style="flex:1"><input id="hsp-f-pw" type="password" autocomplete="new-password" placeholder="${s.auth ? 'unchanged' : 'optional: visitors must sign in'}" style="flex:1"></span>
<label>Deploy targets</label><select id="hsp-f-targets" multiple size="3">${accountsWith('deploy').map((a) => `<option value="${E(a.id)}" ${(s.targets || []).some((t) => t.account === a.id) ? 'selected' : ''}>${E(a.name)} (${E(a.label)})</option>`).join('')}</select>
</div><div class="hsp-row"><button class="btn" id="hsp-f-save" data-id="${E(s.id || '')}">${s.id ? 'Save' : 'Create'}</button><button class="btn ghost" id="hsp-f-cancel">Cancel</button></div></div>`;
  }

  function sitesTab() {
    const sites = st.data.sites;
    const e = st.data.edge;
    return `<div class="hsp-row" style="margin-bottom:10px"><button class="btn" id="hsp-new">New site</button>
<span class="hsp-muted">Web server: ${chip(e.running, `${e.engine} running`, `${e.engine} stopped`)} http ${E(e.http_port)}, https ${E(e.https_port)}</span></div>
${st.form ? siteForm(st.form === 'new' ? null : sites.find((s) => s.id === st.form)) : ''}
${st.live ? liveBox() : ''}
<div class="hsp-grid">${sites.map(siteCard).join('') || '<p class="cardnote">No sites yet. A site is a folder of files, an app on a port or a redirect, plus the domain names it answers to. Create one, then Go live.</p>'}</div>`;
  }

  function liveBox() {
    const L = st.live;
    const site = st.data.sites.find((s) => s.id === L.site) || {};
    const mode = MODES.find((m) => m[0] === L.mode) || MODES[0];
    const needsAcc = mode[0] === 'cloudflare-tunnel' ? 'tunnel' : mode[0] === 'server' ? 'server' : mode[0] === 'provider' ? 'deploy' : '';
    return `<div class="hsp-card" style="margin-bottom:12px"><b>Go live: ${E(site.name || L.site)}</b>
${L.rec ? `<div class="hsp-muted">Recommended here: <b>${E(L.rec.mode)}</b> — ${E(L.rec.why)}</div>` : ''}
<div class="hsp-modes">${MODES.map((m) => `<div class="hsp-mode ${m[0] === L.mode ? 'on' : ''}" data-mode="${m[0]}"><b>${E(m[1])}</b> <span class="hsp-muted">${E(m[2])}</span></div>`).join('')}</div>
${needsAcc ? `<div class="hsp-row"><label>Account</label><select id="hsp-l-acc"><option value="">choose…</option>${accOptions(needsAcc, L.account)}</select>${accountsWith(needsAcc).length ? '' : '<span class="hsp-muted">connect one on the Accounts tab first</span>'}</div>` : ''}
<div id="hsp-l-plan">${L.plan ? L.plan.map((p, i) => `<div class="hsp-step"><b>${i + 1}.</b> ${E(p.text)}</div>`).join('') : '<span class="hsp-muted">…</span>'}</div>
${L.plan && L.plan.some((p) => p.step === 'certificate') && !st.data.settings.agreed_ca_terms ? `<label class="hsp-row"><input type="checkbox" id="hsp-l-tos"> I agree to the certificate authority's terms of service (Let's Encrypt: letsencrypt.org/repository)</label>` : ''}
<div class="hsp-row"><button class="btn" id="hsp-l-run" ${L.running ? 'disabled' : ''}>${L.running ? 'Working…' : 'Go live'}</button><button class="btn ghost" id="hsp-l-close">Close</button></div>
<div class="hsp-log" id="hsp-l-log">${E(L.log || '')}</div>
${L.result ? `<div>${L.result.steps.map((s) => `<div class="hsp-step"><b>${s.ok ? '✓' : '✗'}</b> ${E(s.text)}<br><span class="hsp-muted">${E(s.note)}</span></div>`).join('')}</div>` : ''}</div>`;
  }

  async function refreshPlan() {
    const L = st.live;
    try { L.plan = await api(`${A}/sites/${encodeURIComponent(L.site)}/plan?mode=${encodeURIComponent(L.mode)}&account=${encodeURIComponent(L.account || '')}`); }
    catch (e) { L.plan = [{ step: 'error', text: errText(e) }]; }
    render();
  }

  async function checkSite(id) {
    const out = $(`hsp-out-${id}`);
    if (out) out.innerHTML = '<span class="hsp-muted">checking from outside…</span>';
    try {
      const r = await send(`${A}/sites/${encodeURIComponent(id)}/check`, 'POST');
      if (out) out.innerHTML = r.names.length ? `<table class="hsp-table">${r.names.map((n) => `<tr><td>${chip(n.ok, 'ok', 'problem')}</td><td>${E(n.name)}</td><td>${E((n.a || []).join(', ') || '-')}</td><td>${E(n.problem || `https ${n.https.status} · ${n.https.ms} ms`)}</td></tr>`).join('')}</table>` : '<span class="hsp-muted">no public names to check</span>';
    } catch (e) { if (out) out.textContent = errText(e); }
  }

  function readForm() {
    const kind = $('hsp-f-kind').value;
    const body = { name: $('hsp-f-name').value.trim(), kind, domains: $('hsp-f-domains').value, https: $('hsp-f-https').value };
    if (kind === 'static') {
      body.root = $('hsp-f-root').value.trim(); body.spa = $('hsp-f-spa').checked;
      const cmd = $('hsp-f-bcmd').value.trim(), cwd = $('hsp-f-bcwd').value.trim();
      body.build = (cmd || cwd) ? { command: cmd, cwd: cwd || body.root, output: $('hsp-f-bout').value.trim() || 'dist' } : null;
      if (!body.root && body.build) body.root = null;
    } else if (kind === 'proxy') body.upstream = $('hsp-f-up').value.trim();
    else body.redirect_to = $('hsp-f-redir').value.trim();
    const pw = $('hsp-f-pw').value;
    if (pw) { body.password = pw; body.user = $('hsp-f-user').value.trim() || 'admin'; }
    body.targets = Array.from($('hsp-f-targets').selectedOptions).map((o) => ({ account: o.value }));
    return body;
  }

  // ---- Network ----
  async function networkTab() {
    const [net, upnp] = await Promise.all([api(`${A}/network`), api(`${A}/upnp`)]);
    const n = net.network, e = st.data.edge, s = st.data.settings;
    return `<div class="hsp-grid"><div class="hsp-card"><b>This machine on the internet</b><table class="hsp-table">
<tr><td>Public IPv4</td><td class="hsp-mono">${E(n.public_ipv4 || '-')}</td></tr><tr><td>Public IPv6</td><td class="hsp-mono">${E(n.public_ipv6 || '-')}</td></tr>
<tr><td>LAN address</td><td class="hsp-mono">${E(n.lan_ip || '-')}</td></tr><tr><td>Router (UPnP)</td><td>${n.router_upnp ? `answers; its WAN address ${E(n.router_wan_ip)}` : 'does not answer UPnP'}</td></tr></table>
<div><span class="hsp-chip">${E(n.situation)}</span> ${E(n.advice)}</div><div>Recommended: <b>${E(net.mode)}</b> — ${E(net.why)}</div></div>
<div class="hsp-card"><b>Web server</b><div>${chip(e.running, 'running', 'stopped')} ${E(e.engine)} · http ${E(e.http_port)} · https ${E(e.https_port)}${e.health && e.health.hosts ? ` · ${e.health.hosts.length} host name(s), ${e.health.requests} request(s)` : ''}</div>
<div class="hsp-row"><button class="btn" id="hsp-e-start">${e.running ? 'Restart' : 'Start'}</button><button class="btn ghost" id="hsp-e-stop" ${e.running ? '' : 'disabled'}>Stop</button><button class="btn ghost" data-log="edge">Log</button><button class="btn ghost" data-log="cloudflared">Tunnel log</button></div>
<div class="hsp-log" id="hsp-e-log" style="display:none"></div></div>
<div class="hsp-card"><b>Router port forwards</b>${upnp.available ? `<table class="hsp-table"><tr><th>Port</th><th>To</th><th>Description</th><th></th></tr>${upnp.mappings.map((m) => `<tr><td>${E(m.external_port)}/${E(m.protocol)}</td><td>${E(m.internal_ip)}:${E(m.internal_port)}</td><td>${E(m.description)}</td><td>${m.abp ? `<button class="btn ghost" data-unmap="${E(m.external_port)}/${E(m.protocol)}">Remove</button>` : ''}</td></tr>`).join('')}</table>
<div class="hsp-row"><input id="hsp-u-port" placeholder="port" size="6"><input id="hsp-u-to" placeholder="to port (same)" size="10"><button class="btn ghost" id="hsp-u-add">Forward here</button></div>` : `<div class="hsp-muted">${E(upnp.reason)}. Forward ports in the router's own settings page: TCP 80 → ${E(n.lan_ip)}:${E(s.http_port)} and TCP 443 → ${E(n.lan_ip)}:${E(s.https_port)}.</div>`}</div>
<div class="hsp-card"><b>Settings</b><div class="hsp-form">
<label>Engine</label><select id="hsp-s-engine"><option value="edge" ${s.engine === 'edge' ? 'selected' : ''}>ABP edge (built in)</option><option value="caddy" ${s.engine === 'caddy' ? 'selected' : ''}>Caddy (if installed)</option></select>
<label>HTTP port</label><input id="hsp-s-http" value="${E(s.http_port)}"><label>HTTPS port</label><input id="hsp-s-https" value="${E(s.https_port)}">
<label>Listen on</label><input id="hsp-s-bind" value="${E(s.bind)}">
<label>Certificate e-mail</label><input id="hsp-s-email" value="${E(s.acme_email)}" placeholder="for expiry notices (optional)">
<label>Certificate authority</label><select id="hsp-s-ca">${['letsencrypt', 'letsencrypt-staging', 'zerossl', 'google'].map((c) => `<option ${s.ca === c ? 'selected' : ''}>${c}</option>`).join('')}</select>
<label>Dynamic DNS</label><span><input type="checkbox" id="hsp-s-ddns" ${s.ddns ? 'checked' : ''}> keep A records on this machine's public IP</span>
<label>Keep running</label><span><input type="checkbox" id="hsp-s-auto" ${s.autostart_edge ? 'checked' : ''}> start the web server and tunnel with ABP</span>
</div><div class="hsp-row"><button class="btn" id="hsp-s-save">Save settings</button></div></div></div>`;
  }

  // ---- Accounts ----
  function accountsTab() {
    const P = st.providers || {};
    const sel = st.addProvider || '';
    const spec = P[sel];
    return `<div class="hsp-grid"><div class="hsp-card"><b>Connected</b>${st.data.accounts.length ? `<table class="hsp-table">${st.data.accounts.map((a) => `<tr><td><b>${E(a.name)}</b><br><span class="hsp-muted">${E(a.label)} · ${E(a.caps.join(', '))}</span></td><td>${chip(!!a.verified, 'works', a.verify_note ? 'failing' : 'unchecked')}<div class="hsp-muted">${E(a.verify_note || '')}</div></td><td><button class="btn ghost" data-verify="${E(a.id)}">Verify</button> <button class="btn ghost" data-unacc="${E(a.id)}">Remove</button></td></tr>`).join('')}</table>` : '<p class="cardnote">Nothing connected yet.</p>'}</div>
<div class="hsp-card"><b>Connect an account</b><select id="hsp-a-prov"><option value="">choose a provider…</option>${Object.entries(P).map(([k, v]) => `<option value="${k}" ${k === sel ? 'selected' : ''}>${E(v.label)} — ${E(v.caps.join(', '))}</option>`).join('')}</select>
${spec ? `${spec.help ? `<div class="hsp-muted">Where to get it: ${/^https?:/.test(spec.help) ? `<a href="${E(spec.help)}" target="_blank" rel="noopener">${E(spec.help)}</a>` : E(spec.help)}</div>` : ''}<div class="hsp-form"><label>Name</label><input id="hsp-a-name" placeholder="${E(spec.label)}">
${spec.fields.map((f) => `<label>${E(f.label)}${f.required ? '' : ' <span class="hsp-muted">(optional)</span>'}</label><input data-field="${E(f.key)}" ${f.secret ? 'type="password" autocomplete="off"' : ''}>`).join('')}</div>
<div class="hsp-row"><button class="btn" id="hsp-a-add">Connect and verify</button></div><div class="hsp-muted">Secrets are sealed with ABP's vault key on this machine and are never shown again or given to agents.</div>` : ''}</div></div>`;
  }

  // ---- DNS ----
  async function dnsTab() {
    const accs = accountsWith('dns');
    if (!accs.length) return '<p class="cardnote">Connect a DNS provider (Cloudflare, DigitalOcean, Hetzner, Porkbun, deSEC, Gandi, Route 53, Duck DNS, Netlify, Vercel...) on the Accounts tab.</p>';
    const D = st.dns;
    if (!D.account) D.account = accs[0].id;
    let zones = [], recs = null, err = '';
    try { zones = await api(`${A}/dns/${encodeURIComponent(D.account)}/zones`); if (!D.zone && zones.length) D.zone = zones[0].name; if (D.zone) recs = await api(`${A}/dns/${encodeURIComponent(D.account)}/records?zone=${encodeURIComponent(D.zone)}`); }
    catch (e) { err = errText(e); }
    return `<div class="hsp-row" style="margin-bottom:8px"><select id="hsp-d-acc">${accOptions('dns', D.account)}</select><select id="hsp-d-zone">${zones.map((z) => `<option ${z.name === D.zone ? 'selected' : ''}>${E(z.name)}</option>`).join('')}</select><button class="btn ghost" id="hsp-d-reload">Reload</button></div>
${err ? `<p class="cardnote">${E(err)}</p>` : ''}
<div class="hsp-card"><b>Set a record</b><div class="hsp-row"><input id="hsp-d-name" placeholder="name (www, @)" size="16"><select id="hsp-d-type">${['A', 'AAAA', 'CNAME', 'TXT', 'MX', 'NS', 'SRV', 'CAA'].map((t) => `<option>${t}</option>`).join('')}</select><input id="hsp-d-vals" placeholder="value(s), comma separated" style="flex:1;min-width:200px"><input id="hsp-d-ttl" value="300" size="6"><label><input type="checkbox" id="hsp-d-prox"> proxied (Cloudflare)</label><button class="btn" id="hsp-d-set">Set</button></div></div>
${recs ? `<table class="hsp-table" style="margin-top:10px"><tr><th>Name</th><th>Type</th><th>TTL</th><th>Values</th><th></th></tr>${recs.map((r) => `<tr><td class="hsp-mono">${E(r.name)}</td><td>${E(r.type)}</td><td>${E(r.ttl || '')}</td><td class="hsp-mono">${E(r.values.join('\n'))}${r.proxied ? ' <span class="hsp-chip">proxied</span>' : ''}</td><td><button class="btn ghost" data-edrec="${E(r.name)}|${E(r.type)}">Edit</button> <button class="btn ghost" data-rmrec="${E(r.name)}|${E(r.type)}">Delete</button></td></tr>`).join('')}</table>` : ''}`;
  }

  // ---- Tunnels & certificates ----
  function tunnelsTab() {
    const t = st.data.tunnels, cf = t.cloudflare;
    return `<div class="hsp-grid"><div class="hsp-card"><b>Cloudflare Tunnel</b>
<div>${cf.configured ? `tunnel <span class="hsp-mono">${E((cf.tunnel_id || '').slice(0, 8))}…</span> · ${chip(cf.process.running, 'cloudflared running', 'cloudflared stopped')}<br>routes: ${E((cf.hosts || []).join(', ') || 'none')}` : 'Not set up yet: choose Go live → Cloudflare Tunnel on a site.'}</div>
<div>cloudflared: ${cf.cloudflared ? `<span class="hsp-mono">${E(cf.cloudflared)}</span>` : 'not installed'}</div>
<div class="hsp-row">${cf.cloudflared ? '' : '<button class="btn" id="hsp-t-install">Install cloudflared</button>'}${cf.configured ? `<button class="btn ghost" id="hsp-t-run">${cf.process.running ? 'Restart' : 'Run'}</button><button class="btn ghost" id="hsp-t-stop" ${cf.process.running ? '' : 'disabled'}>Stop</button>` : ''}</div>
<div class="hsp-muted">Install downloads Cloudflare's own release from github.com/cloudflare/cloudflared into ABP's hosting folder.</div><div class="hsp-log" id="hsp-t-log" style="display:none"></div></div>
<div class="hsp-card"><b>Tailscale Funnel</b><div>${t.tailscale.name ? `this machine is <span class="hsp-mono">${E(t.tailscale.name)}</span>; Go live → Tailscale Funnel publishes the web server at https://${E(t.tailscale.name)}` : 'Tailscale is not installed or not signed in here.'}</div></div>
<div class="hsp-card"><b>Certificates</b>${st.data.certs.length ? `<table class="hsp-table">${st.data.certs.map((c) => `<tr><td class="hsp-mono">${E((c.sans || c.names || [c.folder]).join(', '))}</td><td>${chip((c.days_left || 0) > 14, `${c.days_left} days`, `${c.days_left} days`)}</td><td class="hsp-muted">${E((c.issuer || '').replace(/^.*?O=([^,]+).*$/, '$1'))}</td></tr>`).join('')}</table>` : '<div class="hsp-muted">None yet. Sites going live by port forward or direct get one automatically; renewals run 30 days before expiry.</div>'}
<div class="hsp-row"><input id="hsp-c-names" placeholder="example.com www.example.com (*.example.com needs DNS)" style="flex:1;min-width:220px"><select id="hsp-c-method"><option value="http-01">http-01 (port 80 here)</option><option value="dns-01">dns-01 (a connected DNS account)</option></select></div>
${st.data.settings.agreed_ca_terms ? '' : '<label class="hsp-row"><input type="checkbox" id="hsp-c-tos"> I agree to the certificate authority\'s terms of service</label>'}
<div class="hsp-row"><button class="btn" id="hsp-c-issue">Get certificate</button></div><div class="hsp-log" id="hsp-c-log" style="display:none"></div></div></div>`;
  }

  // ---- Servers ----
  async function serversTab() {
    const clouds = accountsWith('vps'), ssh = accountsWith('server');
    const S = st.srv;
    if (!S.account && clouds.length) S.account = clouds[0].id;
    let body = '';
    if (S.account) {
      try {
        const [opts, list] = await Promise.all([S.opts && S.opts.acc === S.account ? S.opts : api(`${A}/servers/${encodeURIComponent(S.account)}/options`).then((o) => Object.assign(o, { acc: S.account })), api(`${A}/servers/${encodeURIComponent(S.account)}`)]);
        S.opts = opts;
        const region = S.region || (opts.regions[0] || {}).id;
        const sizes = opts.sizes.filter((z) => (!z.region || z.region === region) && (!z.regions || !z.regions.length || z.regions.includes(region))).sort((a, b) => a.monthly - b.monthly).slice(0, 60);
        const size = sizes.find((z) => z.id === S.size) || sizes[0];
        body = `<div class="hsp-card"><b>Servers ABP created here</b>${list.length ? `<table class="hsp-table">${list.map((v) => `<tr><td><b>${E(v.name)}</b></td><td class="hsp-mono">${E(v.ip || '')}</td><td>${E(v.size)} · ${E(v.region)}</td><td>${E(v.status)}</td><td><button class="btn ghost" data-destroy="${E(v.id)}|${E(v.name)}">Destroy…</button></td></tr>`).join('')}</table>` : '<div class="hsp-muted">none</div>'}</div>
<div class="hsp-card"><b>Create a server</b><div class="hsp-form"><label>Name</label><input id="hsp-v-name" value="${E(S.name || 'web-1')}">
<label>Region</label><select id="hsp-v-region">${opts.regions.map((r) => `<option value="${E(r.id)}" ${r.id === region ? 'selected' : ''}>${E(r.name)} (${E(r.id)})</option>`).join('')}</select>
<label>Size</label><select id="hsp-v-size">${sizes.map((z) => `<option value="${E(z.id)}" ${size && z.id === size.id ? 'selected' : ''}>${E(z.id)} — ${E(z.cpus)} CPU, ${E(z.ram_gb)} GB, ${E(z.disk_gb)} GB disk — ${z.monthly.toFixed(2)}/month</option>`).join('')}</select></div>
${size ? `<label class="hsp-row"><input type="checkbox" id="hsp-v-ok"> I understand this costs about <b>${size.monthly.toFixed(2)}</b> a month, billed by the provider until I destroy it</label>` : ''}
<div class="hsp-row"><button class="btn" id="hsp-v-create" data-price="${size ? size.monthly : ''}">Create server</button></div>
<div class="hsp-muted">It boots with a user "abp" holding ABP's SSH key, Caddy for automatic HTTPS, and a firewall allowing SSH and web; it is added as an SSH account, ready for Go live → My server.</div><div class="hsp-log" id="hsp-v-log" style="display:none"></div></div>`;
      } catch (e) { body = `<p class="cardnote">${E(errText(e))}</p>`; }
    }
    return `<div class="hsp-row" style="margin-bottom:8px">${clouds.length ? `<select id="hsp-v-acc">${accOptions('vps', S.account)}</select>` : '<span class="hsp-muted">Connect Hetzner, DigitalOcean, Vultr or Linode on the Accounts tab to create servers.</span>'}</div>
<div class="hsp-grid">${body}<div class="hsp-card"><b>Your SSH servers</b>${ssh.length ? `<table class="hsp-table">${ssh.map((a) => `<tr><td><b>${E(a.name)}</b><br><span class="hsp-muted">${E(a.settings.user)}@${E(a.settings.host)}</span></td><td>${E(a.verify_note || '')}</td><td><button class="btn ghost" data-setup="${E(a.id)}">Install Caddy</button></td></tr>`).join('')}</table>` : '<div class="hsp-muted">Add a server you own (Accounts → Server over SSH) to deploy sites to it.</div>'}<div class="hsp-log" id="hsp-ssh-log" style="display:none"></div></div></div>`;
  }

  async function runInto(promise, logId, done) {
    const el = $(logId);
    if (el) { el.style.display = ''; el.textContent = ''; }
    try { const r = await follow(await promise, logId); if (r && r.error) toast(r.error, 'error'); else toast('Done'); if (done) done(r); }
    catch (e) { toast(errText(e), 'error'); if (el) el.textContent += `\n${errText(e)}`; }
  }

  function wire() {
    const root = $('hsp-root');
    root.querySelectorAll('[data-tab]').forEach((b) => b.addEventListener('click', () => { st.tab = b.dataset.tab; keep('hsp.tab', st.tab); render(); }));
    const on = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };
    const act = async (fn, okMsg) => { try { await fn(); if (okMsg) toast(okMsg); await load(); render(); } catch (e) { toast(errText(e), 'error'); } };
    // sites
    on('hsp-new', () => { st.form = 'new'; render(); });
    on('hsp-f-cancel', () => { st.form = null; render(); });
    const kindSel = $('hsp-f-kind');
    if (kindSel) { const sync = () => root.querySelectorAll('.k-static,.k-proxy,.k-redirect').forEach((el) => { el.style.display = el.classList.contains(`k-${kindSel.value}`) ? '' : 'none'; }); kindSel.addEventListener('change', sync); sync(); }
    on('hsp-f-save', (ev) => { const id = ev.target.dataset.id; act(async () => { const b = readForm(); if (id) await send(`${A}/sites/${encodeURIComponent(id)}`, 'PATCH', b); else await send(`${A}/sites`, 'POST', Object.fromEntries(Object.entries(b).filter(([, v]) => v !== null))); st.form = null; }, id ? 'Saved' : 'Site created'); });
    root.querySelectorAll('[data-edit]').forEach((b) => b.addEventListener('click', () => { st.form = b.dataset.edit; render(); }));
    root.querySelectorAll('[data-rm]').forEach((b) => b.addEventListener('click', () => { if (confirm(`Remove site ${b.dataset.rm}? Its files are not deleted.`)) act(() => send(`${A}/sites/${encodeURIComponent(b.dataset.rm)}`, 'DELETE'), 'Removed'); }));
    root.querySelectorAll('[data-check]').forEach((b) => b.addEventListener('click', () => checkSite(b.dataset.check)));
    root.querySelectorAll('[data-publish]').forEach((b) => b.addEventListener('click', () => { const out = $(`hsp-out-${b.dataset.publish}`); out.innerHTML = `<div class="hsp-log" id="hsp-p-${b.dataset.publish}"></div>`; runInto(send(`${A}/sites/${encodeURIComponent(b.dataset.publish)}/publish`, 'POST', {}), `hsp-p-${b.dataset.publish}`, () => load()); }));
    root.querySelectorAll('[data-live]').forEach((b) => b.addEventListener('click', async () => {
      const s = st.data.sites.find((x) => x.id === b.dataset.live) || {};
      st.live = { site: b.dataset.live, mode: (s.exposure || {}).mode || '', account: (s.exposure || {}).account || '', plan: null, log: '' };
      render();
      try { st.live.rec = await api(`${A}/network`); if (!st.live.mode) st.live.mode = st.live.rec.mode; } catch (_) { if (!st.live.mode) st.live.mode = 'cloudflare-tunnel'; }
      refreshPlan();
    }));
    root.querySelectorAll('[data-mode]').forEach((b) => b.addEventListener('click', () => { st.live.mode = b.dataset.mode; st.live.account = ''; refreshPlan(); }));
    const lacc = $('hsp-l-acc');
    if (lacc) lacc.addEventListener('change', () => { st.live.account = lacc.value; refreshPlan(); });
    on('hsp-l-close', () => { st.live = null; render(); });
    on('hsp-l-run', async () => {
      const L = st.live; const tos = $('hsp-l-tos');
      if (tos && !tos.checked) { toast('Agree to the certificate authority\'s terms to get a certificate', 'error'); return; }
      L.running = true; L.log = ''; L.result = null; render();
      try {
        const r = await follow(await send(`${A}/sites/${encodeURIComponent(L.site)}/go-live`, 'POST', { mode: L.mode, account: L.account, agree_ca_terms: tos ? tos.checked : undefined }), 'hsp-l-log');
        L.log = ($('hsp-l-log') || {}).textContent || ''; L.result = r && r.result; if (r && r.error) toast(r.error, 'error');
        toast(L.result && L.result.ok ? 'The site is live' : 'Some steps need attention', L.result && L.result.ok ? undefined : 'error');
      } catch (e) { toast(errText(e), 'error'); }
      L.running = false; await load(); render();
    });
    // network
    on('hsp-e-start', () => act(async () => { if (st.data.edge.running) await send(`${A}/edge/stop`, 'POST'); await send(`${A}/edge/start`, 'POST'); }, 'Web server started'));
    on('hsp-e-stop', () => act(() => send(`${A}/edge/stop`, 'POST'), 'Web server stopped'));
    root.querySelectorAll('[data-log]').forEach((b) => b.addEventListener('click', async () => { const el = $('hsp-e-log'); el.style.display = ''; try { el.textContent = (await api(`${A}/edge/log?name=${b.dataset.log}`)).text || '(empty)'; el.scrollTop = el.scrollHeight; } catch (e) { el.textContent = errText(e); } }));
    on('hsp-u-add', () => act(() => send(`${A}/upnp`, 'POST', { external_port: +$('hsp-u-port').value, internal_port: +($('hsp-u-to').value || $('hsp-u-port').value) }), 'Forwarded'));
    root.querySelectorAll('[data-unmap]').forEach((b) => b.addEventListener('click', () => { const [p, proto] = b.dataset.unmap.split('/'); act(() => send(`${A}/upnp?port=${p}&protocol=${proto}`, 'DELETE'), 'Removed'); }));
    on('hsp-s-save', () => act(() => send(`${A}/settings`, 'PUT', { engine: $('hsp-s-engine').value, http_port: +$('hsp-s-http').value, https_port: +$('hsp-s-https').value, bind: $('hsp-s-bind').value.trim(), acme_email: $('hsp-s-email').value.trim(), ca: $('hsp-s-ca').value, ddns: $('hsp-s-ddns').checked, autostart_edge: $('hsp-s-auto').checked }), 'Saved'));
    // accounts
    const prov = $('hsp-a-prov');
    if (prov) prov.addEventListener('change', () => { st.addProvider = prov.value; render(); });
    on('hsp-a-add', () => act(async () => {
      const values = {}; root.querySelectorAll('[data-field]').forEach((i) => { if (i.value.trim()) values[i.dataset.field] = i.value.trim(); });
      const r = await send(`${A}/accounts`, 'POST', { provider: st.addProvider, name: $('hsp-a-name').value.trim(), values });
      st.addProvider = ''; const v = r.verify || {}; toast(v.ok ? `Connected: ${v.note}` : `Connected, but it does not work yet: ${v.note}`, v.ok ? undefined : 'error');
    }));
    root.querySelectorAll('[data-verify]').forEach((b) => b.addEventListener('click', () => act(async () => { const v = await send(`${A}/accounts/${encodeURIComponent(b.dataset.verify)}/verify`, 'POST'); toast(v.note, v.ok ? undefined : 'error'); })));
    root.querySelectorAll('[data-unacc]').forEach((b) => b.addEventListener('click', () => { if (confirm('Disconnect this account? Its secrets are deleted from this machine.')) act(() => send(`${A}/accounts/${encodeURIComponent(b.dataset.unacc)}`, 'DELETE'), 'Disconnected'); }));
    // dns
    const dacc = $('hsp-d-acc'), dzone = $('hsp-d-zone');
    if (dacc) dacc.addEventListener('change', () => { st.dns = { account: dacc.value, zone: '' }; render(); });
    if (dzone) dzone.addEventListener('change', () => { st.dns.zone = dzone.value; render(); });
    on('hsp-d-reload', () => render());
    on('hsp-d-set', () => act(() => send(`${A}/dns/${encodeURIComponent(st.dns.account)}/records`, 'PUT', { zone: st.dns.zone, name: $('hsp-d-name').value.trim() || '@', type: $('hsp-d-type').value, values: $('hsp-d-vals').value, ttl: +$('hsp-d-ttl').value || 300, proxied: $('hsp-d-prox').checked || undefined }), 'Record set'));
    root.querySelectorAll('[data-rmrec]').forEach((b) => b.addEventListener('click', () => { const [n, t] = b.dataset.rmrec.split('|'); if (confirm(`Delete ${t} ${n}?`)) act(() => send(`${A}/dns/${encodeURIComponent(st.dns.account)}/records?zone=${encodeURIComponent(st.dns.zone)}&name=${encodeURIComponent(n)}&type=${t}`, 'DELETE'), 'Deleted'); }));
    root.querySelectorAll('[data-edrec]').forEach((b) => b.addEventListener('click', async () => { const [n, t] = b.dataset.edrec.split('|'); $('hsp-d-name').value = n; $('hsp-d-type').value = t; try { const recs = await api(`${A}/dns/${encodeURIComponent(st.dns.account)}/records?zone=${encodeURIComponent(st.dns.zone)}`); const r = recs.find((x) => x.name === n && x.type === t); if (r) { $('hsp-d-vals').value = r.values.join(', '); $('hsp-d-ttl').value = r.ttl || 300; } } catch (_) {} }));
    // tunnels, certs
    on('hsp-t-install', () => { if (confirm('Download cloudflared (Cloudflare\'s tunnel client, about 40 MB) from github.com/cloudflare/cloudflared?')) runInto(send(`${A}/tunnels/cloudflared/install`, 'POST'), 'hsp-t-log', async () => { await load(); render(); }); });
    on('hsp-t-run', () => act(() => send(`${A}/tunnels/cloudflare/run`, 'POST'), 'cloudflared running'));
    on('hsp-t-stop', () => act(() => send(`${A}/tunnels/cloudflare/stop`, 'POST'), 'Stopped'));
    on('hsp-c-issue', () => { const tos = $('hsp-c-tos'); if (tos && !tos.checked) { toast('Agree to the certificate authority\'s terms first', 'error'); return; } runInto(send(`${A}/certs`, 'POST', { names: $('hsp-c-names').value, method: $('hsp-c-method').value, agree_ca_terms: tos ? tos.checked : undefined }), 'hsp-c-log', async () => { await load(); }); });
    // servers
    const vacc = $('hsp-v-acc');
    if (vacc) vacc.addEventListener('change', () => { st.srv = { account: vacc.value }; render(); });
    const vreg = $('hsp-v-region'), vsize = $('hsp-v-size'), vname = $('hsp-v-name');
    if (vreg) vreg.addEventListener('change', () => { st.srv.region = vreg.value; st.srv.size = ''; st.srv.name = vname.value; render(); });
    if (vsize) vsize.addEventListener('change', () => { st.srv.size = vsize.value; st.srv.name = vname.value; render(); });
    on('hsp-v-create', (ev) => {
      if (!$('hsp-v-ok') || !$('hsp-v-ok').checked) { toast('Tick the box to confirm the monthly cost', 'error'); return; }
      runInto(send(`${A}/servers/${encodeURIComponent(st.srv.account)}`, 'POST', { name: vname.value.trim(), region: vreg.value, size: vsize.value, confirm_monthly: +ev.target.dataset.price }), 'hsp-v-log', async () => { await load(); render(); });
    });
    root.querySelectorAll('[data-destroy]').forEach((b) => b.addEventListener('click', () => { const [id, name] = b.dataset.destroy.split('|'); const typed = prompt(`Destroying ${name} deletes everything on it. Type its name to confirm:`); if (typed) act(() => send(`${A}/servers/${encodeURIComponent(st.srv.account)}/${encodeURIComponent(id)}?confirm=${encodeURIComponent(typed)}`, 'DELETE'), 'Destroyed'); }));
    root.querySelectorAll('[data-setup]').forEach((b) => b.addEventListener('click', () => { if (confirm('Install Caddy on this server (needs passwordless sudo there)?')) runInto(send(`${A}/servers/${encodeURIComponent(b.dataset.setup)}/setup`, 'POST'), 'hsp-ssh-log'); }));
  }

  async function render() {
    css();
    const root = $('hsp-root');
    if (!root || st.busy) return;
    st.busy = true;
    const tabs = [['sites', 'Sites'], ['network', 'Network & server'], ['accounts', 'Accounts'], ['dns', 'DNS'], ['tunnels', 'Tunnels & certificates'], ['servers', 'Servers']];
    const head = `<div class="hsp-tabs">${tabs.map(([k, l]) => `<button class="btn ghost ${st.tab === k ? 'on' : ''}" data-tab="${k}">${l}</button>`).join('')}</div>`;
    try {
      if (!st.data) await load();
      const html = st.tab === 'network' ? await networkTab() : st.tab === 'accounts' ? accountsTab() : st.tab === 'dns' ? await dnsTab() : st.tab === 'tunnels' ? tunnelsTab() : st.tab === 'servers' ? await serversTab() : sitesTab();
      root.innerHTML = head + html;
    } catch (e) { root.innerHTML = head + `<p class="cardnote">${E(errText(e))}</p>`; }
    st.busy = false;
    wire();
  }

  function init() {
    if (!$('hsp-root')) return;
    const onHash = () => { if ((location.hash || '').replace('#', '') === 'hosting') { st.data = null; render(); } };
    window.addEventListener('hashchange', onHash);
    onHash();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
