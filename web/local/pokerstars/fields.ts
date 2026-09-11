import type { CardRead, OCR, PokerStarsObservation, SeatRead, VisibleAction } from '../contracts';
import { chips, good } from '../fields';
import { SEATS } from './profile';

const ACTIONS: Record<string, VisibleAction> = {
  FOLD: 'fold',
  CHECK: 'check',
  CALL: 'call',
  BET: 'bet',
  RAISE: 'raise',
  'ALL-IN': 'all_in',
  'ALL IN': 'all_in',
  'POST SB': 'post_sb',
  'POST BB': 'post_bb',
};
export const visibleAction = (ocr?: OCR): VisibleAction | null =>
  good(ocr, 85) ? (ACTIONS[ocr!.text.trim().toUpperCase().replace(/\s+/g, ' ')] ?? null) : null;

type Readout = Pick<
  PokerStarsObservation,
  | 'ui_mode'
  | 'hand_number'
  | 'ui_street'
  | 'pot_display'
  | 'board'
  | 'hero_cards'
  | 'hero_turn'
  | 'seats'
  | 'dealer_seat'
  | 'available_actions'
  | 'action_history'
>;
/** Parse only this frame. Chat summaries and pre-action checkboxes are not current actions. */
export function readPokerStarsFields(
  ocr: Record<string, OCR>,
  board: CardRead[],
  hero: CardRead[],
  bets: Record<number, SeatRead['chip_visibility']>,
  buttons: boolean[] = [],
  captions: Record<number, boolean> = {},
): Readout {
  const potText = good(ocr.pot, 85) ? /^Pot:\s*((?:\d|,)+)$/i.exec(ocr.pot.text.trim()) : null;
  const pot = potText ? chips(potText[1]) : null;
  const allCards = [...board, ...hero].filter((c) => c.status === 'visible').map((c) => c.value);
  if (new Set(allCards).size !== allCards.length) {
    board = board.map((c) => (c.status === 'visible' ? { status: 'unreadable', value: null } : c));
    hero = hero.map((c) => (c.status === 'visible' ? { status: 'unreadable', value: null } : c));
  }
  const visible = board.filter((c) => c.status === 'visible').length;
  const contiguous = board.every((c, i) =>
    i < visible ? c.status === 'visible' : c.status === 'empty',
  );
  const street =
    contiguous && board.length === 5 && pot !== null
      ? (({ 3: 'flop', 4: 'turn', 5: 'river' } as const)[visible as 3 | 4 | 5] ??
        (visible === 0 && hero.some((c) => c.status === 'visible') ? 'preflop' : 'unknown'))
      : 'unknown';
  const seats: SeatRead[] = SEATS.map((seat) => {
    const name = ocr[`name${seat}`],
      stack = ocr[`stack${seat}`],
      bet = ocr[`bet${seat}`];
    const action = captions[seat] ? visibleAction(name) : null,
      allIn = visibleAction(stack) === 'all_in';
    return {
      seat_index: seat,
      is_hero: seat === 3,
      // Hero's name is covered by cards; uncertain OCR nicknames never become identities.
      name:
        seat !== 3 &&
        !captions[seat] &&
        good(name, 95) &&
        /^[A-Za-z0-9_]{2,24}$/.test(name.text.trim())
          ? name.text.trim()
          : null,
      stack: !allIn && good(stack, 85) ? chips(stack.text) : null,
      bet: null,
      chip_display: bets[seat] === 'visible' && good(bet, 85) ? chips(bet.text) : null,
      chip_visibility: bets[seat] ?? 'unknown',
      action_label: action,
      status: allIn || action === 'all_in' ? 'all_in' : action === 'fold' ? 'folded' : 'unknown',
    };
  });
  const commands = buttons.map((active, i): VisibleAction | null => {
    const text = ocr[`control${i}`];
    if (!active || !good(text, 85)) return null;
    const normalized = text.text.trim().replace(/\s+/g, ' ');
    if (i === 0 && /^Fold$/i.test(normalized)) return 'fold';
    if (i === 1 && /^Check$/i.test(normalized)) return 'check';
    const m = /^(Call|Bet|Raise To) ([\d,]+)$/i.exec(normalized);
    if (!m || chips(m[2]) === null) return null;
    if (i === 1 && m[1].toLowerCase() === 'call') return 'call';
    if (i === 2 && m[1].toLowerCase() === 'bet') return 'bet';
    if (i === 2 && m[1].toLowerCase() === 'raise to') return 'raise';
    return null;
  });
  const available = commands.filter((a): a is VisibleAction => a !== null);
  const heroTurn = commands[1] && commands[2] ? true : null;
  return {
    ui_mode: 'unknown',
    hand_number: null,
    ui_street: street,
    pot_display: { value: pot, raw: ocr.pot?.text ?? null, unit: 'chips' },
    board,
    hero_cards: hero,
    seats,
    hero_turn: heroTurn,
    dealer_seat: null,
    available_actions: heroTurn ? available : [],
    action_history: { status: 'incomplete', events: [] },
  };
}
