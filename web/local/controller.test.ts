import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import { Detector } from '../core/detector';
import { LocalController } from './controller';
import { openPokerProfile, type Frame, type LocalObservation } from './contracts';
const engine = vi.hoisted(() => ({ start: vi.fn(), close: vi.fn(), recognize: vi.fn() }));
vi.mock('./recognizer', () => ({
  BrowserRecognizer: class {
    start = engine.start;
    close = engine.close;
    recognize = engine.recognize;
  },
}));
let level = 50;
class FakeVideo extends EventTarget {
  videoWidth = 64;
  videoHeight = 64;
  paused = false;
  currentTime = 0;
  callbacks = new Map<number, () => void>();
  serial = 0;
  requestVideoFrameCallback(cb: () => void) {
    this.callbacks.set(++this.serial, cb);
    return this.serial;
  }
  cancelVideoFrameCallback(id: number) {
    this.callbacks.delete(id);
  }
  decode() {
    const [id, cb] = this.callbacks.entries().next().value ?? [];
    if (cb && id !== undefined) {
      this.callbacks.delete(id);
      this.currentTime += 0.2;
      cb();
    }
  }
}
class FakeWorker {
  onmessage: ((e: any) => void) | null = null;
  onerror: (() => void) | null = null;
  detector!: Detector;
  stopped = false;
  postMessage(m: any) {
    if (this.stopped) return;
    if (m.type === 'init') this.detector = new Detector(m.regions, m.config);
    if (m.type === 'release') this.detector.release(m.revision);
    if (m.type === 'frame') {
      const result = this.detector.step(m.pixels, m.now, m.force);
      queueMicrotask(() => this.onmessage?.({ data: { type: 'result', seq: m.seq, result } }));
    }
  }
  terminate() {
    this.stopped = true;
  }
}
const observed = (f: Frame): LocalObservation => ({
  schema_version: 'local-vision.observation.v1',
  source: { ...f.source },
  stable: f.stable,
  status: 'partial',
  evidence: 'single_frame',
  decision_ready: false,
  hand_number: 4812,
  ui_mode: 'live',
  ui_street: 'preflop',
  pot_display: { value: 30, raw: '30', unit: 'chips' },
  board: [],
  hero_cards: null,
  hero_turn: null,
  seats: null,
  pot_includes_current_bets: null,
  diagnostics: { ocr: {}, cards: [], elapsed_ms: 1 },
});
const callbacks = () => ({
  state: vi.fn(),
  outdated: vi.fn(),
  detection: vi.fn(),
  invalidated: vi.fn(),
  error: vi.fn(),
  stopped: vi.fn(),
  count: vi.fn(),
});
async function frames(video: FakeVideo, count: number) {
  for (let i = 0; i < count; i++) {
    video.decode();
    await vi.advanceTimersByTimeAsync(200);
  }
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(10000);
  level = 50;
  for (const fn of Object.values(engine)) fn.mockReset();
  engine.start.mockResolvedValue(undefined);
  engine.close.mockResolvedValue(undefined);
  engine.recognize.mockImplementation(async (f: Frame) => observed(f));
  vi.stubGlobal('Worker', FakeWorker);
  vi.stubGlobal('window', globalThis);
  vi.stubGlobal('fetch', vi.fn());
  vi.stubGlobal('document', {
    hidden: false,
    createElement: () => {
      const canvas = {
        width: 64,
        height: 64,
        getContext: () => ({
          drawImage: () => {},
          getImageData: () => ({
            width: canvas.width,
            height: canvas.height,
            data: new Uint8ClampedArray(canvas.width * canvas.height * 4).fill(level),
          }),
        }),
      };
      return canvas;
    },
  });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
it('starts without a token or API and does not reuse frozen video for manual requests', async () => {
  const video = new FakeVideo(),
    cb = callbacks();
  const c = new LocalController(video as any, openPokerProfile(64, 64), 'recording', cb);
  await c.start();
  await frames(video, 3);
  expect(engine.recognize).toHaveBeenCalledTimes(1);
  c.force();
  c.force();
  await vi.advanceTimersByTimeAsync(1000);
  expect(engine.recognize).toHaveBeenCalledTimes(1);
  expect(fetch).not.toHaveBeenCalled();
  await c.stop();
});
it('invalidates during slow OCR and owns immutable source pixels and calibration', async () => {
  let resolve!: (o: LocalObservation) => void;
  engine.recognize.mockImplementationOnce(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  const video = new FakeVideo(),
    cb = callbacks(),
    profile = openPokerProfile(64, 64);
  const c = new LocalController(video as any, profile, 'recording', cb);
  await c.start();
  await frames(video, 3);
  const captured = engine.recognize.mock.calls[0][0] as Frame;
  profile.regions.amount.x = 0;
  level = 120;
  await frames(video, 1);
  expect(cb.invalidated).toHaveBeenCalled();
  expect(captured.pixels.data[0]).toBe(50);
  expect(engine.recognize.mock.calls[0][1].regions.amount.x).not.toBe(0);
  resolve(observed(captured));
  await vi.advanceTimersByTimeAsync(1);
  expect(cb.state).not.toHaveBeenCalled();
  expect(cb.outdated).toHaveBeenCalledExactlyOnceWith(
    expect.objectContaining({ status: 'stale', source: captured.source }),
    'changed',
  );
  expect(c.recording().observations).toEqual([
    expect.objectContaining({ status: 'stale', decision_ready: false }),
  ]);
  await c.stop();
});
it.each(['seeking', 'pause', 'ended', 'error'])(
  'closes on %s and blocks late OCR',
  async (event) => {
    let resolve!: (o: LocalObservation) => void;
    engine.recognize.mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    const video = new FakeVideo(),
      cb = callbacks(),
      c = new LocalController(video as any, openPokerProfile(64, 64), 'recording', cb);
    await c.start();
    await frames(video, 3);
    const captured = engine.recognize.mock.calls[0][0];
    video.dispatchEvent(new Event(event));
    resolve(observed(captured));
    await vi.advanceTimersByTimeAsync(1);
    expect(cb.state).not.toHaveBeenCalled();
    expect(cb.outdated).not.toHaveBeenCalled();
    expect(engine.close).toHaveBeenCalledOnce();
    expect(video.callbacks.size).toBe(0);
  },
);
it('stop during initialization remains closed when initialization resolves', async () => {
  let resolve!: () => void;
  engine.start.mockImplementation(
    () =>
      new Promise<void>((r) => {
        resolve = r;
      }),
  );
  const video = new FakeVideo(),
    cb = callbacks(),
    c = new LocalController(video as any, openPokerProfile(64, 64), 'recording', cb);
  const start = c.start();
  await c.stop();
  resolve();
  await start;
  await frames(video, 8);
  expect(engine.recognize).not.toHaveBeenCalled();
  expect(video.callbacks.size).toBe(0);
  expect(cb.stopped).toHaveBeenCalledOnce();
});
