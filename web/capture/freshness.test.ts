import { expect, it, vi, afterEach } from 'vitest';
import { VideoFreshness } from './freshness';
afterEach(() => vi.useRealTimers());
it('freshness consumes each decoded video frame only once', () => {
  vi.useFakeTimers();
  vi.setSystemTime(1000);
  let onFrame!: () => void;
  const video = {
    requestVideoFrameCallback: (cb: () => void) => {
      onFrame = cb;
      return 1;
    },
    cancelVideoFrameCallback: vi.fn(),
  } as unknown as HTMLVideoElement;
  const gate = new VideoFreshness(video);
  expect(gate.take(1000)).toBeNull();
  onFrame();
  expect(gate.take(1000)).toBe(1000);
  expect(gate.take(1200)).toBeNull();
  expect(() => gate.take(3001)).toThrow('остановился');
  gate.close();
  expect(video.cancelVideoFrameCallback).toHaveBeenCalledWith(1);
});
it('freshness rejects a browser without decoded-frame notifications', () => {
  expect(() => new VideoFreshness({} as HTMLVideoElement)).toThrow('обновите браузер');
});
