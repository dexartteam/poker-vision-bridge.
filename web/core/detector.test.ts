import { describe, expect, it } from 'vitest';
import { DEFAULTS, Detector, difference, type Pixels } from './detector';
import { defaultRegions, validateProfile, type Region } from './profile';
const regions: Region[] = [{ name: 'pot', x: 0, y: 0, w: 1, h: 1, ignore: false }];
function pixels(value = 60): Pixels {
  const data = new Uint8ClampedArray(32 * 32 * 4);
  for (let i = 0; i < data.length; i += 4) {
    data[i] = data[i + 1] = data[i + 2] = value;
    data[i + 3] = 255;
  }
  return { data, width: 32, height: 32 };
}
function digit(p = pixels(), value = 220) {
  for (let y = 8; y < 12; y++)
    for (let x = 8; x < 11; x++) {
      const i = (y * 32 + x) * 4;
      p.data[i] = p.data[i + 1] = p.data[i + 2] = value;
    }
  return p;
}
function ready() {
  const d = new Detector(regions);
  d.step(pixels(), 0);
  expect(d.step(pixels(), 400).candidate).toBe('initial');
  d.accept(0, pixels());
  return d;
}

describe('detector', () => {
  it('initial waits for 400 ms of stability', () => {
    const d = new Detector(regions);
    expect(d.step(pixels(), 0).candidate).toBeNull();
    expect(d.step(pixels(), 399).candidate).toBeNull();
    expect(d.step(pixels(), 400).candidate).toBe('initial');
  });
  it('camera noise does not create transitions', () => {
    const d = ready();
    for (let i = 1; i <= 100; i++) {
      const r = d.step(pixels(58 + (i % 5)), 400 + i * 200);
      expect(r.changed).toBe(false);
      expect(r.revision).toBe(0);
    }
  });
  it('detects a small digit using local tiles', () => {
    expect(difference(pixels(), digit(), regions).changedRegions).toEqual(['pot']);
  });
  it('detects color changes with similar luminance', () => {
    const a = pixels(),
      b = pixels();
    for (let y = 8; y < 12; y++)
      for (let x = 8; x < 11; x++) {
        const i = (y * 32 + x) * 4;
        a.data.set([200, 0, 0, 255], i);
        b.data.set([0, 59, 0, 255], i);
      }
    expect(difference(a, b, regions).changed).toBe(true);
  });
  it('ignores masked timer but detects adjacent controls', () => {
    const masked = [
      ...regions,
      { name: 'timer', x: 0.25, y: 0.25, w: 0.125, h: 0.125, ignore: true },
    ];
    expect(difference(pixels(), digit(), masked).changed).toBe(false);
    expect(difference(pixels(), pixels(100), masked).changed).toBe(true);
  });
  it('fully masked region has finite zero score', () => {
    const r = difference(pixels(), pixels(255), [
      ...regions,
      { ...regions[0], name: 'mask', ignore: true },
    ]);
    expect(r.score).toBe(0);
    expect(r.changed).toBe(false);
  });
  it('accumulated gradual changes use anchor not previous frame', () => {
    const d = ready();
    for (let n = 1; n < 5; n++)
      expect(d.step(digit(pixels(), 60 + n * 5), 400 + n * 200).changed).toBe(false);
    expect(d.step(digit(pixels(), 85), 1400).revision).toBe(1);
  });
  it('A to B to B creates one revision while baseline stays A', () => {
    const d = ready();
    expect(d.step(digit(), 600).revision).toBe(1);
    expect(d.step(digit(), 800).revision).toBe(1);
    const r = d.step(digit(), 1000);
    expect(r.revision).toBe(1);
    expect(r.candidate).toBe('significant_change');
    expect(d.step(digit(), 1200).candidate).toBeNull();
  });
  it('late B acceptance cannot confirm C', () => {
    const d = ready();
    d.step(digit(), 600);
    d.step(digit(), 1000);
    d.step(pixels(130), 1200);
    expect(d.accept(1, digit())).toBe(false);
    expect(d.step(pixels(130), 1600).candidate).toBe('significant_change');
    expect(d.accept(2, pixels(130))).toBe(true);
  });
  it('A to B to A invalidates twice even when returning to baseline', () => {
    const d = ready();
    d.step(digit(), 600);
    expect(d.step(pixels(), 800).revision).toBe(2);
    expect(d.step(pixels(), 1200).candidate).toBe('significant_change');
    expect(d.accept(1, digit())).toBe(false);
  });
  it('continuous animation hits maximum wait without claiming stability', () => {
    const d = ready();
    for (let n = 1; n < 6; n++) d.step(pixels(60 + n * 30), 400 + n * 200);
    const r = d.step(pixels(240), 1600);
    expect(r.stable).toBe(false);
    expect(r.candidate).not.toBeNull();
    expect(d.step(pixels(60), 1800).candidate).toBeNull();
  });
  it('confirmation permits fresh identical pixels while ordinary duplicate is suppressed', () => {
    const d = new Detector(regions);
    d.step(pixels(), 0);
    d.step(pixels(), 400);
    expect(d.step(pixels(), 600).candidate).toBeNull();
    expect(d.step(pixels(), 800, 'confirmation').candidate).toBe('confirmation');
  });
  it('failed recognition releases inflight but never accepts baseline', () => {
    const d = ready();
    d.step(digit(), 600);
    d.step(digit(), 1000);
    d.release(1);
    expect(d.step(digit(), 2000, 'retry').candidate).toBe('retry');
  });
  it('safety polling sends a new capture on an unchanged table', () => {
    const d = ready();
    expect(d.step(pixels(), 15400).candidate).toBe('safety_poll');
  });
  it('owns previous and anchors when caller reuses same typed array', () => {
    const d = ready(),
      p = pixels();
    d.step(p, 600);
    digit(p);
    expect(d.step(p, 800).changed).toBe(true);
  });
  it('rejects dimension changes and malformed pixel buffers', () => {
    const d = ready();
    expect(() => d.step({ ...pixels(), width: 16 }, 600)).toThrow();
    expect(() =>
      difference(
        pixels(),
        { data: new Uint8ClampedArray(16 * 64 * 4), width: 16, height: 64 },
        regions,
      ),
    ).toThrow('frame_dimensions_changed');
  });
  it('new detector epoch requires initial observation', () => {
    ready();
    const d = new Detector(regions);
    d.step(pixels(), 0);
    expect(d.step(pixels(), 400).candidate).toBe('initial');
  });
  it('rejects invalid configuration', () => {
    expect(() => new Detector(regions, { ...DEFAULTS, maxWaitMs: 100 })).toThrow();
  });
});
describe('profile', () => {
  const profile = () => ({
    calibration_id: 'calibration-1',
    width: 1280,
    height: 720,
    unit: 'chips' as const,
    scale: 0,
    hero_seat: 0,
    regions: defaultRegions(),
  });
  it('round trips six seats and normalized regions', () => {
    expect(validateProfile(JSON.parse(JSON.stringify(profile())))).toEqual(profile());
  });
  it.each([NaN, -1, 1.1])('rejects invalid region coordinate %s', (x) => {
    const p = profile();
    p.regions[0].x = x;
    expect(() => validateProfile(p)).toThrow();
  });
  it('rejects duplicate regions and all masks', () => {
    const p = profile();
    p.regions.push(p.regions[0]);
    expect(() => validateProfile(p)).toThrow();
    expect(() =>
      validateProfile({ ...profile(), regions: [{ ...regions[0], ignore: true }] }),
    ).toThrow();
  });
  it('clones profile instead of retaining mutable user input', () => {
    const p = profile(),
      cloned = validateProfile(p);
    p.regions[0].x = 0.1;
    expect(cloned.regions[0].x).toBe(0.3);
  });
});
