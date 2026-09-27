// @vitest-environment jsdom
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { compose, probe, read } from '../../src/content/webengine';
import type { AdapterDef } from '../../src/adapters/types';

// jsdom never lays anything out, so getBoundingClientRect() is always 0x0 - stub it to a plausible visible rect so
// isVisible() (used by webengine's element resolution) behaves the way it does against a real, rendered page.
beforeAll(() => {
  Element.prototype.getBoundingClientRect = () => ({ width: 100, height: 20, top: 0, left: 0, bottom: 20, right: 100, x: 0, y: 0, toJSON() { return {}; } });
});

const html = (s: string): void => { document.body.innerHTML = s; };

const adapter: AdapterDef = {
  id: 'mock', name: 'Mock', version: 1, hosts: ['127.0.0.1'], home: 'http://127.0.0.1/chat.html',
  tos_note: 'test',
  login: { logged_out: ['#login-wall'], ready: ['#composer'] },
  compose: { input: ['#composer'], submit: ['#send'], key: 'Enter', mode: 'paste' },
  stream: { assistant: ['div.msg.assistant'], generating: ['#stop'], quiet_ms: 250, strip: ['button', '.hidden-meta'] },
  errors: [{ within: ['#banner'], match: '(rate limit|too many)', code: 'E_RATE_LIMITED', message: 'rate limited' }],
  limits: { min_interval_ms: 300, max_prompt_chars: 6000 },
  selftest: { prompt: 'Reply with exactly: ABP-OK', expect: 'ABP-OK' },
};

describe('probe', () => {
  it('reports ready when the compose box is visible and no logged-out marker is present', () => {
    html('<textarea id="composer"></textarea><button id="send">Send</button>');
    const p = probe(adapter);
    expect(p.ready).toBe(true);
    expect(p.logged_out).toBe(false);
    expect(p.matched.input).toBe(0);
  });

  it('reports logged_out and never ready when the login wall is present', () => {
    html('<div id="login-wall">Please log in</div><textarea id="composer"></textarea>');
    const p = probe(adapter);
    expect(p.logged_out).toBe(true);
    expect(p.ready).toBe(false);
  });

  it('is not ready (but not logged out) when the site has changed and nothing matches', () => {
    html('<p>a redesigned page</p>');
    const p = probe(adapter);
    expect(p.ready).toBe(false);
    expect(p.logged_out).toBe(false);
    expect(p.matched.input).toBe(-1);
  });

  it('surfaces a rate-limit banner as a structured error', () => {
    html('<textarea id="composer"></textarea><div id="banner">You have hit the rate limit, try again later.</div>');
    expect(probe(adapter).error).toEqual({ code: 'E_RATE_LIMITED', message: 'rate limited' });
  });
});

describe('compose', () => {
  it('sets a textarea value and clicks the submit button', async () => {
    html('<textarea id="composer"></textarea><button id="send">Send</button>');
    const btn = document.getElementById('send') as HTMLButtonElement;
    const clicked = vi.fn();
    btn.addEventListener('click', clicked);
    const r = await compose(adapter, 'hello there');
    expect((document.getElementById('composer') as HTMLTextAreaElement).value).toBe('hello there');
    expect(clicked).toHaveBeenCalledOnce();
    expect(r.submitted).toBe(true);
  });

  it('falls back to Enter when there is no submit button', async () => {
    const noSubmit: AdapterDef = { ...adapter, compose: { input: ['#composer'], key: 'Enter' } };
    html('<textarea id="composer"></textarea>');
    const input = document.getElementById('composer')!;
    const keys: string[] = [];
    input.addEventListener('keydown', (e) => keys.push((e as KeyboardEvent).key));
    const r = await compose(noSubmit, 'x');
    expect(r.submitted).toBe(true);
    expect(keys).toContain('Enter');
  });

  it('throws when the compose box cannot be found (the site changed)', async () => {
    html('<p>nothing here</p>');
    await expect(compose(adapter, 'x')).rejects.toThrow(/E_ADAPTER_BROKEN/);
  });
});

describe('read', () => {
  it('extracts the last assistant message, stripped of button chrome', () => {
    html(`
      <div class="msg assistant">first reply<button>copy</button></div>
      <div class="msg assistant">second reply<span class="hidden-meta">meta</span><button>copy</button></div>
    `);
    const r = read(adapter);
    expect(r.count).toBe(2);
    expect(r.text).toBe('second reply');
    expect(r.generating).toBe(false);
  });

  it('reports generating while the stop control is present', () => {
    html('<div class="msg assistant">partial</div><button id="stop">Stop</button>');
    expect(read(adapter).generating).toBe(true);
  });
});
