// Doing things on a page, the way a person would: scroll to it, make sure it is really there and not covered, then
// produce the full event sequence a real click/keystroke produces (so React/Vue/Angular handlers fire), and wait for the
// page to settle. Refusals are typed errors, never silent no-ops.
import { BridgeError } from '../shared/protocol';
import { hitTest, isSecretField, isVisible } from './dom';

export interface ActArgs {
  text?: string;
  value?: string;
  key?: string;
  submit?: boolean;
  clear?: boolean;
  direction?: 'up' | 'down' | 'left' | 'right' | 'top' | 'bottom';
  amount?: number;
  checked?: boolean;
  button?: 'left' | 'right';
  count?: number;
  modifiers?: string[];
  timeout_ms?: number;
}

const frame = (): Promise<void> => new Promise((r) => requestAnimationFrame(() => r()));
export const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

export interface PageState { url: string; title: string; scroll_y: number; focus_tag: string; dialogs: number; interactive_hint: number }

export function pageState(): PageState {
  return {
    url: location.href, title: document.title, scroll_y: Math.round(scrollY), focus_tag: document.activeElement?.tagName.toLowerCase() ?? '',
    dialogs: document.querySelectorAll('dialog[open],[role=dialog],[role=alertdialog]').length,
    interactive_hint: document.querySelectorAll('a[href],button,input,select,textarea,[role=button]').length,
  };
}

/** Wait until the DOM has been quiet for `quietMs` (or the timeout), and the document finished loading. */
export async function settle(timeoutMs = 8000, quietMs = 300): Promise<{ settled: boolean; mutations: number }> {
  let mutations = 0;
  let last = performance.now();
  const mo = new MutationObserver((list) => { mutations += list.length; last = performance.now(); });
  mo.observe(document.documentElement, { subtree: true, childList: true, attributes: true, characterData: true });
  const start = performance.now();
  try {
    for (;;) {
      await sleep(50);
      const now = performance.now();
      if (document.readyState === 'complete' && now - last >= quietMs) return { settled: true, mutations };
      if (now - start >= timeoutMs) return { settled: false, mutations };
    }
  } finally { mo.disconnect(); }
}

interface Actionable { el: HTMLElement; x: number; y: number }

/** Scrolls the element into view and proves it is visible, enabled and not covered. Throws a typed error otherwise. */
export async function makeActionable(el: Element, opts: { needEnabled?: boolean } = {}): Promise<Actionable> {
  if (!el.isConnected) throw new BridgeError('E_STALE_REF', 'the element is no longer on the page', { retryable: true, hint: 'take a new snapshot' });
  const he = el as HTMLElement;
  he.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' as ScrollBehavior });
  await frame();
  if (!isVisible(el)) throw new BridgeError('E_NOT_INTERACTABLE', 'the element is not visible', { hint: 'it may be hidden until something else is opened' });
  if (opts.needEnabled !== false && ((el as HTMLButtonElement).disabled || el.getAttribute('aria-disabled') === 'true')) {
    throw new BridgeError('E_NOT_INTERACTABLE', 'the element is disabled', { hint: 'a required field may need to be filled first' });
  }
  for (let attempt = 0; attempt < 3; attempt++) {
    const r = el.getBoundingClientRect();
    const x = Math.min(innerWidth - 1, Math.max(0, r.left + r.width / 2));
    const y = Math.min(innerHeight - 1, Math.max(0, r.top + r.height / 2));
    const top = hitTest(x, y, el);
    if (top && (top === el || el.contains(top) || top.contains(el) || (el as HTMLLabelElement).control === top)) return { el: he, x, y };
    if (attempt === 2) {
      const what = top ? `<${top.tagName.toLowerCase()}${top.id ? '#' + top.id : ''}>` : 'another element';
      throw new BridgeError('E_BLOCKED_BY_PAGE', `the element is covered by ${what}`, { hint: 'a dialog, banner or overlay may need to be dismissed first' });
    }
    await sleep(120);
    await frame();
  }
  throw new BridgeError('E_NOT_INTERACTABLE', 'could not reach the element');
}

const mouse = (el: Element, type: string, x: number, y: number, init: MouseEventInit = {}): boolean =>
  el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, composed: true, view: window, clientX: x, clientY: y, ...init }));
