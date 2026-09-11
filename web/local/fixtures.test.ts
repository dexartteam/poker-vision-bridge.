import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { inflateSync } from 'node:zlib';
import { createWorker, PSM } from 'tesseract.js';
import { PNG } from 'pngjs';
import { prepareHeader, prepareAmount } from './preprocess';
import { readCards, readTemplateSet } from './cards';
import type { CardTemplate } from './cards';
import { readFields } from './fields';
const fixturePath = resolve('tests/vision-fixtures/frames.json');
const hasRecording = existsSync(fixturePath);
const fixtures = hasRecording ? JSON.parse(readFileSync(fixturePath, 'utf8')) : [];
const templates = hasRecording
  ? readTemplateSet(
      JSON.parse(readFileSync(resolve('tests/vision-fixtures/templates.json'), 'utf8')),
    )
  : [];
const pixels = (p: { width: number; height: number; rgba: string }) => ({
  width: p.width,
  height: p.height,
  data: new Uint8ClampedArray(inflateSync(Buffer.from(p.rgba, 'base64'))),
});
describe('source recording regressions', () => {
  it.skipIf(!hasRecording)(
    'reads actual OCR crops with the bundled npm model and rejects both replay pots',
    async () => {
      expect(fixtures).toHaveLength(10);
      const worker = await createWorker('eng', 1, {
        langPath: resolve('node_modules/@tesseract.js-data/eng/4.0.0_best_int'),
        cacheMethod: 'none',
      });
      try {
        for (const fixture of fixtures) {
          const ocr: Record<string, { text: string; confidence: number }> = {};
          for (const field of ['header', 'mode', 'pot', 'amount']) {
            await worker.setParameters({
              tessedit_pageseg_mode: field === 'pot' ? PSM.SINGLE_BLOCK : PSM.SINGLE_LINE,
              tessedit_char_whitelist: '',
              user_defined_dpi: '150',
            });
            let image: Buffer = Buffer.from(fixture.ocr_images[field], 'base64');
            if (field === 'header' || field === 'amount') {
              const decoded = PNG.sync.read(image);
              const prepare = field === 'header' ? prepareHeader : prepareAmount;
              const prepared = prepare({
                width: decoded.width,
                height: decoded.height,
                data: new Uint8ClampedArray(decoded.data),
              });
              image = PNG.sync.write({
                width: prepared.width,
                height: prepared.height,
                data: Buffer.from(prepared.data),
              } as PNG);
            }
            const { data } = await worker.recognize(image);
            ocr[field] = { text: data.text.trim(), confidence: data.confidence };
          }
          const fields = readFields(
            ocr,
            readCards(pixels(fixture.board_image), templates as CardTemplate[]),
          );
          expect(
            {
              ui_mode: fields.ui_mode,
              hand_number: fields.hand_number,
              ui_street: fields.ui_street,
              pot: fields.pot_display.value,
            },
            fixture.frame_id,
          ).toEqual(fixture.expected);
        }
      } finally {
        await worker.terminate();
      }
    },
    30000,
  );
  it.skipIf(!hasRecording)(
    'locates complete paper cards and does not guess a card beneath a bet overlay',
    () => {
      const clear = fixtures.find((f: any) => f.frame_id === 'frame-42s');
      const covered = fixtures.find((f: any) => f.frame_id === 'frame-34s');
      expect(
        readCards(pixels(clear.board_image), templates as CardTemplate[]).map((c) => c.value),
      ).toEqual(['6c', 'Qd', 'Tc', '2h', 'Kh']);
      const flop = fixtures.find((f: any) => f.frame_id === 'frame-22s');
      expect(
        readCards(pixels(flop.board_image), templates as CardTemplate[]).map((c) => c.value),
      ).toEqual(['6c', 'Qd', 'Tc']);
      expect(readCards(pixels(covered.board_image), templates as CardTemplate[])).toHaveLength(3);
    },
  );
  it('rejects empty or unrelated images rather than returning the nearest card', () => {
    const blank = { width: 100, height: 100, data: new Uint8ClampedArray(40000) };
    expect(readCards(blank, templates as CardTemplate[])).toEqual([]);
  });
});
