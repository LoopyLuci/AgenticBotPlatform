// Content script: one instance per frame, injected on demand by the service worker (never statically on every page).
// It answers only messages from this extension (chrome.runtime.onMessage never delivers page-origin messages).
import { BridgeError, toBridgeError, type WireError } from '../shared/protocol';
import * as act from './actions';
import { accessibleName, isSecretField, isVisible, roleOf } from './dom';
import { setOverlay, toast, watchUserInput } from './overlay';
import { RefTable } from './refs';
import { candidates, locatorFor, takeSnapshot, type SnapshotOptions } from './snapshot';

declare global { interface Window { __abpContent?: boolean } }

interface Msg { abp: 1; op: string; [k: string]: unknown }

if (!window.__abpContent) {
  window.__abpContent = true;
  const refs = new RefTable();
  watchUserInput();

  const resolve = (ref: unknown): Element => {
    const el = typeof ref === 'string' ? refs.resolve(ref, () => candidates(document)) : null;
    if (!el) throw new BridgeError('E_STALE_REF', `element ${String(ref)} is no longer on the page or was never in the latest snapshot`,
      { retryable: true, hint: 'take a new snapshot and use the new refs' });
    return el;
  };

  const handle = async (m: Msg): Promise<unknown> => {
    switch (m.op) {
      case 'ping': return { ok: true, url: location.href, top: window === window.top, ready: document.readyState };
      case 'snapshot': return takeSnapshot(refs, (m.opts ?? {}) as SnapshotOptions);
      case 'text': {
        const sel = typeof m.selector === 'string' && m.selector ? document.querySelector(m.selector) : document.body;
        if (!sel) throw new BridgeError('E_PARAMS', 'no element matches that selector');
        return { url: location.href, title: document.title, text: ((sel as HTMLElement).innerText ?? sel.textContent ?? '').replace(/\n{3,}/g, '\n\n').trim().slice(0, Number(m.max_chars) || 20000) };
      }
      case 'find': {
        const q = String(m.query ?? '').trim().toLowerCase();
        if (!q) throw new BridgeError('E_PARAMS', 'query is required');
        const out: Array<{ ref: string; role: string; name: string }> = [];
        for (const c of candidates(document)) {
          const name = accessibleName(c.el);
          if (name.toLowerCase().includes(q) || roleOf(c.el) === q) out.push({ ref: refs.register(c.el, locatorFor(c.el)), role: roleOf(c.el), name });
          if (out.length >= 20) break;
        }
        return { matches: out };
      }
      case 'act': return await doAct(m);
      case 'settle': return await act.settle(Number(m.timeout_ms) || 8000);
      case 'overlay': setOverlay((m.state as 'off' | 'active' | 'paused') ?? 'off', typeof m.label === 'string' ? m.label : undefined); return { ok: true };
      case 'toast': toast(String(m.text ?? '')); return { ok: true };
      case 'history': {
        // done from inside the page so it also follows single-page-app history entries; deferred so this reply is sent first
        const delta = m.dir === 'forward' ? 1 : -1;
        setTimeout(() => history.go(delta), 0);
        return { ok: true };
      }
      case 'state': return act.pageState();
      default: throw new BridgeError('E_METHOD', `unknown content op ${m.op}`);
    }
  };

  const doAct = async (m: Msg): Promise<unknown> => {
    const action = String(m.action ?? '');
    const args = (m.args ?? {}) as act.ActArgs & { expect_origin?: string };
    const before = act.pageState();
    if (action === 'press' && !m.ref) await act.press(String(args.key ?? ''));
    else if (action === 'scroll') {
      const info = await act.scrollAct(args, m.ref ? resolve(m.ref) : undefined);
      return { ok: true, scroll: info, before, after: act.pageState() };
    } else {
      const el = resolve(m.ref);
      switch (action) {
        case 'click': await act.click(el, args); break;
        case 'dblclick': await act.click(el, { ...args, count: 2 }); break;
        case 'rightclick': await act.click(el, { ...args, button: 'right' }); break;
        case 'hover': await act.hover(el); break;
        case 'focus': await act.focusEl(el); break;
        case 'type': await act.type(el, args); break;
        case 'clear': await act.clearField(el); break;
        case 'select': await act.select(el, args); break;
        case 'check': await act.check(el, args); break;
        case 'press': await act.press(String(args.key ?? ''), el); break;
        case 'fill_secret': {
          // The value comes from ABP's vault, never from a model. It is only ever entered on the origin the login belongs to.
          if (!args.expect_origin || args.expect_origin !== location.origin) {
            throw new BridgeError('E_NOT_ALLOWED', 'this stored login belongs to a different site than the page in front of you');
          }
          if (!(el instanceof HTMLInputElement) || !isVisible(el)) throw new BridgeError('E_NOT_INTERACTABLE', 'not a visible input');
          await act.makeActionable(el);
          el.focus({ preventScroll: true });
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
          setter?.call(el, String(args.value ?? ''));
          el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, inputType: 'insertText' }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return { ok: true, filled: true, secret_field: isSecretField(el), before, after: act.pageState() };   // never echoes the value
        }
        default: throw new BridgeError('E_PARAMS', `unknown action ${action}`);
      }
    }
    const s = await act.settle(Number(args.timeout_ms) || 8000);
    return { ok: true, ...s, before, after: act.pageState() };
  };

  chrome.runtime.onMessage.addListener((raw: unknown, sender, sendResponse) => {
    if (sender.id !== chrome.runtime.id) return false;
    const m = raw as Msg;
    if (!m || m.abp !== 1 || typeof m.op !== 'string') return false;
    handle(m).then(
      (result) => sendResponse({ ok: true, result }),
      (e: unknown) => sendResponse({ ok: false, error: toBridgeError(e).toWire() satisfies WireError }),
    );
    return true;                                                     // async response
  });
}
