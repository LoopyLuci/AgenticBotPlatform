// Durable state lives in chrome.storage (the service worker is killed and restarted constantly). Nothing is kept only in memory
// unless losing it on a restart is safe.
export interface Config {
  port: number;
  key: string;                 // the paired browser key; only ever sent to the bridge WebSocket (never in a URL)
  server_id: string;           // pinned at pairing: a different server on the same port is refused
}

export async function getConfig(): Promise<Config | null> {
  const r = await chrome.storage.local.get('abp.config');
  const c = r['abp.config'] as Config | undefined;
  return c && c.key && c.port ? c : null;
}
export const setConfig = (c: Config): Promise<void> => chrome.storage.local.set({ 'abp.config': c });
export const clearConfig = (): Promise<void> => chrome.storage.local.remove('abp.config');

export async function getSession<T>(key: string, fallback: T): Promise<T> {
  const r = await chrome.storage.session.get(key);
  return (r[key] as T | undefined) ?? fallback;
}
export const setSession = (key: string, value: unknown): Promise<void> => chrome.storage.session.set({ [key]: value });

export async function getLocal<T>(key: string, fallback: T): Promise<T> {
  const r = await chrome.storage.local.get(key);
  return (r[key] as T | undefined) ?? fallback;
}
export const setLocal = (key: string, value: unknown): Promise<void> => chrome.storage.local.set({ [key]: value });
