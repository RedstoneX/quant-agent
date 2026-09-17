import { describe, expect, it } from "vitest";
import {
  COCKPIT_DESKTOP_MIN_WIDTH_PX,
  COCKPIT_DESKTOP_QUERY,
  RESEARCH_DESKTOP_MIN_WIDTH_PX,
} from "./useIsDesktop";

describe("cockpit desktop gate", () => {
  it("mounts Dockview at a normal laptop width, not only past xl", () => {
    expect(COCKPIT_DESKTOP_MIN_WIDTH_PX).toBe(1024);
    expect(COCKPIT_DESKTOP_QUERY).toBe("(min-width: 1024px)");
    expect(COCKPIT_DESKTOP_MIN_WIDTH_PX).toBeLessThan(1280);
    expect(COCKPIT_DESKTOP_MIN_WIDTH_PX).toBeLessThan(RESEARCH_DESKTOP_MIN_WIDTH_PX);
  });
});
