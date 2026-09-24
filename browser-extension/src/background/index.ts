// The hub. Wires the bridge, policy, tabs and router together and answers the extension's own UI.
import { BridgeClient, type BridgeState } from './bridge';
import { audit, auditNow, loadAudit, setAuditSink } from './audit';
import { Enforcer } from './policy';
import { pairByApproval, pairWithCode, unpair as clearPairing, discover, DEFAULT_PORT } from './pairing';
import { Router } from './router';
import { getConfig } from './storage';
import { TabManager } from './tabs';
import { DEFAULT_POLICY } from '../shared/urlpolicy';

const enforcer = new Enforcer();
const tabs = new TabManager(enforcer);
let lastError = '';
let approvalAbort: AbortController | null = null;

const browserName = (): string => (/Edg\//.test(navigator.userAgent) ? 'Edge' : /OPR\//.test(navigator.userAgent) ? 'Opera' : 'Chrome');

const bridge: BridgeClient = new BridgeClient({
  url: async () => { const c = await getConfig(); return c ? `ws://127.0.0.1:${c.port}/api/browser/ws` : null; },
  key: async () => { const c = await getConfig(); return c ? { key: c.key, server_id: c.server_id } : null; },
  hello: () => ({
    ext: { name: 'ABP Bridge', version: chrome.runtime.getManifest().version, browser: browserName() },
    capabilities: { debugger: false, offscreen: false, sidepanel: true, native: false, webgpu: 'gpu' in navigator },
  }),
  handler: (method, params, ctx, signal) => router.handle(method, params, ctx, signal),
  onState: (state, detail) => { if (detail) lastError = detail; void render(state); void chrome.runtime.sendMessage({ ui: 'state' }).catch(() => undefined); },
  onHello: (result) => { enforcer.setPolicy((result.policy ?? DEFAULT_POLICY) as never); lastError = ''; },
});

const router = new Router(enforcer, tabs, () => ({
  version: chrome.runtime.getManifest().version, bridge: bridge.state, session: bridge.session, policy: enforcer.policy, tainted: enforcer.tainted,
  stopped: enforcer.stopped, paused: enforcer.paused, tabs: tabs.state,
}));

setAuditSink(async (entries) => { bridge.notify('audit.push', { entries: entries.map((e) => ({ action: e.action, detail: e.detail, at: e.at })) }); });
tabs.onEvent = (name, params) => bridge.notify(name, params);
tabs.onChange = () => { void chrome.runtime.sendMessage({ ui: 'state' }).catch(() => undefined); };

async function render(state: BridgeState = bridge.state): Promise<void> {
  const map: Record<BridgeState, [string, string]> = {
    connected: ['', '#2fa44f'], connecting: ['...', '#e6a23c'], disconnected: ['off', '#8a8f98'], unpaired: ['!', '#d1453b'],
    'server-mismatch': ['!', '#d1453b'], 'protocol-mismatch': ['!', '#d1453b'],
  };
  const [text, color] = map[state];
  await chrome.action.setBadgeText({ text: enforcer.stopped ? 'stop' : text });
  await chrome.action.setBadgeBackgroundColor({ color: enforcer.stopped ? '#d1453b' : color });
}

async function stopAll(reason: string): Promise<void> {
  enforcer.stopped = true;
  bridge.abortInflight();
  await router.overlayAll('off');
  audit('user.stop', reason);
  bridge.notify('event.user.stop', { reason });
  await render();
  void chrome.runtime.sendMessage({ ui: 'state' }).catch(() => undefined);
}

async function pauseForUser(tabId: number | undefined, why: string): Promise<void> {
  if (enforcer.stopped || enforcer.paused) return;
  if (tabId === undefined || tabs.kindOf(tabId) === null) return;
  if (!router.acting.has(tabId)) return;                          // only interrupt an agent that is actually mid-action
  enforcer.paused = true;
  await router.overlayAll('paused');
  audit('user.takeover', why);
  bridge.notify('event.user.takeover', { tab: tabId, why });
}

async function status(): Promise<Record<string, unknown>> {
  const cfg = await getConfig();
  const active = (await chrome.tabs.query({ active: true, currentWindow: true }))[0];
  return {
    state: bridge.state, error: lastError, paired: !!cfg, port: cfg?.port ?? null, version: chrome.runtime.getManifest().version, browser: browserName(),
    stopped: enforcer.stopped, paused: enforcer.paused, tainted: enforcer.tainted, session: bridge.session,
    tabs: await tabs.list(), active_tab: active ? { id: active.id, url: active.url ?? '', title: active.title ?? '', kind: active.id !== undefined ? tabs.kindOf(active.id) : null } : null,
    policy: { max_tabs: enforcer.policy.max_tabs, capabilities: enforcer.policy.capabilities, actions_per_minute: enforcer.policy.actions_per_minute },
  };
}

async function handleUi(m: { ui: string; [k: string]: unknown }): Promise<unknown> {
  switch (m.ui) {
    case 'status': return status();
    case 'connect': bridge.start(); return { ok: true };
    case 'discover': return (await discover((await getConfig())?.port ?? DEFAULT_PORT)) ?? null;
    case 'pair.code': { await pairWithCode(String(m.code ?? ''), Number(m.port) || undefined); bridge.stop(); bridge.start(); return { ok: true }; }
    case 'pair.request': {
      approvalAbort?.abort();
      approvalAbort = new AbortController();
      await pairByApproval(Number(m.port) || undefined, 5 * 60_000, approvalAbort.signal);
      bridge.stop(); bridge.start();
      return { ok: true };
    }
    case 'pair.cancel': approvalAbort?.abort(); return { ok: true };
    case 'unpair': bridge.stop(); await clearPairing(); await tabs.revokeAllGrants(); lastError = ''; await render('unpaired'); return { ok: true };
    case 'attach': { const id = Number(m.tab); await tabs.grant(id, !!m.follow); audit('grant', String(id)); return { ok: true }; }
    case 'detach': await router.overlay(Number(m.tab), 'off'); await tabs.revoke(Number(m.tab)); return { ok: true };
    case 'stop': await stopAll('stopped from the extension'); return { ok: true };
    case 'resume': enforcer.paused = false; enforcer.stopped = false; await router.overlayAll('active'); await render(); return { ok: true };
    case 'audit': return { entries: await loadAudit() };
    case 'sidepanel': { const w = await chrome.windows.getCurrent(); await chrome.sidePanel.open({ windowId: w.id! }); return { ok: true }; }
    default: return { error: 'unknown ui request' };
  }
}

chrome.runtime.onMessage.addListener((raw, sender, sendResponse) => {
  const m = raw as { t?: string; ui?: string; op?: string };
  if (m?.t === 'abp-user' && sender.tab?.id !== undefined && sender.id === chrome.runtime.id) {
    const tabId = sender.tab.id;
    if (m.op === 'stop') void stopAll('stopped from the page');
    else if (m.op === 'takeover' || m.op === 'input') void pauseForUser(tabId, m.op);
    else if (m.op === 'resume') { enforcer.paused = false; void router.overlayAll('active'); }
    return false;
  }
  if (m?.ui && sender.id === chrome.runtime.id) {
    handleUi(m as never).then(sendResponse, (e: unknown) => sendResponse({ error: e instanceof Error ? e.message : String(e) }));
    return true;
  }
  return false;
});

chrome.commands.onCommand.addListener((c) => { if (c === 'stop-all') void stopAll('keyboard shortcut'); });
chrome.tabs.onRemoved.addListener((id) => { void tabs.forget(id); });
chrome.tabs.onUpdated.addListener((id, info, tab) => {
  if (info.url) void tabs.onNavigated(id, info.url);
  if ((info.status === 'complete' || info.url) && tabs.kindOf(id) !== null) bridge.notify('event.tab.updated', { tab: id, url: tab.url ?? '', title: tab.title ?? '', status: tab.status });
});
chrome.alarms.onAlarm.addListener((a) => { if (a.name === 'abp.keepalive') { bridge.start(); void auditNow(); } });
chrome.runtime.onInstalled.addListener((d) => { if (d.reason === 'install') void chrome.runtime.openOptionsPage(); });
chrome.runtime.onStartup.addListener(() => bridge.start());

void (async () => {
  await tabs.load();
  await chrome.alarms.create('abp.keepalive', { periodInMinutes: 0.5 });
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: false }).catch(() => undefined);
  await render('disconnected');
  bridge.start();
})();
