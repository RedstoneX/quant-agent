// Shared defensive parsing for the small JSON-array-of-object "blob"
// fields the evening reflection carries (missed_opportunities_json,
// sell_grades_json, buy_grades_json — all `{symbol, ...}[]` shaped).
// Never throws; a malformed/empty/missing blob degrades to null so
// callers can render an honest "not recorded" state instead of guessing.

export function parseJsonArray<T = Record<string, unknown>>(
  json: string | null | undefined
): T[] | null {
  if (!json) return null;
  try {
    const data = JSON.parse(json);
    return Array.isArray(data) && data.length ? (data as T[]) : null;
  } catch {
    return null;
  }
}

// Renders one blob item as "field one: value · field two: value", skipping
// unpopulated fields; falls back to raw JSON if none of the named fields
// are present so nothing is silently dropped.
export function summarizeBlobItem(item: Record<string, unknown>, fields: string[]): string {
  const parts = fields
    .filter((f) => item[f] !== undefined && item[f] !== null && item[f] !== "")
    .map((f) => `${f.replace(/_/g, " ")}: ${item[f]}`);
  return parts.length ? parts.join(" · ") : JSON.stringify(item);
}
