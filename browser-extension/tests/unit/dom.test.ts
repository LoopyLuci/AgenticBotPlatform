// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { accessibleName, isSecretField, pathHint, roleOf, walk } from '../../src/content/dom';
import { RefTable, similarity, type Locator } from '../../src/content/refs';

const html = (s: string): HTMLElement => { document.body.innerHTML = s; return document.body; };

describe('roles and names', () => {
  it('derives roles from tags and input types', () => {
    html('<a href="/x">l</a><a>no href</a><button>b</button><input type="checkbox"><input type="search"><select></select><textarea></textarea><h2>t</h2>');
    const r = [...document.body.children].map(roleOf);
    expect(r).toEqual(['link', 'generic', 'button', 'checkbox', 'searchbox', 'combobox', 'textbox', 'heading']);
  });

  it('computes accessible names from aria-label, labels, placeholder and text', () => {
    html('<label for="a">Email address</label><input id="a"><input id="b" placeholder="Search here"><button aria-label="Close dialog">x</button><a href="/">  Pricing   plans </a>');
    expect(accessibleName(document.getElementById('a')!)).toBe('Email address');
    expect(accessibleName(document.getElementById('b')!)).toBe('Search here');
    expect(accessibleName(document.querySelector('button')!)).toBe('Close dialog');
    expect(accessibleName(document.querySelector('a')!)).toBe('Pricing plans');
  });
});

describe('secret fields (the agent never types into these)', () => {
  it('flags password, card, one-time-code and SSN fields however they are labelled', () => {
    html('<input type="password"><input name="cardnumber"><input autocomplete="one-time-code"><input id="ssn"><input aria-label="Verification code"><input name="username"><input name="q">');
    const flags = [...document.querySelectorAll('input')].map(isSecretField);
    expect(flags).toEqual([true, true, true, true, true, false, false]);
  });
});

describe('walk', () => {
  it('pierces open shadow roots and skips scripts and styles', () => {
    html('<div id="host"></div><script>1</script><style>a{}</style>');
    const host = document.getElementById('host')!;
    host.attachShadow({ mode: 'open' }).innerHTML = '<button id="inside">Hi</button>';
    const ids = [...walk(document)].map((e) => e.id || e.tagName.toLowerCase());
    expect(ids).toContain('inside');
    expect(ids).not.toContain('script');
    expect(ids).not.toContain('style');
  });

  it('gives a short path hint', () => {
    html('<main><form id="f"><button id="b">x</button></form></main>');
    expect(pathHint(document.getElementById('b')!)).toContain('form#f>button#b');
  });
});

describe('refs re-location', () => {
  const loc = (over: Partial<Locator> = {}): Locator => ({ role: 'button', name: 'Buy now', tag: 'button', type: '', path: 'body>div>button', x: 100, y: 200, ...over });

  it('matches a re-created element with the same role and name', () => {
    expect(similarity(loc(), loc({ x: 105 }))).toBeGreaterThanOrEqual(0.75);
  });
  it('never matches a differently named element or a different role', () => {
    expect(similarity(loc(), loc({ name: 'Delete account' }))).toBe(0);
    expect(similarity(loc(), loc({ role: 'link', tag: 'a' }))).toBe(0);
  });

  it('resolves the live element, re-locates a replaced one, and gives up on a changed one', () => {
    html('<div id="app"><button>Buy now</button></div>');
    const t = new RefTable();
    const first = document.querySelector('button')!;
    const ref = t.register(first, loc());
    expect(t.resolve(ref, () => [])).toBe(first);
    document.getElementById('app')!.innerHTML = '<button>Buy now</button>';         // replaced
    const second = document.querySelector('button')!;
    expect(t.resolve(ref, () => [{ el: second, loc: loc({ x: 101 }) }])).toBe(second);
    const ref2 = t.register(second, loc());
    document.getElementById('app')!.innerHTML = '<button>Delete account</button>';  // a different button now
    const third = document.querySelector('button')!;
    expect(t.resolve(ref2, () => [{ el: third, loc: loc({ name: 'Delete account' }) }])).toBeNull();
  });
});