const pointer = (el: Element, type: string, x: number, y: number, init: PointerEventInit = {}): boolean =>
  el.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true, composed: true, view: window, clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse', isPrimary: true, ...init }));

export async function click(el: Element, args: ActArgs = {}): Promise<void> {
  const { el: t, x, y } = await makeActionable(el);
  const right = args.button === 'right';
  const button = right ? 2 : 0;
  const buttons = right ? 2 : 1;
  const mods = { ctrlKey: !!args.modifiers?.includes('ctrl'), shiftKey: !!args.modifiers?.includes('shift'), altKey: !!args.modifiers?.includes('alt'), metaKey: !!args.modifiers?.includes('meta') };
  for (const type of ['pointerover', 'pointerenter']) pointer(t, type, x, y, mods);
  for (const type of ['mouseover', 'mouseenter', 'mousemove']) mouse(t, type, x, y, mods);
  const times = Math.max(1, Math.min(args.count ?? 1, 3));
  for (let i = 1; i <= times; i++) {
    pointer(t, 'pointerdown', x, y, { button, buttons, ...mods });
    const down = mouse(t, 'mousedown', x, y, { button, buttons, detail: i, ...mods });
    if (down) t.focus?.({ preventScroll: true });
    pointer(t, 'pointerup', x, y, { button, buttons: 0, ...mods });
    mouse(t, 'mouseup', x, y, { button, buttons: 0, detail: i, ...mods });
    if (right) mouse(t, 'contextmenu', x, y, { button, ...mods });
    else mouse(t, 'click', x, y, { button, detail: i, ...mods });
  }
  if (times === 2 && !right) mouse(t, 'dblclick', x, y, { button, detail: 2, ...mods });
}

export async function hover(el: Element): Promise<void> {
  const { el: t, x, y } = await makeActionable(el, { needEnabled: false });
  for (const type of ['pointerover', 'pointerenter']) pointer(t, type, x, y);
  for (const type of ['mouseover', 'mouseenter', 'mousemove']) mouse(t, type, x, y);
}

export async function focusEl(el: Element): Promise<void> {
  const { el: t } = await makeActionable(el, { needEnabled: false });
  t.focus({ preventScroll: true });
}

function setNativeValue(el: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  // React and friends track the value through the element's own setter; assign through the prototype's so their tracker sees a change.
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
  if (setter) setter.call(el, value); else el.value = value;
}

export async function type(el: Element, args: ActArgs): Promise<void> {
  if (isSecretField(el)) {
    throw new BridgeError('E_NOT_ALLOWED', 'that is a password, card or one-time-code field: the agent does not type into these',
      { hint: 'use fill_credential with a stored login, or hand off to the person' });
  }
  const text = String(args.text ?? '');
  const { el: t } = await makeActionable(el);
  t.focus({ preventScroll: true });
  if (t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement) {
    if (t instanceof HTMLInputElement && ['checkbox', 'radio', 'button', 'submit', 'file', 'range', 'color'].includes(t.type)) {
      throw new BridgeError('E_NOT_INTERACTABLE', `an <input type=${t.type}> cannot be typed into`);
    }
    const next = args.clear === false ? t.value + text : text;
    t.select?.();
    t.dispatchEvent(new KeyboardEvent('keydown', { key: text.slice(0, 1) || 'a', bubbles: true, composed: true }));
    setNativeValue(t, next);
    t.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, inputType: 'insertText', data: text }));
    t.dispatchEvent(new KeyboardEvent('keyup', { key: text.slice(-1) || 'a', bubbles: true, composed: true }));
    t.dispatchEvent(new Event('change', { bubbles: true }));
  } else if (t.isContentEditable) {
    const sel = getSelection();
    if (sel && args.clear !== false) { const range = document.createRange(); range.selectNodeContents(t); sel.removeAllRanges(); sel.addRange(range); }
    if (!document.execCommand('insertText', false, text)) t.textContent = (args.clear === false ? t.textContent ?? '' : '') + text;
    t.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, inputType: 'insertText', data: text }));
  } else {
    throw new BridgeError('E_NOT_INTERACTABLE', 'that element does not accept text');
  }
  if (args.submit) await press('Enter', t);
}

