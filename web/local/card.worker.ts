import { readCards } from './cards';
import type { CardTemplate } from './cards';
self.onmessage = (e: MessageEvent) => {
  try {
    self.postMessage({
      id: e.data.id,
      cards: readCards(e.data.pixels, e.data.templates as CardTemplate[]),
    });
  } catch {
    self.postMessage({ id: e.data.id, error: 'Не удалось прочитать область карт' });
  }
};
