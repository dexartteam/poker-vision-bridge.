import { describe, it, expect } from 'vitest';
import { chips, readFields } from './fields';
import { openPokerProfile, validateLocalProfile } from './contracts';
const ocr = (overrides = {}) => ({
  header: { text: '#4812 TURN', confidence: 90 },
  mode: { text: 'LIVE', confidence: 90 },
  pot: { text: 'POT', confidence: 90 },
  amount: { text: '1,288', confidence: 96 },
  ...overrides,
});
describe('local field interpretation', () => {
  it('retains exact integers including zero and rejects speculative repairs', () => {
    expect(chips('0')).toBe(0);
    expect(chips('1,288')).toBe(1288);
    for (const text of ['1,28', '1.288', '1.2K', '-1', '|,288', '1288x', '', '9007199254740992'])
      expect(chips(text)).toBeNull();
  });
  it('blocks replay, unknown modes and payouts independently', () => {
    for (const mode of ['REPLAY', 'LIVE REPLAY', 'LIVELY', ''])
      expect(
        readFields(ocr({ mode: { text: mode, confidence: 96 } }), []).pot_display.value,
      ).toBeNull();
    for (const text of ['AWARDED', 'JACKPOT', 'POT AWARDED'])
      expect(readFields(ocr({ pot: { text, confidence: 96 } }), []).pot_display.value).toBeNull();
  });
  it('uses finite confidence thresholds, never treats score as a probability', () => {
    for (const confidence of [NaN, Infinity, -1, 101, 84])
      expect(
        readFields(ocr({ amount: { text: '1288', confidence } }), []).pot_display.value,
      ).toBeNull();
    expect(
      readFields(ocr({ amount: { text: '1288', confidence: 85 } }), []).pot_display.value,
    ).toBe(1288);
  });
  it('does not shift remaining cards into missing or occluded slots', () => {
    const card = {
      status: 'visible' as const,
      value: 'Qd',
      score: 1,
      margin: 1,
      box: { x: 1, y: 1, w: 50, h: 70 },
    };
    expect(readFields(ocr(), [card]).board.every((c) => c.status === 'unreadable')).toBe(true);
  });
  it('keeps showdown separate, requires consistent card count and rejects duplicate cards', () => {
    expect(
      readFields(ocr({ header: { text: '#4812 SHOWDOWN', confidence: 90 } }), []).ui_street,
    ).toBe('showdown');
    const card = {
      status: 'visible' as const,
      value: 'Qd',
      score: 1,
      margin: 1,
      box: { x: 1, y: 1, w: 50, h: 70 },
    };
    expect(readFields(ocr(), [card, card, card, card]).board.every((c) => c.value === null)).toBe(
      true,
    );
  });
  it('clones calibration and rejects out-of-frame or malformed profiles', () => {
    const original = openPokerProfile();
    const copy = validateLocalProfile(original);
    original.regions.pot.x = 0;
    expect(copy.regions.pot.x).not.toBe(0);
    expect(() => validateLocalProfile({ ...copy, width: 1 })).toThrow();
    expect(() =>
      validateLocalProfile({ ...copy, regions: { ...copy.regions, extra: null } } as any),
    ).toThrow();
    expect(() =>
      validateLocalProfile({
        ...copy,
        regions: { ...copy.regions, pot: { x: 0.9, y: 0, w: 0.2, h: 0.1 } },
      }),
    ).toThrow();
  });
});