const KEYCODES: Record<string, number> = { Enter: 13, Tab: 9, Escape: 27, Backspace: 8, Delete: 46, ArrowUp: 38, ArrowDown: 40, ArrowLeft: 37, ArrowRight: 39, ' ': 32, Home: 36, End: 35, PageUp: 33, PageDown: 34 };

export async function press(key: string, target?: Element): Promise<void> {
  if (!key || key.length > 20) throw new BridgeError('E_PARAMS', 'key must be a key name like Enter, Tab, Escape, ArrowDown or a single character');
  const t = (target ?? document.activeElement ?? document.body) as HTMLElement;
  const code = KEYCODES[key] ?? key.toUpperCase().charCodeAt(0);
  const init: KeyboardEventInit = { key, code: key.length === 1 ? `Key${key.toUpperCase()}` : key, keyCode: code, which: code, bubbles: true, cancelable: true, composed: true };
  const down = t.dispatchEvent(new KeyboardEvent('keydown', init));
  if (down) {
    t.dispatchEvent(new KeyboardEvent('keypress', init));
    // Synthetic key events never trigger the browser's default actions, so perform the meaningful ones ourselves.
    if (key === 'Enter') {
      const form = (t as HTMLInputElement).form;
      if (t instanceof HTMLTextAreaElement) { /* newline, not submit */ }
      else if (t instanceof HTMLButtonElement || t instanceof HTMLAnchorElement) t.click();
      else if (form) { if (form.requestSubmit) form.requestSubmit(); else form.submit(); }
    } else if (key === 'Tab') {
      const order = Array.from(document.querySelectorAll<HTMLElement>('a[href],button,input,select,textarea,[tabindex]')).filter((e) => isVisible(e) && e.tabIndex >= 0);
      const i = order.indexOf(t);
      order[(i + 1) % Math.max(1, order.length)]?.focus();
    } else if (key === 'Escape') {
      document.querySelector<HTMLDialogElement>('dialog[open]')?.close();
    }
  }
  t.dispatchEvent(new KeyboardEvent('keyup', init));
}

export async function select(el: Element, args: ActArgs): Promise<void> {
  if (!(el instanceof HTMLSelectElement)) throw new BridgeError('E_NOT_INTERACTABLE', 'that element is not a <select>');
  await makeActionable(el);
  const want = String(args.value ?? '').trim().toLowerCase();
  const opt = Array.from(el.options).find((o) => o.value.toLowerCase() === want || o.text.trim().toLowerCase() === want)
    ?? Array.from(el.options).find((o) => o.text.trim().toLowerCase().includes(want));
  if (!opt) throw new BridgeError('E_PARAMS', `no option matches "${args.value}"`, { data: { options: Array.from(el.options).slice(0, 30).map((o) => o.text.trim()) } });
  el.value = opt.value;
  el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

export async function check(el: Element, args: ActArgs): Promise<void> {
  const want = args.checked ?? true;
  const cur = el instanceof HTMLInputElement ? el.checked : el.getAttribute('aria-checked') === 'true';
  if (cur !== want) await click(el);
}

export async function scrollAct(args: ActArgs, el?: Element): Promise<{ y: number; max_y: number; at_end: boolean }> {
  const dir = args.direction ?? 'down';
  const target: Element = el && el.scrollHeight > el.clientHeight ? el : document.scrollingElement ?? document.documentElement;
  const page = target === document.scrollingElement || target === document.documentElement;
  const h = page ? innerHeight : (target as HTMLElement).clientHeight;
  const amt = Math.max(50, Math.min(args.amount ?? Math.round(h * 0.8), 5000));
  const dy = dir === 'down' ? amt : dir === 'up' ? -amt : 0;
  const dx = dir === 'right' ? amt : dir === 'left' ? -amt : 0;
  if (dir === 'top') target.scrollTo({ top: 0, behavior: 'instant' as ScrollBehavior });
  else if (dir === 'bottom') target.scrollTo({ top: target.scrollHeight, behavior: 'instant' as ScrollBehavior });
  else target.scrollBy({ top: dy, left: dx, behavior: 'instant' as ScrollBehavior });
  await frame();
  const max = Math.max(0, target.scrollHeight - h);
  return { y: Math.round(target.scrollTop), max_y: Math.round(max), at_end: target.scrollTop >= max - 2 };
}

export async function clearField(el: Element): Promise<void> {
  await type(el, { text: '', clear: true });
}
