import { performance } from 'node:perf_hooks';
import { Detector, type Pixels } from '../web/core/detector';
import { defaultRegions } from '../web/core/profile';
const pixels: Pixels = {
  width: 640,
  height: 360,
  data: new Uint8ClampedArray(640 * 360 * 4).fill(60),
};
const detector = new Detector(defaultRegions());
const times: number[] = [];
for (let i = 0; i < 140; i++) {
  const start = performance.now();
  const result = detector.step(pixels, i * 200);
  if (i === 2) detector.accept(result.revision, pixels);
  if (i >= 20) times.push(performance.now() - start);
}
times.sort((a, b) => a - b);
console.log(
  JSON.stringify(
    {
      kind: 'synthetic_node_detector_benchmark',
      runtime: process.version,
      width: 640,
      height: 360,
      regions: defaultRegions().length,
      samples: times.length,
      p50_ms: times[Math.floor(times.length * 0.5)],
      p95_ms: times[Math.floor(times.length * 0.95)],
      max_ms: times.at(-1),
      includes_camera_decode: false,
      includes_worker_transfer: false,
      real_camera_accuracy_measured: false,
    },
    null,
    2,
  ),
);
