import { createWorker, PSM, type Worker as OCRWorker } from 'tesseract.js';
import type { Pixels } from '../core/detector';
import {
  pixelRect,
  type CardMatch,
  type Frame,
  type LocalObservation,
  type LocalProfile,
  type OCR,
} from './contracts';
import { readFields } from './fields';
import { prepareHeader, prepareAmount } from './preprocess';
import type { CardTemplate } from './cards';

export class BrowserRecognizer {
  constructor(private templates: CardTemplate[] = []) {
    this.templates = structuredClone(templates);
  }
  private ocr: OCRWorker | null = null;
  private closed = false;
  private cards: Worker | null = null;
  private rejectCards: ((e: Error) => void) | null = null;
  private id = 0;
  async start() {
    const ocr = await createWorker('eng', 1, {
      workerPath: new URL('/ocr/worker.min.js', location.origin).href,
      corePath: new URL('/ocr', location.origin).href,
      langPath: new URL('/ocr', location.origin).href,
      workerBlobURL: false,
    });
    if (this.closed) {
      await ocr.terminate();
      return;
    }
    this.ocr = ocr;
    this.cards = new Worker(new URL('./card.worker.ts', import.meta.url), { type: 'module' });
  }
  async close() {
    if (this.closed) return;
    this.closed = true;
    this.rejectCards?.(new Error('Наблюдение остановлено'));
    this.cards?.terminate();
    this.cards = null;
    const ocr = this.ocr;
    this.ocr = null;
    if (ocr) await ocr.terminate();
  }
  private cardRead(pixels: Pixels): Promise<CardMatch[]> {
    return new Promise((resolve, reject) => {
      if (!this.cards || this.closed) {
        reject(new Error('Распознаватель остановлен'));
        return;
      }
      const id = ++this.id;
      const timeout = setTimeout(() => {
        this.rejectCards = null;
        reject(new Error('Таймаут чтения карт'));
      }, 5000);
      const done = () => {
        clearTimeout(timeout);
        this.rejectCards = null;
      };
      this.rejectCards = (error) => {
        done();
        reject(error);
      };
      this.cards.onmessage = (e) => {
        if (e.data.id !== id) return;
        done();
        if (e.data.error) reject(new Error(e.data.error));
        else resolve(e.data.cards);
      };
      this.cards.onerror = () => {
        done();
        reject(new Error('Ошибка Worker карт'));
      };
      this.cards.postMessage({ id, pixels, templates: this.templates }, [pixels.data.buffer]);
    });
  }
  async recognize(frame: Frame, profile: LocalProfile): Promise<LocalObservation> {
    if (!this.ocr || this.closed) throw new Error('Распознаватель не запущен');
    if (frame.pixels.width !== profile.width || frame.pixels.height !== profile.height)
      throw new Error('Размер кадра изменился. Настройте области заново.');
    const begin = performance.now();
    const canvas = document.createElement('canvas');
    canvas.width = profile.width;
    canvas.height = profile.height;
    const ctx = canvas.getContext('2d')!;
    ctx.putImageData(
      new ImageData(new Uint8ClampedArray(frame.pixels.data), profile.width, profile.height),
      0,
      0,
    );
    const crop = document.createElement('canvas');
    const ocr: Record<string, OCR> = {};
    for (const name of ['header', 'mode', 'pot', 'amount'] as const) {
      if (this.closed) throw new Error('Наблюдение остановлено');
      const r = pixelRect(profile.regions[name], profile.width, profile.height);
      crop.width = r.w;
      crop.height = r.h;
      crop.getContext('2d')!.drawImage(canvas, r.x, r.y, r.w, r.h, 0, 0, r.w, r.h);
      if (name === 'header' || name === 'amount') {
        const prepare = name === 'header' ? prepareHeader : prepareAmount;
        const prepared = prepare(crop.getContext('2d')!.getImageData(0, 0, r.w, r.h));
        crop.width = prepared.width;
        crop.height = prepared.height;
        crop
          .getContext('2d')!
          .putImageData(
            new ImageData(
              prepared.data as Uint8ClampedArray<ArrayBuffer>,
              prepared.width,
              prepared.height,
            ),
            0,
            0,
          );
      }
      await this.ocr.setParameters({
        tessedit_pageseg_mode: name === 'pot' ? PSM.SINGLE_BLOCK : PSM.SINGLE_LINE,
        tessedit_char_whitelist: '',
        user_defined_dpi: '150',
      });
      const { data } = await this.ocr.recognize(crop);
      ocr[name] = { text: data.text.trim(), confidence: data.confidence };
    }
    const r = pixelRect(profile.regions.board, profile.width, profile.height);
    const pixels = ctx.getImageData(r.x, r.y, r.w, r.h);
    const cards = await this.cardRead({ width: r.w, height: r.h, data: pixels.data });
    return {
      schema_version: 'local-vision.observation.v1',
      source: { ...frame.source },
      ...readFields(ocr, cards),
      status: 'partial',
      stable: frame.stable,
      evidence: frame.stable ? 'single_frame' : 'moving',
      decision_ready: false,
      hero_cards: null,
      hero_turn: null,
      seats: null,
      pot_includes_current_bets: null,
      diagnostics: { ocr, cards, elapsed_ms: Math.round(performance.now() - begin) },
    };
  }
}
