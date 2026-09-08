/**
 * The per-reach stage table, read at an arbitrary multiplier.
 *
 * The server ships one row per reach and one column per rung of the multiplier ladder,
 * packed into the red channel of a PNG. Reading it between columns is what lets the
 * slider move the water without another request.
 *
 * This indexes by arithmetic, which requires the ladder to be evenly spaced. It is:
 * `discharge_ladder` is a linspace and its step is validated to divide 1.0 so the
 * observed discharge lands on a rung. The damage ladder is *not* evenly spaced - it
 * gains rungs where the curve bends - which is why that one is searched instead.
 */
export function stageAt(
  lut: Uint8Array,
  nMult: number,
  nReaches: number,
  multipliers: readonly number[],
  multiplier: number,
): Float32Array {
  const first = multipliers[0] ?? 0;
  const last = multipliers[multipliers.length - 1] ?? 1;
  const span = last - first || 1;
  const x = Math.min(nMult - 1.001, Math.max(0, ((multiplier - first) / span) * (nMult - 1)));
  const i0 = Math.floor(x);
  const f = x - i0;
  const out = new Float32Array(nReaches);
  for (let i = 0; i < nReaches; i++) {
    const a = lut[i * nMult + i0] ?? 0;
    const b = lut[i * nMult + Math.min(i0 + 1, nMult - 1)] ?? a;
    out[i] = a + (b - a) * f;
  }
  return out;
}
