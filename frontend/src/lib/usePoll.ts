import { useEffect } from "react";

const REFRESH_MS = 20000;

/** Same polling posture as the legacy dashboards — no websocket, no new
 * transport. Runs `fn` immediately, then every REFRESH_MS while the tab
 * is visible, and once more whenever the tab becomes visible again. */
export function usePoll(fn: () => void, deps: React.DependencyList = []) {
  useEffect(() => {
    fn();
    const id = setInterval(() => {
      if (!document.hidden) fn();
    }, REFRESH_MS);
    const onVisible = () => {
      if (!document.hidden) fn();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
