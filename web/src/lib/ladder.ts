import type { Ladder } from "../types/bundle";

/**
 * Reading the damage ladder between its rungs.
 *
 * This is twelve lines of arithmetic that were untestable in the old page, and they
 * carried a real defect for as long as they existed: interpolating damage and its
 * in-channel component *both from the origin* preserves their ratio, so the share that
 * decides whether a currency figure is reportable stayed pinned at its first-rung value
 * all the way down to zero discharge. The withholding never fired where it was needed.
 * The server-side fix was to give the ladder rungs where the curve bends; the test
 * beside this file is what stops the reasoning being lost again.
 */
export interface LadderReading {
  multiplier: number;
  damage: number;
  structure: number;
  contents: number;
  inundated: number;
  residents: number;
  /** Share of the damage coming from structures the model placed on a drainage cell. */
  inChannelShare: number;
  /** False when that share is over the threshold, and the currency total is withheld. */
  reportable: boolean;
}

export function ladderAt(ladder: Ladder | undefined, multiplier: number): LadderReading | null {
  if (!ladder) return null;
  const xs = ladder.multipliers;
  const first = xs[0];
  const last = xs[xs.length - 1];
  if (first === undefined || last === undefined) return null;

  const clamped = Math.max(first, Math.min(last, multiplier));
  let i = 0;
  while (i < xs.length - 2 && (xs[i + 1] as number) < clamped) i++;
  const lo = xs[i] as number;
  const hi = xs[i + 1] as number;
  const span = hi - lo;
  const t = span > 0 ? (clamped - lo) / span : 0;
  const lerp = (a: number[]): number => {
    const x = a[i] ?? 0;
    const y = a[i + 1] ?? x;
    return x + (y - x) * t;
  };

  const damage = lerp(ladder.damage);
  const channel = ladder.in_channel ? lerp(ladder.in_channel) : 0;
  const share = damage > 0 ? channel / damage : 0;
  const limit = ladder.max_in_channel_share ?? 1;

  return {
    multiplier: clamped,
    damage,
    structure: lerp(ladder.structure),
    contents: lerp(ladder.contents),
    inundated: Math.round(lerp(ladder.inundated)),
    residents: lerp(ladder.residents),
    inChannelShare: share,
    reportable: share <= limit,
  };
}
