// The side panel: what ABP is doing in this browser right now, with a Stop button that is always one click away.
import { ask, chip, clock, el, hostOf, type Status } from '../shared/ui';

const root = document.getElementById('root')!;

async function render(): Promise<void> {
  const [s, a] = await Promise.all([ask<Status>({ ui: 'status' }), ask<{ entries: Array<{ at: number; action: string; detail: string }> }>({ ui: 'audit' })]);
  const kids: Node[] = [el('h1', {}, 'ABP'), el('div', { class: 'row' }, chip(s.state),
    s.stopped ? el('button', { onclick: async () => { await ask({ ui: 'resume' }); void render(); } }, 'Allow ABP to continue')
      : el('button', { class: 'danger', onclick: async () => { await ask({ ui: 'stop' }); void render(); } }, 'Stop'))];
  if (s.paused) kids.push(el('p', { class: 'small' }, 'You have control. ABP is paused until you resume it from the page.'));
  kids.push(el('h2', {}, 'Tabs'));
  kids.push(s.tabs.length ? el('table', {}, ...s.tabs.map((t) => el('tr', {}, el('td', {}, hostOf(t.url)), el('td', { class: 'muted small' }, t.kind))))
    : el('p', { class: 'muted small' }, 'No tabs in use.'));
  kids.push(el('h2', {}, 'Timeline'));
  const rows = a.entries.slice(-60).reverse();
  kids.push(el('ul', { class: 'timeline', role: 'log', ariaLive: 'polite' },
    ...(rows.length ? rows.map((e) => el('li', {}, el('time', {}, clock(e.at)), el('b', {}, e.action), el('span', { class: 'muted' }, e.detail)))
      : [el('li', { class: 'muted' }, 'Nothing yet.')])));
  root.replaceChildren(...kids);
}

chrome.runtime.onMessage.addListener((m) => { if ((m as { ui?: string }).ui === 'state') void render(); });
setInterval(() => void render(), 3000);
void render();
