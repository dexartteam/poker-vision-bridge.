import type { Pixels } from '../../core/detector';
import type { Rect } from '../contracts';

export function cropPixels(p: Pixels, r: Rect): Pixels {
  const x = Math.round(r.x),
    y = Math.round(r.y),
    width = Math.round(r.w),
    height = Math.round(r.h);
  if (x < 0 || y < 0 || width < 1 || height < 1 || x + width > p.width || y + height > p.height)
    throw new Error('Область выходит за границы кадра');
  const data = new Uint8ClampedArray(width * height * 4);
  for (let row = 0; row < height; row++) {
    const start = ((y + row) * p.width + x) * 4;
    data.set(p.data.subarray(start, start + width * 4), row * width * 4);
  }
  return { width, height, data };
}

export function actionButtonPixels(p: Pixels): Pixels[] {
  return [
    [7, 202],
    [219, 199],
    [432, 202],
  ].map(([x, w]) =>
    cropPixels(p, {
      x: (x * p.width) / 640,
      y: (4 * p.height) / 93,
      w: (w * p.width) / 640,
      h: (83 * p.height) / 93,
    }),
  );
}

export function activeButton(p: Pixels): boolean {
  let red = 0;
  for (let i = 0; i < p.data.length; i += 4) {
    const [r, g, b] = [p.data[i], p.data[i + 1], p.data[i + 2]];
    if (r >= 160 && g <= 80 && b <= 80 && r - g >= 80) red++;
  }
  return red / (p.width * p.height) >= 0.5;
}

/** Blue captions replace neutral nicknames in this interface. */
export function actionCaption(p: Pixels): boolean {
  let blue = 0;
  for (let i = 0; i < p.data.length; i += 4) {
    const [r, g, b] = [p.data[i], p.data[i + 1], p.data[i + 2]];
    if (b >= 140 && b - r >= 40 && b - g >= 10) blue++;
  }
  return blue / (p.width * p.height) >= 0.01;
}

/** Invert the light captions and enlarge with deterministic bilinear interpolation. */
export function prepareText(p: Pixels): Pixels {
  const scale = 3,
    border = 12,
    width = p.width * scale + border * 2,
    height = p.height * scale + border * 2;
  const data = new Uint8ClampedArray(width * height * 4).fill(255);
  const gray = (x: number, y: number) => {
    const i =
      (Math.max(0, Math.min(p.height - 1, y)) * p.width + Math.max(0, Math.min(p.width - 1, x))) *
      4;
    return 255 - (0.299 * p.data[i] + 0.587 * p.data[i + 1] + 0.114 * p.data[i + 2]);
  };
  for (let y = 0; y < p.height * scale; y++)
    for (let x = 0; x < p.width * scale; x++) {
      const sx = (x + 0.5) / scale - 0.5,
        sy = (y + 0.5) / scale - 0.5;
      const x0 = Math.floor(sx),
        y0 = Math.floor(sy),
        fx = sx - x0,
        fy = sy - y0;
      const value =
        gray(x0, y0) * (1 - fx) * (1 - fy) +
        gray(x0 + 1, y0) * fx * (1 - fy) +
        gray(x0, y0 + 1) * (1 - fx) * fy +
        gray(x0 + 1, y0 + 1) * fx * fy;
      const i = ((y + border) * width + x + border) * 4;
      data[i] = data[i + 1] = data[i + 2] = value;
    }
  return { width, height, data };
}

export function betVisibility(p: Pixels): 'visible' | 'empty' | 'unknown' {
  let white = 0;
  for (let i = 0; i < p.data.length; i += 4) {
    const rgb = [p.data[i], p.data[i + 1], p.data[i + 2]];
    if (Math.min(...rgb) > 150 && Math.max(...rgb) - Math.min(...rgb) < 70) white++;
  }
  const ratio = white / (p.width * p.height);
  return ratio >= 0.004 ? 'visible' : ratio <= 0.001 ? 'empty' : 'unknown';
}
