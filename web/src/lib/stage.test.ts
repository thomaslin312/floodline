import { describe, expect, it } from "vitest";

import { stageAt } from "./stage";

// One reach, three rungs: 0, 10, 30 tenths of a metre at multipliers 0, 0.5, 1.
const LUT = Uint8Array.from([0, 10, 30]);
const MULTS = [0, 0.5, 1];

describe("stageAt", () => {
  it("reads a rung exactly", () => {
    expect(stageAt(LUT, 3, 1, MULTS, 0.5)[0]).toBeCloseTo(10);
  });

  it("interpolates between rungs", () => {
    expect(stageAt(LUT, 3, 1, MULTS, 0.75)[0]).toBeCloseTo(20);
  });

  it("clamps below and above the ladder instead of extrapolating", () => {
    expect(stageAt(LUT, 3, 1, MULTS, -1)[0]).toBeCloseTo(0);
    expect(stageAt(LUT, 3, 1, MULTS, 5)[0]).toBeGreaterThan(29);
  });

  it("keeps reaches separate", () => {
    // Two reaches, two rungs: reach 0 rises to 20, reach 1 stays dry.
    const lut = Uint8Array.from([0, 20, 0, 0]);
    const got = stageAt(lut, 2, 2, [0, 1], 1);
    expect(got[0]).toBeCloseTo(20, 1);
    expect(got[1]).toBeCloseTo(0);
  });

  it("approaches the top rung without landing on it", () => {
    // The index is clamped to nMult - 1.001 so that i0 + 1 is always in range. The
    // cost is a thousandth of a rung at the very top, which is well inside the
    // model's own error and is why the clamp is written that way rather than with a
    // branch. Pinned here so it reads as a choice rather than an off-by-one.
    const top = stageAt(LUT, 3, 1, MULTS, 1)[0] as number;
    expect(top).toBeLessThan(30);
    expect(top).toBeGreaterThan(29.9);
  });
});
