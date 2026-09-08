export interface Pixels {
  d: Uint8ClampedArray;
  w: number;
  h: number;
}

/** Decode a data URL into raw RGBA. The grids arrive as PNGs because that is the only
 *  lossless raster a browser will decode for free. */
export function pixels(src: string): Promise<Pixels> {
  return new Promise((resolve, reject) => {
    const im = new Image();
    im.onload = () => {
      const c = document.createElement("canvas");
      c.width = im.naturalWidth;
      c.height = im.naturalHeight;
      const ctx = c.getContext("2d", { willReadFrequently: true });
      if (!ctx) {
        reject(new Error("no 2d canvas context"));
        return;
      }
      ctx.drawImage(im, 0, 0);
      resolve({ d: ctx.getImageData(0, 0, c.width, c.height).data, w: c.width, h: c.height });
    };
    im.onerror = () => reject(new Error("could not decode the grid image"));
    im.src = src;
  });
}

/** Unpack the three grids the bundle ships into typed arrays.
 *  HAND is the red channel; a reach id spans red and green. */
export function decodeGrids(
  hand: Pixels,
  reach: Pixels,
  stageTable: Pixels,
  width: number,
  height: number,
): { hand: Uint8Array; reach: Uint16Array; lut: Uint8Array; nMult: number } {
  const n = width * height;
  const h = new Uint8Array(n);
  const r = new Uint16Array(n);
  for (let p = 0; p < n; p++) {
    h[p] = hand.d[p * 4] ?? 255;
    r[p] = ((reach.d[p * 4] ?? 255) << 8) | (reach.d[p * 4 + 1] ?? 255);
  }
  const lut = new Uint8Array(stageTable.w * stageTable.h);
  for (let p = 0; p < lut.length; p++) lut[p] = stageTable.d[p * 4] ?? 0;
  return { hand: h, reach: r, lut, nMult: stageTable.w };
}
