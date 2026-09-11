import { it, expect } from 'vitest';
import { readTemplateSet } from './cards';
const card = () => ({
  value: 'Ah',
  color: 'red',
  rank: Array(400).fill(0.5),
  suit: Array(400).fill(0.25),
});
const set = () => ({ schema_version: 'local-vision.cards.v1', templates: [card()] });
it('owns a copy of imported card features', () => {
  const input = set(),
    result = readTemplateSet(input);
  input.templates[0].rank[0] = 1;
  expect(result[0].rank[0]).toBe(0.5);
});
it('rejects unknown versions, duplicate cards and inconsistent suit colors', () => {
  expect(() => readTemplateSet({ ...set(), schema_version: 'future' })).toThrow();
  expect(() => readTemplateSet({ ...set(), templates: [card(), card()] })).toThrow();
  expect(() => readTemplateSet({ ...set(), templates: [{ ...card(), color: 'black' }] })).toThrow();
  expect(() => readTemplateSet({ ...set(), templates: [] })).toThrow();
});
it('rejects malformed or empty feature vectors before a Worker sees them', () => {
  for (const rank of [
    Array(399).fill(0.5),
    Array(400).fill(0),
    Array(400).fill(NaN),
    Array(400).fill(1.1),
    ['0.5'],
  ])
    expect(() => readTemplateSet({ ...set(), templates: [{ ...card(), rank }] })).toThrow();
});
