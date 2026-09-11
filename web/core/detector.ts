import type { Region } from './profile';
export type Pixels = { data: Uint8ClampedArray; width: number; height: number };
export type Reason =
  | 'initial'
  | 'significant_change'
  | 'confirmation'
  | 'safety_poll'
  | 'retry'
  | 'manual'
  | 'hero_turn';
export type Detection = {
  revision: number;
  changed: boolean;
  changedRegions: string[];
  motion: boolean;
  stable: boolean;
  candidate: Reason | null;
  score: number;
  elapsedMs: number;
};
export const DEFAULTS = {
  pixelDelta: 24,
  changedRatio: 0.015,
  tileRatio: 0.15,
  settleMs: 400,
  maxWaitMs: 1000,
  safetyPollMs: 15000,
};
type Config = typeof DEFAULTS;
const copy = (p: Pixels): Pixels => ({ ...p, data: p.data.slice() });

/** Compare RGB per region and local 8×8 tile. Timers may be masked explicitly. */
export function difference(a: Pixels, b: Pixels, regions: Region[], config: Config = DEFAULTS) {
  if (a.width !== b.width || a.height !== b.height || a.data.length !== b.data.length)
    throw new Error('frame_dimensions_changed');
  const changedRegions: string[] = [];
  let score = 0;
  const masks = regions.filter((r) => r.ignore);
  for (const r of regions.filter((r) => !r.ignore)) {
    let total = 0,
      changed = 0;
    const tiles = new Map<number, [number, number]>();
    for (
      let y = Math.floor(r.y * a.height);
      y < Math.min(a.height, Math.ceil((r.y + r.h) * a.height));
      y++
    ) {
      for (
        let x = Math.floor(r.x * a.width);
        x < Math.min(a.width, Math.ceil((r.x + r.w) * a.width));
        x++
      ) {
        if (
          masks.some(
            (m) =>
              x / a.width >= m.x &&
              x / a.width < m.x + m.w &&
              y / a.height >= m.y &&
              y / a.height < m.y + m.h,
          )
        )
          continue;
        const i = (y * a.width + x) * 4;
        const delta = Math.max(
          Math.abs(a.data[i] - b.data[i]),
          Math.abs(a.data[i + 1] - b.data[i + 1]),
          Math.abs(a.data[i + 2] - b.data[i + 2]),
        );
        const hit = delta >= config.pixelDelta ? 1 : 0;
        total++;
        changed += hit;
        const key = Math.floor(y / 8) * Math.ceil(a.width / 8) + Math.floor(x / 8);
        const tile = tiles.get(key) ?? [0, 0];
        tile[0] += hit;
        tile[1]++;
        tiles.set(key, tile);
      }
    }
    const ratio = total ? changed / total : 0;
    const tileHit = [...tiles.values()].some(([n, d]) => n >= 4 && n / d >= config.tileRatio);
    score = Math.max(score, ratio);
    if (ratio >= config.changedRatio || tileHit) changedRegions.push(r.name);
  }
  return { changed: changedRegions.length > 0, changedRegions, score };
}

export class Detector {
  private previous: Pixels | null = null;
  private anchor: Pixels | null = null;
  private baseline: Pixels | null = null;
  private inflight: { pixels: Pixels; revision: number } | null = null;
  private lastMotion = 0;
  private dirtySince: number | null = null;
  private lastOffer = -Infinity;
  private offeredRevision = -1;
  private awaitingStable = false;
  revision = 0;
  constructor(
    private regions: Region[],
    private config: Config = DEFAULTS,
  ) {
    if (
      config.pixelDelta <= 0 ||
      config.changedRatio <= 0 ||
      config.settleMs < 0 ||
      config.maxWaitMs < config.settleMs ||
      config.safetyPollMs < 1000
    )
      throw new Error('invalid_detector_config');
  }
  accept(revision: number, pixels: Pixels): boolean {
    if (revision !== this.revision) return false;
    this.baseline = copy(pixels);
    this.dirtySince = null;
    this.inflight = null;
    this.awaitingStable = false;
    return true;
  }
  release(revision: number) {
    if (this.inflight?.revision === revision) this.inflight = null;
  }
  step(pixels: Pixels, now: number, force: Reason | null = null): Detection {
    const start = performance.now();
    if (pixels.data.length !== pixels.width * pixels.height * 4)
      throw new Error('invalid_pixel_buffer');
    let changed = false,
      changedRegions: string[] = [],
      motion = false,
      score = 0;
    if (!this.previous) {
      this.lastMotion = now;
      this.dirtySince = now;
      this.anchor = copy(pixels);
    } else {
      motion = difference(this.previous, pixels, this.regions, this.config).changed;
      const delta = difference(this.anchor!, pixels, this.regions, this.config);
      ({ changed, changedRegions, score } = delta);
      if (changed) {
        this.revision++;
        this.anchor = copy(pixels);
        if (this.dirtySince === null) this.dirtySince = now;
      }
      if (motion) this.lastMotion = now;
    }
    this.previous = copy(pixels);
    const stable = now - this.lastMotion >= this.config.settleMs;
    const maxWait = this.dirtySince !== null && now - this.dirtySince >= this.config.maxWaitMs;
    const differs =
      !this.baseline || difference(this.baseline, pixels, this.regions, this.config).changed;
    const duplicate =
      this.inflight &&
      this.inflight.revision === this.revision &&
      !difference(this.inflight.pixels, pixels, this.regions, this.config).changed;
    let candidate: Reason | null = null;
    if (stable || maxWait) {
      if (force) candidate = force;
      else if (stable && this.awaitingStable) candidate = 'significant_change';
      else if (
        !duplicate &&
        this.offeredRevision !== this.revision &&
        (differs || this.dirtySince !== null)
      )
        candidate = this.baseline ? 'significant_change' : 'initial';
      else if (!duplicate && now - this.lastOffer >= this.config.safetyPollMs)
        candidate = 'safety_poll';
    }
    if (candidate) {
      this.lastOffer = now;
      this.offeredRevision = this.revision;
      if (!stable) this.dirtySince = now;
      this.awaitingStable = !stable;
      this.inflight = { pixels: copy(pixels), revision: this.revision };
    }
    return {
      revision: this.revision,
      changed,
      changedRegions,
      motion,
      stable,
      candidate,
      score,
      elapsedMs: performance.now() - start,
    };
  }
}
