/** Chart-vs-bottoms sash helpers for the desktop cockpit.

The dockview split between the chart row and Positions/Orders is a real
library sash. Past the bottoms floor, the workspace box itself has to
grow (the sash has nowhere else to take height from). That extra height
used to reset on reload, so a drag that "took" would snap back. Growth
is persisted next to the layout blob; the math is a pure function so a
later drag from a grown sash adds/subtracts instead of replacing. */

export const COCKPIT_LAYOUT_KEY = "qamc.dockview.cockpit.v6";
export const COCKPIT_GROWTH_KEY = "qamc.dockview.cockpit.v6.growth";
/** Same floor `buildDefaultLayout` sets as Positions/Orders minimumHeight. */
export const BOTTOMS_FLOOR_PX = 260;

export function nextSashGrowth(startGrowth: number, startY: number, clientY: number): number {
  return Math.max(0, startGrowth + (clientY - startY));
}

/** Own the drag when the bottoms row is already on its floor (so native
 * dockview has nothing left to give the chart) or when this box is already
 * grown (so dragging back up can give that height back). Extra height is
 * written onto the workspace node during the gesture, not through React
 * state, so mouse-up cannot snap the sash back to the last rendered size. */
export function growthOwnsDrag(
  currentGrowth: number,
  bottomsHeight: number,
  floorPx = BOTTOMS_FLOOR_PX,
): boolean {
  return currentGrowth > 0 || bottomsHeight <= floorPx + 4;
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
