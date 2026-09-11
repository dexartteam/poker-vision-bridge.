import { readFileSync, writeFileSync } from 'node:fs';
import { inflateSync } from 'node:zlib';
import { format, resolveConfig } from 'prettier';
import { feature, locateCards } from '../web/local/cards';
const sources = JSON.parse(readFileSync('tests/vision-fixtures/cards.json', 'utf8'));
const templates = sources
  .filter((s: any) => s.source === 'frame-50s')
  .map((s: any) => {
    const p = {
      width: s.width,
      height: s.height,
      data: new Uint8ClampedArray(inflateSync(Buffer.from(s.rgba, 'base64'))),
    };
    const boxes = locateCards(p);
    if (boxes.length !== 1)
      throw new Error(`Expected one complete card in ${s.value}: ${JSON.stringify(boxes)}`);
    return { value: s.value, ...feature(p, boxes[0]) };
  });
writeFileSync(
  'tests/vision-fixtures/templates.json',
  await format(JSON.stringify({ schema_version: 'local-vision.cards.v1', templates }), {
    ...(await resolveConfig('tests/vision-fixtures/templates.json')),
    parser: 'json',
  }),
);
console.log(`Prepared ${templates.length} source-derived card templates`);
