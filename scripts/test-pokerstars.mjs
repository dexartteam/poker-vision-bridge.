import { existsSync, readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
const root = 'tests/pokerstars-fixtures/';
if (!existsSync(root + 'frames.json') || !existsSync(root + 'pokerstars-symbols.json')) {
  console.error(
    'Unpack pokerstars-training-kit.zip into the repository first. See tests/pokerstars-fixtures/README.md.',
  );
  process.exit(1);
}
const frames = JSON.parse(readFileSync(root + 'frames.json', 'utf8'));
if (frames.some((frame) => !existsSync(root + frame.file))) {
  console.error('Incomplete PokerStars fixture archive.');
  process.exit(1);
}
const result = spawnSync(
  process.execPath,
  ['node_modules/vitest/vitest.mjs', 'run', 'web/local/pokerstars/fixtures.test.ts'],
  { stdio: 'inherit' },
);
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
