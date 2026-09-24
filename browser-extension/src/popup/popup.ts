import { ask, chip, el, hostOf, type Status } from '../shared/ui';

const root = document.getElementById('root')!;

async function render(): Promise<void> {
  const s = await ask<Status>({ ui: 'status' });
  const kids: Array<Node> = [el('h1', {}, 'ABP Bridge'), el('div', { class: 'row' }, chip(s.state))];
  if (s.error) kids.push(el('p', { class: 'small err' }, s.error));

  if (!s.paired) {
    kids.push(el('div', { class: 'card' }, el('p', {}, 'Connect this browser to the ABP desktop app.'),
      el('button', { class: 'primary', onclick: () => void chrome.runtime.openOptionsPage() }, 'Connect...')));
  } else {
    const at = s.active_tab;
    const usable = at && /^https?:/.test(at.url);
    const card = el('div', { class: 'card' });
    if (usable) {
      const granted = at.kind !== null;
      card.append(el('div', { class: 'small muted' }, 'This tab'), el('div', {}, hostOf(at.url)),
        el('button', { class: granted ? '' : 'primary', onclick: async () => {
          await ask({ ui: granted ? 'detach' : 'attach', tab: at.id });
          void render();
        } }, granted ? 'Stop letting ABP use this tab' : 'Let ABP use this tab'));
    } else card.append(el('p', { class: 'muted small' }, 'Open a website to let ABP use it.'));
    kids.push(card);
    if (s.tabs.length) kids.push(el('p', { class: 'small muted' }, `ABP can use ${s.tabs.length} tab${s.tabs.length === 1 ? '' : 's'}.`));
    if (s.stopped) kids.push(el('div', { class: 'card' }, el('p', { class: 'err' }, 'ABP was stopped.'), el('button', { onclick: async () => { await ask({ ui: 'resume' }); void render(); } }, 'Allow ABP to continue')));
    else kids.push(el('button', { class: 'danger', onclick: async () => { await ask({ ui: 'stop' }); void render(); } }, 'Stop ABP now'));
    if (s.state !== 'connected') kids.push(el('button', { onclick: async () => { await ask({ ui: 'connect' }); setTimeout(() => void render(), 600); } }, 'Reconnect'));
  }
  kids.push(el('div', { class: 'row' },
    el('button', { onclick: () => void ask({ ui: 'sidepanel' }).then(() => window.close()) }, 'Side panel'),
    el('button', { onclick: () => void chrome.runtime.openOptionsPage() }, 'Options')));
  root.replaceChildren(...kids);
}

chrome.runtime.onMessage.addListener((m) => { if ((m as { ui?: string }).ui === 'state') void render(); });
void render();
