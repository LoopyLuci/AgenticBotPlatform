// Pairing: no token is ever typed. Either the person reads a 6-digit code off the ABP desktop app and enters it here, or this
// extension asks and the person approves on the desktop. Both end with a key stored in extension-private storage.
import { clearConfig, getConfig, setConfig, type Config } from './storage';

export const DEFAULT_PORT = 8787;
const PORTS = [DEFAULT_PORT, 8788, 8080];

export interface Hello { abp: true; protocol: number; server_id: string; pairing_open: boolean }

async function get(port: number, path: string, timeoutMs = 1500): Promise<Response> {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), timeoutMs);
  try { return await fetch(`http://127.0.0.1:${port}${path}`, { signal: ac.signal }); } finally { clearTimeout(t); }
}

/** Find a running ABP: the last known port first, then the usual ones. */
export async function discover(preferred?: number, onlyPreferred = false): Promise<{ port: number; hello: Hello } | null> {
  const ports = onlyPreferred && preferred ? [preferred] : [...new Set([preferred, ...PORTS].filter((p): p is number => !!p))];
  for (const port of ports) {
    try {
      const r = await get(port, '/api/browser/hello');
      if (!r.ok) continue;
      const hello = (await r.json()) as Hello;
      if (hello.abp === true) return { port, hello };
    } catch { /* not here */ }
  }
  return null;
}

async function post(port: number, path: string, body: unknown): Promise<{ status: number; json: any }> {
  const r = await fetch(`http://127.0.0.1:${port}${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  let json: any = {};
  try { json = await r.json(); } catch { /* empty */ }
  return { status: r.status, json };
}

const browserName = (): string => {
  const ua = navigator.userAgent;
  if (/Edg\//.test(ua)) return 'Edge';
  if (/OPR\//.test(ua)) return 'Opera';
  if (/Brave/.test(ua)) return 'Brave';
  if (/Firefox\//.test(ua)) return 'Firefox';
  return 'Chrome';
};

export async function pairWithCode(code: string, portHint?: number): Promise<Config> {
  const found = await discover(portHint, !!portHint);
  if (!found) throw new Error('ABP is not running on this computer. Start the ABP desktop app first.');
  const { status, json } = await post(found.port, '/api/browser/pair/complete', { code: code.trim(), browser: browserName(), version: chrome.runtime.getManifest().version });
  if (status !== 200) throw new Error(json?.detail?.message ?? `pairing failed (${status})`);
  const cfg: Config = { port: found.port, key: json.key, server_id: json.server_id };
  await setConfig(cfg);
  return cfg;
}

/** Ask the desktop to approve this browser; resolves when the person decides (or after `timeoutMs`). */
export async function pairByApproval(portHint?: number, timeoutMs = 5 * 60_000, signal?: AbortSignal): Promise<Config> {
  const found = await discover(portHint, !!portHint);
  if (!found) throw new Error('ABP is not running on this computer. Start the ABP desktop app first.');
  const { status, json } = await post(found.port, '/api/browser/pair/request', { browser: browserName(), version: chrome.runtime.getManifest().version });
  if (status !== 200) throw new Error(json?.detail?.message ?? `could not ask ABP (${status})`);
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (signal?.aborted) throw new Error('cancelled');
    await new Promise((r) => setTimeout(r, 1500));
    const c = await post(found.port, '/api/browser/pair/collect', { request_id: json.request_id, nonce: json.nonce });
    if (c.status !== 200) throw new Error(c.json?.detail?.message ?? 'lost the pairing request');
    if (c.json.state === 'approved') { const cfg: Config = { port: found.port, key: c.json.key, server_id: c.json.server_id }; await setConfig(cfg); return cfg; }
    if (c.json.state === 'denied') throw new Error('You denied the request in ABP.');
    if (c.json.state === 'expired' || c.json.state === 'collected') throw new Error('The pairing request expired. Try again.');
  }
  throw new Error('Timed out waiting for approval in the ABP desktop app.');
}

export async function unpair(): Promise<void> { await clearConfig(); }
export const pairedConfig = getConfig;
