// DOM understanding shared by snapshot and actions: roles, accessible names, visibility, secret-field detection.

export const isElement = (n: Node | null): n is Element => !!n && n.nodeType === 1;

const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'META', 'LINK', 'HEAD', 'TITLE', 'BASE', 'SVG', 'PATH']);

export function isVisible(el: Element): boolean {
  const he = el as HTMLElement;
  if (typeof he.checkVisibility === 'function') {
    if (!he.checkVisibility({ checkOpacity: false, checkVisibilityCSS: true })) return false;
  } else {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
  }
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}

export function inViewport(r: DOMRect): boolean {
  return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
}

const INPUT_ROLES: Record<string, string> = {
  button: 'button', submit: 'button', reset: 'button', image: 'button', checkbox: 'checkbox', radio: 'radio', range: 'slider',
  search: 'searchbox', email: 'textbox', tel: 'textbox', url: 'textbox', number: 'spinbutton', password: 'textbox', text: 'textbox',
  date: 'textbox', 'datetime-local': 'textbox', month: 'textbox', time: 'textbox', week: 'textbox', color: 'button', file: 'button',
};

export function roleOf(el: Element): string {
  const explicit = el.getAttribute('role');
  if (explicit) return explicit.split(/\s+/)[0]!.toLowerCase();
  const tag = el.tagName.toLowerCase();
  switch (tag) {
    case 'a': return el.hasAttribute('href') ? 'link' : 'generic';
    case 'button': return 'button';
    case 'select': return (el as HTMLSelectElement).multiple ? 'listbox' : 'combobox';
    case 'textarea': return 'textbox';
    case 'summary': return 'button';
    case 'input': return INPUT_ROLES[((el as HTMLInputElement).type || 'text').toLowerCase()] ?? 'textbox';
    case 'option': return 'option';
    case 'h1': case 'h2': case 'h3': case 'h4': case 'h5': case 'h6': return 'heading';
    case 'img': return 'img';
    case 'nav': return 'navigation';
    case 'main': return 'main';
    case 'dialog': return 'dialog';
    case 'form': return 'form';
    default:
      if ((el as HTMLElement).isContentEditable) return 'textbox';
      return 'generic';
  }
}

const INTERACTIVE_ROLES = new Set(['button', 'link', 'textbox', 'searchbox', 'checkbox', 'radio', 'combobox', 'listbox', 'option', 'menuitem',
  'menuitemcheckbox', 'menuitemradio', 'tab', 'switch', 'slider', 'spinbutton', 'treeitem']);

export function isInteractive(el: Element): boolean {
  const role = roleOf(el);
  if (INTERACTIVE_ROLES.has(role)) return true;
  const he = el as HTMLElement;
  if (he.isContentEditable && el.getAttribute('contenteditable') !== 'inherit') return true;
  const tab = el.getAttribute('tabindex');
  if (tab !== null && Number(tab) >= 0 && role === 'generic') return true;
  if (el.hasAttribute('onclick')) return true;
  return false;
}

const text = (s: string | null | undefined, max = 120): string => (s ?? '').replace(/\s+/g, ' ').trim().slice(0, max);

export function accessibleName(el: Element): string {
  const aria = el.getAttribute('aria-label');
  if (aria && aria.trim()) return text(aria);
  const by = el.getAttribute('aria-labelledby');
  if (by) {
    const root = el.getRootNode() as Document | ShadowRoot;
    const t = by.split(/\s+/).map((id) => root.getElementById?.(id)?.textContent ?? '').join(' ');
    if (t.trim()) return text(t);
  }
  if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) {
    const labels = (el as HTMLInputElement).labels;
    if (labels && labels.length) return text(Array.from(labels).map((l) => l.textContent).join(' '));
    if (el instanceof HTMLInputElement && ['button', 'submit', 'reset'].includes(el.type)) return text(el.value);
    const ph = el.getAttribute('placeholder');
    if (ph) return text(ph);
    if (el.title) return text(el.title);
    if (el.name) return text(el.name);
    return '';
  }
  if (el instanceof HTMLImageElement) return text(el.alt || el.title);
  const own = text(el.textContent);
  if (own) return own;
  const img = el.querySelector('img[alt]');
  if (img) return text(img.getAttribute('alt'));
  return text(el.getAttribute('title'));
}

export const SECRET_RE = /pass(word|wd|code)?|passwd|card[-_ ]?(number|no)|cc[-_ ]?(num|number)|cvv|cvc|security[-_ ]?code|otp|one[-_ ]?time|2fa|mfa|totp|verification[-_ ]?code|ssn|social[-_ ]?security/i;

/** Password, card number, one-time code, SSN fields: the agent never types into these, whatever the model says. */
export function isSecretField(el: Element): boolean {
  if (el instanceof HTMLInputElement) {
    if (el.type === 'password') return true;
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
    if (/(^|\s)(cc-|one-time-code|current-password|new-password)/.test(ac)) return true;
    return SECRET_RE.test(`${el.name} ${el.id} ${el.getAttribute('aria-label') ?? ''} ${el.placeholder}`);
  }
  if (el instanceof HTMLTextAreaElement) return SECRET_RE.test(`${el.name} ${el.id} ${el.getAttribute('aria-label') ?? ''}`);
  return false;
}

/** A short, stable-ish breadcrumb used only as a re-location hint. */
export function pathHint(el: Element): string {
  const parts: string[] = [];
  let cur: Element | null = el;
  for (let i = 0; cur && i < 4; i++) {
    const tag = cur.tagName.toLowerCase();
    const id = cur.id && !/\d{4,}/.test(cur.id) ? `#${cur.id}` : '';
    parts.unshift(tag + id);
    cur = cur.parentElement ?? ((cur.getRootNode() as ShadowRoot).host ?? null);
  }
  return parts.join('>');
}

export { SKIP };

/** Every element under `root`, piercing open shadow roots, in document order. */
export function* walk(root: Document | ShadowRoot | Element): Generator<Element> {
  const start: Element[] = root instanceof Document ? (root.documentElement ? [root.documentElement] : []) : root instanceof ShadowRoot ? Array.from(root.children) : [root];
  const stack: Element[] = [...start].reverse();
  while (stack.length) {
    const el = stack.pop()!;
    if (SKIP.has(el.tagName.toUpperCase())) continue;
    yield el;
    const kids: Element[] = [];
    if (el.shadowRoot) kids.push(...Array.from(el.shadowRoot.children));
    kids.push(...Array.from(el.children));
    for (let i = kids.length - 1; i >= 0; i--) stack.push(kids[i]!);
  }
}

export function hitTest(x: number, y: number, within: Element): Element | null {
  const root = within.getRootNode() as Document | ShadowRoot;
  let top: Element | null = (root.elementFromPoint ? root.elementFromPoint(x, y) : document.elementFromPoint(x, y));
  // descend into nested shadow roots the same way the browser hit-tests
  while (top && top.shadowRoot) {
    const inner = top.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === top) break;
    top = inner;
  }
  return top;
}
