import { ask, chip, clock, el, hostOf, type Status } from '../shared/ui';

const root = document.getElementById('root')!;
let message: { text: string; bad: boolean } | null = null;
let pairing = false;

async function render(): Promise<void> {
  const s = await ask<Status>({ ui: 'status' });
  const kids: Node[] = [el('h1', {}, 'ABP Bridge'), el('div', { class: 'row' }, chip(s.state), el('span', { class: 'muted small' }, `v${s.version} - ${s.browser}`))];
  if (s.error) kids.push(el('p', { class: 'small err' }, s.error));
  if (message) kids.push(el('p', { class: message.bad ? 'err' : 'ok' }, message.text));

  if (!s.paired || s.state === 'unpaired' || s.state === 'server-mismatch') {
    const port = el('input', { id: 'port', inputMode: 'numeric', maxLength: 5, placeholder: 'automatic', ariaLabel: 'ABP port', style: { width: '9ch' } } as never);
    const code = el('input', { class: 'code', inputMode: 'numeric', maxLength: 6, placeholder: '000000', ariaLabel: 'Pairing code' });
    const run = async (fn: () => Promise<unknown>, ok: string): Promise<void> => {
      message = null;
      try { await fn(); message = { text: ok, bad: false }; } catch (e) { message = { text: e instanceof Error ? e.message : String(e), bad: true }; }
      pairing = false;
      void render();
    };
    kids.push(el('div', { class: 'card' },
      el('h2', {}, 'Connect to the ABP desktop app'),
      el('p', { class: 'muted' }, 'Make sure ABP is running. Then choose one:'),
      el('p', {}, el('button', { class: 'primary', disabled: pairing, onclick: () => { pairing = true; void render(); void run(async () => { await ask({ ui: 'pair.request', port: port.value }); }, 'Connected.'); } },
        pairing ? 'Waiting for you to approve in ABP...' : 'Ask ABP to approve this browser'),
      pairing ? el('button', { onclick: () => void ask({ ui: 'pair.cancel' }) }, 'Cancel') : ''),
      el('p', { class: 'muted small' }, 'A prompt appears in the ABP desktop app - press Allow.'),
      el('h2', {}, 'Or use a code'),
      el('div', { class: 'row' }, code, el('button', { onclick: () => void run(() => ask({ ui: 'pair.code', code: code.value, port: port.value }).then((r) => { const e = (r as { error?: string }).error; if (e) throw new Error(e); }), 'Connected.') }, 'Connect')),
      el('p', { class: 'muted small' }, 'In ABP: Settings > Browser > "Show pairing code".'),
      el('details', {}, el('summary', { class: 'small muted' }, 'Advanced'), el('label', { class: 'small' }, 'ABP port ', port))));
  } else {
    kids.push(el('div', { class: 'card' }, el('h2', {}, 'Connection'),
      el('p', {}, `Paired with ABP on this computer (port ${s.port}).`),
      el('div', { class: 'row' },
        el('button', { onclick: async () => { await ask({ ui: 'connect' }); setTimeout(() => void render(), 600); } }, 'Reconnect'),
        el('button', { class: 'danger', onclick: async () => { if (confirm('Disconnect this browser from ABP?')) { await ask({ ui: 'unpair' }); message = { text: 'Disconnected.', bad: false }; void render(); } } }, 'Disconnect'))));

    const caps = Object.entries(s.policy.capabilities);
    kids.push(el('div', { class: 'card' }, el('h2', {}, 'What ABP may do (set in the ABP desktop app)'),
      el('table', {}, ...caps.map(([k, v]) => el('tr', {}, el('td', {}, k), el('td', { class: v ? 'ok' : 'muted' }, v ? 'allowed' : 'off')))),
      el('p', { class: 'muted small' }, `Up to ${s.policy.max_tabs} tabs, ${s.policy.actions_per_minute} actions per minute. Banks, payment pages, password managers, admin consoles and login pages are never automated.`)));

    kids.push(el('div', { class: 'card' }, el('h2', {}, 'Tabs ABP can use'),
      s.tabs.length ? el('table', {}, ...s.tabs.map((t) => el('tr', {}, el('td', {}, hostOf(t.url)), el('td', { class: 'muted' }, t.kind),
        el('td', {}, el('button', { onclick: async () => { await ask({ ui: 'detach', tab: t.id }); void render(); } }, 'Release')))))
        : el('p', { class: 'muted' }, 'None. ABP opens its own tabs (in a purple "ABP agent" group), or you can give it the tab you are on from the toolbar popup.')));

    const webRoot = el('div', {});
    kids.push(el('div', { class: 'card' }, el('h2', {}, 'Browser-session models'),
      el('p', { class: 'muted small' }, 'Off by default. Turning one on lets ABP send it a prompt and read back the reply, using your own logged-in session in a background tab - the site’s terms of service apply, and it is never used for anything marked sensitive.'),
      webRoot));
    void renderWeb(webRoot);

    const log = el('ul', { class: 'timeline' });
    kids.push(el('div', { class: 'card' }, el('h2', {}, 'Recent activity'), log));
    void ask<{ entries: Array<{ at: number; action: string; detail: string }> }>({ ui: 'audit' }).then((r) => {
      const rows = r.entries.slice(-40).reverse();
      log.replaceChildren(...(rows.length ? rows.map((e) => el('li', {}, el('time', {}, clock(e.at)), el('b', {}, e.action), el('span', { class: 'muted' }, e.detail)))
        : [el('li', { class: 'muted' }, 'Nothing yet.')]));
    });
  }
  root.replaceChildren(...kids);
}

