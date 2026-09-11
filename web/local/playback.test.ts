import { afterEach, expect, it, vi } from 'vitest';
import { pauseBeforeRecognition } from './playback';

class QueuedVideo extends EventTarget {
  paused = false;
  pause = vi.fn(() => {
    this.paused = true;
    setTimeout(() => this.dispatchEvent(new Event('pause')), 0);
  });
}
afterEach(() => vi.useRealTimers());

it('waits for queued pause before controller stop listeners can be attached', async () => {
  vi.useFakeTimers();
  const video = new QueuedVideo(),
    stopped = vi.fn(),
    started = vi.fn();
  const ready = pauseBeforeRecognition(video, () => true).then((current) => {
    if (current) {
      started();
      video.addEventListener('pause', stopped);
    }
  });
  await Promise.resolve();
  expect(started).not.toHaveBeenCalled();
  await vi.runAllTimersAsync();
  await ready;
  expect(started).toHaveBeenCalledOnce();
  expect(stopped).not.toHaveBeenCalled();
});

it('does not start when the source or session changed while waiting for pause', async () => {
  vi.useFakeTimers();
  const video = new QueuedVideo();
  let current = true;
  const ready = pauseBeforeRecognition(video, () => current);
  current = false;
  await vi.runAllTimersAsync();
  expect(await ready).toBe(false);
});

it('does not wait for a new pause event when video is already paused', async () => {
  const video = new QueuedVideo();
  video.paused = true;
  expect(await pauseBeforeRecognition(video, () => true)).toBe(true);
  expect(video.pause).not.toHaveBeenCalled();
});
