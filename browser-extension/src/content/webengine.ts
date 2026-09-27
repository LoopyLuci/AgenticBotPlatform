// Interprets an AdapterDef (src/adapters) against the live DOM of a chat website: this is the "declarative, no site-specific
// code" engine promised by DESIGN.md 7.1. Runs inside the content script of a web-session tab only — never in an ordinary
// agent-controlled tab, and never touches anything the adapter's own selectors don't name.
import type { AdapterDef, ProbeResult, ReadResult, Sel } from '../adapters/types';
import { isVisible } from './dom';

/** Resolve one selector-list entry to the (visible) elements it names. Unknown prefixes fail closed (match nothing) rather
 * than silently falling back to a raw CSS parse of adapter-authored text. */
function matchOne(sel: string): Element[] {
  if (sel.startsWith('aria:')) {
    const needle = sel.slice(5).trim().toLowerCase();
    return [...document.querySelectorAll('[aria-label]')].filter((el) => (el.getAttribute('aria-label') || '').toLowerCase().includes(needle));
  }
  if (sel.startsWith('text:')) {
    const rest = sel.slice(5);
    const i = rest.indexOf(':');
    const tag = i < 0 ? '*' : rest.slice(0, i);
    const needle = (i < 0 ? rest : rest.slice(i + 1)).trim().toLowerCase();
    return [...document.querySelectorAll(tag)].filter((el) => (el.textContent || '').trim().toLowerCase().includes(needle));
  }
  try { return [...document.querySelectorAll(sel)]; } catch { return []; }
}

/** First selector in the ranked list with at least one visible match, and how many entries were tried before it hit
 * (the "which fallback matched" signal DESIGN.md asks for, so drift is visible before it breaks). */
function resolve(list: Sel | undefined): { el: Element | null; index: number; total: number } {
  const arr = list ?? [];
  for (let i = 0; i < arr.length; i++) {
    const found = matchOne(arr[i]!).filter(isVisible);
    if (found.length) return { el: found[0]!, index: i, total: arr.length };
  }
  return { el: null, index: -1, total: arr.length };
}

function stripped(el: Element, strip: string[] = []): string {
  const clone = el.cloneNode(true) as Element;
  for (const sel of strip) { try { clone.querySelectorAll(sel).forEach((n) => n.remove()); } catch { /* a bad selector just strips nothing */ } }
  return (clone as HTMLElement).innerText?.trim() ?? (clone.textContent || '').trim();
}

function errorBanner(a: AdapterDef): { code: string; message: string } | null {
  for (const rule of a.errors ?? []) {
    const re = new RegExp(rule.match, 'i');
    const scope = rule.within?.length ? rule.within.flatMap((s) => matchOne(s)) : [document.body];
    for (const el of scope) {
      const text = (el as HTMLElement).innerText ?? el.textContent ?? '';
      if (text && re.test(text)) return { code: rule.code, message: rule.message };
    }
  }
  return null;
}

export function probe(a: AdapterDef): ProbeResult {
  const input = resolve(a.compose.input);
  const submit = resolve(a.compose.submit);
  const assistant = resolve(a.stream.assistant);
  const generating = resolve(a.stream.generating);
  const loggedOut = (a.login.logged_out?.length ? resolve(a.login.logged_out).el !== null : false);
  const ready = !loggedOut && resolve(a.login.ready).el !== null;
  return {
    ready, logged_out: loggedOut, url: location.href, title: document.title,
    matched: { input: input.index, submit: submit.index, assistant: assistant.index, generating: generating.index },
    error: errorBanner(a),
  };
}

function setEditableValue(el: Element, text: string): void {
  if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    setter?.call(el, text);
    el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, inputType: 'insertFromPaste', data: text }));
    return;
  }
  // A contenteditable rich-text box (ProseMirror/Quill/Lexical and friends): a real paste event is what these editors listen for.
  el.dispatchEvent(new FocusEvent('focus', { bubbles: true }));
  (el as HTMLElement).focus?.();
  const dt = new DataTransfer();
  dt.setData('text/plain', text);
  const pasted = new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: dt });
  if (el.dispatchEvent(pasted)) {
    // Nothing handled the paste event (a plain contenteditable with no editor framework): fall back to execCommand/textContent.
    try { document.execCommand('insertText', false, text); } catch { /* ignore */ }
    if (!(el.textContent || '').trim()) {
      el.textContent = text;
      el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true }));
    }
  }
}

export async function compose(a: AdapterDef, text: string): Promise<{ submitted: boolean }> {
  const { el: input } = resolve(a.compose.input);
  if (!input) throw new Error('E_ADAPTER_BROKEN: could not find the compose box on this page');
  setEditableValue(input, text.slice(0, a.limits.max_prompt_chars));
  await new Promise((r) => setTimeout(r, 60));                    // let the site's own framework react before we submit
  const { el: submit } = resolve(a.compose.submit);
  if (submit && !(submit as HTMLButtonElement).disabled) { (submit as HTMLElement).click(); return { submitted: true }; }
  if (a.compose.key === 'Enter' || !a.compose.key) {
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
    input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
    return { submitted: true };
  }
  return { submitted: false };
}

export function read(a: AdapterDef): ReadResult {
  const nodes = (a.stream.assistant.length ? a.stream.assistant.flatMap((s) => matchOne(s)) : []);
  const last = nodes.length ? nodes[nodes.length - 1]! : null;
  const gen = resolve(a.stream.generating);
  return {
    count: nodes.length, text: last ? stripped(last, a.stream.strip) : '', generating: gen.el !== null,
    error: errorBanner(a), matched: { assistant: nodes.length ? nodes.length - 1 : -1, generating: gen.index },
  };
}

/** Wait until the reply looks finished: the "generating"/stop indicator is gone AND the assistant text has not changed for
 * `quietMs`, checked by MutationObserver (not polling) so a slow, still-typing reply is never cut short. */
export function waitDone(a: AdapterDef, timeoutMs: number, onDelta: (text: string) => void): Promise<ReadResult> {
  return new Promise((resolvePromise, reject) => {
    const quiet = a.stream.quiet_ms ?? 1200;
    let lastText = '';
    let lastChange = Date.now();
    let settled = false;
    const finish = (result: ReadResult | null, err?: Error): void => {
      if (settled) return;
      settled = true;
      obs.disconnect();
      clearInterval(tick);
      if (err) reject(err); else resolvePromise(result!);
    };
    const check = (): void => {
      const r = read(a);
      if (r.error) { finish(null, Object.assign(new Error(r.error.message), { code: r.error.code })); return; }
      if (r.text !== lastText) { lastText = r.text; lastChange = Date.now(); onDelta(r.text); }
      if (!r.generating && Date.now() - lastChange >= quiet) { finish(r); return; }
      if (Date.now() - lastChange >= quiet && r.count === 0) { finish(r); return; }         // nothing ever appeared, but it settled
    };
    const obs = new MutationObserver(() => check());
    obs.observe(document.body, { childList: true, subtree: true, characterData: true });
    const tick = setInterval(check, 400);
    setTimeout(() => finish(read(a)), timeoutMs);
    check();
  });
}
