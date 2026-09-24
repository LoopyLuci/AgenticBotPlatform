// Talking to the content script in a tab/frame, injecting it on demand (never statically into every page).
import { BridgeError, type WireError } from '../shared/protocol';

interface Reply<T> { ok: boolean; result?: T; error?: WireError }

async function ping(tabId: number, frameId: number): Promise<boolean> {
  try {
    const r = (await chrome.tabs.sendMessage(tabId, { abp: 1, op: 'ping' }, { frameId })) as Reply<unknown> | undefined;
    return !!r?.ok;
  } catch { return false; }
}

export async function ensureContent(tabId: number, frameId = 0): Promise<void> {
  if (await ping(tabId, frameId)) return;
  try {
    await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files: ['content.js'] });
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (/cannot access|permission|host/i.test(msg)) {
      throw new BridgeError('E_NOT_ALLOWED', 'the extension does not have access to this site', { hint: 'the person can allow ABP on this site from the extension popup' });
    }
    throw new BridgeError('E_NO_TAB', msg.slice(0, 200), { retryable: true });
  }
  for (let i = 0; i < 10; i++) {
    if (await ping(tabId, frameId)) return;
    await new Promise((r) => setTimeout(r, 60));
  }
  throw new BridgeError('E_BLOCKED_BY_PAGE', 'the page did not accept the ABP helper script', { retryable: true });
}

export async function send<T = unknown>(tabId: number, msg: Record<string, unknown>, frameId = 0): Promise<T> {
  await ensureContent(tabId, frameId);
  let reply: Reply<T> | undefined;
  try {
    reply = (await chrome.tabs.sendMessage(tabId, { abp: 1, ...msg }, { frameId })) as Reply<T> | undefined;
  } catch (e) {
    throw new BridgeError('E_PAGE_CHANGED', `the page changed while ABP was working (${e instanceof Error ? e.message.slice(0, 120) : 'no reply'})`, { retryable: true, hint: 'take a new snapshot' });
  }
  if (!reply) throw new BridgeError('E_PAGE_CHANGED', 'the page did not answer', { retryable: true });
  if (!reply.ok) {
    const e = reply.error ?? { code: 'E_INTERNAL', message: 'unknown error' };
    throw new BridgeError(e.code, e.message, { retryable: e.retryable, data: e.data, hint: e.hint });
  }
  return reply.result as T;
}

export async function frames(tabId: number): Promise<Array<{ frameId: number; url: string; parentFrameId: number }>> {
  const all = await chrome.webNavigation.getAllFrames({ tabId });
  return (all ?? []).map((f) => ({ frameId: f.frameId, url: f.url, parentFrameId: f.parentFrameId }));
}
