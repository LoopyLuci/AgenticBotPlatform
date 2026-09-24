import { describe, expect, it } from 'vitest';
import vectors from '../fixtures/url_vectors.json';
import { DEFAULT_POLICY, classify, navigationVerdict } from '../../src/shared/urlpolicy';

describe('url classification (shared vectors with bot/browser_policy.py)', () => {
  for (const [url, allowed, sensitive] of vectors.cases as Array<[string, boolean, boolean]>) {
    it(`${url} -> allowed=${allowed} sensitive=${sensitive}`, () => {
      const v = classify(url);
      expect([v.allowed, v.sensitive]).toEqual([allowed, sensitive]);
    });
  }

  it('user lists extend it and trusted sites relax only the path rule, never a bank', () => {
    expect(classify('https://intranet.corp/', { ...DEFAULT_POLICY, extra_sensitive: ['intranet.corp'] }).sensitive).toBe(true);
    expect(classify('https://my.app/login', { ...DEFAULT_POLICY, trusted_sites: ['my.app'] }).sensitive).toBe(false);
    expect(classify('https://my.chase.com/x', { ...DEFAULT_POLICY, trusted_sites: ['chase.com'] }).sensitive).toBe(true);
  });

  it('navigation is refused on sensitive pages unless that exact site was granted', () => {
    expect(navigationVerdict('https://www.chase.com/', DEFAULT_POLICY).allowed).toBe(false);
    expect(navigationVerdict('https://www.chase.com/', { ...DEFAULT_POLICY, sensitive_grants: ['chase.com'] }).allowed).toBe(true);
    expect(navigationVerdict('chrome://settings', { ...DEFAULT_POLICY, sensitive_grants: ['settings'] }).allowed).toBe(false);   // never
  });
});
