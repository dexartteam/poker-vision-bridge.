import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { resolve, relative } from 'node:path';

const root = resolve('dist/pages');
const html = readFileSync(resolve(root, 'index.html'), 'utf8');
const references = [...html.matchAll(/(?:src|href)="([^"#]+)"/g)].map((m) => m[1]);
assert(references.length >= 2, 'Missing built entry assets');
for (const reference of references) {
  assert(
    !reference.startsWith('/') && !reference.includes(':'),
    `Non-relative page asset: ${reference}`,
  );
  assert(existsSync(resolve(root, reference)), `Missing page asset: ${reference}`);
}
assert(!existsSync(resolve(root, 'server.html')), 'Server console must not be published to Pages');
const assets = readdirSync(resolve(root, 'assets'));
assert(
  assets.some((name) => /^worker-.*\.js$/.test(name)),
  'Missing detector Worker',
);
assert(
  assets.some((name) => /^card\.worker-.*\.js$/.test(name)),
  'Missing card Worker',
);
const entry = references.find((name) => name.endsWith('.js'));
assert(entry, 'Missing application script');
const code = readFileSync(resolve(root, entry), 'utf8');
assert(
  !code.includes('server.html') && !code.includes('Серверное распознавание'),
  'Server UI leaked into Pages',
);
assert(!/new URL\(["']\/ocr/.test(code), 'OCR assets point outside the project path');
for (const name of [
  'worker.min.js',
  'eng.traineddata.gz',
  ...['lstm', 'simd-lstm', 'relaxedsimd-lstm'].flatMap((v) => [
    `tesseract-core-${v}.wasm`,
    `tesseract-core-${v}.wasm.js`,
  ]),
])
  assert(statSync(resolve(root, 'ocr', name)).size > 0, `Missing OCR asset: ${name}`);
const walk = (directory) =>
  readdirSync(directory, { withFileTypes: true }).flatMap((item) => {
    const path = resolve(directory, item.name);
    assert(!item.isSymbolicLink(), `Unexpected symlink: ${path}`);
    return item.isDirectory() ? walk(path) : [path];
  });
const files = walk(root);
for (const file of files) {
  const name = relative(root, file);
  assert(
    name === 'index.html' || /^(assets|ocr)\//.test(name),
    `Unexpected published file: ${name}`,
  );
  assert(
    !/\.(mov|mp4|png|jpe?g|json)$/i.test(name),
    `Private data or source file in Pages: ${name}`,
  );
}
console.log(
  `Pages artifact passed: ${files.length} files, relative assets, two Workers, local OCR, no private inputs`,
);
