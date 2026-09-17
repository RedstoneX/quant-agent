import { describe, expect, it } from "vitest";
import {
  COCKPIT_BOTTOM_ROW_MIN_HEIGHT,
  sashDragGrowthPx,
  sashDragShouldGrowWorkspace,
} from "./cockpitWorkspaceLayout";

describe("sashDragShouldGrowWorkspace", () => {
  it("does not grow while the bottom row still has room to shrink", () => {
    expect(sashDragShouldGrowWorkspace(340, COCKPIT_BOTTOM_ROW_MIN_HEIGHT)).toBe(false);
    expect(sashDragShouldGrowWorkspace(COCKPIT_BOTTOM_ROW_MIN_HEIGHT + 3)).toBe(false);
  });

  it("grows only once the bottom row is on its floor", () => {
    expect(sashDragShouldGrowWorkspace(COCKPIT_BOTTOM_ROW_MIN_HEIGHT)).toBe(true);
    expect(sashDragShouldGrowWorkspace(COCKPIT_BOTTOM_ROW_MIN_HEIGHT + 2)).toBe(true);
    expect(sashDragShouldGrowWorkspace(COCKPIT_BOTTOM_ROW_MIN_HEIGHT - 8)).toBe(true);
  });
});

describe("sashDragGrowthPx", () => {
  it("is zero at or above the floor origin", () => {
    expect(sashDragGrowthPx(400, 400)).toBe(0);
    expect(sashDragGrowthPx(380, 400)).toBe(0);
  });

  it("tracks how far the mouse has gone past the floor origin", () => {
    expect(sashDragGrowthPx(460, 400)).toBe(60);
  });
});
