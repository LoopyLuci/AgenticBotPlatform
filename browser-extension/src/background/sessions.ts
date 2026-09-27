// Browser-session agents (DESIGN.md section 7): a chat website the person is already logged into, driven like a person would,
// through one background tab per adapter kept in its own "ABP web-agents" tab group. Off by default, per-site opt-in — the
// enabled list lives only in this browser's local storage, next to the plain-language notice the options page shows for it,
// never pushed from ABP and never implied by anything else being enabled.
import { BridgeError } from '../shared/protocol';
import { ADAPTERS, adapterById } from '../adapters';
import type { AdapterDef } from '../adapters/types';
import { audit } from './audit';
import { ensureContent, send } from './content-rpc';
import { getLocal, getSession, setLocal, setSession } from './storage';

const ENABLED_KEY = 'abp.web.enabled';
const STATE_KEY = 'abp.web.tabs';
const GROUP_TITLE = 'ABP web-agents';

interface SessionState { tabs: Record<string, number>; groupId: number | null }

export async function enabledMap(): Promise<Record<string, boolean>> { return getLocal<Record<string, boolean>>(ENABLED_KEY, {}); }
export async function isEnabled(adapterId: string): Promise<boolean> { return !!(await enabledMap())[adapterId]; }
export async function setEnabled(adapterId: string, on: boolean): Promise<void> {
  const m = await enabledMap();
  if (on) m[adapterId] = true; else delete m[adapterId];
  await setLocal(ENABLED_KEY, m);
}

class WebSessions {
  private state: SessionState = { tabs: {}, groupId: null };
  private loaded = false;
  private queues = new Map<string, Promise<unknown>>();          // one in-flight prompt per adapter (tab)
  private lastAt = new Map<string, number>();

  private async load(): Promise<void> {
    if (this.loaded) return;
    this.loaded = true;
    this.state = await getSession<SessionState>(STATE_KEY, { tabs: {}, groupId: null });
    const live = new Set((await chrome.tabs.query({})).map((t) => t.id));
    for (const id of Object.keys(this.state.tabs)) if (!live.has(this.state.tabs[id])) delete this.state.tabs[id];
  }

  private async persist(): Promise<void> { await setSession(STATE_KEY, this.state); }

  async forgetTab(tabId: number): Promise<void> {
    await this.load();
    let changed = false;
    for (const [id, t] of Object.entries(this.state.tabs)) if (t === tabId) { delete this.state.tabs[id]; changed = true; }
    if (changed) await this.persist();
  }

  private async ensureGroup(tabId: number): Promise<void> {
    try {
      if (this.state.groupId !== null) { try { await chrome.tabs.group({ tabIds: [tabId], groupId: this.state.groupId }); return; } catch { this.state.groupId = null; } }
      const gid = await chrome.tabs.group({ tabIds: [tabId] });
      await chrome.tabGroups.update(gid, { title: GROUP_TITLE, color: 'cyan', collapsed: true });
      this.state.groupId = gid;
    } catch { /* grouping is a courtesy */ }
  }

  private async tabFor(a: AdapterDef): Promise<number> {
    await this.load();
    const existing = this.state.tabs[a.id];
    if (existing !== undefined) {
      try { await chrome.tabs.get(existing); return existing; } catch { delete this.state.tabs[a.id]; }
    }
    const granted = chrome.runtime.getManifest().host_permissions ?? [];
    const originPattern = `${a.dev ? 'http' : 'https'}://${a.hosts[0]!.split('/')[0]}/*`;
    const hasHost = granted.includes(originPattern) || (await chrome.permissions.contains({ origins: [originPattern] }).catch(() => false));
    if (!hasHost) {
      throw new BridgeError('E_NOT_ALLOWED', `ABP does not have permission to open ${a.name} yet`,
        { hint: `allow ${a.hosts[0]} from the extension options page` });
    }
    const tab = await chrome.tabs.create({ url: a.home, active: false });
    this.state.tabs[a.id] = tab.id!;
    await this.ensureGroup(tab.id!);
    await this.persist();
    await new Promise((r) => setTimeout(r, 400));
    return tab.id!;
  }

