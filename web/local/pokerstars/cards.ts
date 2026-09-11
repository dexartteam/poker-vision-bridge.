import type { Pixels } from '../../core/detector';
import type { CardMatch } from '../contracts';
import { classifySymbolCard, type SymbolSet } from './symbols';

export function readPokerStarsCards(
  p: Pixels,
  kind: 'board' | 'hero',
  set: SymbolSet | null,
): CardMatch[] {
  const ref = kind === 'board' ? [360, 100] : [106, 108];
  const origins =
    kind === 'board'
      ? Array.from({ length: 5 }, (_, i) => [6 + 71 * i, 4])
      : [
          [6, 6],
          [32, 11],
        ];
  return origins.map(([x, y]) => {
    const box = {
      x: (x * p.width) / ref[0],
      y: (y * p.height) / ref[1],
      w: (67 * p.width) / ref[0],
      h: (93 * p.height) / ref[1],
    };
    const m = classifySymbolCard(p, box, set);
    return {
      ...m,
      score: Math.min(m.rankScore ?? 0, m.suitScore ?? 0),
      margin: Math.min(m.rankMargin ?? 0, m.suitMargin ?? 0),
    };
  });
}
