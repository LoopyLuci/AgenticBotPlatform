// Tiny DOM helpers for the extension's own pages. Everything is built with textContent/createElement - page-derived strings
// are never assigned to innerHTML, so nothing a website says can become markup in the extension.
export interface Status {
  state: string; error: string; paired: boolean; port: number | null; version: string; browser: string; stopped: boolean; paused: boolean; tainted: boolean;
  session: string | null; tabs: Array<{ id: number; url: string; title: string; kind: string; active: boolean }>;
  active_tab: { id: number; url: string; title: string; kind: string | null } | null;
  policy: { max_tabs: number; capabilities: Record<string, boolean>; actions_per_minute: number };
}

export const ask = <T = unknown>(msg: Record<string, unknown>): Promise<T> => chrome.runtime.sendMessage(msg) as Promise<T>;

export function el<K extends keyof HTMLElementTagNameMap>(tag: K, props: Partial<HTMLElementTagNameMap[K]> & { class?: string } = {}, ...kids: Array<Node | string>): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  const { class: cls, ...rest } = props as { class?: string };
  if (cls) e.className = cls;
  Object.assign(e, rest);
  e.append(...kids);
  return e;
}

export const STATE_TEXT: Record<string, string> = {
  connected: 'Connected to ABP', connecting: 'Connecting...', disconnected: 'ABP is not reachable', unpaired: 'Not paired',
  'server-mismatch': 'A different ABP answered', 'protocol-mismatch': 'Version mismatch - update ABP or the extension',
};

export function chip(state: string): HTMLElement { return el('span', { class: `chip ${state}` }, STATE_TEXT[state] ?? state); }

export const hostOf = (u: string): string => { try { return new URL(u).host; } catch { return u.slice(0, 40); } };
export const clock = (t: number): string => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
