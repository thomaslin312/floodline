import { describe, expect, it } from "vitest";

import type { Ladder } from "../types/bundle";
import { ladderAt } from "./ladder";

/** Two rungs, and a tenth of the damage from the channel at the top. */
function ladder(overrides: Partial<Ladder> = {}): Ladder {
  return {
    multipliers: [0, 0.5, 1],
    discharge_cms: [0, 500, 1000],
    damage: [0, 400, 1000],
    structure: [0, 320, 800],
    contents: [0, 80, 200],
    inundated: [0, 4, 10],
    residents: [0, 40, 100],
    in_channel: [0, 40, 100],
    max_in_channel_share: 0.5,
    ...overrides,
  };
}

describe("ladderAt", () => {
  it("returns a rung exactly when asked for one", () => {
    const at = ladderAt(ladder(), 0.5);
    expect(at?.damage).toBe(400);
    expect(at?.inundated).toBe(4);
  });

  it("lands between the rungs that bracket it", () => {
    const at = ladderAt(ladder(), 0.75);
    expect(at?.damage).toBeGreaterThan(400);
    expect(at?.damage).toBeLessThan(1000);
  });

  it("clamps rather than extrapolating past either end", () => {
    expect(ladderAt(ladder(), 9)?.damage).toBe(1000);
    expect(ladderAt(ladder(), -3)?.damage).toBe(0);
  });

  it("withholds the currency total when the channel dominates it", () => {
    // 90% of the damage from structures on a drainage cell: the flood being priced is
    // the one the model invented by putting them there.
    const at = ladderAt(ladder({ in_channel: [0, 360, 900] }), 1);
    expect(at?.reportable).toBe(false);
    expect(at?.inChannelShare).toBeCloseTo(0.9);
    // The counts are the model's answer and survive the withholding.
    expect(at?.inundated).toBe(10);
    expect(at?.residents).toBe(100);
  });

  it("reports the total when the channel is a minority of it", () => {
    expect(ladderAt(ladder(), 1)?.reportable).toBe(true);
  });

  it("treats a ladder with no in-channel data as fully reportable", () => {
    // Bundles cached before that was measured still have to render.
    const at = ladderAt(ladder({ in_channel: undefined, max_in_channel_share: undefined }), 1);
    expect(at?.inChannelShare).toBe(0);
    expect(at?.reportable).toBe(true);
  });

  it("resolves the share between rungs rather than holding the first one", () => {
    // The defect this file exists for. Interpolating damage and its in-channel part
    // from a shared origin preserves their ratio, so with one segment from zero the
    // share is constant along it and the withholding cannot fire. Rungs where the
    // curve bends are what make the share move; this asserts it does.
    const steep = ladder({
      multipliers: [0, 0.01, 0.05, 1],
      damage: [0, 100, 300, 1000],
      in_channel: [0, 95, 150, 100],
      structure: [0, 80, 240, 800],
      contents: [0, 20, 60, 200],
      inundated: [0, 1, 3, 10],
      residents: [0, 10, 30, 100],
    });
    const low = ladderAt(steep, 0.01)?.inChannelShare ?? 0;
    const high = ladderAt(steep, 1)?.inChannelShare ?? 0;
    expect(low).toBeGreaterThan(0.9);
    expect(high).toBeLessThan(0.2);
    expect(ladderAt(steep, 0.01)?.reportable).toBe(false);
    expect(ladderAt(steep, 1)?.reportable).toBe(true);
  });

  it("is null without a ladder, so a caller cannot read zeroes as an answer", () => {
    expect(ladderAt(undefined, 1)).toBeNull();
  });
});
