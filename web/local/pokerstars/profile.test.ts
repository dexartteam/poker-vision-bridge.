import { describe, it, expect } from 'vitest';
import { openPokerProfile, validateLocalProfile, regionNames, type CardRead } from '../contracts';
import { pokerStarsProfile } from './profile';
import { readPokerStarsFields } from './fields';
import { activeButton, actionCaption, betVisibility } from './preprocess';
import { readSymbolSet, classifySymbolCard, type SymbolSet } from './symbols';

const empty = (): CardRead => ({ status: 'empty', value: null });
const card = (value: string): CardRead => ({ status: 'visible', value });
const unknown = (): CardRead => ({ status: 'unreadable', value: null });
const ocr = (text: string, confidence = 96) => ({ text, confidence });
const board = () => Array.from({ length: 5 }, empty);
const paint = (rgb: number[]) => ({
  width: 100,
  height: 60,
  data: new Uint8ClampedArray(Array.from({ length: 6000 }, () => [...rgb, 255]).flat()),
});

describe('PokerStars partial observations', () => {
  it('keeps profile versions explicit, complete and isolated', () => {
    expect(regionNames(validateLocalProfile(openPokerProfile()))).toHaveLength(5);
    expect(regionNames(validateLocalProfile(pokerStarsProfile()))).toHaveLength(23);
    const p = pokerStarsProfile();
    delete p.regions.hero;
    expect(() => validateLocalProfile(p)).toThrow();
    expect(() =>
      validateLocalProfile({ ...pokerStarsProfile(), layout: 'another' } as never),
    ).toThrow();
    expect(() =>
      validateLocalProfile({ ...openPokerProfile(), layout: 'pokerstars-classic' }),
    ).toThrow();
  });
  it('ignores chat results, preserves unknown bets and never reuses a past hand', () => {
    const p = readPokerStarsFields(
      {
        pot: ocr('Pot: 350'),
        chat: ocr('Hand #2128574 won 22,444'),
        stack0: ocr('All-In'),
        bet1: ocr('oo'),
        stack1: ocr('7,680'),
      },
      board(),
      [card('5c'), card('Qh')],
      { 0: 'empty', 1: 'visible' },
    );
    expect(p.hand_number).toBeNull();
    expect(p.pot_display.value).toBe(350);
    expect(p.seats[0]).toMatchObject({
      stack: null,
      bet: null,
      status: 'all_in',
      chip_visibility: 'empty',
    });
    expect(p.seats[1]).toMatchObject({ stack: 7680, bet: null });
    expect(p.action_history).toEqual({ status: 'incomplete', events: [] });
  });
  it('requires visible contiguous cards for street and rejects duplicates', () => {
    const read = (b: CardRead[], h = [card('Th'), card('Jd')]) =>
      readPokerStarsFields({ pot: ocr('Pot: 500') }, b, h, {});
    expect(read(Array.from({ length: 5 }, unknown)).ui_street).toBe('unknown');
    expect(read([card('Ks'), unknown(), card('4c'), empty(), empty()]).ui_street).toBe('unknown');
    expect(read([card('Ks'), card('Ac'), card('4c'), empty(), empty()]).ui_street).toBe('flop');
    const duplicate = read([card('Th'), card('Ac'), card('4c'), empty(), empty()]);
    expect(duplicate.hero_cards.every((c) => c.value === null)).toBe(true);
    expect(duplicate.board.every((c) => c.value === null)).toBe(true);
    expect(duplicate.ui_street).toBe('unknown');
  });
  it('distinguishes nicknames from blue action captions', () => {
    const text = { name0: ocr('Fold'), name1: ocr('Raise'), name2: ocr('WrongName', 94) };
    const p = readPokerStarsFields(text, board(), [], {}, [], { 1: true });
    expect(p.seats[0]).toMatchObject({ name: 'Fold', action_label: null, status: 'unknown' });
    expect(p.seats[1]).toMatchObject({ name: null, action_label: 'raise' });
    expect(p.seats[2].name).toBeNull();
    expect(actionCaption(paint([210, 210, 210]))).toBe(false);
    expect(actionCaption(paint([80, 150, 210]))).toBe(true);
  });
  it('requires bright active buttons and valid commands, including the two-button check/bet case', () => {
    const text = {
      control0: ocr('Fold'),
      control1: ocr('Call\n672'),
      control2: ocr('Raise To\n1,244'),
    };
    const read = (t = text, active = [true, true, true]) =>
      readPokerStarsFields(t, board(), [], {}, active);
    expect(read().hero_turn).toBe(true);
    expect(read(text, [false, false, false]).hero_turn).toBeNull();
    expect(read({ ...text, control2: ocr('Raise To 1,24') }).hero_turn).toBeNull();
    const two = read({ ...text, control1: ocr('Check'), control2: ocr('Bet 100') }, [
      false,
      true,
      true,
    ]);
    expect(two.available_actions).toEqual(['check', 'bet']);
    expect(two.hero_turn).toBe(true);
    expect(activeButton(paint([100, 20, 20]))).toBe(false);
    expect(activeButton(paint([205, 30, 30]))).toBe(true);
  });
  it('does not invent zero when a bet crop has no text', () => {
    expect(betVisibility(paint([20, 40, 20]))).toBe('empty');
    expect(betVisibility(paint([210, 210, 210]))).toBe('visible');
  });
});

describe('symbol pack validation', () => {
  const pack = (): SymbolSet => ({
    schema_version: 'local-vision.symbols.v1',
    profile: 'pokerstars-mobile-landscape-corner-v1',
    ranks: [...'23456789TJQKA'].map((value) => ({
      value,
      color: 'black',
      feature: Array(560).fill(0.5),
    })),
    suits: [...'cdhs'].map((value) => ({
      value,
      color: 'dh'.includes(value) ? 'red' : 'black',
      feature: Array(400).fill(0.5),
    })),
  });
  it('rejects missing classes, numeric labels, incompatible colors and invalid features', () => {
    expect(readSymbolSet(pack()).ranks).toHaveLength(13);
    const incomplete = pack();
    incomplete.ranks.pop();
    expect(() => readSymbolSet(incomplete)).toThrow();
    const numeric = pack();
    numeric.ranks[0].value = 2 as never;
    expect(() => readSymbolSet(numeric)).toThrow();
    const color = pack();
    color.suits[0].color = 'red';
    expect(() => readSymbolSet(color)).toThrow();
    const invalid = pack();
    invalid.ranks[0].feature[0] = NaN;
    expect(() => readSymbolSet(invalid)).toThrow();
  });
  it('abstains without a pack and rejects tied matches or blank paper', () => {
    const box = { x: 0, y: 0, w: 67, h: 60 };
    expect(classifySymbolCard(paint([230, 230, 230]), box, null).status).toBe('unreadable');
    expect(classifySymbolCard(paint([230, 230, 230]), box, pack()).value).toBeNull();
    expect(classifySymbolCard(paint([20, 50, 20]), box, null).status).toBe('empty');
  });
});
