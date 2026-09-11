import type { Pixels } from '../core/detector';
/** OCR needs a quiet margin around large digits; source glyphs stay unchanged. */
export function prepareAmount(p: Pixels): Pixels {
  const margin = 20,
    width = p.width + margin * 2,
    height = p.height + margin * 2;
  const data = new Uint8ClampedArray(width * height * 4);
  for (let i = 0; i < data.length; i += 4) {
    data[i] = data[i + 1] = data[i + 2] = 8;
    data[i + 3] = 255;
  }
  for (let y = 0; y < p.height; y++)
    data.set(
      p.data.subarray(y * p.width * 4, (y + 1) * p.width * 4),
      ((y + margin) * width + margin) * 4,
    );
  return { width, height, data };
}
/** High-contrast header at 2x. Removes the green status dot, not text glyphs. */
export function prepareHeader(p: Pixels): Pixels {
  const width = p.width * 2,
    height = p.height * 2;
  const data = new Uint8ClampedArray(width * height * 4);
  for (let y = 0; y < height; y++)
    for (let x = 0; x < width; x++) {
      const at = (Math.floor(y / 2) * p.width + Math.floor(x / 2)) * 4;
      const r = p.data[at],
        g = p.data[at + 1],
        b = p.data[at + 2];
      const v = Math.max(r, g, b) > 100 && !(g > r * 1.3 && g > b * 1.2) ? 0 : 255;
      const out = (y * width + x) * 4;
      data[out] = data[out + 1] = data[out + 2] = v;
      data[out + 3] = 255;
    }
  return { width, height, data };
}
