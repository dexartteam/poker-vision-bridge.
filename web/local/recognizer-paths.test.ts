import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import { BrowserRecognizer } from './recognizer';
const ocr = vi.hoisted(() => ({ create: vi.fn(), terminate: vi.fn() }));
vi.mock('tesseract.js', () => ({ createWorker: ocr.create, PSM: {} }));
beforeEach(() => {
  ocr.create.mockReset();
  ocr.terminate.mockReset();
  ocr.create.mockResolvedValue({ terminate: ocr.terminate });
  vi.stubGlobal(
    'Worker',
    class {
      terminate() {}
    },
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});
it.each([
  ['/', 'https://vision.example/index.html', 'https://vision.example/ocr'],
  [
    './',
    'https://dexartteam.github.io/poker-vision-bridge./',
    'https://dexartteam.github.io/poker-vision-bridge./ocr',
  ],
  [
    './',
    'https://dexartteam.github.io/poker-vision-bridge./index.html',
    'https://dexartteam.github.io/poker-vision-bridge./ocr',
  ],
])(
  'starts the real recognizer with OCR assets under base %s at %s',
  async (base, page, expected) => {
    vi.stubEnv('BASE_URL', base);
    vi.stubGlobal('document', { baseURI: page });
    const recognizer = new BrowserRecognizer();
    try {
      await recognizer.start();
      expect(ocr.create).toHaveBeenCalledWith('eng', 1, {
        workerPath: `${expected}/worker.min.js`,
        corePath: expected,
        langPath: expected,
        workerBlobURL: false,
      });
    } finally {
      await recognizer.close();
    }
  },
);
