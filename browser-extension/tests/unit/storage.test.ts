// Upgrade/downgrade compatibility (DESIGN.md 17, P5): there is no version-tag migration system, because every stored
// shape so far has only ever grown new, separately-namespaced keys (abp.config, abp.tabs, abp.web.enabled,
// abp.web.tabs, abp.models.installed, ...) rather than changing an existing key's shape in place. What actually needs
// to hold, and what this file checks, is that every reader is defensive: a missing key, an empty object, or a stale/
// partial value from an older version must fall back cleanly, never throw.
import { beforeEach, describe, expect, it } from 'vitest';
import { getConfig, getLocal, getSession, setConfig, setLocal, setSession } from '../../src/background/storage';

const memory = new Map<string, unknown>();
function fakeArea() {
  return {
    get: async (key: string) => (memory.has(key) ? { [key]: memory.get(key) } : {}),
    set: async (items: Record<string, unknown>) => { for (const [k, v] of Object.entries(items)) memory.set(k, v); },
    remove: async (key: string) => { memory.delete(key); },
  };
}

beforeEach(() => {
  memory.clear();
  (globalThis as unknown as { chrome: unknown }).chrome = { storage: { local: fakeArea(), session: fakeArea() } };
});

describe('getConfig', () => {
  it('is null when nothing has been stored yet', async () => {
    expect(await getConfig()).toBeNull();
  });

  it('is null for a partial/stale object missing a required field (a future version removed or renamed one)', async () => {
    memory.set('abp.config', { port: 8787 });                    // no key/server_id - as an old or corrupted record might be
    expect(await getConfig()).toBeNull();
  });

  it('round-trips a complete config', async () => {
    const c = { port: 8787, key: 'k', server_id: 'srv' };
    await setConfig(c);
    expect(await getConfig()).toEqual(c);
  });

  it('tolerates unknown extra fields from a newer version without losing the known ones', async () => {
    memory.set('abp.config', { port: 8787, key: 'k', server_id: 'srv', future_field: { anything: true } });
    expect(await getConfig()).toMatchObject({ port: 8787, key: 'k', server_id: 'srv' });
  });
});

describe('getLocal / getSession fallbacks', () => {
  it('return the given fallback for a key that has never been set', async () => {
    expect(await getLocal('abp.models.installed', [])).toEqual([]);
    expect(await getSession('abp.tabs', { agent: [] })).toEqual({ agent: [] });
  });

  it('round-trip whatever shape is written, unchanged', async () => {
    await setLocal('abp.web.enabled', { grok: true });
    expect(await getLocal('abp.web.enabled', {})).toEqual({ grok: true });
    await setSession('abp.tabs', { agent: [1, 2] });
    expect(await getSession('abp.tabs', { agent: [] })).toEqual({ agent: [1, 2] });
  });
});