interface WebAdapter {
  id: string; name: string; hosts: string[]; models: string[]; vision: boolean; enabled: boolean; tos_note: string;
  logged_in: boolean | null; degraded: boolean; connected_tab: boolean;
}

async function renderWeb(root: HTMLElement): Promise<void> {
  root.replaceChildren(el('p', { class: 'muted small' }, 'Loading...'));
  const { adapters } = await ask<{ adapters: WebAdapter[] }>({ ui: 'web.list' });
  let testMsg: Record<string, string> = {};
  const draw = (): void => {
    root.replaceChildren(el('table', {}, ...adapters.map((a) => {
      const statusText = !a.enabled ? 'off' : a.degraded ? 'the page changed - may need an update' : a.logged_in === false ? 'not logged in' : a.logged_in ? 'ready' : 'unknown';
      const toggle = el('button', { onclick: async () => {
        if (a.enabled) { await ask({ ui: 'web.disable', adapter: a.id }); }
        else {
          if (!confirm(`Let ABP use your ${a.name} session?\n\n${a.tos_note}`)) return;
          const r = await ask<{ ok: boolean; error?: string }>({ ui: 'web.enable', adapter: a.id, origin: `https://${a.hosts[0]}/*` });
          if (!r.ok) { testMsg = { ...testMsg, [a.id]: r.error ?? 'not enabled' }; draw(); return; }
        }
        void renderWeb(root);
      } }, a.enabled ? 'Turn off' : 'Turn on');
      const test = el('button', { disabled: !a.enabled, onclick: async () => {
        testMsg = { ...testMsg, [a.id]: 'Testing...' }; draw();
        try { const r = await ask<{ ok: boolean; got: string }>({ ui: 'web.selftest', adapter: a.id }); testMsg = { ...testMsg, [a.id]: r.ok ? 'Self-test passed.' : `Self-test failed: ${r.got}` }; }
        catch (e) { testMsg = { ...testMsg, [a.id]: e instanceof Error ? e.message : String(e) }; }
        draw();
      } }, 'Self-test');
      return el('tr', {}, el('td', {}, a.name), el('td', { class: 'muted small' }, statusText), el('td', {}, toggle, ' ', test),
        el('td', { class: 'muted small' }, testMsg[a.id] ?? ''));
    })));
  };
  draw();
}

chrome.runtime.onMessage.addListener((m) => {
  // never re-render under someone who is typing (a background state change must not wipe the code they entered)
  if ((m as { ui?: string }).ui === 'state' && !pairing && !(document.activeElement instanceof HTMLInputElement)) void render();
});
void render();
