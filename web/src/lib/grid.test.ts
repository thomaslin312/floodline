import { describe, expect, it } from "vitest";

import type { Mark } from "../types/bundle";
import { agreementAt, depthAtPixel, markPixel, scorableMarks, type Grid } from "./grid";
import { lngLatToMerc } from "./mercator";

/** A 4x4 grid over a small box, with one reach. */
function grid(hand: number[], reach: number[]): Grid {
  const [w, s] = lngLatToMerc(-95.5, 29.5);
  const [e, n] = lngLatToMerc(-95.4, 29.6);
  return {
    hand: Uint8Array.from(hand),
    reach: Uint16Array.from(reach),
    width: 4,
    height: 4,
    bounds: [w, s, e, n],
    nReaches: 1,
  };
}

const flat = () => grid(Array(16).fill(10), Array(16).fill(0));

describe("markPixel", () => {
  it("puts a mark inside the box on a cell", () => {
    expect(markPixel(flat(), { lon: -95.45, lat: 29.55 })).toBeGreaterThanOrEqual(0);
  });

  it("returns -1 for a mark outside the grid rather than clamping to an edge", () => {
    // Clamping would silently score a mark against terrain that is not under it.
    expect(markPixel(flat(), { lon: -80, lat: 40 })).toBe(-1);
  });
});

describe("depthAtPixel", () => {
  const stage = Float32Array.from([30]); // 3.0 m, in tenths

  it("is stage minus HAND, in metres", () => {
    expect(depthAtPixel(flat(), stage, 0)).toBeCloseTo(2.0);
  });

  it("is null where the water is under half a metre", () => {
    // The model's own threshold: below it the cell is not called wet.
    expect(depthAtPixel(flat(), Float32Array.from([10.4]), 0)).toBeNull();
  });

  it("is null on a nodata cell", () => {
    expect(depthAtPixel(grid(Array(16).fill(255), Array(16).fill(0)), stage, 0)).toBeNull();
  });

  it("is null where no reach drains the cell", () => {
    expect(depthAtPixel(grid(Array(16).fill(10), Array(16).fill(65535)), stage, 0)).toBeNull();
  });
});

describe("scorableMarks", () => {
  const marks = [
    { quality: 1, environment: "Riverine" },
    { quality: 3, environment: "Riverine" },
    { quality: 1, environment: "Coastal" },
  ] as Mark[];

  it("always drops coastal marks, because HAND has no surge term", () => {
    expect(scorableMarks(marks, false)).toHaveLength(2);
    expect(scorableMarks(marks, false).every((m) => m.environment !== "Coastal")).toBe(true);
  });

  it("keeps only graded surveys when asked", () => {
    expect(scorableMarks(marks, true)).toHaveLength(1);
  });
});

describe("agreementAt", () => {
  it("counts a mark the model leaves dry rather than skipping it", () => {
    // Skipping them would reward a model that floods nothing: every miss would vanish
    // from the denominator instead of costing the depth that was actually there.
    const dry = Float32Array.from([0]);
    const marks = [{ lon: -95.45, lat: 29.55, ground_m: 10, elev_m: 12 }] as Mark[];
    const got = agreementAt(flat(), dry, marks);
    expect(got?.wet).toBe(0);
    expect(got?.n).toBe(1);
    expect(got?.rmse).toBeCloseTo(2);
  });

  it("scores a wet mark on the modelled water surface", () => {
    const marks = [{ lon: -95.45, lat: 29.55, ground_m: 10, elev_m: 12 }] as Mark[];
    const got = agreementAt(flat(), Float32Array.from([30]), marks);
    expect(got?.wet).toBe(1);
    expect(got?.rmse).toBeCloseTo(0);
  });

  it("is null with nothing to score, rather than an RMSE of zero", () => {
    expect(agreementAt(flat(), Float32Array.from([30]), [])).toBeNull();
  });
});
