// Sentinel panel: ad-blocking (blocklists, a filtering DNS forwarder, a live query log) and firewall rules
// (bot/netguard.py, the engine is the Sentinel module). Every setting is a switch here, and every change to the host
// (firewall rules, adapters' DNS) is shown as the exact commands before it runs.
// This file is identical in the dashboard (bot/dashboard/static/) and the desktop app (desktop-app/ui/);
// tests/test_netguard.py fails if the two differ. It uses the page's own api(), esc() and showToast(), and draws
// into #ng-root.
(function (api) {
  'use strict';
  if (typeof api !== 'function') return;
  const $ = (id) => document.getElementById(id);
  const E = (s) => (typeof esc === 'function' ? esc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const toast = (m, t) => (typeof showToast === 'function' ? showToast(m, t) : console.log(m));
  const errText = (e) => { if (window.ABP && ABP.unitUnavailable && ABP.unitUnavailable(e)) return 'This module is not loaded.'; let m = e && e.message ? e.message : String(e); try { const d = JSON.parse(m).detail; m = (d && d.error) || d || m; } catch (_) {} return m; };
  const send = (method, path, body) => api(path, { method, body: JSON.stringify(body || {}) });
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : '');
  const size = (b) => (b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : b > 1024 ? Math.round(b / 1024) + ' KB' : b + ' B');
  let timer = null;
  let qtimer = null;
  let onlyBlocked = false;

  function css() {
    if ($('ng-css')) return;
    const s = document.createElement('style');
    s.id = 'ng-css';
    s.textContent = `.ng-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(380px,100%),1fr));gap:12px}
.ng-card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--surface);min-width:0}
.ng-card.wide{grid-column:1/-1}
.ng-card h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.ng-muted{color:var(--muted);font-size:12px}.ng-mono{font-family:var(--font-mono,monospace);font-size:12px;word-break:break-all}
.ng-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:4px 0}
.ng-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--surface-2);color:var(--ink-soft)}
.ng-chip.on{background:var(--good-soft);color:var(--good,#1f9d55)}.ng-chip.warn{background:var(--warning-soft);color:#8a5c00}.ng-chip.bad{background:var(--danger-soft,#fde8e8);color:var(--danger,#c0392b)}
.ng-table{width:100%;border-collapse:collapse;font-size:12.5px}.ng-table th{text-align:left;color:var(--muted);font-weight:600;padding:5px;border-bottom:1px solid var(--line)}
.ng-table td{padding:5px;border-bottom:1px solid var(--line);vertical-align:top}.ng-scroll{overflow:auto;max-width:100%;max-height:340px}
.ng-pre{white-space:pre-wrap;background:var(--surface-2);border-radius:6px;padding:8px;margin:6px 0;max-height:260px;overflow:auto}
.ng-sw{display:inline-flex;gap:6px;align-items:center;cursor:pointer}
#ng-root input,#ng-root select,#ng-root textarea{background:var(--surface-2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;font-size:12.5px;max-width:100%}
#ng-root textarea{width:100%;min-height:80px;font-family:var(--font-mono,monospace)}`;
    document.head.appendChild(s);
  }

  // A switch for a boolean setting: saved the moment it is flipped.
  const sw = (sec, key, on, label) => `<label class="ng-sw"><input type="checkbox" data-set="${sec}.${key}" ${on ? 'checked' : ''}> ${E(label)}</label>`;

  async function save(body, ok) {
    try { await send('PUT', '/api/netguard/settings', body); if (ok) toast(ok, 'success'); } catch (e) { toast(errText(e), 'error'); }
    render();
  }

  function rulesTable(fw) {
    if (!fw.rules.length) return '<p class="ng-muted">No rules yet.</p>';
    const on = new Set((fw.current || []).map((c) => c.display_name));
    return `<div class="ng-scroll"><table class="ng-table"><tr><th>On</th><th>Rule</th><th>Matches</th><th>On the host</th><th></th></tr>${fw.rules.map((r) => {
      const what = [r.protocol !== 'any' ? r.protocol.toUpperCase() : '', (r.remote_addresses || []).join(', '), (r.remote_ports || []).length ? 'remote port ' + r.remote_ports.join(',') : '', (r.local_ports || []).length ? 'local port ' + r.local_ports.join(',') : '', r.program].filter(Boolean).join(' · ');
      return `<tr><td><input type="checkbox" data-rule-on="${E(r.name)}" ${r.enabled ? 'checked' : ''} title="switch this rule"></td><td><b>${E(r.name)}</b><div class="ng-muted">${E(r.action)} ${r.direction === 'out' ? 'outbound' : 'inbound'}</div></td><td class="ng-mono">${E(what)}</td><td>${on.has('Sentinel - ' + r.name) ? '<span class="ng-chip on">present</span>' : '<span class="ng-chip">absent</span>'}</td><td><button class="ghost" data-rule-del="${E(r.name)}">Delete</button></td></tr>`;
    }).join('')}</table></div>`;
  }

  async function render() {
    css();
    const root = $('ng-root');
    if (!root) return;
    let st;
    try { st = await api('/api/netguard/status'); } catch (e) { root.innerHTML = `<p class="cardnote">${E(errText(e))}</p>`; return; }
    if (!st.core.available) {
      root.innerHTML = `<div class="ng-card"><h4>Sentinel is not installed</h4><p>${E(st.core.reason)}</p><p class="ng-muted">Open <a href="#modules">Modules</a>, find Sentinel and install it; this page comes alive once its checkout is there.</p></div>`;
      return;
    }
    const [fw, sd, jr] = await Promise.all([
      api('/api/netguard/firewall').catch((e) => ({ error: errText(e), rules: [], changes: [], script: '' })),
      api('/api/netguard/system-dns').catch((e) => ({ error: errText(e), adapters: [], plan: { commands: [] } })),
      api('/api/netguard/journal?limit=40').catch(() => ({ entries: [] })),
    ]);
    const s = st.settings, ab = s.adblock, d = st.dns;
    const fwLive = s.firewall.enabled && !s.firewall.dry_run;
    root.innerHTML = `<div class="ng-grid">
<div class="ng-card"><h4>Ad-blocking</h4>
  <div class="ng-row">${sw('adblock', 'enabled', ab.enabled, 'Block ads and trackers (filtering DNS)')}</div>
  <div class="ng-row">${d.running ? `<span class="ng-chip on">filtering on ${E(d.address)}</span><span class="ng-muted">${d.domains} blocked domains · ${d.stats.queries} queries · ${d.stats.blocked} blocked · ${d.stats.errors} errors</span>` : ab.enabled ? `<span class="ng-chip bad">not running</span>` : '<span class="ng-chip">off</span>'}</div>
  ${d.error ? `<p class="ng-muted" style="color:var(--danger,#c0392b)">${E(d.error)}</p>` : ''}
  <div class="ng-row"><label class="ng-muted">Listen <input id="ng-listen" value="${E(ab.listen)}" style="width:150px"></label><label class="ng-muted">Upstream <input id="ng-up" value="${E(ab.upstream)}" style="width:150px"></label></div>
  <div class="ng-row"><label class="ng-muted">Blocked names get <select id="ng-resp"><option value="nxdomain" ${ab.response === 'nxdomain' ? 'selected' : ''}>"does not exist" (NXDOMAIN)</option><option value="zero" ${ab.response === 'zero' ? 'selected' : ''}>0.0.0.0 / ::</option></select></label><button class="primary" id="ng-save-dns">Save</button></div>
  <p class="ng-muted">Point a program or a device at the listen address to filter it. To filter this whole machine, listen on 127.0.0.1:53 and switch on "Use it for this machine" below.</p>
  <div class="ng-row"><input id="ng-check" placeholder="check a name, e.g. ads.example.com" style="flex:1"><button class="ghost" id="ng-check-go">Check</button></div><div id="ng-check-out" class="ng-muted"></div>
</div>
<div class="ng-card"><h4>Blocklists</h4>
  <table class="ng-table"><tr><th>On</th><th>List</th><th>Cached</th><th></th></tr>${st.lists.map((l) => `<tr><td><input type="checkbox" data-list="${E(l.id)}" ${l.enabled ? 'checked' : ''}></td><td>${E(l.name)}<div class="ng-muted ng-mono">${E(l.url)}</div></td><td class="ng-muted">${l.cached ? size(l.bytes) + '<br>' + E(when(l.updated)) : 'not downloaded'}</td><td><button class="ghost" data-refresh="${E(l.id)}">Download</button></td></tr>`).join('')}</table>
  <div class="ng-row"><button class="primary" id="ng-refresh-all">Download the switched-on lists</button></div>
  <p class="ng-muted">A list blocks nothing until it is switched on and downloaded (https only).</p>
</div>
<div class="ng-card"><h4>Your own rules</h4>
  <label class="ng-muted">Always block (one domain per line; subdomains too)</label><textarea id="ng-block">${E(ab.block.join('\n'))}</textarea>
  <label class="ng-muted">Always allow (wins over every list)</label><textarea id="ng-allow">${E(ab.allow.join('\n'))}</textarea>
  <div class="ng-row"><button class="primary" id="ng-save-own">Save</button></div>
</div>
<div class="ng-card"><h4>Use it for this machine</h4>
  <div class="ng-row">${sw('system_dns', 'enabled', s.system_dns.enabled, 'Point this machine\'s network adapters at the filter')}</div>
  <div class="ng-row">${sw('system_dns', 'dry_run', s.system_dns.dry_run, 'Dry run (only show what would change)')}</div>
  ${sd.error ? `<p class="ng-muted">${E(sd.error)}</p>` : ''}
  ${(sd.adapters || []).length ? `<table class="ng-table"><tr><th>Adapter</th><th>DNS servers now</th></tr>${sd.adapters.map((a) => `<tr><td>${E(a.name)}</td><td class="ng-mono">${E(a.servers.join(', ') || '-')}${a.dhcp ? ' <span class="ng-chip">automatic</span>' : ''}</td></tr>`).join('')}</table>` : ''}
  <div class="ng-muted">Plan (${E((sd.plan && sd.plan.action) || 'none')}):</div><pre class="ng-pre ng-mono">${E(((sd.plan && sd.plan.commands) || []).join('\n') || 'nothing to change')}</pre>
  <div class="ng-row"><button class="${s.system_dns.dry_run ? 'ghost' : 'primary'}" id="ng-sd-apply" ${((sd.plan && sd.plan.commands) || []).length ? '' : 'disabled'}>${s.system_dns.dry_run ? 'Run as a dry run' : 'Apply now (a UAC prompt asks you)'}</button></div>
  ${st.system_dns_saved ? '<p class="ng-muted">The adapters\' previous DNS servers are saved; switching this off and applying puts them back exactly.</p>' : ''}
</div>
<div class="ng-card wide"><h4>Firewall</h4>
  <div class="ng-row">${sw('firewall', 'enabled', s.firewall.enabled, 'Firewall rules on (off = Sentinel\'s rules are removed on the next apply)')}</div>
  <div class="ng-row">${sw('firewall', 'dry_run', s.firewall.dry_run, 'Dry run (Apply only shows the plan)')} ${fwLive ? '<span class="ng-chip warn">live: Apply changes this machine\'s firewall</span>' : ''}</div>
  ${!st.platform.windows ? '<p class="ng-muted">Firewall management is Windows-only for now.</p>' : ''}
  ${fw.error ? `<p class="ng-muted" style="color:var(--danger,#c0392b)">${E(fw.error)}</p>` : ''}
  ${rulesTable(fw)}
  <div class="ng-row" style="margin-top:8px"><input id="ng-r-name" placeholder="rule name" style="width:150px"><select id="ng-r-dir"><option value="out">outbound</option><option value="in">inbound</option></select><select id="ng-r-act"><option value="block">block</option><option value="allow">allow</option></select><select id="ng-r-proto"><option value="any">any protocol</option><option value="tcp">TCP</option><option value="udp">UDP</option><option value="icmpv4">ICMPv4</option><option value="icmpv6">ICMPv6</option></select></div>
  <div class="ng-row"><input id="ng-r-addr" placeholder="remote addresses (1.2.3.4, 10.0.0.0/8, a-b)" style="flex:1"><input id="ng-r-rport" placeholder="remote ports" style="width:120px"><input id="ng-r-lport" placeholder="local ports" style="width:110px"></div>
  <div class="ng-row"><input id="ng-r-prog" placeholder="program (full path to an .exe)" style="flex:1"><button class="primary" id="ng-r-add">Add rule</button></div>
  <div class="ng-muted">Plan: ${(fw.changes || []).length ? (fw.changes || []).map((c) => `${E(c.op)} ${E(c.name)} (${E(c.why)})`).join('; ') : 'the host already matches'}</div>
  <pre class="ng-pre ng-mono">${E((fw.changes || []).length ? fw.script : '# nothing to change')}</pre>
  <div class="ng-row"><button class="${s.firewall.dry_run ? 'ghost' : 'primary'}" id="ng-fw-apply" ${(fw.changes || []).length ? '' : 'disabled'}>${s.firewall.dry_run ? 'Run as a dry run' : 'Apply these changes now (a UAC prompt asks you)'}</button>${fw.last && fw.last.can_roll_back ? `<button class="ghost" id="ng-fw-rollback">Roll back the last apply (${E(when(fw.last.at))})</button>` : ''}</div>
  <p class="ng-muted">Sentinel only ever touches rules in its own firewall group ("Sentinel - …"); every other rule on this machine is left alone.</p>
</div>
<div class="ng-card wide"><h4>Live queries</h4>
  <div class="ng-row"><label class="ng-sw"><input type="checkbox" id="ng-only-blocked" ${onlyBlocked ? 'checked' : ''}> Blocked only</label><span class="ng-muted">refreshes every 3 s while this page is open</span></div>
  <div class="ng-scroll" id="ng-queries"></div>
</div>
<div class="ng-card wide"><h4>Journal</h4>${jr.entries.length ? `<div class="ng-scroll"><table class="ng-table">${jr.entries.map((j) => `<tr><td class="ng-muted" style="white-space:nowrap">${E(when(j.t))}</td><td><span class="ng-chip">${E(j.kind)}</span></td><td>${E(j.detail)}${(j.commands || []).length ? `<div class="ng-mono ng-muted">${E(j.commands.join(' | '))}</div>` : ''}</td></tr>`).join('')}</table></div>` : '<p class="ng-muted">Nothing yet.</p>'}</div>
</div>`;
    wire(root, s);
    queries();
  }

  async function queries() {
    const box = $('ng-queries');
    if (!box) return;
    try {
      const r = await api('/api/netguard/queries?limit=200' + (onlyBlocked ? '&blocked=true' : ''));
      box.innerHTML = r.queries.length ? `<table class="ng-table"><tr><th>Time</th><th>Name</th><th>Type</th><th>Client</th><th>Result</th></tr>${r.queries.map((q) => `<tr><td class="ng-muted">${E(new Date(q.t * 1000).toLocaleTimeString())}</td><td class="ng-mono">${E(q.name)}</td><td>${E(q.type)}</td><td class="ng-muted">${E(q.client)}</td><td>${q.blocked ? `<span class="ng-chip bad">blocked</span> <span class="ng-muted">${E(q.rule)}</span>` : q.rule ? `<span class="ng-chip on">allowed</span> <span class="ng-muted">${E(q.rule)}</span>` : '<span class="ng-chip">forwarded</span>'}</td></tr>`).join('')}</table>` : `<p class="ng-muted">${r.dns.running ? 'No queries yet.' : 'The filter is not running.'}</p>`;
    } catch (_) { /* the next tick tries again */ }
  }

  function wire(root, s) {
    const act = async (fn, ok) => { try { const r = await fn(); if (ok) toast(typeof ok === 'function' ? ok(r) : ok, 'success'); } catch (e) { toast(errText(e), 'error'); } render(); };
    root.querySelectorAll('[data-set]').forEach((c) => c.addEventListener('change', () => {
      const [sec, key] = c.dataset.set.split('.');
      save({ [sec]: { [key]: c.checked } }, `${c.checked ? 'On' : 'Off'}: ${c.parentElement.textContent.trim()}`);
    }));
    root.querySelectorAll('[data-list]').forEach((c) => c.addEventListener('change', () => save({ adblock: { lists: { ...s.adblock.lists, [c.dataset.list]: c.checked } } }, 'Saved')));
    root.querySelectorAll('[data-refresh]').forEach((b) => b.addEventListener('click', () => { b.disabled = true; b.textContent = 'Downloading…'; act(() => send('POST', '/api/netguard/lists/refresh', { id: b.dataset.refresh }), (r) => Object.entries(r.results).map(([k, v]) => `${k}: ${v.ok ? v.domains + ' domains' : v.error}`).join('; ')); }));
    $('ng-refresh-all').addEventListener('click', (ev) => { ev.target.disabled = true; ev.target.textContent = 'Downloading…'; act(() => send('POST', '/api/netguard/lists/refresh', {}), (r) => Object.keys(r.results).length ? Object.entries(r.results).map(([k, v]) => `${k}: ${v.ok ? v.domains + ' domains' : v.error}`).join('; ') : 'No list is switched on'); });
    $('ng-save-dns').addEventListener('click', () => save({ adblock: { listen: $('ng-listen').value.trim(), upstream: $('ng-up').value.trim(), response: $('ng-resp').value } }, 'Saved'));
    const lines = (id) => $(id).value.split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
    $('ng-save-own').addEventListener('click', () => save({ adblock: { block: lines('ng-block'), allow: lines('ng-allow') } }, 'Saved'));
    $('ng-check-go').addEventListener('click', async () => {
      try { const r = await send('POST', '/api/netguard/check', { name: $('ng-check').value.trim() }); $('ng-check-out').textContent = r.blocked ? `blocked by ${r.rule}` : r.rule ? `allowed by ${r.rule}` : 'not blocked'; } catch (e) { $('ng-check-out').textContent = errText(e); }
    });
    root.querySelectorAll('[data-rule-on]').forEach((c) => c.addEventListener('change', () => act(() => send('PUT', '/api/netguard/firewall/rules/' + encodeURIComponent(c.dataset.ruleOn), { enabled: c.checked }), 'Saved: apply to put it on the host')));
    root.querySelectorAll('[data-rule-del]').forEach((b) => b.addEventListener('click', () => { if (confirm(`Delete the rule "${b.dataset.ruleDel}"? (It leaves the host on the next apply.)`)) act(() => api('/api/netguard/firewall/rules/' + encodeURIComponent(b.dataset.ruleDel), { method: 'DELETE' }), 'Deleted'); }));
    const csv = (id) => $(id).value.split(/[,\s]+/).map((x) => x.trim()).filter(Boolean);
    $('ng-r-add').addEventListener('click', () => act(() => send('POST', '/api/netguard/firewall/rules', { name: $('ng-r-name').value.trim(), direction: $('ng-r-dir').value, action: $('ng-r-act').value, protocol: $('ng-r-proto').value, remote_addresses: csv('ng-r-addr'), remote_ports: csv('ng-r-rport'), local_ports: csv('ng-r-lport'), program: $('ng-r-prog').value.trim(), enabled: true }), 'Rule added: apply to put it on the host'));
    $('ng-fw-apply').addEventListener('click', () => {
      if (!s.firewall.dry_run && !confirm('Change this machine\'s firewall now, exactly as the plan shows? Windows asks you once (UAC).')) return;
      act(() => send('POST', '/api/netguard/firewall/apply'), (r) => r.dry_run ? `Dry run: ${r.changes.length} change(s) would run` : r.ok ? `Applied ${r.applied} change(s)` : 'Not applied: ' + (r.error || 'see the journal'));
    });
    if ($('ng-fw-rollback')) $('ng-fw-rollback').addEventListener('click', () => { if (s.firewall.dry_run || confirm('Put Sentinel\'s firewall rules back as they were before the last apply?')) act(() => send('POST', '/api/netguard/firewall/rollback'), (r) => r.dry_run ? 'Dry run: rollback plan shown in the journal' : r.ok ? 'Rolled back' : 'Not rolled back: ' + (r.error || '')); });
    $('ng-sd-apply').addEventListener('click', () => {
      if (!s.system_dns.dry_run && !confirm('Change this machine\'s DNS settings now, exactly as the plan shows? Windows asks you once (UAC).')) return;
      act(() => send('POST', '/api/netguard/system-dns/apply'), (r) => r.dry_run ? `Dry run: ${r.commands.length} command(s) would run` : r.ok ? 'Done' : 'Not applied: ' + (r.error || ''));
    });
    $('ng-only-blocked').addEventListener('change', (ev) => { onlyBlocked = ev.target.checked; queries(); });
  }

  function init() {
    const root = $('ng-root');
    if (!root) return;
    const onHash = () => {
      clearInterval(timer); clearInterval(qtimer); timer = qtimer = null;
      if ((location.hash || '').replace('#', '') === 'netguard') {
        render();
        timer = setInterval(() => { if (!document.hidden && !root.contains(document.activeElement)) render(); }, 30000);
        qtimer = setInterval(() => { if (!document.hidden) queries(); }, 3000);
      }
    };
    window.addEventListener('hashchange', onHash);
    onHash();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof api === 'function' ? api : null);
