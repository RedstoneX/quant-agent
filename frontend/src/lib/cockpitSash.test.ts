import { describe, expect, it } from "vitest";
import {
  BOTTOMS_FLOOR_PX,
  CHART_FLOOR_PX,
  nextBottomsSashState,
  nextSashGrowth,
  sashSitsBetween,
} from "./cockpitSash";

describe("nextSashGrowth", () => {
  it("grows 1:1 when dragging down from a resting sash on the bottoms floor", () => {
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

describe("nextBottomsSashState", () => {
  const floor = { growth: 0, chartH: 500, bottomsH: BOTTOMS_FLOOR_PX };

  it("grows the page and the chart when dragging down at the bottoms floor", () => {
    expect(nextBottomsSashState(floor, 40)).toEqual({
      growth: 40,
      chartH: 540,
      bottomsH: BOTTOMS_FLOOR_PX,
    });
  });

  it("gives page height back before touching bottoms when dragging up from a grown sash", () => {
    expect(nextBottomsSashState({ growth: 40, chartH: 540, bottomsH: BOTTOMS_FLOOR_PX }, -40)).toEqual(floor);
  });

  it("borrows from the chart to grow bottoms when dragging up at the floor with no page growth", () => {
    expect(nextBottomsSashState(floor, -40)).toEqual({
      growth: 0,
      chartH: 460,
      bottomsH: BOTTOMS_FLOOR_PX + 40,
    });
  });

  it("shrinks bottoms toward the floor before growing the page when there is room below", () => {
    expect(nextBottomsSashState({ growth: 0, chartH: 420, bottomsH: BOTTOMS_FLOOR_PX + 80 }, 50)).toEqual({
      growth: 0,
      chartH: 470,
      bottomsH: BOTTOMS_FLOOR_PX + 30,
    });
  });

  it("does not shrink the chart below its floor when dragging up", () => {
    const tight = { growth: 0, chartH: CHART_FLOOR_PX, bottomsH: 400 };
    expect(nextBottomsSashState(tight, -80)).toEqual(tight);
  });
});

describe("sashSitsBetween", () => {
  it("recognizes a sash sitting in the gap between two stacked groups", () => {
    expect(sashSitsBetween(400, 396, 404)).toBe(true);
  });

  it("rejects a sash that belongs to a split above the lower group", () => {
    expect(sashSitsBetween(200, 396, 404)).toBe(false);
  });
});
