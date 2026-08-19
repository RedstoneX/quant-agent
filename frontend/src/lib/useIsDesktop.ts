import { useEffect, useState } from "react";

// Matches Tailwind's default `xl:` breakpoint (1280px) — the same
// breakpoint the primary cockpit already switches on for its
// Candidates/Chart/Decision-Room tab strip. Dockview is only ever
// instantiated above this width; below it, App.tsx renders the plain
// `SupportTabs` strip instead, so a touch/narrow viewport never pays for
// (or has to fight) a drag-and-drop workspace it can't meaningfully use.
const QUERY = "(min-width: 1280px)";

export function useIsDesktop(): boolean {
  const [isDesktop, setIsDesktop] = useState(() => (typeof window === "undefined" ? true : window.matchMedia(QUERY).matches));

  useEffect(() => {
    const mql = window.matchMedia(QUERY);
    const onChange = () => setIsDesktop(mql.matches);
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  return isDesktop;
}
