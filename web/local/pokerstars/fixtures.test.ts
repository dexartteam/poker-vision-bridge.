import { it, expect } from 'vitest';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createWorker, PSM } from 'tesseract.js';
import { PNG } from 'pngjs';
import { pokerStarsProfile } from './profile';
import { prepareText } from './preprocess';
import { readSymbolSet } from './symbols';
import { readPokerStarsCards } from './cards';
import { recognizePokerStars } from './recognize';
import type { PokerStarsObservation } from '../contracts';

const root = resolve('tests/pokerstars-fixtures');
const available = existsSync(resolve(root, 'frames.json'));
it.skipIf(!available)(
  'reads held video frames with the production pixel pipeline and real local OCR',
  async () => {
    const fixtures = JSON.parse(readFileSync(resolve(root, 'frames.json'), 'utf8'));
    const symbols = readSymbolSet(
      JSON.parse(readFileSync(resolve(root, 'pokerstars-symbols.json'), 'utf8')),
    );
    const profile = pokerStarsProfile();
    const worker = await createWorker('eng', 1, {
      langPath: resolve('node_modules/@tesseract.js-data/eng/4.0.0_best_int'),
      cacheMethod: 'none',
    });
    const observations: PokerStarsObservation[] = [];
    try {
      for (const f of fixtures) {
        const png = PNG.sync.read(readFileSync(resolve(root, f.file)));
        const observation = await recognizePokerStars(
          {
            source: {
              kind: 'recording',
              epoch: 'fixture-epoch',
              calibration_id: profile.id,
              frame_seq: observations.length + 1,
              revision: 0,
              captured_at: 1000,
              media_time: f.seconds,
            },
            stable: false,
            pixels: { width: png.width, height: png.height, data: new Uint8ClampedArray(png.data) },
          },
          profile,
          {
            text: async (pixels, block) => {
              const p = prepareText(pixels);
              await worker.setParameters({
                tessedit_pageseg_mode: block ? PSM.SINGLE_BLOCK : PSM.SINGLE_LINE,
                tessedit_char_whitelist: '',
                user_defined_dpi: '150',
              });
              const { data } = await worker.recognize(
                PNG.sync.write({
                  width: p.width,
                  height: p.height,
                  data: Buffer.from(p.data),
                } as PNG),
              );
              return { text: data.text.trim(), confidence: data.confidence };
            },
            cards: async (pixels, kind) => readPokerStarsCards(pixels, kind, symbols),
          },
        );
        observations.push(observation);
      }
    } finally {
      await worker.terminate();
    }
    writeFileSync(resolve(root, 'observations.json'), JSON.stringify(observations, null, 2));
    expect(observations).toHaveLength(15);
    let readableCards = 0,
      presentCards = 0,
      readableStacks = 0,
      readableBets = 0,
      readableActions = 0;
    for (const [i, o] of observations.entries()) {
      const f = fixtures[i],
        context = `frame ${f.seconds}s`;
      if (o.pot_display.value !== null) expect(o.pot_display.value, context + ' pot').toBe(f.pot);
      expect(o.hand_number, context).toBeNull();
      expect(o.decision_ready, context).toBe(false);
      expect(o.hero_turn, context + ' turn controls').toBe(f.hero_turn ? true : null);
      for (const [reads, expected] of [
        [o.board, f.board],
        [o.hero_cards, f.hero],
      ] as const) {
        presentCards += expected.length;
        reads.forEach((card, slot) => {
          if (card.status === 'visible') {
            readableCards++;
            expect(card.value, context + ` card ${slot}`).toBe(expected[slot]);
          }
          if (!expected[slot]) expect(card.value, context + ' absent card').toBeNull();
        });
      }
      for (const seat of o.seats) {
        const truth = f.seats[seat.seat_index];
        if (seat.action_label !== null) {
          readableActions++;
          expect(seat.action_label, context + ' action caption').toBe(
            truth.caption?.toLowerCase().replace(/ /g, '_'),
          );
        }
        if (seat.stack !== null) {
          readableStacks++;
          expect(seat.stack, context + ` stack ${seat.seat_index}`).toBe(truth.stack);
        }
        expect(seat.bet).toBeNull();
        if (seat.chip_display !== null) {
          readableBets++;
          expect(seat.chip_display, context + ` chip label ${seat.seat_index}`).toBe(
            truth.chip_display,
          );
        }
      }
    }
    expect(observations.filter((o) => o.pot_display.value !== null).length).toBeGreaterThanOrEqual(
      13,
    );
    expect(readableCards / presentCards).toBeGreaterThanOrEqual(59 / 64);
    expect(readableStacks).toBeGreaterThanOrEqual(86);
    expect(readableBets).toBeGreaterThanOrEqual(23);
    expect(readableActions).toBeGreaterThanOrEqual(9);
  },
  60000,
);
