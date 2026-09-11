/** PokerStars corner-glyph proof. No video frames, labels by time, or embedded templates. */
export type Pixels = { width: number; height: number; data: Uint8Array | Uint8ClampedArray };
export type Rect = { x: number; y: number; w: number; h: number };
export type SymbolTemplate = {
  value: string;
  color: 'red' | 'black';
  feature: number[];
  source?: string;
};
export type SymbolSet = {
  schema_version: 'local-vision.symbols.v1';
  profile: 'pokerstars-mobile-landscape-corner-v1';
  ranks: SymbolTemplate[];
  suits: SymbolTemplate[];
};
export type Match = {
  box: Rect;
  status: 'empty' | 'unreadable' | 'visible';
  value: string | null;
  rankScore?: number;
  suitScore?: number;
  rankMargin?: number;
  suitMargin?: number;
};

export function readSymbolSet(input: unknown): SymbolSet {
  const set = input as SymbolSet | null;
  if (
    !set ||
    set.schema_version !== 'local-vision.symbols.v1' ||
    set.profile !== 'pokerstars-mobile-landscape-corner-v1'
  )
    throw new Error('invalid_symbol_set');
  for (const [key, size, expression] of [
    ['ranks', 560, /^[2-9TJQKA]$/],
    ['suits', 400, /^[cdhs]$/],
  ] as const) {
    const entries = set[key];
    if (!Array.isArray(entries) || !entries.length || entries.length > 104)
      throw new Error('invalid_symbol_entries');
    for (const t of entries) {
      if (
        !t ||
        typeof t.value !== 'string' ||
        !expression.test(t.value) ||
        !['red', 'black'].includes(t.color) ||
        (key === 'suits' && t.color !== ('dh'.includes(t.value) ? 'red' : 'black')) ||
        !Array.isArray(t.feature) ||
        t.feature.length !== size ||
        !t.feature.every((v) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1) ||
        !t.feature.some((v) => v > 0)
      )
        throw new Error('invalid_symbol_feature');
    }
  }
  // Missing symbol classes make runner-up margins misleading. This profile requires the complete alphabet.
  if (
    new Set(set.ranks.map((t) => t.value)).size !== 13 ||
    new Set(set.suits.map((t) => t.value)).size !== 4
  )
    throw new Error('incomplete_symbol_alphabet');
  return structuredClone(set);
}

