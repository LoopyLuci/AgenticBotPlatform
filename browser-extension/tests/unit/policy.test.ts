import { describe, expect, it } from 'vitest';
import { BridgeError } from '../../src/shared/protocol';
import { Enforcer } from '../../src/background/policy';

const code = (fn: () => void): string | null => { try { fn(); return null; } catch (e) { return e instanceof BridgeError ? e.code : 'other'; } };

describe('Enforcer', () => {
  it('stops everything once the person pressed Stop, until a new session starts', () => {
    const e = new Enforcer();
    e.stopped = true;
    expect(code(() => e.assertRunning())).toBe('E_CANCELLED');
    e.newSession();
    expect(code(() => e.assertRunning())).toBeNull();
  });

  it('pauses while the person has control', () => {
    const e = new Enforcer();
    e.paused = true;
    expect(code(() => e.assertRunning())).toBe('E_BUSY');
  });

  it('refuses capabilities that are off and needs an approval id for changes once tainted', () => {
    const e = new Enforcer();
    expect(code(() => e.assertCapability('downloads'))).toBe('E_NOT_ALLOWED');
    expect(code(() => e.assertCapability('read'))).toBeNull();
    e.tainted = true;
    expect(code(() => e.assertApproved('read', {}))).toBeNull();
    expect(code(() => e.assertApproved('interact', {}))).toBe('E_NEEDS_APPROVAL');
    expect(code(() => e.assertApproved('interact', { approval: { id: 'a1', by: 'user' } }))).toBeNull();
  });

  it('rate-limits actions in a sliding minute window', () => {
    let t = 1_000_000;
    const e = new Enforcer(() => t);
    e.setPolicy({ actions_per_minute: 3 });
    for (let i = 0; i < 3; i++) e.rate('action');
    expect(code(() => e.rate('action'))).toBe('E_RATE_LIMITED');
    t += 61_000;
    expect(code(() => e.rate('action'))).toBeNull();
  });

  it('ends a session that runs too long', () => {
    let t = 0;
    const e = new Enforcer(() => t);
    e.setPolicy({ max_session_minutes: 1 });
    t += 61_000;
    expect(code(() => e.assertRunning())).toBe('E_RATE_LIMITED');
  });

  it('sensitive pages: reading needs a grant, acting never, browser pages never', () => {
    const e = new Enforcer();
    expect(code(() => e.assertReadable('https://www.chase.com/'))).toBe('E_SENSITIVE_SITE');
    e.setPolicy({ sensitive_grants: ['chase.com'] });
    expect(code(() => e.assertReadable('https://www.chase.com/'))).toBeNull();
    expect(code(() => e.assertActionable('https://www.chase.com/'))).toBe('E_SENSITIVE_SITE');
    expect(code(() => e.assertReadable('chrome://settings'))).toBe('E_SENSITIVE_SITE');
    expect(code(() => e.assertActionable('https://example.com/'))).toBeNull();
  });
});
