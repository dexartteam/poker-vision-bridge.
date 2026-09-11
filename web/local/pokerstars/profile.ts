import type { LocalProfile, Rect } from '../contracts';

export const SEATS = [0, 1, 2, 3, 4, 5] as const;
export const pokerStarsRegionNames = [
  'pot',
  'board',
  'hero',
  'controls',
  'chat',
  ...SEATS.flatMap((seat) => [`name${seat}`, `stack${seat}`, `bet${seat}`]),
] as const;
export const pokerStarsLabels: Record<string, string> = {
  pot: 'Общий банк',
  board: 'Общие карты',
  hero: 'Свои карты',
  controls: 'Кнопки действий',
  chat: 'Чат — не читается',
  ...Object.fromEntries(
    SEATS.flatMap((seat) => [
      [`name${seat}`, `Место ${seat + 1} — имя / подпись действия`],
      [`stack${seat}`, `Место ${seat + 1} — стек`],
      [`bet${seat}`, `Место ${seat + 1} — фишки у места`],
    ]),
  ),
};
/** Fixed six-seat layout; confirmation is required even at the reference resolution. */
export function pokerStarsProfile(width = 1280, height = 640): LocalProfile {
  const r = (x: number, y: number, w: number, h: number): Rect => ({
    x: x / 1280,
    y: y / 640,
    w: w / 1280,
    h: h / 640,
  });
  return {
    schema_version: 'local-vision.profile.v2',
    layout: 'pokerstars-classic',
    id: crypto.randomUUID(),
    width,
    height,
    regions: {
      pot: r(578, 242, 128, 25),
      board: r(462, 266, 360, 100),
      hero: r(662, 440, 106, 108),
      controls: r(0, 547, 640, 93),
      chat: r(564, 550, 598, 84),
      name0: r(604, 132, 136, 29),
      stack0: r(613, 163, 122, 28),
      name1: r(990, 219, 116, 29),
      stack1: r(998, 251, 108, 28),
      name2: r(988, 366, 119, 29),
      stack2: r(994, 399, 113, 28),
      name3: r(541, 475, 128, 29),
      stack3: r(550, 508, 117, 28),
      name4: r(173, 366, 131, 29),
      stack4: r(183, 399, 119, 28),
      name5: r(174, 219, 129, 29),
      stack5: r(184, 251, 114, 28),
      bet0: r(779, 177, 88, 23),
      bet1: r(856, 226, 79, 25),
      bet2: r(856, 407, 79, 26),
      bet3: r(447, 420, 78, 26),
      bet4: r(345, 407, 88, 26),
      bet5: r(345, 226, 88, 25),
    },
  };
}
