import type { CardMatch, Fields, OCR } from './contracts';
export const good = (r: OCR | undefined, threshold: number) =>
  r &&
  typeof r.text === 'string' &&
  Number.isFinite(r.confidence) &&
  r.confidence >= threshold &&
  r.confidence <= 100;
export function chips(text: string): number | null {
  if (!/^(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)$/.test(text.trim())) return null;
  const n = Number(text.trim().replaceAll(',', ''));
  return Number.isSafeInteger(n) ? n : null;
}
export function readFields(ocr: Record<string, OCR>, cards: CardMatch[]): Fields {
  const modes = good(ocr.mode, 60)
    ? [...ocr.mode.text.toUpperCase().matchAll(/\b(LIVE|REPLAY)\b/g)]
    : [];
  const mode = modes.length === 1 ? (modes[0][1].toLowerCase() as 'live' | 'replay') : 'unknown';
  const headers = good(ocr.header, 60)
    ? [
        ...ocr.header.text
          .toUpperCase()
          .matchAll(/#([1-9]\d*)\s+(PRE-FLOP|FLOP|TURN|RIVER|SHOWDOWN)\b/g),
      ]
    : [];
  const hand =
    headers.length === 1 && Number.isSafeInteger(Number(headers[0][1]))
      ? Number(headers[0][1])
      : null;
  const street =
    hand !== null
      ? (headers[0][2].toLowerCase().replace('-', '') as Fields['ui_street'])
      : 'unknown';
  const potLabel =
    good(ocr.pot, 40) &&
    /\bPOT\b/.test(ocr.pot.text.toUpperCase()) &&
    !/\bAWARDED\b/.test(ocr.pot.text.toUpperCase());
  const amount =
    mode === 'live' && potLabel && good(ocr.amount, 85) ? chips(ocr.amount.text) : null;
  const board: Fields['board'] = Array.from({ length: 5 }, () => ({
    status: 'unreadable',
    value: null,
  }));
  const count = { preflop: 0, flop: 3, turn: 4, river: 5, showdown: -1, unknown: -1 }[street];
  // Do not shift slots when an occluded/animating card is missing.
  if (mode === 'live' && count >= 0 && cards.length === count) {
    const values = cards.map((c) => c.value).filter((v) => v !== null);
    if (new Set(values).size === values.length) {
      for (let i = 0; i < 5; i++)
        board[i] =
          i < cards.length
            ? { status: cards[i].status, value: cards[i].value }
            : { status: 'empty', value: null };
    }
  }
  return {
    ui_mode: mode,
    hand_number: hand,
    ui_street: street,
    pot_display: { value: amount, raw: ocr.amount?.text ?? null, unit: 'chips' },
    board,
  };
}
