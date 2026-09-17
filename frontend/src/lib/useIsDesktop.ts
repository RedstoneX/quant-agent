import { useEffect, useState } from "react";

// Cockpit Dockview vs the composed Positions/Candidates/Chart pane strip.
// Tailwind `lg` (1024px). The previous `xl` (1280px) gate treated a normal
// non-maximized desktop window as an iPad: Dockview never mounted, so
// tabs could not be rearranged until the window crossed 1280 and the
// layout flipped. iPad portrait (~820px) stays on the composed strip.
export const COCKPIT_DESKTOP_MIN_WIDTH_PX = 1024;
export const COCKPIT_DESKTOP_QUERY = `(min-width: ${COCKPIT_DESKTOP_MIN_WIDTH_PX}px)`;

// Research desk keeps `xl` so iPad landscape stays a reading composition
// rather than a squeezed Dockview — that surface was not the rearrange bug.
export const RESEARCH_DESKTOP_MIN_WIDTH_PX = 1280;
export const RESEARCH_DESKTOP_QUERY = `(min-width: ${RESEARCH_DESKTOP_MIN_WIDTH_PX}px)`;

function useMatch(query: string): boolean {
  const [matches, setMatches] = useState(() => (typeof window === "undefined" ? true : window.matchMedia(query).matches));

  useEffect(() => {
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}

/** True when the cockpit should mount Dockview (move/resize/dock). */
export function useIsDesktop(): boolean {
  return useMatch(COCKPIT_DESKTOP_QUERY);
}

/** True when the Research desk should mount its Dockview workspace. */
export function useIsResearchDesktop(): boolean {
  return useMatch(RESEARCH_DESKTOP_QUERY);
}