  async list(): Promise<Array<Record<string, unknown>>> {
    const enabled = await enabledMap();
    const out: Array<Record<string, unknown>> = [];
    for (const a of ADAPTERS) {
      if (a.dev) continue;
      let logged_in: boolean | null = null;
      let degraded = false;
      const tabId = (await this.load(), this.state.tabs[a.id]);
      if (enabled[a.id] && tabId !== undefined) {
        try {
          const p = await send<{ ready: boolean; logged_out: boolean }>(tabId, { op: 'web.probe', adapter: a.id });
          logged_in = !p.logged_out && p.ready ? true : p.logged_out ? false : null;
          degraded = !p.ready && !p.logged_out;
        } catch { degraded = true; }
      }
      out.push({ id: a.id, name: a.name, hosts: a.hosts, models: a.models ?? [], vision: !!a.vision, enabled: !!enabled[a.id],
                tos_note: a.tos_note, logged_in, degraded, connected_tab: tabId !== undefined });
    }
    return out;
  }

  async prompt(adapterId: string, model: string | undefined, text: string, opts: { req?: string; timeoutMs?: number; newChat?: boolean } = {}): Promise<{ text: string; url: string; title: string }> {
    const a = adapterById(adapterId);
    if (!a || a.dev) throw new BridgeError('E_MODEL_UNAVAILABLE', `there is no adapter called ${adapterId}`);
    if (!(await isEnabled(a.id))) {
      throw new BridgeError('E_NOT_ALLOWED', `${a.name} is not enabled for ABP`, { hint: `turn it on in the extension options (${a.tos_note})` });
    }
    const key = a.id;
    const prior = this.queues.get(key) ?? Promise.resolve();
    const run = prior.catch(() => undefined).then(() => this._promptOnce(a, model, text, opts));
    this.queues.set(key, run);
    try { return await run as { text: string; url: string; title: string }; }
    finally { if (this.queues.get(key) === run) this.queues.delete(key); }
  }

  private async _promptOnce(a: AdapterDef, model: string | undefined, text: string, opts: { req?: string; timeoutMs?: number; newChat?: boolean }): Promise<{ text: string; url: string; title: string }> {
    const wait = a.limits.min_interval_ms - (Date.now() - (this.lastAt.get(a.id) ?? 0));
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
    this.lastAt.set(a.id, Date.now());
    const tabId = await this.tabFor(a);
    await ensureContent(tabId);
    if (opts.newChat !== false && a.new_chat?.navigate) {
      await chrome.tabs.update(tabId, { url: model ? a.new_chat.navigate : a.new_chat.navigate });
      await new Promise((r) => setTimeout(r, 600));
      await ensureContent(tabId);
    } else if (opts.newChat !== false && a.new_chat?.click) {
      try { await send(tabId, { op: 'act', action: 'click', ref: undefined }); } catch { /* best-effort new-chat click, selector-based clicks go through web.probe instead */ }
    }
    audit('web.prompt', a.id);
    const result = await send<{ text: string; url: string; title: string }>(tabId, { op: 'web.prompt', adapter: a.id, text, req: opts.req, timeout_ms: opts.timeoutMs });
    return result;
  }

  async selftest(adapterId: string): Promise<{ ok: boolean; got: string; adapter: string }> {
    const a = adapterById(adapterId);
    if (!a) throw new BridgeError('E_PARAMS', `unknown adapter ${adapterId}`);
    const r = await this.prompt(adapterId, undefined, a.selftest.prompt, { timeoutMs: 60_000 });
    const ok = r.text.trim().includes(a.selftest.expect);
    audit('web.selftest', `${a.id} ${ok ? 'ok' : 'FAILED'}`);
    return { ok, got: r.text.slice(0, 300), adapter: a.id };
  }
}

export const sessions = new WebSessions();
export const onDelta: { fn: (req: string, text: string) => void } = { fn: () => undefined };

chrome.runtime.onMessage.addListener((raw, sender) => {
  const m = raw as { t?: string; req?: string; text?: string };
  if (m?.t === 'abp-web-delta' && sender.id === chrome.runtime.id && m.req) onDelta.fn(m.req, String(m.text ?? ''));
  return false;
});
