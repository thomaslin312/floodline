import { describe, expect, it } from "vitest";

import { damageAt } from "./damage";

const REFS = [0.5, 1, 2, 3];

describe("damageAt", () => {
  it("fades from nothing below the first reference", () => {
    expect(damageAt(REFS, 0)).toEqual({ a: -1, b: 0, t: 0 });
    expect(damageAt(REFS, 0.25)?.t).toBeCloseTo(0.5);
  });

  it("brackets a multiplier between two channels", () => {
    const pick = damageAt(REFS, 1.5);
    expect(pick).toMatchObject({ a: 1, b: 2 });
    expect(pick?.t).toBeCloseTo(0.5);
  });

  it("lands exactly on a reference", () => {
    expect(damageAt(REFS, 1)).toMatchObject({ a: 0, b: 1, t: 1 });
  });

  it("holds the last channel past the top", () => {
    expect(damageAt(REFS, 9)).toEqual({ a: 3, b: 3, t: 0 });
  });

  it("is null with no references, rather than picking channel zero", () => {
    expect(damageAt([], 1)).toBeNull();
  });
});
