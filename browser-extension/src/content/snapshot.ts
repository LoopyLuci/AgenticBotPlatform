// The agent's eyes: a compact, ranked, structured description of what a person could see and use on the page.
import { RefTable, type Locator } from './refs';
import { accessibleName, inViewport, isInteractive, isSecretField, isVisible, pathHint, roleOf, walk } from './dom';

export interface SnapElement {
  ref: string;
  role: string;
  name: string;
  tag: string;
  type?: string;
  value?: string;
  href?: string;
  external?: boolean;          // a link that leaves this site
  checked?: boolean;
  disabled?: boolean;
  required?: boolean;
  expanded?: boolean;
  selected?: boolean;
  secret?: boolean;            // password / card / one-time-code field: never typed into by the agent
  in_view: boolean;
  bbox: [number, number, number, number];
  options?: string[];
}

export interface Snapshot {
  generation: number;
  url: string;
  title: string;
  scroll: { y: number; max_y: number; x: number };
  viewport: { w: number; h: number };
  focus: string | null;
  elements: SnapElement[];
  total_interactive: number;
  truncated: boolean;
  outline: string[];
  overlays: string[];
  text: string;
  frame_url: string;
}

export interface SnapshotOptions { max_elements?: number; text_chars?: number; include_text?: boolean }

const ROLE_WEIGHT: Record<string, number> = { textbox: 5, searchbox: 5, button: 4, link: 3, combobox: 4, checkbox: 4, radio: 4, switch: 4, tab: 3, menuitem: 3 };

export function candidates(doc: Document): Array<{ el: Element; loc: Locator }> {
  const out: Array<{ el: Element; loc: Locator }> = [];
  for (const el of walk(doc)) {
    if (!isInteractive(el) || !isVisible(el)) continue;
    out.push({ el, loc: locatorFor(el) });
  }
  return out;
}

export function locatorFor(el: Element): Locator {
  const r = el.getBoundingClientRect();
  return {
    role: roleOf(el), name: accessibleName(el), tag: el.tagName.toLowerCase(),
    type: el instanceof HTMLInputElement ? el.type : '', path: pathHint(el),
    x: Math.round(r.left + r.width / 2 + scrollX), y: Math.round(r.top + r.height / 2 + scrollY),
  };
}

export function takeSnapshot(refs: RefTable, opts: SnapshotOptions = {}): Snapshot {
  const max = Math.max(10, Math.min(opts.max_elements ?? 80, 1000));
  refs.reset();
  const found: Array<{ el: Element; r: DOMRect; role: string; weight: number; index: number }> = [];
  const outline: string[] = [];
  const overlays: string[] = [];
  let index = 0;
  for (const el of walk(document)) {
    index += 1;
    const tag = el.tagName.toLowerCase();
    if (/^h[1-3]$/.test(tag) && outline.length < 20 && isVisible(el)) {
      const t = (el.textContent ?? '').replace(/\s+/g, ' ').trim();
      if (t) outline.push(`${tag}: ${t.slice(0, 100)}`);
    }
    if ((el.getAttribute('role') === 'dialog' || el.getAttribute('role') === 'alertdialog' || (tag === 'dialog' && (el as HTMLDialogElement).open)) && isVisible(el)) {
      overlays.push(`${el.getAttribute('aria-modal') === 'true' ? 'modal ' : ''}dialog: ${accessibleName(el) || (el.textContent ?? '').trim().slice(0, 80)}`);
    }
    if (!isInteractive(el) || !isVisible(el)) continue;
    const r = el.getBoundingClientRect();
    const role = roleOf(el);
    found.push({ el, r, role, weight: ROLE_WEIGHT[role] ?? 2, index });
  }
  // in-viewport first (by weight), then the rest in document order
  found.sort((a, b) => {
    const av = inViewport(a.r) ? 1 : 0;
    const bv = inViewport(b.r) ? 1 : 0;
    if (av !== bv) return bv - av;
    if (av && a.weight !== b.weight) return b.weight - a.weight;
    return a.index - b.index;
  });
  const chosen = found.slice(0, max).sort((a, b) => a.index - b.index);      // present in document order
  const here = location.hostname;
  const elements: SnapElement[] = chosen.map(({ el, r, role }) => {
    const ref = refs.register(el, locatorFor(el));
    const s: SnapElement = {
      ref, role, name: accessibleName(el), tag: el.tagName.toLowerCase(), in_view: inViewport(r),
      bbox: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
    };
    if (el instanceof HTMLInputElement) {
      s.type = el.type;
      if (el.type === 'checkbox' || el.type === 'radio') s.checked = el.checked;
      else if (el.type !== 'password' && el.type !== 'file') s.value = el.value.slice(0, 200);
      if (isSecretField(el)) s.secret = true;
    } else if (el instanceof HTMLTextAreaElement) {
      s.value = el.value.slice(0, 200);
      if (isSecretField(el)) s.secret = true;
    } else if (el instanceof HTMLSelectElement) {
      s.value = el.selectedOptions[0]?.text ?? '';
      s.options = Array.from(el.options).slice(0, 30).map((o) => o.text.trim().slice(0, 60));
    } else if ((el as HTMLElement).isContentEditable) {
      s.value = (el.textContent ?? '').slice(0, 200);
    }
    if (el instanceof HTMLAnchorElement && el.href) {
      s.href = el.href.slice(0, 300);
      try { s.external = new URL(el.href).hostname !== here; } catch { /* ignore */ }
    }
    if ((el as HTMLButtonElement).disabled || el.getAttribute('aria-disabled') === 'true') s.disabled = true;
    if ((el as HTMLInputElement).required || el.getAttribute('aria-required') === 'true') s.required = true;
    const ex = el.getAttribute('aria-expanded');
    if (ex) s.expanded = ex === 'true';
    const sel = el.getAttribute('aria-selected');
    if (sel) s.selected = sel === 'true';
    const ck = el.getAttribute('aria-checked');
    if (ck && s.checked === undefined) s.checked = ck === 'true';
    return s;
  });
  let text = '';
  if (opts.include_text !== false) {
    text = (document.body?.innerText ?? '').replace(/\n{3,}/g, '\n\n').trim().slice(0, Math.max(500, opts.text_chars ?? 6000));
  }
  const active = document.activeElement;
  let focus: string | null = null;
  if (active) for (const el of elements) { const live = refs.resolve(el.ref, () => []); if (live === active) { focus = el.ref; break; } }
  const doc = document.documentElement;
  return {
    generation: refs.generation, url: location.href, title: document.title, frame_url: location.href,
    scroll: { y: Math.round(scrollY), max_y: Math.max(0, Math.round(doc.scrollHeight - innerHeight)), x: Math.round(scrollX) },
    viewport: { w: innerWidth, h: innerHeight }, focus, elements, total_interactive: found.length, truncated: found.length > max,
    outline, overlays, text,
  };
}
