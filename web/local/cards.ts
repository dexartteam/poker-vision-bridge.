import type { Pixels } from '../core/detector';
import type { CardMatch, Rect } from './contracts';
export type CardFeature = { rank: number[]; suit: number[]; color: 'red' | 'black' };
export type CardTemplate = CardFeature & { value: string };
export function readTemplateSet(input: unknown): CardTemplate[] {
  const set = input as { schema_version?: string; templates?: CardTemplate[] } | null;
  if (
    !set ||
    set.schema_version !== 'local-vision.cards.v1' ||
    !Array.isArray(set.templates) ||
    set.templates.length < 1 ||
    set.templates.length > 52
  )
    throw new Error('Некорректный набор шаблонов карт');
  const seen = new Set<string>();
  for (const card of set.templates) {
    if (
      !card ||
      typeof card.value !== 'string' ||
      !/^[2-9TJQKA][cdhs]$/.test(card.value) ||
      seen.has(card.value) ||
      card.color !== ('dh'.includes(card.value[1]) ? 'red' : 'black') ||
      ![card.rank, card.suit].every(
        (feature) =>
          Array.isArray(feature) &&
          feature.length === 400 &&
          feature.every((v) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1) &&
          feature.some((v) => v > 0),
      )
    )
      throw new Error('Некорректные признаки карты');
    seen.add(card.value);
  }
  return structuredClone(set.templates);
}
const paper = (r: number, g: number, b: number) =>
  r > 150 && g > 125 && b > 70 && r >= g && g >= b && r - g < 65 && g - b < 100;
const ink = (r: number, g: number, b: number, threshold: number) =>
  (r + g + b) / 3 < threshold || (r > g * 1.35 && r > b * 1.35 && r > 110);

