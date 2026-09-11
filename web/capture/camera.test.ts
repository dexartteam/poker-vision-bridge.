import { expect, it, vi, afterEach } from 'vitest';
import { Camera } from './camera';
afterEach(() => vi.unstubAllGlobals());
it('camera stops every track on explicit stop', async () => {
  const stop = vi.fn();
  vi.stubGlobal('navigator', {
    mediaDevices: {
      getUserMedia: async () => ({
        getTracks: () => [{ stop }],
        getVideoTracks: () => [{ getSettings: () => ({ width: 1280 }) }],
      }),
    },
  });
  const camera = new Camera();
  const video = { play: async () => {}, srcObject: null } as unknown as HTMLVideoElement;
  expect(await camera.start(video)).toEqual({ width: 1280 });
  camera.stop();
  expect(stop).toHaveBeenCalledOnce();
});
it('late camera permission cannot reopen a stopped source', async () => {
  let resolve!: (s: any) => void;
  const stop = vi.fn();
  vi.stubGlobal('navigator', {
    mediaDevices: { getUserMedia: () => new Promise((r) => (resolve = r)) },
  });
  const camera = new Camera();
  const promise = camera.start({} as HTMLVideoElement);
  camera.stop();
  resolve({ getTracks: () => [{ stop }] });
  await expect(promise).rejects.toThrow('camera_start_cancelled');
  expect(stop).toHaveBeenCalledOnce();
});
it('camera permission denial stays an error', async () => {
  vi.stubGlobal('navigator', {
    mediaDevices: {
      getUserMedia: async () => {
        throw new Error('denied');
      },
    },
  });
  await expect(new Camera().start({} as HTMLVideoElement)).rejects.toThrow('denied');
});
it('missing secure media API explains unavailable camera', async () => {
  vi.stubGlobal('navigator', {});
  await expect(new Camera().start({} as HTMLVideoElement)).rejects.toThrow('HTTPS');
});
