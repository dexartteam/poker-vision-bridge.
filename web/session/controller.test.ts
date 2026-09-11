import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { Detector } from '../core/detector';
import { SessionController } from './controller';

const transport = vi.hoisted(() => ({
  start: vi.fn(),
  change: vi.fn(),
  frame: vi.fn(),
  state: vi.fn(),
  close: vi.fn(),
  recording: vi.fn(),
}));
vi.mock('../transport/api', () => ({
  VisionAPI: class {
    start = transport.start;
    change = transport.change;
    frame = transport.frame;
    state = transport.state;
    close = transport.close;
    recording = transport.recording;
  },
}));
let level = 60;
function pixels() {
  const data = new Uint8ClampedArray(64 * 64 * 4);
  for (let i = 0; i < data.length; i += 4) {
    data[i] = data[i + 1] = data[i + 2] = level;
    data[i + 3] = 255;
  }
  return data;
}
class FakeWorker {
  onmessage: ((event: any) => void) | null = null;
  onerror: (() => void) | null = null;
  detector!: Detector;
  stopped = false;
  postMessage(m: any) {
    if (this.stopped) return;
    if (m.type === 'init') this.detector = new Detector(m.regions, m.config);
    if (m.type === 'accept') this.detector.accept(m.revision, m.pixels);
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
const profile = {
  calibration_id: 'calibration-1',
  width: 64,
  height: 64,
  unit: 'chips' as const,
  scale: 0,
  hero_seat: 0,
  regions: [{ name: 'table', x: 0, y: 0, w: 1, h: 1, ignore: false }],
};
const callbacks = () => ({
  detection: vi.fn(),
  state: vi.fn(),
  error: vi.fn(),
  invalidated: vi.fn(),
});
const source = () => ({}) as CanvasImageSource;
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(10000);
  level = 60;
  Object.values(transport).forEach((fn) => fn.mockReset());
  transport.start.mockResolvedValue(undefined);
  transport.change.mockResolvedValue({});
  transport.frame.mockResolvedValue({});
  transport.close.mockResolvedValue(undefined);
  transport.state.mockResolvedValue({
    state: { capture_epoch: 'irrelevant', visual_revision: 0, needs_confirmation: false },
    outcomes: [],
    queue: { active: false, pending: false },
  });
  vi.stubGlobal('Worker', FakeWorker);
  vi.stubGlobal('HTMLVideoElement', class {});
  vi.stubGlobal('window', globalThis);
  vi.stubGlobal('document', {
    hidden: false,
    createElement: () => ({
      width: 64,
      height: 64,
      getContext: () => ({ drawImage: () => {}, getImageData: () => ({ data: pixels() }) }),
      toDataURL: () => 'data:image/jpeg;base64,fixture',
    }),
  });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it('controller invalidates independently while image upload is unresolved', async () => {
  let release!: (v: any) => void;
  transport.frame.mockImplementationOnce(() => new Promise((r) => (release = r)));
  const cb = callbacks(),
    controller = new SessionController(source, profile, cb, false);
  await controller.start('fixture-token');
  await vi.advanceTimersByTimeAsync(600);
  expect(transport.frame).toHaveBeenCalledTimes(1);
  level = 120;
  await vi.advanceTimersByTimeAsync(200);
  expect(cb.invalidated).toHaveBeenCalled();
  expect(transport.change).toHaveBeenCalledWith(expect.objectContaining({ visual_revision: 1 }));
  release({});
  await controller.stop();
});
it('controller waits for change acknowledgment before uploading its new revision', async () => {
  let release!: (v: any) => void;
  transport.change.mockImplementationOnce(() => new Promise((r) => (release = r)));
  const controller = new SessionController(source, profile, callbacks(), false);
  await controller.start('fixture-token');
  await vi.advanceTimersByTimeAsync(600);
  level = 120;
  await vi.advanceTimersByTimeAsync(1000);
  expect(transport.frame).toHaveBeenCalledTimes(1);
  release({});
  await vi.advanceTimersByTimeAsync(1);
  expect(transport.frame).toHaveBeenCalledTimes(2);
  await controller.stop();
});
it('controller backs off failed invalidation instead of posting every capture', async () => {
  transport.change.mockRejectedValue(new Error('offline'));
  const controller = new SessionController(source, profile, callbacks(), false);
  await controller.start('fixture-token');
  await vi.advanceTimersByTimeAsync(600);
  level = 120;
  await vi.advanceTimersByTimeAsync(1000);
  expect(transport.change).toHaveBeenCalledTimes(1);
  await controller.stop();
});
it('controller stop prevents a late transport response from publishing state', async () => {
  let release!: (v: any) => void;
  transport.state.mockImplementationOnce(() => new Promise((r) => (release = r)));
  const cb = callbacks();
  const controller = new SessionController(source, profile, cb, false);
  await controller.start('fixture-token');
  await vi.advanceTimersByTimeAsync(600);
  await controller.stop();
  release({ state: {}, outcomes: [], queue: {} });
  await vi.advanceTimersByTimeAsync(500);
  expect(cb.state).not.toHaveBeenCalled();
  expect(transport.close).toHaveBeenCalledOnce();
});
it('controller repeated unchanged frames do not flood image uploads', async () => {
  const controller = new SessionController(source, profile, callbacks(), false);
  await controller.start('fixture-token');
  await vi.advanceTimersByTimeAsync(10000);
  expect(transport.frame).toHaveBeenCalledTimes(1);
  await controller.stop();
});
