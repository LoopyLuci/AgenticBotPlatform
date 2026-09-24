// Which tabs ABP may touch. Two kinds and nothing else:
//   agent   - tabs ABP opened itself, kept in a coloured tab group "ABP agent" so the person can see them at a glance
//   granted - one tab the person explicitly handed over ("Let ABP use this tab"); the grant ends if the tab leaves that origin
// Every other tab is invisible and untouchable to the agent.
import { BridgeError } from '../shared/protocol';
import type { Enforcer } from './policy';
import { getSession, setSession } from './storage';

export interface TabState { groupId: number | null; agent: number[]; granted: Record<string, { origin: string; follow: boolean }> }
export interface TabInfo { id: number; url: string; title: string; kind: 'agent' | 'granted'; active: boolean; status: string }

const KEY = 'abp.tabs';

const originOf = (url: string): string => { try { return new URL(url).origin; } catch { return ''; } };

export class TabManager {
  state: TabState = { groupId: null, agent: [], granted: {} };
  onChange: () => void = () => undefined;
  onEvent: (name: string, params: unknown) => void = () => undefined;

  constructor(private enforcer: Enforcer) {}

  async load(): Promise<void> {
    this.state = await getSession<TabState>(KEY, { groupId: null, agent: [], granted: {} });
    // drop anything that no longer exists (a restart can outlive tabs)
    const live = new Set((await chrome.tabs.query({})).map((t) => t.id));
    this.state.agent = this.state.agent.filter((id) => live.has(id));
    for (const id of Object.keys(this.state.granted)) if (!live.has(Number(id))) delete this.state.granted[id];
  }

  private async persist(): Promise<void> { await setSession(KEY, this.state); this.onChange(); }

  kindOf(tabId: number): 'agent' | 'granted' | null {
    if (this.state.agent.includes(tabId)) return 'agent';
    if (this.state.granted[String(tabId)]) return 'granted';
    return null;
  }

  assertAccessible(tabId: number): void {
    if (this.kindOf(tabId) === null) {
      throw new BridgeError('E_NO_TAB', `tab ${tabId} is not one ABP may use`, { hint: 'open a tab with tabs.open, or ask the person to press "Let ABP use this tab"' });
    }
  }

  /** Resolve the tab a call refers to; if none is named and exactly one is available, use it. */
  resolve(tab: unknown): number {
    if (typeof tab === 'number') { this.assertAccessible(tab); return tab; }
    const all = [...this.state.agent, ...Object.keys(this.state.granted).map(Number)];
    if (all.length === 1) return all[0]!;
    throw new BridgeError('E_PARAMS', all.length ? 'name the tab (there is more than one)' : 'no tab is open for ABP: use tabs.open first', { data: { tabs: all } });
  }

  async list(): Promise<TabInfo[]> {
    const out: TabInfo[] = [];
    for (const id of [...this.state.agent, ...Object.keys(this.state.granted).map(Number)]) {
      try {
        const t = await chrome.tabs.get(id);
        out.push({ id, url: t.url ?? t.pendingUrl ?? '', title: t.title ?? '', kind: this.kindOf(id)!, active: !!t.active, status: t.status ?? '' });
      } catch { /* closed meanwhile */ }
    }
    return out;
  }

  async open(url: string, active = false): Promise<TabInfo> {
    this.enforcer.assertNavigable(url);
    if (this.state.agent.length >= this.enforcer.policy.max_tabs) {
      throw new BridgeError('E_BUSY', `ABP already has ${this.state.agent.length} tabs open (the limit)`, { hint: 'close one with tabs.close' });
    }
    const tab = await chrome.tabs.create({ url, active });
    const id = tab.id!;
    await this.ensureGroup(id);
    this.state.agent.push(id);
    await this.persist();
    await this.waitLoad(id, 20_000).catch(() => undefined);
    const t = await chrome.tabs.get(id);
    return { id, url: t.url ?? url, title: t.title ?? '', kind: 'agent', active: !!t.active, status: t.status ?? '' };
  }

  private async ensureGroup(tabId: number): Promise<void> {
    try {
      if (this.state.groupId !== null) {
        try { await chrome.tabs.group({ tabIds: [tabId], groupId: this.state.groupId }); return; } catch { this.state.groupId = null; }
      }
      const gid = await chrome.tabs.group({ tabIds: [tabId] });
      await chrome.tabGroups.update(gid, { title: 'ABP agent', color: 'purple', collapsed: false });
      this.state.groupId = gid;
    } catch { /* grouping is a courtesy; the tab still works without it */ }
  }

  async close(tabId: number): Promise<void> {
    if (this.kindOf(tabId) !== 'agent') throw new BridgeError('E_NOT_ALLOWED', 'ABP only closes tabs it opened itself');
    await chrome.tabs.remove(tabId);
    await this.forget(tabId);
  }

  async focus(tabId: number): Promise<void> {
    this.assertAccessible(tabId);
    const t = await chrome.tabs.update(tabId, { active: true });
    if (t?.windowId !== undefined) await chrome.windows.update(t.windowId, { focused: true });
  }

  async grant(tabId: number, follow = false): Promise<void> {
    const t = await chrome.tabs.get(tabId);
    const origin = originOf(t.url ?? '');
    if (!origin) throw new BridgeError('E_NOT_ALLOWED', 'that page cannot be used by ABP');
    this.enforcer.assertReadable(t.url ?? '');
    this.state.granted[String(tabId)] = { origin, follow };
    await this.persist();
  }

  async revoke(tabId: number): Promise<void> {
    delete this.state.granted[String(tabId)];
    this.state.agent = this.state.agent.filter((i) => i !== tabId);
    await this.persist();
  }

  async revokeAllGrants(): Promise<void> { this.state.granted = {}; await this.persist(); }

  async forget(tabId: number): Promise<void> {
    if (this.kindOf(tabId) === null) return;
    this.state.agent = this.state.agent.filter((i) => i !== tabId);
    delete this.state.granted[String(tabId)];
    await this.persist();
    this.onEvent('event.tab.removed', { tab: tabId });
  }

  /** A granted tab that navigates to another origin loses its grant (unless the person chose "follow"). */
  async onNavigated(tabId: number, url: string): Promise<void> {
    const g = this.state.granted[String(tabId)];
    if (g && !g.follow && originOf(url) !== g.origin) { await this.revoke(tabId); this.onEvent('event.tab.grant_ended', { tab: tabId, reason: 'navigated to another site' }); }
  }

  waitLoad(tabId: number, timeoutMs: number): Promise<void> {
    return new Promise((resolve, reject) => {
      let done = false;
      const finish = (err?: Error): void => { if (done) return; done = true; clearTimeout(timer); chrome.tabs.onUpdated.removeListener(listener); err ? reject(err) : resolve(); };
      const listener = (id: number, info: { status?: string }): void => { if (id === tabId && info.status === 'complete') finish(); };
      const timer = setTimeout(() => finish(new BridgeError('E_TIMEOUT', 'the page did not finish loading', { retryable: true })), timeoutMs);
      chrome.tabs.onUpdated.addListener(listener);
      chrome.tabs.get(tabId).then((t) => { if (t.status === 'complete') finish(); }, () => finish(new BridgeError('E_NO_TAB', 'the tab closed')));
    });
  }
}
