import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
const files = [
  'tests/vision-fixtures/frames.json',
  'tests/vision-fixtures/cards.json',
  'tests/vision-fixtures/templates.json',
];
if (files.some((file) => !existsSync(file))) {
  console.error(
    'Unpack table-vision-recording-fixtures.zip into the repository first. See tests/vision-fixtures/README.md.',
  );
  process.exit(1);
}
const result = spawnSync(
  process.execPath,
  ['node_modules/vitest/vitest.mjs', 'run', 'web/local/fixtures.test.ts'],
  { stdio: 'inherit' },
);
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