/** Connected card-colored paper, not OCR text boxes or betting overlays. */
export function locateCards(p: Pixels): Rect[] {
  if (p.data.length !== p.width * p.height * 4) throw new Error('invalid_card_pixels');
  const marks = new Uint8Array(p.width * p.height),
    stack = new Int32Array(marks.length);
  for (let i = 0; i < marks.length; i++)
    marks[i] = paper(p.data[i * 4], p.data[i * 4 + 1], p.data[i * 4 + 2]) ? 1 : 0;
  const boxes: Rect[] = [];
  for (let start = 0; start < marks.length; start++) {
    if (marks[start] !== 1) continue;
    let top = 0,
      count = 0,
      x0 = p.width,
      x1 = 0,
      y0 = p.height,
      y1 = 0;
    stack[top++] = start;
    marks[start] = 2;
    while (top) {
      const at = stack[--top],
        x = at % p.width,
        y = Math.floor(at / p.width);
      count++;
      x0 = Math.min(x0, x);
      x1 = Math.max(x1, x);
      y0 = Math.min(y0, y);
      y1 = Math.max(y1, y);
      for (const next of [
        x ? at - 1 : -1,
        x + 1 < p.width ? at + 1 : -1,
        at - p.width,
        at + p.width,
      ])
        if (next >= 0 && next < marks.length && marks[next] === 1) {
          marks[next] = 2;
          stack[top++] = next;
        }
    }
    const w = x1 - x0 + 1,
      h = y1 - y0 + 1;
    if (
      w >= 18 &&
      h >= 25 &&
      w / h > 0.57 &&
      w / h < 1 &&
      count / (w * h) > 0.63 &&
      x0 > 0 &&
      y0 > 0 &&
      x1 < p.width - 1 &&
      y1 < p.height - 1
    )
      boxes.push({ x: x0, y: y0, w, h });
  }
  return boxes.sort((a, b) => a.x - b.x);
}
export function feature(p: Pixels, box: Rect): CardFeature {
  let red = 0,
    black = 0;
  const sample = (area: Rect) => {
    const width = Math.max(1, Math.ceil(area.w * box.w)),
      height = Math.max(1, Math.ceil(area.h * box.h));
    const mask = new Uint8Array(width * height);
    const light: number[] = [];
    for (let y = 0; y < height; y++)
      for (let x = 0; x < width; x++) {
        const px = Math.min(
          p.width - 1,
          Math.floor(box.x + (area.x + ((x + 0.5) / width) * area.w) * box.w),
        );
        const py = Math.min(
          p.height - 1,
          Math.floor(box.y + (area.y + ((y + 0.5) / height) * area.h) * box.h),
        );
        const i = (py * p.width + px) * 4;
        light.push((p.data[i] + p.data[i + 1] + p.data[i + 2]) / 3);
      }
    light.sort((a, b) => a - b);
    const index = (light.length - 1) * 0.9,
      low = Math.floor(index),
      fraction = index - low;
    const threshold =
      (light[low] * (1 - fraction) + light[Math.min(low + 1, light.length - 1)] * fraction) * 0.8;
    let x0 = width,
      y0 = height,
      x1 = -1,
      y1 = -1;
    for (let y = 0; y < height; y++)
      for (let x = 0; x < width; x++) {
        const px = Math.min(
          p.width - 1,
          Math.floor(box.x + (area.x + ((x + 0.5) / width) * area.w) * box.w),
        );
        const py = Math.min(
          p.height - 1,
          Math.floor(box.y + (area.y + ((y + 0.5) / height) * area.h) * box.h),
        );
        const i = (py * p.width + px) * 4,
          r = p.data[i],
          g = p.data[i + 1],
          b = p.data[i + 2];
        if (ink(r, g, b, threshold)) {
          mask[y * width + x] = 1;
          x0 = Math.min(x0, x);
          y0 = Math.min(y0, y);
          x1 = Math.max(x1, x);
          y1 = Math.max(y1, y);
          if (r > g * 1.35) red++;
          else black++;
        }
      }
    const values: number[] = [];
    // Normalize the ink bounds, so a small perspective-induced shift is not a new symbol.
    for (let y = 0; y < 20; y++)
      for (let x = 0; x < 20; x++) {
        let hits = 0;
        if (x1 >= x0 && y1 >= y0)
          for (const dy of [0.25, 0.75])
            for (const dx of [0.25, 0.75]) {
              const px = Math.min(x1, Math.floor(x0 + ((x + dx) / 20) * (x1 - x0 + 1)));
              const py = Math.min(y1, Math.floor(y0 + ((y + dy) / 20) * (y1 - y0 + 1)));
              hits += mask[py * width + px];
            }
        values.push(hits / 4);
      }
    return values;
  };
  const rank = sample({ x: 0.09, y: 0.065, w: 0.37, h: 0.27 });
  const suit = sample({ x: 0.17, y: 0.42, w: 0.67, h: 0.51 });
  return { rank, suit, color: red > black ? 'red' : 'black' };
}
function similarity(a: number[], b: number[]) {
  let overlap = 0,
    sum = 0;
  for (let i = 0; i < a.length; i++) {
    overlap += Math.min(a[i], b[i]);
    sum += a[i] + b[i];
  }
  return sum > 3 ? (2 * overlap) / sum : 0;
}
export function classifyCard(p: Pixels, box: Rect, templates: CardTemplate[]): CardMatch {
  const f = feature(p, box);
  const matches = templates
    .filter((t) => t.color === f.color)
    .map((t) => ({
      value: t.value,
      rank: similarity(f.rank, t.rank),
      suit: similarity(f.suit, t.suit),
    }))
    .map((m) => ({ ...m, score: Math.min(m.rank, m.suit) }))
    .sort((a, b) => b.score - a.score);
  const best = matches[0],
    second = matches.find((m) => m.value !== best?.value);
  const score = best?.score ?? 0,
    margin = score - (second?.score ?? 0);
  const readable = score >= 0.78 && margin >= 0.1;
  return {
    box,
    score,
    margin,
    status: readable ? 'visible' : 'unreadable',
    value: readable ? best.value : null,
  };
}
export function readCards(p: Pixels, templates: CardTemplate[]): CardMatch[] {
  return locateCards(p).map((box) => classifyCard(p, box, templates));
}
