// Small localStorage helpers backing DataTable's opt-in column resizing
// (Task 1) and column reordering (Task 2) persistence. Kept out of
// DataTable.tsx so the table component itself stays focused on rendering.
//
// Every call is wrapped in try/catch: localStorage can throw (private
// browsing, storage disabled, quota exceeded) and a persistence failure
// must never break the table itself.

const PREFIX = "qamc-datatable";

function storageKeyFor(storageKey: string, slice: "sizing" | "order"): string {
  return `${PREFIX}:${storageKey}:${slice}`;
}

export function readPersistedColumnState<T>(storageKey: string | undefined, slice: "sizing" | "order", fallback: T): T {
  if (!storageKey) return fallback;
  try {
    const raw = window.localStorage.getItem(storageKeyFor(storageKey, slice));
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export function writePersistedColumnState<T>(storageKey: string | undefined, slice: "sizing" | "order", value: T): void {
  if (!storageKey) return;
  try {
    window.localStorage.setItem(storageKeyFor(storageKey, slice), JSON.stringify(value));
  } catch {
    // Persistence is a convenience, not a requirement - swallow and move on.
  }
}
