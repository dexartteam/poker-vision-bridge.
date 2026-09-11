import { copyFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
const target = resolve('web/public/ocr');
await mkdir(target, { recursive: true });
await copyFile('node_modules/tesseract.js/dist/worker.min.js', `${target}/worker.min.js`);
for (const variant of ['lstm', 'simd-lstm', 'relaxedsimd-lstm']) {
  for (const suffix of ['wasm', 'wasm.js']) {
    const name = `tesseract-core-${variant}.${suffix}`;
    await copyFile(`node_modules/tesseract.js-core/${name}`, `${target}/${name}`);
  }
}
await copyFile(
  'node_modules/@tesseract.js-data/eng/4.0.0_best_int/eng.traineddata.gz',
  `${target}/eng.traineddata.gz`,
);
await copyFile('node_modules/tesseract.js-core/LICENSE', `${target}/LICENSE-core.txt`);
await copyFile('node_modules/tesseract.js/LICENSE.md', `${target}/LICENSE-tesseract-js.md`);
console.log('Local OCR Worker, WASM and English model prepared');
