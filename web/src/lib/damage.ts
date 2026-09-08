/**
 * The damage layer holds four channels, one per reference discharge, each log-scaled
 * damage per cell. Shipping a raster per rung of the ladder would be ten megabytes;
 * four brackets the slider closely enough that interpolating between them is smaller
 * than the model's own error.
 */
export interface ChannelPick {
  /** Index of the lower reference channel, or -1 below the first one. */
  a: number;
  b: number;
  t: number;
}

/** Which two reference channels bracket this multiplier, and how far between them.
 *  Below the first reference the layer fades to nothing at zero discharge, which is
 *  what the ladder says happens. */
export function damageAt(refs: readonly number[], multiplier: number): ChannelPick | null {
  const first = refs[0];
  if (first === undefined) return null;
  if (multiplier <= first) {
    return { a: -1, b: 0, t: Math.max(0, multiplier / first) };
  }
  for (let i = 0; i < refs.length - 1; i++) {
    const hi = refs[i + 1] as number;
    if (multiplier <= hi) {
      const lo = refs[i] as number;
      return { a: i, b: i + 1, t: (multiplier - lo) / (hi - lo) };
    }
  }
  return { a: refs.length - 1, b: refs.length - 1, t: 0 };
}
