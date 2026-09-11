import { Detector, type Pixels, type Reason } from '../core/detector';
import type { Region } from '../core/profile';
let detector: Detector | null = null;
self.onmessage = (event: MessageEvent) => {
  const m = event.data;
  try {
    if (m.type === 'init') detector = new Detector(m.regions as Region[], m.config);
    else if (m.type === 'accept') detector?.accept(m.revision, m.pixels as Pixels);
    else if (m.type === 'release') detector?.release(m.revision);
    else if (m.type === 'frame') {
      if (!detector) throw new Error('detector_not_initialized');
      self.postMessage({
        type: 'result',
        seq: m.seq,
        result: detector.step(m.pixels as Pixels, m.now, m.force as Reason | null),
      });
    }
  } catch (error) {
    self.postMessage({
      type: 'error',
      message: error instanceof Error ? error.message : 'worker_failed',
    });
  }
};
