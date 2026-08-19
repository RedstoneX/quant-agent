import { AccountResponse, PositionItem } from "../api/client";
import { fmtMoneyCompact } from "../lib/format";

/* Always-visible header gauge — real portfolio exposure (Long / Bearish
 * hedge / Cash & equivalents as a share of portfolio_value), not the
 * historical/aggregate candidate-direction stats DirectionalBiasPanel
 * already covers in its own tab. This is "what is deployed right now,"
 * derived purely from already-fetched account+positions state — no new
 * fetch, no fabricated figures. Sits under TopStrip so exposure posture
 * never requires opening a tab to see. */
export function ExposureStrip({ account, positions }: { account: AccountResponse | null; positions: PositionItem[] }) {
  if (!account || account.error || !account.portfolio_value) return null;
  const total = account.portfolio_value;

  const longMv = positions.filter((p) => p.direction === "long").reduce((s, p) => s + (p.market_value || 0), 0);
  const hedgeMv = positions.filter((p) => p.direction === "bearish_hedge").reduce((s, p) => s + (p.market_value || 0), 0);
  const cashMv = Math.max(total - longMv - hedgeMv, 0);

  const segments = [
    { label: "Long", value: longMv, cls: "bg-pos" },
    { label: "Bearish hedge", value: hedgeMv, cls: "bg-hedge" },
    { label: "Cash & equivalents", value: cashMv, cls: "bg-dim" },
  ].filter((s) => s.value > 0);

  if (segments.length === 0) return null;

  return (
    <div className="flex items-center gap-3 px-4 py-1.5 border-b border-border bg-bg text-[0.7rem] flex-wrap">
      <span className="text-dim uppercase tracking-wide font-semibold flex-shrink-0">Exposure</span>
      <div className="flex-1 min-w-[140px] flex h-2 rounded-full overflow-hidden bg-panel-alt border border-border">
        {segments.map((s) => (
          <div key={s.label} className={s.cls} style={{ width: `${(s.value / total) * 100}%` }} title={`${s.label}: ${fmtMoneyCompact(s.value)}`} />
        ))}
      </div>
      <div className="flex items-center gap-3 flex-shrink-0">
        {segments.map((s) => (
          <span key={s.label} className="inline-flex items-center gap-1">
            <span className={`w-1.5 h-1.5 rounded-full inline-block ${s.cls}`} />
            <span className="text-dim">{s.label}</span>
            <span className="font-semibold tabular-nums">{fmtMoneyCompact(s.value)}</span>
          </span>
        ))}
      </div>
    </div>
  );
}
