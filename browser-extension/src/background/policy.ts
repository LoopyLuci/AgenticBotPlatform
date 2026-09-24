// Extension-side enforcement of the safety model (DESIGN.md section 6). The server enforces the same rules; this copy exists
// so a buggy or compromised server still cannot exceed what the person granted.
import { BridgeError, type Ctx } from '../shared/protocol';
import { DEFAULT_POLICY, classify, navigationVerdict, type Policy } from '../shared/urlpolicy';

export type Capability = keyof Policy['capabilities'];

export class Enforcer {
  policy: Policy = DEFAULT_POLICY;
  /** Set by ABP after the session read untrusted page content: state-changing calls then need an approval id. */
  tainted = false;
  /** The user pressed Stop (or Take over): every call fails until ABP starts a new session. */
  stopped = false;
  paused = false;
  private actions: number[] = [];
  private navs: number[] = [];
  private sessionStart = Date.now();

  constructor(private now: () => number = Date.now) { this.sessionStart = now(); }

  setPolicy(p: Partial<Policy>): void {
    this.policy = { ...DEFAULT_POLICY, ...p, capabilities: { ...DEFAULT_POLICY.capabilities, ...(p.capabilities ?? {}) } };
  }

  newSession(): void {
    this.stopped = false;
    this.paused = false;
    this.tainted = false;
    this.actions = [];
    this.navs = [];
    this.sessionStart = this.now();
  }

  private prune(list: number[]): void {
    const cut = this.now() - 60_000;
    while (list.length && list[0]! < cut) list.shift();
  }

  assertRunning(): void {
    if (this.stopped) throw new BridgeError('E_CANCELLED', 'the person stopped the agent', { hint: 'ABP must start a new session' });
    if (this.paused) throw new BridgeError('E_BUSY', 'the person took over the browser', { retryable: true, hint: 'wait for them to resume' });
    if (this.now() - this.sessionStart > this.policy.max_session_minutes * 60_000) {
      throw new BridgeError('E_RATE_LIMITED', `the session ran longer than ${this.policy.max_session_minutes} minutes`, { hint: 'start a new session' });
    }
  }

  assertCapability(cap: Capability): void {
    if (!this.policy.capabilities[cap]) throw new BridgeError('E_NOT_ALLOWED', `the "${cap}" capability is switched off`, { hint: 'the person can enable it in the extension options' });
  }

  /** Capabilities that change something need an approval id once the session is tainted. */
  assertApproved(cap: Capability, ctx: Ctx | undefined): void {
    if (!this.tainted) return;
    if (cap === 'read' || cap === 'history') return;
    if (!ctx?.approval?.id) throw new BridgeError('E_NEEDS_APPROVAL', 'this session read untrusted page content, so a person must approve changes', { retryable: true });
  }

  rate(kind: 'action' | 'nav'): void {
    const list = kind === 'action' ? this.actions : this.navs;
    const limit = kind === 'action' ? this.policy.actions_per_minute : this.policy.navigations_per_minute;
    this.prune(list);
    if (list.length >= limit) throw new BridgeError('E_RATE_LIMITED', `more than ${limit} ${kind === 'action' ? 'actions' : 'navigations'} in a minute`, { retryable: true, hint: 'slow down' });
    list.push(this.now());
  }

  /** Reading a sensitive page needs a per-site grant; acting on it never. */
  assertReadable(url: string): void {
    const v = classify(url, this.policy);
    if (!v.allowed) throw new BridgeError('E_SENSITIVE_SITE', v.reason);
    if (v.sensitive && !this.granted(url)) throw new BridgeError('E_SENSITIVE_SITE', `${v.reason}. Reading it needs your per-site grant.`, { data: { category: v.category } });
  }

  assertActionable(url: string): void {
    const v = classify(url, this.policy);
    if (!v.allowed) throw new BridgeError('E_SENSITIVE_SITE', v.reason);
    if (v.sensitive) throw new BridgeError('E_SENSITIVE_SITE', `${v.reason}. The agent never acts on these pages.`, { data: { category: v.category } });
  }

  assertNavigable(url: string): void {
    const v = navigationVerdict(url, this.policy);
    if (!v.allowed) throw new BridgeError('E_SENSITIVE_SITE', v.reason, { data: { category: v.category } });
  }

  private granted(url: string): boolean {
    let host = '';
    try { host = new URL(url).hostname.toLowerCase(); } catch { return false; }
    return this.policy.sensitive_grants.some((g) => host === g.toLowerCase() || host.endsWith('.' + g.toLowerCase()));
  }
}
