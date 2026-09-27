import { describe, expect, it } from 'vitest';
import { CATALOG, type Capabilities, fitFor, rankedCatalog } from '../../src/models/catalog';

const caps = (overrides: Partial<Capabilities> = {}): Capabilities => ({
  webgpu: true, device_memory_gb: 8, hardware_concurrency: 8, cross_origin_isolated: true, max_storage_buffer_binding_mb: 2048, ...overrides,
});

describe('fitFor', () => {
  it('is unsupported when a WebGPU model has no WebGPU', () => {
    const entry = CATALOG.find((c) => c.requires_webgpu)!;
    const r = fitFor(entry, caps({ webgpu: false, webgpu_reason: 'no adapter' }));
    expect(r.fit).toBe('unsupported');
    expect(r.reason).toMatch(/WebGPU/);
  });

  it('is good for a small CPU-only model regardless of GPU', () => {
    const entry = CATALOG.find((c) => !c.requires_webgpu)!;
    expect(fitFor(entry, caps({ webgpu: false })).fit).toBe('good');
  });

  it('is good for a WebGPU model that comfortably fits reported memory', () => {
    const entry = CATALOG.find((c) => c.requires_webgpu)!;
    expect(fitFor(entry, caps({ device_memory_gb: 32 })).fit).toBe('good');
  });

  it('is tight when the model is a large fraction of a small device\'s memory', () => {
    const entry = CATALOG.find((c) => c.id === 'phi-3.5-mini')!;
    expect(fitFor(entry, caps({ device_memory_gb: 6, max_storage_buffer_binding_mb: 4096 })).fit).toBe('tight');
  });

  it('is unsupported when the model would not plausibly fit at all', () => {
    const entry = CATALOG.find((c) => c.id === 'phi-3.5-mini')!;
    expect(fitFor(entry, caps({ device_memory_gb: 1 })).fit).toBe('unsupported');
  });

  it('is unsupported when the GPU buffer-binding limit is smaller than the weights', () => {
    const entry = CATALOG.find((c) => c.requires_webgpu)!;
    const r = fitFor(entry, caps({ max_storage_buffer_binding_mb: 64 }));
    expect(r.fit).toBe('unsupported');
    expect(r.reason).toMatch(/buffer-binding/);
  });

  it('does not require memory info to say a model fits (unknown device memory is not treated as "too little")', () => {
    const entry = CATALOG.find((c) => !c.requires_webgpu)!;
    expect(fitFor(entry, caps({ device_memory_gb: null })).fit).toBe('good');
  });
});

describe('rankedCatalog', () => {
  it('sorts good fits before tight before unsupported, smallest first within a tier', () => {
    const ranked = rankedCatalog(caps({ device_memory_gb: 2 }), 'chat');
    const fits = ranked.map((r) => r.fit);
    const firstUnsupported = fits.indexOf('unsupported');
    if (firstUnsupported >= 0) expect(fits.slice(firstUnsupported).every((f) => f === 'unsupported')).toBe(true);
    const sizes = ranked.filter((r) => r.fit === ranked[0]!.fit).map((r) => r.approx_size_mb);
    expect(sizes).toEqual([...sizes].sort((a, b) => a - b));
  });

  it('filters by task', () => {
    const ranked = rankedCatalog(caps(), 'embed');
    expect(ranked.every((r) => r.task === 'embed')).toBe(true);
    expect(ranked.length).toBeGreaterThan(0);
  });
});
