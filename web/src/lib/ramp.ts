type Stop = readonly [number, number, number];

/** Modelled depth. Square-rooted rather than linear: depth is capped at 8 m for
 *  colour and an urban flood is mostly 0.5-2 m, so a linear ramp spends three quarters
 *  of its range on depths that almost never occur. The scale stays absolute, so the
 *  same depth is the same colour in every watershed. */
export const DEPTH_RAMP: readonly Stop[] = [
  [207, 230, 242],
  [127, 184, 220],
  [61, 143, 192],
  [26, 96, 152],
  [11, 63, 115],
  [6, 34, 66],
];

/** Damage per cell, log-scaled by the server before it reaches the image. */
export const DAMAGE_RAMP: readonly Stop[] = [
  [253, 227, 199],
  [247, 178, 103],
  [239, 123, 69],
  [214, 73, 51],
  [140, 28, 19],
];

/** Sample a ramp at 0..1, interpolating between its stops. */
export function sample(ramp: readonly Stop[], t: number): Stop {
  const x = Math.min(0.999, Math.max(0, t)) * (ramp.length - 1);
  const i = Math.floor(x);
  const f = x - i;
  const a = ramp[i] as Stop;
  const b = (ramp[Math.min(i + 1, ramp.length - 1)] ?? a) as Stop;
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
}
