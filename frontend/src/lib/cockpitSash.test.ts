import { describe, expect, it } from "vitest";
import { BOTTOMS_FLOOR_PX, growthOwnsDrag, nextSashGrowth } from "./cockpitSash";

describe("nextSashGrowth", () => {
  it("grows 1:1 when dragging down from a resting sash", () => {
    expect(nextSashGrowth(0, 400, 460)).toBe(60);
  });

  it("adds to existing growth instead of replacing it (no snap-back)", () => {
    expect(nextSashGrowth(120, 520, 550)).toBe(150);
  });

  it("shrinks existing growth when dragging back up", () => {
    expect(nextSashGrowth(120, 520, 500)).toBe(100);
  });

  it("does not go negative", () => {
    expect(nextSashGrowth(20, 520, 400)).toBe(0);
  });
});

describe("growthOwnsDrag", () => {
  it("owns the drag when the bottoms row is on its floor", () => {
    expect(growthOwnsDrag(0, BOTTOMS_FLOOR_PX)).toBe(true);
  });

  it("leaves in-box resize to dockview when there is still room below", () => {
    expect(growthOwnsDrag(0, BOTTOMS_FLOOR_PX + 80)).toBe(false);
  });

  it("owns the drag while extra page height is already out, so it can be given back", () => {
    expect(growthOwnsDrag(40, BOTTOMS_FLOOR_PX + 80)).toBe(true);
  });
});
