// @vitest-environment jsdom
import { beforeAll, describe, expect, it } from 'vitest';
import { upload } from '../../src/content/actions';
import { BridgeError } from '../../src/shared/protocol';

// jsdom has no DataTransfer implementation at all; a minimal stand-in is enough to exercise the real upload() code path
// (a real browser's DataTransfer is what actions.ts actually uses - this only fills the gap jsdom leaves).
class FakeDataTransfer {
  private list: File[] = [];
  items = { add: (f: File): void => { this.list.push(f); } };
  get files(): FileList {
    const arr = this.list.slice();
    return Object.assign(arr, { item: (i: number) => arr[i] ?? null }) as unknown as FileList;
  }
}

beforeAll(() => {
  Element.prototype.getBoundingClientRect = () => ({ width: 100, height: 20, top: 0, left: 0, bottom: 20, right: 100, x: 0, y: 0, toJSON() { return {}; } });
  Element.prototype.scrollIntoView = () => undefined;                    // jsdom does not implement layout/scrolling at all
  (globalThis as unknown as { DataTransfer: unknown }).DataTransfer = FakeDataTransfer;
});

const b64 = (s: string): string => Buffer.from(s, 'utf-8').toString('base64');

describe('upload', () => {
  it('attaches a file to a real file input via DataTransfer and fires input/change', async () => {
    document.body.innerHTML = '<input type="file" id="f">';
    const input = document.getElementById('f') as HTMLInputElement;
    document.elementFromPoint = () => input;                             // jsdom's own stub throws "not implemented"; nothing covers the input here
    // jsdom's native `files` setter brand-checks for a real FileList, which nothing in jsdom can construct (it has no
    // DataTransfer either) - override it on this element only, the same way a browser's own setter behaves otherwise.
    let stored: FileList | undefined;
    Object.defineProperty(input, 'files', { configurable: true, get: () => stored, set: (v: FileList) => { stored = v; } });
    const seen: string[] = [];
    input.addEventListener('input', () => seen.push('input'));
    input.addEventListener('change', () => seen.push('change'));
    const info = await upload(input, 'hello.txt', 'text/plain', b64('hello world'));
    expect(info).toEqual({ name: 'hello.txt', size: 11 });
    expect(input.files?.length).toBe(1);
    expect(input.files?.[0]?.name).toBe('hello.txt');
    expect(input.files?.[0]?.type).toBe('text/plain');
    expect(seen).toEqual(['input', 'change']);
  });

  it('refuses an element that is not a file input', async () => {
    document.body.innerHTML = '<input type="text" id="t">';
    const input = document.getElementById('t') as HTMLInputElement;
    await expect(upload(input, 'x.txt', 'text/plain', b64('x'))).rejects.toThrow(BridgeError);
  });
});
