export function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function fmtMoneyCompact(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const sign = v < 0 ? "-" : "";
  const abs = Math.abs(v);
  if (abs >= 1000) return `${sign}$${(abs / 1000).toFixed(1)}k`;
  return `${sign}$${abs.toFixed(0)}`;
}

export function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}

export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// Mirrors src/api/db_reads.py::is_executed_trade exactly — a TradeItem row
// that actually executed some quantity, not just an attempted/HOLD entry.
export function isExecutedTrade(t: { fill_status?: string | null; action?: string | null; fill_qty?: number | null }): boolean {
  const fillQty = t.fill_qty || 0;
  return (t.fill_status == null && t.action !== "HOLD") || t.fill_status === "filled" || fillQty > 0;
}

export function pnlClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return "";
  return v > 0 ? "text-pos" : v < 0 ? "text-neg" : "";
}
