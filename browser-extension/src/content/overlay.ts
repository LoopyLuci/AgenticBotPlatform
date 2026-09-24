// The "ABP is controlling this tab" indicator: a border, a pill with Stop / Take over, and small action toasts.
// Built in a closed shadow root so the page cannot read or restyle it; it never takes focus and never blocks the page.
type State = 'off' | 'active' | 'paused';

let host: HTMLElement | null = null;
let root: ShadowRoot | null = null;
let pill: HTMLElement | null = null;
let state: State = 'off';

const CSS = `
:host { all: initial; }
.border { position: fixed; inset: 0; pointer-events: none; z-index: 2147483646; box-shadow: inset 0 0 0 3px #7c5cff, inset 0 0 24px #7c5cff55; }
.border.paused { box-shadow: inset 0 0 0 3px #e6a23c; }
.pill { position: fixed; right: 12px; bottom: 12px; z-index: 2147483647; display: flex; gap: 8px; align-items: center; pointer-events: auto;
  font: 600 12px/1 system-ui, sans-serif; color: #fff; background: #2b2540; border: 1px solid #7c5cff; border-radius: 999px; padding: 7px 8px 7px 12px;
  box-shadow: 0 4px 18px #0006; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #7c5cff; animation: pulse 1.4s infinite; }
.paused .dot { background: #e6a23c; animation: none; }
button { all: unset; cursor: pointer; padding: 5px 10px; border-radius: 999px; background: #ffffff1f; }
button:hover, button:focus-visible { background: #ffffff40; }
button.stop { background: #c0392b; }
.toast { position: fixed; right: 12px; bottom: 52px; z-index: 2147483647; max-width: 320px; font: 12px/1.35 system-ui, sans-serif; color: #fff; background: #2b2540ee;
  border-radius: 8px; padding: 7px 10px; pointer-events: none; opacity: 0; transition: opacity .2s; }
.toast.show { opacity: 1; }
@media (prefers-reduced-motion: reduce) { .dot { animation: none; } .toast { transition: none; } }
@keyframes pulse { 50% { opacity: .35; } }
`;

function ensure(): void {
  if (host && host.isConnected) return;
  host = document.createElement('abp-overlay');
  host.style.cssText = 'all: initial; position: fixed; inset: 0; pointer-events: none; z-index: 2147483647;';
  root = host.attachShadow({ mode: 'closed' });
  const style = document.createElement('style');
  style.textContent = CSS;
  root.append(style);
  (document.documentElement || document.body).append(host);
}

function send(op: string): void {
  try { void chrome.runtime.sendMessage({ t: 'abp-user', op }); } catch { /* the extension was reloaded; nothing to tell */ }
}

export function setOverlay(next: State, label = 'ABP is controlling this tab'): void {
  state = next;
  if (next === 'off') { host?.remove(); host = null; root = null; pill = null; return; }
  ensure();
  root!.querySelector('.border')?.remove();
  pill?.remove();
  const border = document.createElement('div');
  border.className = `border${next === 'paused' ? ' paused' : ''}`;
  pill = document.createElement('div');
  pill.className = `pill${next === 'paused' ? ' paused' : ''}`;
  pill.setAttribute('role', 'status');
  const dot = document.createElement('span'); dot.className = 'dot';
  const text = document.createElement('span'); text.textContent = next === 'paused' ? 'ABP paused - you have control' : label;
  const stop = document.createElement('button'); stop.className = 'stop'; stop.textContent = 'Stop'; stop.onclick = () => send('stop');
  pill.append(dot, text);
  if (next === 'paused') { const r = document.createElement('button'); r.textContent = 'Resume'; r.onclick = () => send('resume'); pill.append(r); }
  else { const t = document.createElement('button'); t.textContent = 'Take over'; t.onclick = () => send('takeover'); pill.append(t); }
  pill.append(stop);
  root!.append(border, pill);
}

let toastTimer: number | undefined;
export function toast(message: string): void {
  if (state === 'off') return;
  ensure();
  let t = root!.querySelector('.toast') as HTMLElement | null;
  if (!t) { t = document.createElement('div'); t.className = 'toast'; t.setAttribute('aria-live', 'polite'); root!.append(t); }
  t.textContent = message.slice(0, 160);
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => t!.classList.remove('show'), 2200);
}

/** A real person touched the page while ABP was acting: tell the hub so the agent pauses instead of fighting them. */
let lastInput = 0;
export function watchUserInput(): void {
  const handler = (e: Event): void => {
    if (!e.isTrusted || state !== 'active') return;
    if (host && e.composedPath().includes(host)) return;
    const now = Date.now();
    if (now - lastInput < 500) return;
    lastInput = now;
    send('input');
  };
  for (const type of ['pointerdown', 'keydown', 'wheel']) window.addEventListener(type, handler, { capture: true, passive: true });
}
