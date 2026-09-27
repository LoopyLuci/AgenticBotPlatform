import chatgpt from './chatgpt.json';
import claude from './claude.json';
import gemini from './gemini.json';
import grok from './grok.json';
import perplexity from './perplexity.json';
import mock from './mock.json';
import type { AdapterDef } from './types';

declare const __ABP_DEV__: boolean;

const real = [chatgpt, claude, gemini, grok, perplexity] as unknown as AdapterDef[];
/** Store builds ship only the real adapters; development builds add a mock chat page for the test suite. */
export const ADAPTERS: AdapterDef[] = typeof __ABP_DEV__ !== 'undefined' && __ABP_DEV__ ? [...real, mock as unknown as AdapterDef] : real;

export const adapterById = (id: string): AdapterDef | undefined => ADAPTERS.find((a) => a.id === id);

/** Does this URL belong to the adapter's site? ("host/path" entries restrict to a path prefix.) */
export function urlMatches(a: AdapterDef, url: string): boolean {
  let u: URL;
  try { u = new URL(url); } catch { return false; }
  if (u.protocol !== 'https:' && !(a.dev && u.protocol === 'http:')) return false;
  const host = u.hostname.toLowerCase();
  return a.hosts.some((h) => {
    const slash = h.indexOf('/');
    const hh = (slash < 0 ? h : h.slice(0, slash)).toLowerCase();
    if (!(host === hh || host.endsWith('.' + hh))) return false;
    return slash < 0 || u.pathname.toLowerCase().startsWith(h.slice(slash).toLowerCase());
  });
}

export const originsFor = (a: AdapterDef): string[] => {
  const bases = Array.from(new Set(a.hosts.map((h) => h.split('/')[0]!)));
  const scheme = a.dev ? 'http' : 'https';
  return bases.flatMap((b) => [`${scheme}://${b}/*`, `${scheme}://*.${b}/*`]);
};
