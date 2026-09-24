// Element references. A ref ("e12") names an element in the latest snapshot. It is held weakly, and if the element was
// replaced (frameworks re-render constantly) it is re-located from a locator bundle - role + accessible name + a path
// hint + position - rather than by index. If nothing matches well enough the ref is STALE and the caller gets an error:
// clicking the wrong element is worse than failing.

export interface Locator {
  role: string;
  name: string;
  tag: string;
  type: string;
  path: string;          // short CSS-ish breadcrumb, a hint only
  x: number;             // centre of the bounding box in the document (not viewport) - a tie-breaker
  y: number;
}

interface Entry { el: WeakRef<Element>; loc: Locator }

export class RefTable {
  generation = 0;
  private map = new Map<string, Entry>();
  private counter = 0;

  reset(): void {
    this.generation += 1;
    this.map.clear();
    this.counter = 0;
  }

  register(el: Element, loc: Locator): string {
    const ref = `e${++this.counter}`;
    this.map.set(ref, { el: new WeakRef(el), loc });
    return ref;
  }

  /** The live element for a ref, re-locating it if it was replaced; null when it cannot be found confidently. */
  resolve(ref: string, candidates: () => Iterable<{ el: Element; loc: Locator }>): Element | null {
    const entry = this.map.get(ref);
    if (!entry) return null;
    const el = entry.el.deref();
    if (el && el.isConnected) return el;
    let best: { el: Element; score: number } | null = null;
    for (const c of candidates()) {
      const s = similarity(entry.loc, c.loc);
      if (s > (best?.score ?? 0)) best = { el: c.el, score: s };
    }
    if (best && best.score >= 0.75) {
      this.map.set(ref, { el: new WeakRef(best.el), loc: entry.loc });
      return best.el;
    }
    return null;
  }

  has(ref: string): boolean { return this.map.has(ref); }
  locator(ref: string): Locator | undefined { return this.map.get(ref)?.loc; }
  size(): number { return this.map.size; }
}

export function similarity(a: Locator, b: Locator): number {
  if (a.role !== b.role || a.tag !== b.tag) return 0;
  let score = 0.4;
  if (a.type === b.type) score += 0.05;
  const an = a.name.trim().toLowerCase();
  const bn = b.name.trim().toLowerCase();
  if (an && an === bn) score += 0.4;
  else if (an && bn && (an.includes(bn) || bn.includes(an))) score += 0.15;
  else if (an !== bn) return 0;                                   // both named but differently: a different element
  if (a.path === b.path) score += 0.1;
  const d = Math.hypot(a.x - b.x, a.y - b.y);
  if (d < 40) score += 0.1; else if (d < 200) score += 0.05;
  return Math.min(1, score);
}
