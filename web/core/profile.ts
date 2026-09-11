export type Region = { name: string; x: number; y: number; w: number; h: number; ignore: boolean };
export type Profile = {
  calibration_id: string;
  width: number;
  height: number;
  unit: 'chips' | 'USD' | 'EUR' | 'BB';
  scale: number;
  hero_seat: number;
  regions: Region[];
};
export const defaultRegions = (): Region[] => [
  { name: 'board', x: 0.3, y: 0.36, w: 0.4, h: 0.2, ignore: false },
  { name: 'pot', x: 0.4, y: 0.23, w: 0.2, h: 0.12, ignore: false },
  { name: 'hero_cards', x: 0.39, y: 0.68, w: 0.22, h: 0.16, ignore: false },
  { name: 'controls', x: 0.62, y: 0.84, w: 0.37, h: 0.15, ignore: false },
  ...[
    [0.39, 0.83],
    [0.04, 0.59],
    [0.06, 0.11],
    [0.39, 0.03],
    [0.75, 0.11],
    [0.77, 0.59],
  ].map(([x, y], i) => ({ name: `seat_${i}`, x, y, w: 0.2, h: 0.13, ignore: false })),
];
export function validateProfile(p: Profile): Profile {
  if (
    !p ||
    typeof p.calibration_id !== 'string' ||
    p.calibration_id.length < 8 ||
    !Number.isInteger(p.width) ||
    !Number.isInteger(p.height) ||
    p.width < 64 ||
    p.width > 4096 ||
    p.height < 64 ||
    p.height > 4096 ||
    !['chips', 'USD', 'EUR', 'BB'].includes(p.unit) ||
    !Number.isInteger(p.scale) ||
    p.scale < 0 ||
    p.scale > 6 ||
    !Number.isInteger(p.hero_seat) ||
    p.hero_seat < 0 ||
    p.hero_seat > 5 ||
    !Array.isArray(p.regions) ||
    !p.regions.length ||
    p.regions.length > 40 ||
    !p.regions.some((r) => !r.ignore)
  )
    throw new Error('Некорректный профиль');
  const names = new Set<string>();
  for (const r of p.regions) {
    if (
      !/^[a-zA-Z0-9_-]{1,64}$/.test(r.name) ||
      names.has(r.name) ||
      typeof r.ignore !== 'boolean' ||
      ![r.x, r.y, r.w, r.h].every(Number.isFinite) ||
      r.x < 0 ||
      r.y < 0 ||
      r.w <= 0 ||
      r.h <= 0 ||
      r.x + r.w > 1.000001 ||
      r.y + r.h > 1.000001
    )
      throw new Error('Области должны находиться внутри кадра и иметь уникальные имена');
    names.add(r.name);
  }
  return structuredClone(p);
}
