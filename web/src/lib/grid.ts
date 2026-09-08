import type { Mark } from "../types/bundle";
import { lngLatToMerc } from "./mercator";

/**
 * The decoded display grid, and the questions asked of it.
 *
 * HAND and stage are stored in tenths of a metre so they fit a byte, which is why
 * every depth here is divided by ten on the way out and compared against 5 (half a
 * metre) on the way in. That threshold is the model's own: below it the cell is not
 * called wet.
 */
export interface Grid {
  hand: Uint8Array;
  reach: Uint16Array;
  width: number;
  height: number;
  /** Web Mercator metres, [west, south, east, north]. */
  bounds: readonly [number, number, number, number];
  nReaches: number;
}

const NO_HAND = 255;
const NO_REACH = 65535;
/** Half a metre, in the grid's tenths. */
const WET_ABOVE = 0.5;

/** Index of the cell a mark falls in, or -1 when it falls outside the grid. */
export function markPixel(grid: Grid, mark: Pick<Mark, "lon" | "lat">): number {
  const [px, py] = lngLatToMerc(mark.lon, mark.lat);
  const [w, s, e, n] = grid.bounds;
  const col = Math.round(((px - w) / (e - w)) * grid.width);
  const row = Math.round(((n - py) / (n - s)) * grid.height);
  if (col < 0 || row < 0 || col >= grid.width || row >= grid.height) return -1;
  return row * grid.width + col;
}

/** Water depth in metres at one cell, or null where the model leaves it dry. */
export function depthAtPixel(grid: Grid, stage: Float32Array, pixel: number): number | null {
  if (pixel < 0) return null;
  const h = grid.hand[pixel];
  const r = grid.reach[pixel];
  if (h === undefined || r === undefined) return null;
  if (h === NO_HAND || r === NO_REACH || r >= grid.nReaches) return null;
  const s = stage[r];
  if (s === undefined) return null;
  const d = s - h;
  return d > WET_ABOVE ? d / 10 : null;
}

/** Marks that count towards the reported error.
 *
 *  Coastal marks are dropped whatever the toggle says, matching `scorable_marks` on
 *  the server: HAND has no surge term, so scoring against one measures a mechanism the
 *  model does not contain rather than a fit it got wrong. The graded filter is the
 *  toggle - on one watershed the quality 1-2 marks gave 1.0 m and the rest 4.9 m, so
 *  scoring everything lets the roughest surveys set the headline. */
const GOOD_QUALITY = new Set([1, 2]);

export function scorableMarks(marks: readonly Mark[], gradedOnly: boolean): Mark[] {
  const riverine = marks.filter((m) => (m.environment ?? "").toLowerCase() !== "coastal");
  return gradedOnly ? riverine.filter((m) => GOOD_QUALITY.has(m.quality)) : riverine;
}

export interface Agreement {
  /** Root mean square residual against the surveyed elevation, in metres. */
  rmse: number;
  /** Marks the model actually put water on. */
  wet: number;
  n: number;
}

/** Score the modelled water surface against surveyed marks.
 *
 *  A mark the model leaves dry still scores: its residual is how far the surveyed
 *  water was above the ground, which is the error of having predicted nothing there.
 *  Dropping those would reward a model that floods less. */
export function agreementAt(
  grid: Grid,
  stage: Float32Array,
  marks: readonly Mark[],
): Agreement | null {
  if (!marks.length) return null;
  let wet = 0;
  let sum = 0;
  for (const m of marks) {
    const depth = depthAtPixel(grid, stage, markPixel(grid, m));
    const residual =
      depth === null ? m.ground_m - m.elev_m : (wet++, m.ground_m + depth - m.elev_m);
    sum += residual * residual;
  }
  return { rmse: Math.sqrt(sum / marks.length), wet, n: marks.length };
}
