/** Default and floor height of the Positions|Orders row under the chart.
 * Chart is the primary stage: the bottom row starts just tall enough for
 * a header plus a couple of rows, and can be dragged down to this floor.
 * Operators who want a taller blotter drag the sash; that choice persists. */
export const COCKPIT_BOTTOM_ROW_DEFAULT_HEIGHT = 220;
export const COCKPIT_BOTTOM_ROW_MIN_HEIGHT = 160;

/** Bumped v6 → v7 so browsers that saved the chart-compressed v6 split
 * pick up the chart-taller default instead of restoring the snapped layout. */
export const COCKPIT_LAYOUT_STORAGE_KEY = "qamc.dockview.cockpit.v7";

/** Dockview reports integer pixel heights; a 1–2px slack treats "on the
 * floor" as actually on the floor rather than 1px of subpixel noise. */
export const COCKPIT_FLOOR_SLACK_PX = 2;

/** The outer workspace box should grow only once the bottom row is already
 * on its minimumHeight. Growing on every downward sash drag races Dockview's
 * own proportion save and snaps the chart back on mouse-up. */
export function sashDragShouldGrowWorkspace(
  positionsRowHeight: number,
  minPositionsHeight: number = COCKPIT_BOTTOM_ROW_MIN_HEIGHT,
  slackPx: number = COCKPIT_FLOOR_SLACK_PX,
): boolean {
  return positionsRowHeight <= minPositionsHeight + slackPx;
}

/** Extra pixels to add to the workspace box once the sash is past the floor.
 * `growthOriginY` is the sash's Y at the moment the bottom row first hit
 * its floor (not the Y at mousedown — that would count the in-box portion
 * of the drag as growth and reintroduce the snap-back). */
export function sashDragGrowthPx(mouseY: number, growthOriginY: number): number {
  return Math.max(0, mouseY - growthOriginY);
}
