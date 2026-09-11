import { readCards } from './cards';
import type { CardTemplate } from './cards';
import { readPokerStarsCards } from './pokerstars/cards';
self.onmessage = (e: MessageEvent) => {
  try {
    self.postMessage({
      id: e.data.id,
      cards:
        e.data.kind === 'board' || e.data.kind === 'hero'
          ? readPokerStarsCards(e.data.pixels, e.data.kind, e.data.symbols)
          : readCards(e.data.pixels, e.data.templates as CardTemplate[]),
    });
  } catch {
    self.postMessage({ id: e.data.id, error: 'Не удалось прочитать область карт' });
  }
};
