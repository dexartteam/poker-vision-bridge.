import type { Pixels } from '../../core/detector';
import {
  pixelRect,
  type CardMatch,
  type Frame,
  type LocalProfile,
  type OCR,
  type PokerStarsObservation,
  type SeatRead,
} from '../contracts';
import {
  activeButton,
  actionButtonPixels,
  actionCaption,
  betVisibility,
  cropPixels,
} from './preprocess';
import { readPokerStarsFields } from './fields';

export type PokerStarsReader = {
  text: (pixels: Pixels, block: boolean) => Promise<OCR>;
  cards: (pixels: Pixels, kind: 'board' | 'hero') => Promise<CardMatch[]>;
};
/** One immutable frame, no DOM, no cross-frame merging or action-history inference. */
export async function recognizePokerStars(
  frame: Frame,
  profile: LocalProfile,
  reader: PokerStarsReader,
): Promise<PokerStarsObservation> {
  const begin = performance.now();
  const crop = (name: string) =>
    cropPixels(
      frame.pixels,
      pixelRect(profile.regions[name], frame.pixels.width, frame.pixels.height),
    );
  const ocr: Record<string, OCR> = {};
  const bets: Record<number, SeatRead['chip_visibility']> = {};
  const captions: Record<number, boolean> = {};
  for (const name of Object.keys(profile.regions)) {
    if (['board', 'hero', 'controls', 'chat'].includes(name)) continue;
    const pixels = crop(name);
    if (name.startsWith('name')) captions[Number(name.slice(4))] = actionCaption(pixels);
    if (name.startsWith('bet')) {
      bets[Number(name.slice(3))] = betVisibility(pixels);
      if (bets[Number(name.slice(3))] !== 'visible') continue;
    }
    ocr[name] = await reader.text(pixels, false);
  }
  const buttonPixels = actionButtonPixels(crop('controls'));
  const buttons = buttonPixels.map(activeButton);
  for (let i = 0; i < buttons.length; i++) {
    if (buttons[i]) ocr[`control${i}`] = await reader.text(buttonPixels[i], true);
  }
  const board = await reader.cards(crop('board'), 'board');
  const hero = await reader.cards(crop('hero'), 'hero');
  return {
    schema_version: 'local-vision.observation.v2',
    layout: 'pokerstars-classic',
    source: { ...frame.source },
    ...readPokerStarsFields(ocr, board, hero, bets, buttons, captions),
    status: 'partial',
    stable: frame.stable,
    evidence: frame.stable ? 'single_frame' : 'moving',
    decision_ready: false,
    pot_includes_current_bets: null,
    diagnostics: {
      ocr,
      cards: [...board, ...hero],
      elapsed_ms: Math.round(performance.now() - begin),
    },
  };
}
