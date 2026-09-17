/** Chart-vs-bottoms sash helpers for the desktop cockpit.

The dockview split between the chart row and Positions/Orders is a real
library sash. Other vertical sashes (Holdings above the chart, stacked
rearrangements) stay native — this module only models the bottoms sash,
where past the Positions/Orders floor the workspace box itself has to
grow. Extra height used to reset on reload, so a drag that "took" would
snap back. Growth is persisted next to the layout blob.

Two-way: dragging that sash down shrinks bottoms until the floor then
grows the page; dragging it up returns page height first, then borrows
from the chart to grow bottoms. Native dockview alone could only steal
from the row under the chart, and once that row was on its floor an
upward drag was swallowed. */

export const COCKPIT_LAYOUT_KEY = "qamc.dockview.cockpit.v8";
export const COCKPIT_GROWTH_KEY = "qamc.dockview.cockpit.v8.growth";
/** Same floor `buildDefaultLayout` sets as Positions/Orders minimumHeight. */
export const BOTTOMS_FLOOR_PX = 260;
/** Chart row floor so a sash above the candles cannot crush the stage. */
export const CHART_FLOOR_PX = 200;
/** Holdings row floor: tab strip plus one chip row. */
export const HOLDINGS_FLOOR_PX = 88;
/** Starting Holdings height — two chip rows, chart still the largest pane. */
export const HOLDINGS_DEFAULT_PX = 176;

export type BottomsSashStart = {
  growth: number;
  chartH: number;
  bottomsH: number;
};

export type BottomsSashState = BottomsSashStart;

/** Pure layout step for the chart-vs-bottoms sash.
 * `deltaY` is pointer travel since pointerdown: positive is down. */
export function nextBottomsSashState(
  start: BottomsSashStart,
  deltaY: number,
  chartMin = CHART_FLOOR_PX,
  bottomsMin = BOTTOMS_FLOOR_PX,
): BottomsSashState {
  let { growth, chartH, bottomsH } = start;

  if (deltaY > 0) {
    const shrinkBottoms = Math.min(deltaY, Math.max(0, bottomsH - bottomsMin));
    bottomsH -= shrinkBottoms;
    chartH += shrinkBottoms;
    const leftover = deltaY - shrinkBottoms;
    growth += leftover;
    chartH += leftover;
  } else {
    const up = -deltaY;
    const fromGrowth = Math.min(up, growth);
    growth -= fromGrowth;
    chartH -= fromGrowth;
    const leftover = up - fromGrowth;
    const growBottoms = Math.min(leftover, Math.max(0, chartH - chartMin));
    bottomsH += growBottoms;
    chartH -= growBottoms;
  }

  return { growth, chartH, bottomsH };
}

/** True when a sash's vertical midpoint sits between two stacked groups. */
export function sashSitsBetween(
  sashMidY: number,
  upperBottom: number,
  lowerTop: number,
  slop = 8,
): boolean {
  return sashMidY >= upperBottom - slop && sashMidY <= lowerTop + slop;
}

export function nextSashGrowth(startGrowth: number, startY: number, clientY: number): number {
  return nextBottomsSashState(
    { growth: startGrowth, chartH: CHART_FLOOR_PX, bottomsH: BOTTOMS_FLOOR_PX },
    clientY - startY,
  ).growth;
}

export function readPersistedGrowth(): number {
  try {
    const raw = window.localStorage.getItem(COCKPIT_GROWTH_KEY);
    if (raw == null) return 0;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : 0;
  } catch {
    return 0;
  }
}

export function writePersistedGrowth(px: number): void {
  try {
    if (px <= 0) window.localStorage.removeItem(COCKPIT_GROWTH_KEY);
    else window.localStorage.setItem(COCKPIT_GROWTH_KEY, String(Math.round(px)));
  } catch {
    /* UI-only best effort */
  }
}

export function clearPersistedGrowth(): void {
  try {
    window.localStorage.removeItem(COCKPIT_GROWTH_KEY);
  } catch {
    /* UI-only best effort */
  }
}
