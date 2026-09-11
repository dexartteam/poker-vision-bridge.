import type { Pixels } from '../core/detector';
export type Rect = { x: number; y: number; w: number; h: number };
export const REGION_NAMES = ['header', 'mode', 'pot', 'amount', 'board'] as const;
export type RegionName = (typeof REGION_NAMES)[number];
export type LocalProfile = {
  schema_version: 'local-vision.profile.v1';
  id: string;
  width: number;
  height: number;
  regions: Record<RegionName, Rect>;
};
export type Source = {
  kind: 'camera' | 'recording';
  epoch: string;
  calibration_id: string;
  frame_seq: number;
  revision: number;
  captured_at: number;
  media_time: number | null;
};
export type Frame = { source: Source; stable: boolean; pixels: Pixels };
export type OCR = { text: string; confidence: number };
export type CardRead = { status: 'visible' | 'empty' | 'unreadable'; value: string | null };
export type CardMatch = CardRead & { box: Rect; score: number; margin: number };
export type Fields = {
  ui_mode: 'live' | 'replay' | 'unknown';
  hand_number: number | null;
  ui_street: 'preflop' | 'flop' | 'turn' | 'river' | 'showdown' | 'unknown';
  pot_display: { value: number | null; raw: string | null; unit: 'chips' };
  board: CardRead[];
};
export type LocalObservation = Fields & {
  schema_version: 'local-vision.observation.v1';
  source: Source;
  status: 'partial' | 'stale';
  evidence: 'single_frame' | 'repeated' | 'moving';
  stable: boolean;
  decision_ready: false;
  hero_cards: null;
  hero_turn: null;
  seats: null;
  pot_includes_current_bets: null;
  diagnostics: { ocr: Record<string, OCR>; cards: CardMatch[]; elapsed_ms: number };
};
export function validateLocalProfile(value: LocalProfile): LocalProfile {
  if (
    !value ||
    value.schema_version !== 'local-vision.profile.v1' ||
    typeof value.id !== 'string' ||
    value.id.length < 8 ||
    !Number.isInteger(value.width) ||
    !Number.isInteger(value.height) ||
    value.width < 64 ||
    value.height < 64 ||
    value.width > 4096 ||
    value.height > 4096
  )
    throw new Error('Некорректный профиль локального распознавания');
  if (
    !value.regions ||
    Object.keys(value.regions).some((name) => !REGION_NAMES.includes(name as RegionName))
  )
    throw new Error('Неизвестные области профиля');
  for (const name of REGION_NAMES) {
    const r = value.regions?.[name];
    if (
      !r ||
      ![r.x, r.y, r.w, r.h].every(Number.isFinite) ||
      r.x < 0 ||
      r.y < 0 ||
      r.w <= 0 ||
      r.h <= 0 ||
      r.x + r.w > 1.000001 ||
      r.y + r.h > 1.000001
    )
      throw new Error(`Некорректная область: ${name}`);
  }
  return structuredClone(value);
}
export function openPokerProfile(width = 1206, height = 2622): LocalProfile {
  const rect = (x: number, y: number, w: number, h: number): Rect => ({
    x: x / 1206,
    y: y / 2622,
    w: w / 1206,
    h: h / 2622,
  });
  return {
    schema_version: 'local-vision.profile.v1',
    id: crypto.randomUUID(),
    width,
    height,
    regions: {
      header: rect(765, 520, 380, 65),
      mode: rect(60, 725, 250, 70),
      pot: rect(470, 1300, 350, 300),
      amount: rect(550, 1400, 185, 75),
      board: rect(330, 1160, 560, 160),
    },
  };
}
export function pixelRect(r: Rect, width: number, height: number) {
  const x = Math.round(r.x * width),
    y = Math.round(r.y * height);
  return {
    x,
    y,
    w: Math.max(1, Math.min(width - x, Math.round(r.w * width))),
    h: Math.max(1, Math.min(height - y, Math.round(r.h * height))),
  };
}
