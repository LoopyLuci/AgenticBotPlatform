// Tailscale, Containers, Virtual Machines and Infra Automation pages. This file is identical in the dashboard
// (bot/dashboard/static/) and the desktop app (desktop-app/ui/); tests/test_infra_pages.py fails if the two differ.
// It uses each page's own api(), esc() and showToast(), and draws into #ts-root, #ct-root, #vm-root and #ir-root.
(function (pageApi) {
  'use strict';
  // Every infra request goes to the selected machine: "local" or a linked server's id (?host=<id>).
  let HOST = 'local';
  try { HOST = sessionStorage.getItem('infra-host') || 'local'; } catch (_) {}
  const HOSTED = /^\/api\/(docker|vms|tailscale|infra\/rules|terminals)/;
  const api = (path, opts) => pageApi(HOST !== 'local' && HOSTED.test(path)
    ? path + (path.includes('?') ? '&' : '?') + 'host=' + encodeURIComponent(HOST) : path, opts);
  const $ = (id) => document.getElementById(id);
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const J = (method, body) => ({ method, body: JSON.stringify(body || {}) });
  const fail = (e) => { let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} toast(m, 'error'); return null; };
  const errText = (e) => { let m = e && e.message ? e.message : String(e); try { m = JSON.parse(m).detail || m; } catch (_) {} return m; };
  const sure = (msg) => window.confirm(msg);
  const pre = (t) => `<pre class="infra-pre">${esc(typeof t === 'string' ? t : JSON.stringify(t, null, 2))}</pre>`;
  const btn = (act, label, attrs, cls) => `<button class="btn${cls === 'primary' ? ' primary' : ''}" style="padding:4px 10px;margin:1px" data-act="${act}" ${attrs || ''}>${esc(label)}</button>`;
  const table = (cols, rows) => rows.length
    ? `<div class="infra-scroll"><table class="infra-table"><thead><tr>${cols.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`
    : '<p class="cardnote">Nothing here yet.</p>';
  const field = (id, label, value, ph) => `<label class="infra-f"><span>${esc(label)}</span><input id="${id}" value="${esc(value || '')}" placeholder="${esc(ph || '')}"></label>`;
  const val = (id) => ($(id) ? $(id).value.trim() : '');
  const lines = (id) => val(id).split(/[\n,]+/).map(s => s.trim()).filter(Boolean);

  function css() {
    if ($('infra-css')) return;
    const s = document.createElement('style');
    s.id = 'infra-css';
    s.textContent = `.infra-scroll{overflow:auto;max-width:100%}.infra-table{width:100%;border-collapse:collapse;font-size:12.5px}
.infra-table th,.infra-table td{padding:6px 8px;border-bottom:1px solid var(--border,#3333);text-align:left;vertical-align:top}
.infra-pre{white-space:pre-wrap;max-height:340px;overflow:auto;font-size:12px;background:var(--surface-2,#0002);padding:8px;border-radius:6px}
.infra-tabs{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.infra-tabs button.on{outline:2px solid var(--accent,#5b8def)}
.infra-f{display:flex;flex-direction:column;gap:3px;font-size:12px;min-width:150px}.infra-row{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin:8px 0}
.infra-pill{padding:1px 8px;border-radius:10px;font-size:11px;background:var(--surface-2,#0003)}.infra-ok{color:var(--good,#2fa44f)}.infra-bad{color:var(--critical,#d1453b)}
.infra-warn{border-left:3px solid var(--critical,#d1453b);padding:6px 10px;margin:8px 0;font-size:12.5px}`;
    document.head.appendChild(s);
  }

  const pill = (t, ok) => `<span class="infra-pill ${ok === true ? 'infra-ok' : ok === false ? 'infra-bad' : ''}">${esc(t)}</span>`;

  // ================================================================= Tailscale
  const TS = { tab: 'overview', data: null };
  const TS_TABS = { overview: 'Overview', settings: 'Settings', serve: 'Serve & Funnel', peers: 'Peers', tools: 'Tools', admin: 'Tailnet admin' };
  const BOOL_LABELS = {
    accept_dns: 'Use Tailscale DNS', accept_routes: 'Accept subnet routes', advertise_exit_node: 'Offer this machine as an exit node',
    advertise_connector: 'Act as an app connector', auto_update: 'Update Tailscale automatically', exit_node_allow_lan_access: 'Allow LAN access while using an exit node',
    report_posture: 'Report device posture', shields_up: 'Shields up (block all incoming)', ssh: 'Tailscale SSH server', unattended: 'Keep running when logged out (Windows)',
    update_check: 'Notify about updates', webclient: 'Web admin client on port 5252',
  };
  const TEXT_PREFS = ['hostname', 'exit_node', 'advertise_routes', 'nickname'];

  async function tsLoad() {
    const root = $('ts-root'); if (!root) return;
    try { TS.data = await api('/api/tailscale/overview'); } catch (e) { root.innerHTML = `<p class="cardnote">${esc(errText(e))}</p>`; return; }
    tsRender();
  }
  function tsRender() {
    const root = $('ts-root'), d = TS.data;
    if (!d.installed) { root.innerHTML = '<div class="card"><p>Tailscale is not installed on this machine. Install it from tailscale.com/download, then reopen this page.</p></div>'; return; }
    const tabs = Object.entries(TS_TABS).map(([k, v]) => `<button class="btn${TS.tab === k ? ' primary' : ''}" data-act="ts-tab" data-tab="${k}">${v}</button>`).join('');
    root.innerHTML = `<div class="infra-tabs">${tabs}</div><div id="ts-body"></div>`;
    ({ overview: tsOverview, settings: tsSettings, serve: tsServe, peers: tsPeers, tools: tsTools, admin: tsAdmin })[TS.tab]();
  }
  function tsOverview() {
    const st = TS.data.status || {}, self = st.Self || {}, v = TS.data.version || {};
    const on = st.BackendState === 'Running';
    $('ts-body').innerHTML = `<div class="card"><h3>This machine ${pill(st.BackendState || 'unknown', on)}</h3>
      <p class="cardnote">${esc(self.DNSName || '')} &middot; ${esc((self.TailscaleIPs || []).join(', '))} &middot; Tailscale ${esc(v.majorMinorPatch || '')}
      ${st.CurrentTailnet ? `&middot; tailnet <b>${esc(st.CurrentTailnet.Name)}</b>` : ''}</p>
      <div class="infra-row">${on ? btn('ts-post', 'Disconnect', 'data-path="down"') : btn('ts-post', 'Connect', 'data-path="up"', 'primary')}
      ${btn('ts-post', 'Log out', 'data-path="logout" data-confirm="Log this machine out of Tailscale? You will need to sign in again."')}
      ${btn('ts-refresh', 'Refresh')}</div>
      ${(st.Health || []).length ? `<div class="infra-warn"><b>Health</b><br>${(st.Health || []).map(esc).join('<br>')}</div>` : ''}</div>`;
  }
  function tsSettings() {
    const p = TS.data.prefs || {}, schema = TS.data.prefs_schema || {};
    const cur = { accept_dns: p.CorpDNS, accept_routes: p.RouteAll, advertise_exit_node: (p.AdvertiseRoutes || []).some(r => r === '0.0.0.0/0'),
      shields_up: p.ShieldsUp, ssh: p.RunSSH, auto_update: (p.AutoUpdate || {}).Apply, update_check: (p.AutoUpdate || {}).Check,
      report_posture: p.PostureChecking, unattended: p.ForceDaemon, webclient: p.RunWebClient, advertise_connector: p.AppConnector && p.AppConnector.Advertise,
      exit_node_allow_lan_access: p.ExitNodeAllowLANAccess };
    const bools = Object.keys(schema).filter(k => schema[k] === 'bool').map(k =>
      `<label class="row" style="gap:8px;align-items:center"><input type="checkbox" data-act="ts-bool" data-key="${k}" ${cur[k] ? 'checked' : ''}> ${esc(BOOL_LABELS[k] || k)}</label>`).join('');
    const cur2 = { hostname: p.Hostname, exit_node: p.ExitNodeIP, advertise_routes: (p.AdvertiseRoutes || []).filter(r => r !== '0.0.0.0/0' && r !== '::/0').join(','), nickname: '' };
    $('ts-body').innerHTML = `<div class="card"><h3>Preferences</h3><div class="grid" style="gap:6px">${bools}</div></div>
      <div class="card"><h3>Values</h3><div class="infra-row">${TEXT_PREFS.map(k => field('tsp-' + k, k.replace(/_/g, ' '), cur2[k])).join('')}
      ${btn('ts-save-text', 'Apply', '', 'primary')}</div><p class="cardnote">Leave exit node empty to stop using one; <code>auto:any</code> picks the best. Advertise routes takes comma-separated CIDRs.</p></div>`;
  }
  function tsServe() {
    $('ts-body').innerHTML = `<div class="card"><h3>Published now</h3><div id="ts-serve-list"><p class="cardnote">Loading…</p></div></div>
      <div class="card"><h3>Publish something</h3>
      <div class="infra-row">${field('tss-target', 'What to publish', '', '3000, localhost:8080, http://127.0.0.1:8787')}
      <label class="infra-f"><span>Port</span><select id="tss-port"><option>443</option><option>8443</option><option>10000</option></select></label>
      <label class="infra-f"><span>Mode</span><select id="tss-mode"><option>https</option><option>http</option><option>tcp</option><option>tls-terminated-tcp</option></select></label>
      ${field('tss-path', 'Path (optional)', '', '/app')}
      <label class="infra-f"><span>Who can reach it</span><select id="tss-funnel"><option value="0">Only my tailnet (Serve)</option><option value="1">Anyone on the internet (Funnel)</option></select></label>
      ${btn('ts-serve-add', 'Publish', '', 'primary')}</div>
      <p class="cardnote">Funnel makes the target reachable by <b>anyone on the internet</b>. Only publish things that are safe to expose.</p></div>`;
    Promise.all([api('/api/tailscale/serve'), api('/api/tailscale/serve?funnel=true')]).then(([s]) => {
      const web = s.Web || {}, funnel = s.AllowFunnel || {}, rows = [];
      Object.entries(web).forEach(([host, w]) => Object.entries(w.Handlers || {}).forEach(([path, h]) =>
        rows.push([esc(host), esc(path), esc(h.Proxy || h.Path || h.Text || ''), funnel[host] ? pill('PUBLIC (Funnel)', false) : pill('tailnet only', true),
          btn('ts-serve-off', 'Stop', `data-host="${esc(host)}" data-path="${esc(path)}" data-funnel="${funnel[host] ? 1 : 0}"`)])));
      Object.entries(s.TCP || {}).forEach(([port, t]) => { if (t.TCPForward) rows.push([`:${esc(port)}`, 'tcp', esc(t.TCPForward), pill('tailnet only', true), btn('ts-serve-off', 'Stop', `data-tcp="${esc(port)}"`)]); });
      $('ts-serve-list').innerHTML = table(['Address', 'Path', 'Target', 'Reach', ''], rows) + `<div class="infra-row">${btn('ts-serve-reset', 'Reset all Serve', '', 'ghost')}${btn('ts-funnel-reset', 'Reset all Funnel', '', 'ghost')}</div>`;
    }).catch(fail);
  }
  function tsPeers() {
    const st = TS.data.status || {}, peers = Object.values(st.Peer || {});
    $('ts-body').innerHTML = `<div class="card"><h3>Machines on your tailnet (${peers.length})</h3>${table(['Name', 'IPs', 'OS', 'Status', 'Exit node', ''],
      peers.map(p => [esc(p.HostName), esc((p.TailscaleIPs || []).join(', ')), esc(p.OS || ''), pill(p.Online ? 'online' : 'offline', p.Online),
        p.ExitNodeOption ? (p.ExitNode ? pill('in use', true) : btn('ts-exit', 'Use', `data-ip="${esc((p.TailscaleIPs || [])[0])}"`)) : '',
        btn('ts-ping', 'Ping', `data-ip="${esc((p.TailscaleIPs || [])[0])}"`) + btn('ts-whois', 'Who', `data-ip="${esc((p.TailscaleIPs || [])[0])}"`)]))}
      ${TS.data.prefs && TS.data.prefs.ExitNodeIP ? btn('ts-exit-off', 'Stop using exit node') : ''}<div id="ts-peer-out"></div></div>`;
  }
  function tsTools() {
    $('ts-body').innerHTML = `<div class="card"><h3>Diagnostics</h3><div class="infra-row">
      ${['netcheck', 'metrics', 'dns-status', 'ips', 'accounts', 'lock', 'app-connector-routes', 'update-check'].map(k => btn('ts-get', k, `data-path="${k}"`)).join('')}</div></div>
      <div class="card"><h3>Send a file (Taildrop)</h3><div class="infra-row">${field('tsf-path', 'File path on this machine', '')}${field('tsf-target', 'To machine', '')}${btn('ts-file-send', 'Send', '', 'primary')}${btn('ts-file-recv', 'Receive waiting files here…')}</div></div>
      <div class="card"><h3>Share a folder (Drive)</h3><div class="infra-row">${field('tsd-name', 'Share name', '')}${field('tsd-path', 'Folder path', '')}${btn('ts-drive-share', 'Share', '', 'primary')}${btn('ts-get', 'List shares', 'data-path="drive"')}</div></div>
      <div class="card"><h3>HTTPS certificate</h3><div class="infra-row">${field('tsc-domain', 'Tailnet DNS name', (TS.data.status && TS.data.status.Self && (TS.data.status.Self.DNSName || '').replace(/\.$/, '')) || '')}${btn('ts-cert', 'Get certificate', '', 'primary')}</div></div>
      <div id="ts-tool-out"></div>`;
  }
  async function tsAdmin() {
    const b = $('ts-body');
    b.innerHTML = '<p class="cardnote">Loading…</p>';
    let devices;
    try { devices = await api('/api/tailscale/api/devices'); } catch (e) {
      const msg = e.message || '';
      if (/API key/i.test(msg)) {
        b.innerHTML = `<div class="card"><h3>Tailnet admin needs an API key</h3><p class="cardnote">Create one at <b>login.tailscale.com/admin/settings/keys</b> (an API access token), then paste it here. It is stored in ABP's private settings file and is never shown again.</p>
          <div class="infra-row">${field('tsk-key', 'API access token', '', 'tskey-api-…')}${field('tsk-tailnet', 'Tailnet (optional)', '', 'leave empty for your default')}${btn('ts-save-key', 'Save', '', 'primary')}</div></div>`;
      } else b.innerHTML = `<p class="cardnote">${esc(msg)}</p>`;
      return;
    }
    const list = devices.devices || [];
    b.innerHTML = `<div class="card"><h3>Devices (${list.length})</h3>${table(['Name', 'IPs', 'OS', 'Tags', 'Key expires', ''], list.map(d => [esc(d.name || d.hostname), esc((d.addresses || []).join(', ')), esc(d.os || ''),
      esc((d.tags || []).join(', ')), esc(d.keyExpiryDisabled ? 'never' : (d.expires || '').slice(0, 10)),
      (d.authorized ? '' : btn('ts-dev', 'Authorize', `data-id="${d.id}" data-op="authorize"`)) + btn('ts-dev', 'Expire key', `data-id="${d.id}" data-op="expire"`) + btn('ts-dev', 'Tags…', `data-id="${d.id}" data-op="tags"`) + btn('ts-dev', 'Delete', `data-id="${d.id}" data-op="delete"`)]))}</div>
      <div class="card"><h3>Auth keys</h3><div class="infra-row">${field('tak-desc', 'Description', '')}<label class="infra-f"><span>Options</span><select id="tak-opt"><option value="">One-time</option><option value="r">Reusable</option><option value="e">Ephemeral</option><option value="re">Reusable + ephemeral</option></select></label>${field('tak-tags', 'Tags', '', 'tag:server')}${btn('ts-key-create', 'Create key', '', 'primary')}${btn('ts-admin-get', 'List keys', 'data-path="keys"')}</div></div>
      <div class="card"><h3>Access controls (ACL)</h3><div class="infra-row">${btn('ts-acl-load', 'Load current policy')}${btn('ts-acl-validate', 'Validate')}${btn('ts-acl-save', 'Save', '', 'primary')}</div><textarea id="ts-acl" rows="14" style="width:100%;font-family:monospace" placeholder="Load the current policy to edit it"></textarea></div>
      <div class="card"><h3>DNS &amp; tailnet settings</h3><div class="infra-row">${btn('ts-admin-get', 'DNS', 'data-path="dns"')}${btn('ts-admin-get', 'Tailnet settings', 'data-path="settings"')}${btn('ts-admin-get', 'Users', 'data-path="users"')}${btn('ts-admin-get', 'Webhooks', 'data-path="webhooks"')}</div></div><div id="ts-admin-out"></div>`;
  }

  async function tsAct(el) {
    const act = el.dataset.act;
    const post = async (path, body, okMsg) => { try { const r = await api('/api/tailscale/' + path, J('POST', body)); toast(okMsg || 'Done', 'success'); return r; } catch (e) { return fail(e); } };
    const out = (id, r) => { const o = $(id); if (o && r) o.innerHTML = pre(r.output || r.error || r); };
    if (act === 'ts-tab') { TS.tab = el.dataset.tab; tsRender(); }
    else if (act === 'ts-refresh') tsLoad();
    else if (act === 'ts-post') {
      if (el.dataset.confirm && !sure(el.dataset.confirm)) return;
      const r = await post(el.dataset.path, {}, 'Done');
      if (r && r.output && /https:\/\//.test(r.output)) toast(r.output, 'info');
      tsLoad();
    } else if (act === 'ts-bool') {
      if ((el.dataset.key === 'advertise_exit_node' || el.dataset.key === 'ssh') && el.checked && !sure('This can change how this machine is reachable. Continue?')) { el.checked = false; return; }
      await post('prefs', { [el.dataset.key]: el.checked }, 'Setting saved'); tsLoad();
    } else if (act === 'ts-save-text') {
      const changes = {}; TEXT_PREFS.forEach(k => { const v = $('tsp-' + k).value.trim(); if (v !== '' || k === 'exit_node' || k === 'advertise_routes') changes[k] = v; });
      await post('prefs', changes, 'Saved'); tsLoad();
    } else if (act === 'ts-serve-add') {
      const funnel = val('tss-funnel') === '1';
      if (funnel && !sure('Funnel exposes this to the entire internet. Publish it publicly?')) return;
      await post('serve', { target: val('tss-target'), port: +val('tss-port'), mode: val('tss-mode'), path: val('tss-path') || null, funnel }, funnel ? 'Published on the internet' : 'Published to your tailnet'); tsServe();
    } else if (act === 'ts-serve-off') {
      const funnel = el.dataset.funnel === '1', hostPort = (el.dataset.host || '').split(':').pop();
      await post('serve/off', el.dataset.tcp ? { mode: 'tcp', port: +el.dataset.tcp } : { funnel, port: +(hostPort || 443), path: el.dataset.path === '/' ? null : el.dataset.path }, 'Stopped'); tsServe();
    } else if (act === 'ts-serve-reset' || act === 'ts-funnel-reset') {
      if (!sure('Remove every published entry?')) return;
      await post('serve/reset', { funnel: act === 'ts-funnel-reset' }, 'Reset'); tsServe();
    } else if (act === 'ts-exit') { await post('prefs', { exit_node: el.dataset.ip }, 'Exit node set'); tsLoad(); }
    else if (act === 'ts-exit-off') { await post('prefs', { exit_node: '' }, 'Exit node cleared'); tsLoad(); }
    else if (act === 'ts-ping') { try { out('ts-peer-out', await api('/api/tailscale/ping?target=' + encodeURIComponent(el.dataset.ip))); } catch (e) { fail(e); } }
    else if (act === 'ts-whois') { try { out('ts-peer-out', await api('/api/tailscale/whois?address=' + encodeURIComponent(el.dataset.ip))); } catch (e) { fail(e); } }
    else if (act === 'ts-get') { try { out('ts-tool-out', await api('/api/tailscale/' + el.dataset.path)); } catch (e) { fail(e); } }
    else if (act === 'ts-file-send') out('ts-tool-out', await post('file/send', { paths: [val('tsf-path')], target: val('tsf-target') }, 'Sent'));
    else if (act === 'ts-file-recv') { const d = window.prompt('Save received files into which folder?'); if (d) out('ts-tool-out', await post('file/receive', { directory: d }, 'Received')); }
    else if (act === 'ts-drive-share') out('ts-tool-out', await post('drive/share', { name: val('tsd-name'), path: val('tsd-path') }, 'Shared'));
    else if (act === 'ts-cert') out('ts-tool-out', await post('cert', { domain: val('tsc-domain') }, 'Certificate ready'));
    else if (act === 'ts-save-key') {
      try { await api('/api/env/set', J('POST', { key: 'TAILSCALE_API_KEY', value: val('tsk-key') })); if (val('tsk-tailnet')) await api('/api/env/set', J('POST', { key: 'TAILSCALE_TAILNET', value: val('tsk-tailnet') })); toast('Key saved', 'success'); tsAdmin(); } catch (e) { fail(e); }
    } else if (act === 'ts-dev') {
      const id = el.dataset.id, op = el.dataset.op;
      try {
        if (op === 'delete') { if (!sure('Remove this device from the tailnet?')) return; await api('/api/tailscale/api/devices/' + id, { method: 'DELETE' }); }
        else if (op === 'tags') { const t = window.prompt('Tags, comma separated (e.g. tag:server)'); if (t === null) return; await api(`/api/tailscale/api/devices/${id}/tags`, J('POST', { tags: t.split(',').map(s => s.trim()).filter(Boolean) })); }
        else await api(`/api/tailscale/api/devices/${id}/${op}`, J('POST', {}));
        toast('Done', 'success'); tsAdmin();
      } catch (e) { fail(e); }
    } else if (act === 'ts-key-create') {
      const o = val('tak-opt');
      try {
        const r = await api('/api/tailscale/api/keys', J('POST', { description: val('tak-desc'), reusable: o.includes('r'), ephemeral: o.includes('e'), tags: lines('tak-tags') }));
        $('ts-admin-out').innerHTML = `<div class="card"><h3>New key</h3><p class="cardnote">Copy it now - Tailscale never shows it again.</p>${pre(r.key || r)}</div>`;
      } catch (e) { fail(e); }
    } else if (act === 'ts-admin-get') { try { $('ts-admin-out').innerHTML = pre(await api('/api/tailscale/api/' + el.dataset.path)); } catch (e) { fail(e); } }
    else if (act === 'ts-acl-load') { try { $('ts-acl').value = JSON.stringify(await api('/api/tailscale/api/acl'), null, 2); } catch (e) { fail(e); } }
    else if (act === 'ts-acl-validate' || act === 'ts-acl-save') {
      let policy; try { policy = JSON.parse($('ts-acl').value); } catch (_) { toast('The policy is not valid JSON', 'error'); return; }
      if (act === 'ts-acl-save' && !sure('Replace the tailnet access policy? A wrong policy can lock machines out.')) return;
      try { const r = await api(act === 'ts-acl-save' ? '/api/tailscale/api/acl' : '/api/tailscale/api/acl/validate', J('POST', policy)); toast(act === 'ts-acl-save' ? 'Policy saved' : 'Policy is valid', 'success'); $('ts-admin-out').innerHTML = pre(r); } catch (e) { fail(e); }
    }
  }

  // ================================================================= Containers
  const CT = { tab: 'containers', info: null };
  const CT_TABS = { containers: 'Containers', images: 'Images', volumes: 'Volumes', networks: 'Networks', stacks: 'Stacks', templates: 'App templates', registries: 'Registries', system: 'System' };
  async function ctLoad() {
    const root = $('ct-root'); if (!root) return;
    try { CT.info = await api('/api/docker/info'); } catch (e) { root.innerHTML = `<p class="cardnote">${esc(errText(e))}</p>`; return; }
    ctRender();
  }
  function ctRender() {
    const root = $('ct-root'), i = CT.info;
    if (!i.installed) { root.innerHTML = '<div class="card"><p>Docker is not installed on this machine.</p></div>'; return; }
    const banner = i.running ? `<p class="cardnote">${pill('Docker ' + esc(i.server_version), true)} ${i.running_containers}/${i.containers} containers running &middot; ${i.images} images &middot; ${i.cpus} CPUs</p>`
      : `<div class="infra-warn"><b>Docker isn't responding.</b> Start Docker Desktop (or the Docker service) and press Refresh. ${esc(i.error || '')} ${btn('ct-refresh', 'Refresh')}</div>`;
    const tabs = Object.entries(CT_TABS).map(([k, v]) => `<button class="btn${CT.tab === k ? ' primary' : ''}" data-act="ct-tab" data-tab="${k}">${v}</button>`).join('');
    root.innerHTML = `${banner}<div class="infra-tabs">${tabs}</div><div id="ct-body"></div><div id="ct-out"></div>`;
    if (i.running) ctTab();
  }
  const short = (s) => String(s || '').replace(/^sha256:/, '').slice(0, 12);
  async function ctTab() {
    const b = $('ct-body'); b.innerHTML = '<p class="cardnote">Loading…</p>';
    try {
      const t = CT.tab;
      if (t === 'containers') {
        const rows = await api('/api/docker/containers');
        b.innerHTML = `<div class="infra-row">${btn('ct-new', 'Deploy a container', '', 'primary')}${btn('ct-tab-reload', 'Refresh')}</div>` + table(['Name', 'Image', 'State', 'Ports', ''], rows.map(c => {
          const n = esc(c.Names), run = c.State === 'running';
          return [n, esc(c.Image), pill(c.Status, run), esc(c.Ports || ''), (run ? btn('ct-c', 'Stop', `data-id="${n}" data-op="stop"`) + btn('ct-c', 'Restart', `data-id="${n}" data-op="restart"`) : btn('ct-c', 'Start', `data-id="${n}" data-op="start"`))
            + (run ? btn('ct-shell', 'Shell', `data-id="${n}"`, 'primary') : '') + btn('ct-logs', 'Logs', `data-id="${n}"`) + btn('ct-inspect', 'Inspect', `data-id="${n}"`) + btn('ct-stats', 'Stats', `data-id="${n}"`) + btn('ct-exec', 'Run command', `data-id="${n}"`) + btn('ct-c', 'Remove', `data-id="${n}" data-op="remove"`)];
        }));
      } else if (t === 'images') {
        const rows = await api('/api/docker/images');
        b.innerHTML = `<div class="infra-row">${field('ci-ref', 'Pull image', '', 'nginx:stable')}${btn('ct-pull', 'Pull', '', 'primary')}${field('ci-q', 'Search Docker Hub', '', 'redis')}${btn('ct-search', 'Search')}${btn('ct-build', 'Build from folder…')}</div>` +
          table(['Repository', 'Tag', 'ID', 'Size', 'Created', ''], rows.map(i => [esc(i.Repository), esc(i.Tag), esc(short(i.ID)), esc(i.Size), esc(i.CreatedSince), btn('ct-img', 'Remove', `data-ref="${esc(i.ID)}" data-op="remove"`) + btn('ct-img', 'History', `data-ref="${esc(i.ID)}" data-op="history"`)]));
      } else if (t === 'volumes') {
        const rows = await api('/api/docker/volumes');
        b.innerHTML = `<div class="infra-row">${field('cv-name', 'New volume', '')}${btn('ct-vol-create', 'Create', '', 'primary')}</div>` + table(['Name', 'Driver', 'Scope', ''], rows.map(v => [esc(v.Name), esc(v.Driver), esc(v.Scope), btn('ct-vol-rm', 'Remove', `data-name="${esc(v.Name)}"`)]));
      } else if (t === 'networks') {
        const rows = await api('/api/docker/networks');
        b.innerHTML = `<div class="infra-row">${field('cn-name', 'New network', '')}${field('cn-subnet', 'Subnet (optional)', '', '172.30.0.0/16')}${btn('ct-net-create', 'Create', '', 'primary')}</div>` + table(['Name', 'Driver', 'Scope', ''], rows.map(n => [esc(n.Name), esc(n.Driver), esc(n.Scope), btn('ct-net-rm', 'Remove', `data-name="${esc(n.Name)}"`)]));
      } else if (t === 'stacks') {
        const rows = await api('/api/docker/stacks');
        b.innerHTML = table(['Stack', 'Status', 'Config', ''], rows.map(s => [esc(s.Name), esc(s.Status), esc(s.ConfigFiles || ''),
          ['up', 'stop', 'restart', 'pull', 'down'].map(a => btn('ct-stack', a, `data-name="${esc(s.Name)}" data-op="${a}"`)).join('') + btn('ct-stack-edit', 'Edit', `data-name="${esc(s.Name)}"`)])) +
          `<div class="card"><h3>Deploy a Compose stack</h3><div class="infra-row">${field('cs-name', 'Stack name', '', 'my-app')}${btn('ct-stack-deploy', 'Deploy', '', 'primary')}</div><textarea id="cs-yaml" rows="12" style="width:100%;font-family:monospace" placeholder="services:\n  web:\n    image: nginx:stable\n    ports: ['8080:80']"></textarea></div>`;
      } else if (t === 'templates') {
        const rows = await api('/api/docker/templates');
        b.innerHTML = table(['App', 'Image', 'Ports', ''], rows.map(x => [esc(x.title), esc(x.image), esc((x.ports || []).join(', ')), btn('ct-tpl', 'Deploy', `data-id="${esc(x.id)}"`, 'primary')]));
      } else if (t === 'registries') {
        const rows = await api('/api/docker/registries');
        b.innerHTML = table(['Registry with saved login', ''], rows.map(r => [esc(r), btn('ct-logout', 'Log out', `data-server="${esc(r)}"`)])) +
          `<div class="card"><h3>Log in to a registry</h3><div class="infra-row">${field('cr-server', 'Registry', '', 'ghcr.io')}${field('cr-user', 'Username', '')}<label class="infra-f"><span>Password / token</span><input id="cr-pass" type="password" autocomplete="off"></label>${btn('ct-login', 'Log in', '', 'primary')}</div></div>`;
      } else if (t === 'system') {
        const [df, ev] = await Promise.all([api('/api/docker/df'), api('/api/docker/events?since=1h&limit=50')]);
        b.innerHTML = `<div class="card"><h3>Disk usage</h3>${table(['Type', 'Total', 'Active', 'Size', 'Reclaimable'], df.map(d => [esc(d.Type), esc(d.TotalCount), esc(d.Active), esc(d.Size), esc(d.Reclaimable)]))}
          <div class="infra-row">${['container', 'image', 'volume', 'network', 'builder'].map(k => btn('ct-prune', 'Prune ' + k + 's', `data-kind="${k}"`)).join('')}</div></div>
          <div class="card"><h3>Recent events</h3>${table(['Time', 'Type', 'Action', 'Object'], ev.slice().reverse().map(e => [esc(new Date((e.time || 0) * 1000).toLocaleTimeString()), esc(e.Type), esc(e.Action), esc((e.Actor && e.Actor.Attributes && e.Actor.Attributes.name) || '')]))}</div>`;
      }
    } catch (e) { b.innerHTML = `<p class="cardnote">${esc(e.message)}</p>`; }
  }
  async function ctAct(el) {
    const act = el.dataset.act, out = (r) => { $('ct-out').innerHTML = pre(r.logs || r.output || r.processes || r); };
    const call = async (path, method, body, msg) => { try { const r = await api('/api/docker/' + path, method === 'GET' ? {} : method === 'DELETE' ? { method } : J(method, body)); if (msg) toast(msg, 'success'); return r; } catch (e) { return fail(e); } };
    if (act === 'ct-tab') { CT.tab = el.dataset.tab; ctRender(); }
    else if (act === 'ct-refresh') ctLoad();
    else if (act === 'ct-tab-reload') ctTab();
    else if (act === 'ct-c') {
      if (el.dataset.op === 'remove' && !sure(`Remove container ${el.dataset.id}? Its writable layer is deleted.`)) return;
      await call(`containers/${el.dataset.id}/action`, 'POST', { action: el.dataset.op }, 'Done'); ctTab();
    } else if (act === 'ct-shell') openTerm('container', el.dataset.id, 'Shell in ' + el.dataset.id);
    else if (act === 'ct-logs') { const r = await call(`containers/${el.dataset.id}/logs?tail=300`, 'GET'); if (r) out(r); }
    else if (act === 'ct-inspect') { const r = await call(`containers/${el.dataset.id}`, 'GET'); if (r) out(r); }
    else if (act === 'ct-stats') { const r = await call(`containers/${el.dataset.id}/stats`, 'GET'); if (r) out(r); }
    else if (act === 'ct-exec') {
      const c = window.prompt('Command to run inside ' + el.dataset.id + ' (one line, split on spaces):'); if (!c) return;
      const r = await call(`containers/${el.dataset.id}/exec`, 'POST', { command: c.trim().split(/\s+/) }); if (r) out(r);
    } else if (act === 'ct-new') {
      $('ct-body').insertAdjacentHTML('afterbegin', `<div class="card"><h3>Deploy a container</h3><div class="infra-row">${field('cn-image', 'Image', '', 'nginx:stable')}${field('cn-cname', 'Name', '')}${field('cn-ports', 'Ports', '', '8080:80')}${field('cn-env', 'Environment', '', 'KEY=value')}${field('cn-vols', 'Volumes', '', 'data:/data')}
        <label class="infra-f"><span>Restart</span><select id="cn-restart"><option>unless-stopped</option><option>always</option><option>no</option><option>on-failure</option></select></label>${btn('ct-deploy', 'Deploy', '', 'primary')}</div></div>`);
    } else if (act === 'ct-deploy') {
      const r = await call('containers', 'POST', { image: val('cn-image'), name: val('cn-cname') || null, ports: lines('cn-ports'), env: lines('cn-env'), volumes: lines('cn-vols'), restart: val('cn-restart') }, 'Container started'); if (r) ctTab();
    } else if (act === 'ct-pull') { toast('Pulling…', 'info'); if (await call('images/pull', 'POST', { ref: val('ci-ref') }, 'Image pulled')) ctTab(); }
    else if (act === 'ct-search') { const r = await call('images/search?term=' + encodeURIComponent(val('ci-q')), 'GET'); if (r) $('ct-out').innerHTML = table(['Name', 'Stars', 'Official', 'Description'], r.map(x => [esc(x.Name), esc(x.StarCount), esc(x.IsOfficial), esc(x.Description)])); }
    else if (act === 'ct-build') { const d = window.prompt('Folder containing a Dockerfile:'); const t = d && window.prompt('Tag for the new image (e.g. myapp:latest):'); if (d && t) { toast('Building…', 'info'); const r = await call('images/build', 'POST', { context: d, tag: t }, 'Built'); if (r) out(r); } }
    else if (act === 'ct-img') {
      if (el.dataset.op === 'history') { const r = await call('images/history?ref=' + encodeURIComponent(el.dataset.ref), 'GET'); if (r) $('ct-out').innerHTML = pre(r); }
      else if (sure('Remove this image?')) { await call('images/remove', 'POST', { ref: el.dataset.ref }, 'Removed'); ctTab(); }
    } else if (act === 'ct-vol-create') { if (await call('volumes', 'POST', { name: val('cv-name') }, 'Volume created')) ctTab(); }
    else if (act === 'ct-vol-rm') { if (sure('Delete this volume and its data?')) { await call('volumes/' + encodeURIComponent(el.dataset.name), 'DELETE', null, 'Removed'); ctTab(); } }
    else if (act === 'ct-net-create') { if (await call('networks', 'POST', { name: val('cn-name'), subnet: val('cn-subnet') || null }, 'Network created')) ctTab(); }
    else if (act === 'ct-net-rm') { if (sure('Remove this network?')) { await call('networks/' + encodeURIComponent(el.dataset.name), 'DELETE', null, 'Removed'); ctTab(); } }
    else if (act === 'ct-stack') {
      if (['down'].includes(el.dataset.op) && !sure('Stop and remove this stack\'s containers?')) return;
      toast('Working…', 'info'); const r = await call(`stacks/${el.dataset.name}/action`, 'POST', { action: el.dataset.op }); if (r) { out(r); ctTab(); }
    } else if (act === 'ct-stack-edit') { const r = await call('stacks/' + el.dataset.name, 'GET'); if (r) { $('cs-name').value = r.name; $('cs-yaml').value = r.compose; } }
    else if (act === 'ct-stack-deploy') { toast('Deploying…', 'info'); const r = await call('stacks', 'POST', { name: val('cs-name'), compose: $('cs-yaml').value }); if (r) { out(r); ctTab(); } }
    else if (act === 'ct-tpl') { toast('Deploying…', 'info'); if (await call(`templates/${el.dataset.id}/deploy`, 'POST', {}, 'Deployed')) { CT.tab = 'containers'; ctRender(); } }
    else if (act === 'ct-login') { if (await call('registries/login', 'POST', { server: val('cr-server'), username: val('cr-user'), password: $('cr-pass').value }, 'Logged in')) { $('cr-pass').value = ''; ctTab(); } }
    else if (act === 'ct-logout') { await call('registries/logout', 'POST', { server: el.dataset.server }, 'Logged out'); ctTab(); }
    else if (act === 'ct-prune') { if (sure('Prune unused ' + el.dataset.kind + 's? This cannot be undone.')) { const r = await call('prune', 'POST', { kind: el.dataset.kind }); if (r) { out(r); ctTab(); } } }
  }

  // ================================================================= Virtual machines
  const VM = { backends: null, vms: null };
  async function vmLoad() {
    const root = $('vm-root'); if (!root) return;
    try { [VM.backends, VM.vms] = await Promise.all([api('/api/vms/backends'), api('/api/vms')]); } catch (e) { root.innerHTML = `<p class="cardnote">${esc(errText(e))}</p>`; return; }
    const B = VM.backends, q = B.qemu;
    const qrows = Array.isArray(VM.vms.qemu) ? VM.vms.qemu : [];
    root.innerHTML = `<p class="cardnote">${pill('QEMU ' + (q.available ? 'ready' : 'not installed'), q.available)} ${q.accelerators && q.accelerators.length ? pill('accelerators: ' + q.accelerators.join(', ')) : ''} ${pill('Hyper-V ' + (B.hyperv.available ? 'ready' : 'unavailable'), B.hyperv.available)} ${pill('libvirt ' + (B.libvirt.available ? 'ready' : 'not installed'), B.libvirt.available)}</p>
      <div class="card"><h3>QEMU machines</h3>${q.available ? table(['Name', 'CPUs', 'Memory', 'State', ''], qrows.map(v => {
        const n = esc(v.name), on = v.running;
        return [n, esc(v.cpus), esc(v.memory), pill(v.state, on), (on ? btn('vm-q', 'Shut down', `data-name="${n}" data-op="stop"`) + btn('vm-q', 'Pause', `data-name="${n}" data-op="pause"`) + btn('vm-q', 'Resume', `data-name="${n}" data-op="resume"`) + btn('vm-q', 'Reset', `data-name="${n}" data-op="reset"`) + btn('vm-q', 'Force off', `data-name="${n}" data-op="force"`) : btn('vm-q', 'Start', `data-name="${n}" data-op="start"`, 'primary') + btn('vm-del', 'Delete', `data-name="${n}"`))
            + (on ? btn('vm-console', 'Console', `data-name="${n}"`, 'primary') + btn('vm-monitor', 'Monitor', `data-name="${n}"`) : '') + btn('vm-snap', 'Snapshots', `data-name="${n}"`) + btn('vm-detail', 'Details', `data-name="${n}"`)];
      })) : '<p class="cardnote">Install QEMU (for example <code>scoop install qemu</code> on Windows) to run virtual machines.</p>'}
      ${q.available ? `<div class="infra-row">${btn('vm-new', 'Create a machine', '', 'primary')}${btn('vm-disk', 'Disk images…')}${btn('vm-refresh', 'Refresh')}</div>` : ''}<div id="vm-form"></div></div>
      ${Array.isArray(VM.vms.hyperv) ? `<div class="card"><h3>Hyper-V machines</h3>${table(['Name', 'State', 'CPUs', 'Generation', ''], VM.vms.hyperv.map(v => [esc(v.Name), pill(HV_STATE[v.State] || v.State, v.State === 2), esc(v.ProcessorCount), esc(v.Generation),
        ['start', 'stop', 'force-stop', 'pause', 'resume', 'checkpoint'].map(a => btn('vm-hv', a, `data-name="${esc(v.Name)}" data-op="${a}"`)).join('')]))}</div>` : (B.hyperv.available && VM.vms.hyperv ? `<div class="infra-warn">Hyper-V: ${esc(VM.vms.hyperv.error || '')}</div>` : '')}
      ${Array.isArray(VM.vms.libvirt) ? `<div class="card"><h3>libvirt machines</h3>${table(['Name', 'State', ''], VM.vms.libvirt.map(v => [esc(v.name), pill(v.state, v.running), ['start', 'stop', 'force-stop', 'pause', 'resume', 'autostart'].map(a => btn('vm-lv', a, `data-name="${esc(v.name)}" data-op="${a}"`)).join('') + (v.running ? btn('vm-lv-console', 'Console', `data-name="${esc(v.name)}"`, 'primary') : '')]))}</div>` : ''}
      <div id="vm-out"></div>`;
  }
  const HV_STATE = { 2: 'Running', 3: 'Off', 6: 'Saved', 9: 'Paused' };
  async function vmAct(el) {
    const act = el.dataset.act, name = el.dataset.name;
    const call = async (path, method, body, msg) => { try { const r = await api('/api/vms/' + path, method === 'GET' ? {} : method === 'DELETE' ? { method } : J(method, body)); if (msg) toast(msg, 'success'); return r; } catch (e) { return fail(e); } };
    const out = (r) => { $('vm-out').innerHTML = pre(r.output || r); };
    if (act === 'vm-refresh') vmLoad();
    else if (act === 'vm-q') {
      const op = el.dataset.op;
      if (op === 'force') { if (!sure('Force the machine off? Unsaved work in the guest is lost.')) return; await call(`qemu/${name}/stop`, 'POST', { force: true }, 'Stopped'); }
      else await call(`qemu/${name}/${op}`, 'POST', {}, 'Done');
      vmLoad();
    } else if (act === 'vm-del') { if (sure(`Delete the definition of ${name}? Disk images are kept.`)) { await call('qemu/' + name, 'DELETE', null, 'Deleted'); vmLoad(); } }
    else if (act === 'vm-console') openTerm('vm-serial', name, 'Serial console: ' + name);
    else if (act === 'vm-monitor') openTerm('vm-monitor', name, 'QEMU monitor: ' + name);
    else if (act === 'vm-detail') { const r = await call('qemu/' + name, 'GET'); if (r) out(r); }
    else if (act === 'vm-snap') {
      const r = await call(`qemu/${name}/snapshot`, 'POST', { action: 'list' }); if (r) out(r);
      $('vm-out').insertAdjacentHTML('beforeend', `<div class="infra-row">${field('vs-tag', 'Snapshot name', '')}${btn('vm-snap-do', 'Create', `data-name="${esc(name)}" data-op="create"`, 'primary')}${btn('vm-snap-do', 'Restore', `data-name="${esc(name)}" data-op="restore"`)}${btn('vm-snap-do', 'Delete', `data-name="${esc(name)}" data-op="delete"`)}</div>`);
    } else if (act === 'vm-snap-do') { const r = await call(`qemu/${name}/snapshot`, 'POST', { action: el.dataset.op, tag: val('vs-tag') }, 'Done'); if (r) out(r); }
    else if (act === 'vm-new') {
      $('vm-form').innerHTML = `<div class="infra-row">${field('vn-name', 'Name', '')}${field('vn-cpus', 'CPUs', '2')}${field('vn-mem', 'Memory', '2048M')}${field('vn-disk', 'New disk size', '40G')}${field('vn-iso', 'Install ISO path (optional)', '')}${field('vn-fwd', 'Port forward (optional)', '', 'tcp::2222-:22')}
        <label class="infra-f"><span>Display</span><select id="vn-disp"><option>vnc</option><option>sdl</option><option>gtk</option><option>none</option></select></label>${btn('vm-create', 'Create', '', 'primary')}</div>`;
    } else if (act === 'vm-create') {
      const n = val('vn-name'), size = val('vn-disk');
      const dirs = await call('paths', 'GET'); if (!dirs) return;
      const disk = dirs.disks + '/' + n + '.qcow2';
      if (size && await call('disks/create', 'POST', { path: disk, size, format: 'qcow2' })) {
        const iso = val('vn-iso'), fwd = val('vn-fwd');
        const r = await call('qemu', 'POST', { name: n, cpus: +val('vn-cpus'), memory: val('vn-mem'), disks: [{ path: disk }], cdrom: iso || null, boot: iso ? 'cdrom' : 'disk', display: val('vn-disp'), nics: [{ model: 'virtio-net-pci', mode: 'user', hostfwd: fwd ? [fwd] : [] }] }, 'Machine created');
        if (r) vmLoad();
      }
    } else if (act === 'vm-disk') {
      $('vm-form').innerHTML = `<div class="infra-row">${field('vd-path', 'Image path', '')}${field('vd-size', 'Size', '', '+10G or 40G')}${btn('vm-disk-do', 'Info', 'data-op="info"')}${btn('vm-disk-do', 'Resize', 'data-op="resize"')}${btn('vm-disk-do', 'Check', 'data-op="check"')}${field('vd-dest', 'Convert to path', '')}${btn('vm-disk-do', 'Convert to qcow2', 'data-op="convert"')}</div>`;
    } else if (act === 'vm-disk-do') {
      const op = el.dataset.op, p = val('vd-path');
      const body = op === 'convert' ? { source: p, dest: val('vd-dest'), format: 'qcow2', compress: true } : op === 'resize' ? { path: p, size: val('vd-size') } : { path: p };
      const r = await call('disks/' + op, 'POST', body, 'Done'); if (r) out(r);
    } else if (act === 'vm-hv') {
      if (['force-stop', 'delete'].includes(el.dataset.op) && !sure('Are you sure?')) return;
      await call(`hyperv/${name}/${el.dataset.op}`, 'POST', {}, 'Done'); vmLoad();
    } else if (act === 'vm-lv-console') openTerm('libvirt', name, 'libvirt console: ' + name);
    else if (act === 'vm-lv') { await call(`libvirt/${name}/${el.dataset.op}`, 'POST', {}, 'Done'); vmLoad(); }
  }

  // ================================================================= Automation rules
  async function irLoad() {
    const root = $('ir-root'); if (!root) return;
    let rules; try { rules = await api('/api/infra/rules'); } catch (e) { root.innerHTML = `<p class="cardnote">${esc(e.message)}</p>`; return; }
    const desc = (r) => { const t = r.trigger, a = r.action;
      const tt = t.type === 'interval' ? `every ${t.every}` : `when ${t.resource}${t.target ? ' ' + t.target : ''} is ${t.when}`;
      const aa = a.type === 'container' ? `${a.action || 'restart'} container ${a.target}` : a.type === 'vm' ? `${a.action || 'start'} ${a.backend || 'qemu'} VM ${a.target}` : a.type === 'stack' ? `${a.action || 'up'} stack ${a.target}` : a.type === 'prune' ? `prune ${a.kind || 'image'}s` : a.type === 'exec' ? `run ${(a.command || []).join(' ')} in ${a.target}` : a.type.replace('_', ' ');
      return `${tt} → ${aa}`; };
    root.innerHTML = `<div class="card"><h3>Rules</h3>${table(['Name', 'What it does', 'Last run', 'Runs', ''], rules.map(r => [esc(r.name), esc(desc(r)), r.last_run ? esc(new Date(r.last_run * 1000).toLocaleString()) + '<br>' + esc((r.last_result || '').slice(0, 80)) : 'never', esc(r.runs),
      `<label><input type="checkbox" data-act="ir-toggle" data-id="${r.id}" ${r.enabled ? 'checked' : ''}> on</label>` + btn('ir-run', 'Run now', `data-id="${r.id}"`) + btn('ir-hist', 'History', `data-id="${r.id}"`) + btn('ir-del', 'Delete', `data-id="${r.id}"`)]))}</div>
      <div class="card"><h3>New rule</h3><div class="infra-row">${field('ir-name', 'Name', '', 'Keep web up')}
      <label class="infra-f"><span>When</span><select id="ir-trig"><option value="watch-container">a container…</option><option value="watch-vm">a VM…</option><option value="watch-tailscale">Tailscale is disconnected</option><option value="interval">every…</option></select></label>
      ${field('ir-target', 'Container / VM name', '')}<label class="infra-f"><span>Condition</span><select id="ir-when"><option>exited</option><option>stopped</option><option>unhealthy</option><option>running</option><option>off</option><option>cpu>80</option><option>mem>90</option></select></label>${field('ir-every', 'Interval', '', '6h')}</div>
      <div class="infra-row"><label class="infra-f"><span>Then</span><select id="ir-act"><option value="container">act on a container</option><option value="vm">act on a VM</option><option value="stack">act on a stack</option><option value="prune">prune unused</option><option value="tailscale_up">reconnect Tailscale</option></select></label>
      ${field('ir-atarget', 'Target name', '')}<label class="infra-f"><span>Action</span><select id="ir-aop"><option>restart</option><option>start</option><option>stop</option><option>up</option><option>image</option></select></label>${field('ir-cool', 'Wait between runs (s)', '300')}${btn('ir-create', 'Create rule', '', 'primary')}</div></div><div id="ir-out"></div>`;
  }
  async function irAct(el) {
    const act = el.dataset.act, id = el.dataset.id;
    try {
      if (act === 'ir-create') {
        const tv = val('ir-trig'), a = val('ir-act');
        const trigger = tv === 'interval' ? { type: 'interval', every: val('ir-every') } : tv === 'watch-tailscale' ? { type: 'watch', resource: 'tailscale', when: 'disconnected' } : { type: 'watch', resource: tv === 'watch-vm' ? 'vm' : 'container', target: val('ir-target'), when: val('ir-when') };
        const action = a === 'tailscale_up' ? { type: a } : a === 'prune' ? { type: a, kind: val('ir-aop') === 'image' ? 'image' : 'container' } : { type: a, target: val('ir-atarget'), action: val('ir-aop') };
        await api('/api/infra/rules', J('POST', { name: val('ir-name'), trigger, action, cooldown_s: +val('ir-cool') || 300 })); toast('Rule created', 'success'); irLoad();
      } else if (act === 'ir-toggle') { await api(`/api/infra/rules/${id}/enable`, J('POST', { enabled: el.checked })); }
      else if (act === 'ir-run') { const r = await api(`/api/infra/rules/${id}/run`, J('POST', {})); toast(r.ok ? 'Ran' : 'Failed: ' + r.detail, r.ok ? 'success' : 'error'); irLoad(); }
      else if (act === 'ir-hist') { $('ir-out').innerHTML = pre((await api(`/api/infra/rules/${id}/history`)).map(h => `${new Date(h.at * 1000).toLocaleString()}  ${h.ok ? 'ok' : 'FAILED'}  ${h.detail}`).join('\n') || 'No runs yet'); }
      else if (act === 'ir-del') { if (sure('Delete this rule?')) { await api('/api/infra/rules/' + id, { method: 'DELETE' }); irLoad(); } }
    } catch (e) { fail(e); }
  }


  // ================================================================= Interactive terminals
  let xtermReady = null;
  function loadXterm() {
    if (xtermReady) return xtermReady;
    const base = (typeof API_BASE === 'string' ? API_BASE : '') + '/desktop-ui/vendor/xterm/';
    const add = (tag, attrs) => new Promise((res, rej) => { const el = document.createElement(tag); Object.assign(el, attrs); el.onload = res; el.onerror = () => rej(new Error('could not load ' + (attrs.src || attrs.href))); document.head.appendChild(el); });
    xtermReady = add('link', { rel: 'stylesheet', href: base + 'xterm.min.css' })
      .then(() => add('script', { src: base + 'xterm.min.js' }))
      .then(() => add('script', { src: base + 'addon-fit.min.js' }));
    return xtermReady;
  }
  async function openTerm(kind, target, title, extra) {
    try { await loadXterm(); } catch (e) { return fail(e); }
    const wrap = document.createElement('div');
    wrap.style.cssText = 'position:fixed;inset:5% 4%;z-index:400;background:#0b0d12;border:1px solid var(--line,#444);border-radius:8px;display:flex;flex-direction:column;box-shadow:0 12px 40px #000a';
    wrap.innerHTML = `<div style="display:flex;align-items:center;gap:8px;padding:6px 10px;border-bottom:1px solid var(--line,#333)"><b style="flex:1">${esc(title)}</b><span id="tm-state" class="infra-pill">connecting…</span><button class="btn" id="tm-close" style="padding:3px 10px">Close</button></div><div id="tm-body" style="flex:1;min-height:0;padding:6px"></div>`;
    document.body.appendChild(wrap);
    const term = new Terminal({ cursorBlink: true, fontSize: 13, convertEol: false, scrollback: 5000, theme: { background: '#0b0d12' } });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(wrap.querySelector('#tm-body'));
    try { fit.fit(); } catch (_) {}
    const base = (typeof API_BASE === 'string' && API_BASE ? API_BASE : location.origin).replace(/^http/, 'ws');
    const q = new URLSearchParams(Object.assign(HOST !== 'local' ? { host: HOST } : {}, { kind, target, token: (typeof getToken === 'function' ? getToken() : '') || '', cols: term.cols, rows: term.rows }, extra || {}));
    const ws = new WebSocket(`${base}/api/terminals/ws?${q}`);
    const state = wrap.querySelector('#tm-state');
    const send = (o) => { if (ws.readyState === 1) ws.send(JSON.stringify(o)); };
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === 'ready') { state.textContent = 'connected'; term.focus(); }
      else if (m.type === 'output') term.write(m.data);
      else if (m.type === 'exit') { state.textContent = 'ended'; term.write('\r\n\x1b[90m[session ended]\x1b[0m\r\n'); }
      else if (m.type === 'error') { state.textContent = 'error'; term.write('\r\n\x1b[31m' + m.message + '\x1b[0m\r\n'); }
    };
    ws.onclose = () => { if (state.textContent === 'connecting…' || state.textContent === 'connected') state.textContent = 'closed'; };
    term.onData((d) => send({ type: 'input', data: d }));
    const onResize = () => { try { fit.fit(); send({ type: 'resize', cols: term.cols, rows: term.rows }); } catch (_) {} };
    const ro = new ResizeObserver(onResize); ro.observe(wrap);
    wrap.querySelector('#tm-close').onclick = () => { ro.disconnect(); try { ws.close(); } catch (_) {} term.dispose(); wrap.remove(); };
  }


  // ================================================================= Host picker + remote-access switch
  const LOADED = () => ({ tailscale: tsLoad, containers: ctLoad, vms: vmLoad, 'infra-rules': irLoad });
  async function buildHostBars() {
    let hosts = [{ id: 'local', name: 'This machine', local: true }], access = false;
    try { hosts = (await pageApi('/api/infra/hosts')).hosts; access = (await pageApi('/api/infra/peer-access')).enabled; } catch (_) {}
    if (!hosts.some(h => h.id === HOST)) { HOST = 'local'; }
    ['ts-root', 'ct-root', 'vm-root', 'ir-root'].forEach((id) => {
      const root = $(id); if (!root) return;
      let bar = $(id + '-hosts');
      if (!bar) { bar = document.createElement('div'); bar.id = id + '-hosts'; bar.className = 'infra-row'; root.insertAdjacentElement('beforebegin', bar); }
      bar.innerHTML = `<label class="infra-f"><span>Manage</span><select data-hostpick>${hosts.map(h => `<option value="${esc(h.id)}" ${h.id === HOST ? 'selected' : ''}>${esc(h.name)}${h.local ? '' : ' (linked server)'}</option>`).join('')}</select></label>`
        + (HOST === 'local' ? `<label style="display:flex;gap:6px;align-items:center;font-size:12px"><input type="checkbox" data-peeraccess ${access ? 'checked' : ''}> Allow linked servers to manage this machine</label>` : `<span class="infra-pill">acting on a linked server</span>`);
      bar.querySelector('[data-hostpick]').onchange = (ev) => {
        HOST = ev.target.value;
        try { sessionStorage.setItem('infra-host', HOST); } catch (_) {}
        buildHostBars();
        Object.values(LOADED()).forEach((fn) => { if (fn) fn(); });
      };
      const pa = bar.querySelector('[data-peeraccess]');
      if (pa) pa.onchange = async () => {
        if (pa.checked && !sure('Let servers linked to this one start, stop and change containers, VMs and Tailscale settings here, and open terminals in them? Only enable this for servers you control.')) { pa.checked = false; return; }
        try { await pageApi('/api/infra/peer-access', J('POST', { enabled: pa.checked })); toast(pa.checked ? 'Linked servers can now manage this machine' : 'Remote management turned off', 'success'); } catch (e) { fail(e); pa.checked = !pa.checked; }
      };
    });
  }

  // ================================================================= wiring
  const LOADERS = { tailscale: tsLoad, containers: ctLoad, vms: vmLoad, 'infra-rules': irLoad };
  const HANDLERS = { ts: tsAct, ct: ctAct, vm: vmAct, ir: irAct };
  function onHash() { const h = (location.hash || '').replace('#', ''); if (LOADERS[h]) LOADERS[h](); }
  function init() {
    css();
    ['ts-root', 'ct-root', 'vm-root', 'ir-root'].forEach((id) => {
      const root = $(id); if (!root) return;
      const fn = HANDLERS[id.slice(0, 2)];
      root.addEventListener('click', (ev) => { const el = ev.target.closest('[data-act]'); if (el && el.type !== 'checkbox') fn(el); });
      root.addEventListener('change', (ev) => { if (ev.target.type === 'checkbox' && ev.target.dataset.act) fn(ev.target); });
    });
    buildHostBars();
    window.addEventListener('hashchange', onHash);
    onHash();
    const seen = new Set();
    const watch = (id, key) => { const el = $(id); if (!el || !('IntersectionObserver' in window)) return;
      new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting && !seen.has(key)) { seen.add(key); LOADERS[key](); } })).observe(el); };
    watch('ts-root', 'tailscale'); watch('ct-root', 'containers'); watch('vm-root', 'vms'); watch('ir-root', 'infra-rules');
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