type Part = { feature: number[]; ink: number; red: number; bounds: [number, number] };
type Features = {
  rank: Part;
  suit: Part;
  paper: number;
  background: number;
  color: 'red' | 'black' | 'mixed';
};
function sample(p: Pixels, box: Rect, roi: readonly [number, number, number, number]) {
  const [rx, ry, rw, rh] = roi;
  const width = Math.max(1, Math.ceil((rw * box.w) / 67));
  const height = Math.max(1, Math.ceil((rh * box.h) / 93));
  const rgb: number[][] = [];
  for (let y = 0; y < height; y++)
    for (let x = 0; x < width; x++) {
      const px = Math.min(
        p.width - 1,
        Math.max(0, Math.floor(box.x + ((rx + ((x + 0.5) * rw) / width) * box.w) / 67)),
      );
      const py = Math.min(
        p.height - 1,
        Math.max(0, Math.floor(box.y + ((ry + ((y + 0.5) * rh) / height) * box.h) / 93)),
      );
      const i = (py * p.width + px) * 4;
      rgb.push([p.data[i], p.data[i + 1], p.data[i + 2]]);
    }
  return { width, height, rgb };
}
function extract(
  p: Pixels,
  box: Rect,
  roi: readonly [number, number, number, number],
  ow: number,
  oh: number,
): Part {
  const a = sample(p, box, roi),
    mask = new Uint8Array(a.width * a.height);
  let x0 = a.width,
    x1 = -1,
    y0 = a.height,
    y1 = -1,
    ink = 0,
    red = 0;
  a.rgb.forEach(([r, g, b], at) => {
    const isRed = r > 100 && r > g * 1.4 && r > b * 1.4;
    const hi = Math.max(r, g, b),
      lo = Math.min(r, g, b);
    if (isRed || (hi < 190 && hi - lo < 55)) {
      mask[at] = 1;
      ink++;
      if (isRed) red++;
      const x = at % a.width,
        y = Math.floor(at / a.width);
      x0 = Math.min(x0, x);
      x1 = Math.max(x1, x);
      y0 = Math.min(y0, y);
      y1 = Math.max(y1, y);
    }
  });
  const feature: number[] = [];
  for (let y = 0; y < oh; y++)
    for (let x = 0; x < ow; x++) {
      let hits = 0;
      if (ink)
        for (const dy of [0.25, 0.75])
          for (const dx of [0.25, 0.75]) {
            const px = Math.min(x1, Math.floor(x0 + ((x + dx) / ow) * (x1 - x0 + 1)));
            const py = Math.min(y1, Math.floor(y0 + ((y + dy) / oh) * (y1 - y0 + 1)));
            hits += mask[py * a.width + px];
          }
      feature.push(hits / 4);
    }
  return { feature, ink, red, bounds: ink ? [x1 - x0 + 1, y1 - y0 + 1] : [0, 0] };
}
export function symbolFeatures(p: Pixels, box: Rect): Features {
  const a = sample(p, box, [2, 2, 23, 51]);
  let paper = 0,
    background = 0;
  for (const [r, g, b] of a.rgb) {
    const hi = Math.max(r, g, b),
      lo = Math.min(r, g, b);
    if (lo > 185 && hi - lo < 55) paper++;
    if (hi < 110 || (g > r * 1.25 && g > b * 1.15)) background++;
  }
  const rank = extract(p, box, [2, 2, 23, 27], 20, 28);
  const suit = extract(p, box, [2, 29, 23, 24], 20, 20);
  const red = (rank.red + suit.red) / Math.max(1, rank.ink + suit.ink);
  return {
    rank,
    suit,
    paper: paper / a.rgb.length,
    background: background / a.rgb.length,
    color: red >= 0.6 ? 'red' : red <= 0.1 ? 'black' : 'mixed',
  };
}
function dice(a: number[], b: number[]) {
  let overlap = 0,
    sum = 0;
  for (let i = 0; i < a.length; i++) {
    overlap += Math.min(a[i], b[i]);
    sum += a[i] + b[i];
  }
  return sum > 3 ? (2 * overlap) / sum : 0;
}
function best(feature: number[], templates: SymbolTemplate[], color?: 'red' | 'black') {
  const scores = new Map<string, number>();
  for (const t of templates) {
    if (color && t.color !== color) continue;
    scores.set(t.value, Math.max(scores.get(t.value) ?? 0, dice(feature, t.feature)));
  }
  const sorted = [...scores].sort((a, b) => b[1] - a[1]);
  const first = sorted[0];
  return {
    value: first?.[0] ?? '',
    score: first?.[1] ?? 0,
    margin: (first?.[1] ?? 0) - (sorted[1]?.[1] ?? 0),
  };
}
export function classifySymbolCard(p: Pixels, box: Rect, set: SymbolSet | null): Match {
  const unknown: Match = { box, status: 'unreadable', value: null };
  if (
    p.data.length !== p.width * p.height * 4 ||
    box.w <= 0 ||
    box.h <= 0 ||
    box.x < 0 ||
    box.y < 0 ||
    box.x + box.w > p.width ||
    box.y + box.h > p.height
  )
    return unknown;
  const f = symbolFeatures(p, box);
  if (f.paper < 0.1) return { ...unknown, status: f.background > 0.9 ? 'empty' : 'unreadable' };
  if (!set) return unknown;
  if (f.color === 'mixed') return unknown;
  const rank = best(f.rank.feature, set.ranks),
    suit = best(f.suit.feature, set.suits, f.color);
  const [rw, rh] = f.rank.bounds,
    [sw, sh] = f.suit.bounds;
  const quality =
    f.paper >= 0.42 &&
    rw >= (5 * box.w) / 67 &&
    rh >= (12 * box.h) / 93 &&
    sw >= (5 * box.w) / 67 &&
    sh >= (8 * box.h) / 93;
  const visible =
    quality &&
    rank.score >= 0.88 &&
    suit.score >= 0.86 &&
    rank.margin >= 0.075 &&
    suit.margin >= 0.1;
  return {
    box,
    status: visible ? 'visible' : 'unreadable',
    value: visible ? rank.value + suit.value : null,
    rankScore: rank.score,
    suitScore: suit.score,
    rankMargin: rank.margin,
    suitMargin: suit.margin,
  };
}

/** Fixed geometry is a declared layout calibration, never a card-answer lookup. */
export function referenceSlots(width: number, height: number): { hero: Rect[]; board: Rect[] } {
  const scale = (x: number, y: number): Rect => ({
    x: (x * width) / 1280,
    y: (y * height) / 640,
    w: (67 * width) / 1280,
    h: (93 * height) / 640,
  });
  return {
    hero: [scale(668, 446), scale(694, 451)],
    board: Array.from({ length: 5 }, (_, i) => scale(468 + 71 * i, 270)),
  };
}
