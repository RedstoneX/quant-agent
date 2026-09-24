import { Badge, Text } from "@tremor/react";
import { AccountResponse, MarginInterestCumulative, MarginInterestEstimate } from "../api/client";
import { fmtMoney } from "../lib/format";

/** A single compact label for margin interest — HeroBand's collapsed
 * header line and its own primary-stat tile, which cannot afford the full
 * strip below. Reads the SAME `margin_interest.cumulative` object this
 * file's own strip does and applies the SAME rules: a missing/errored
 * figure degrades to "not available", never a fabricated "$0.00", and "no
 * data yet" is its own honest state — not a zero.
 *
 * Shows THIS WEEK's running total, replacing the old flat "$X/day" label
 * (owner ask 2026-09-24: remove the per-day figure everywhere in the
 * cockpit, not just the full strip). */
export function marginInterestCompactLabel(
  mi: MarginInterestEstimate | null | undefined,
): { text: string; note: string } {
  if (!mi || mi.error) {
    return {
      text: "Interest: —",
      note: `Margin interest not available — ${mi?.error || "the account could not be read"}`,
    };
  }
  const c = mi.cumulative;
  if (!c) {
    return { text: "Interest: —", note: "Margin interest cumulative view not available" };
  }
  if (c.source === "no_data") {
    return { text: "Interest: no data yet", note: "Tracking starts today" };
  }
  const tag = c.is_estimate && c.this_week_usd !== 0 ? " (est.)" : "";
  return {
    text: `Interest this week: ${fmtMoney(c.this_week_usd)}${tag}`,
    note: `This week's margin interest, running total${tag ? " — estimated, not broker-confirmed" : ""}.`,
  };
}

/* Margin interest — the price of money the desk BORROWED, shown as its own
 * strip.
 *
 * Rewritten 2026-09-24, owner ask: replace the per-day/per-year figures and
 * the ESTIMATE-caveat paragraph with a CUMULATIVE view — this week, the
 * current month, each of up to five more recent months that had any
 * interest (zero months skipped), and an all-time total. The entire
 * estimate marker is now the small "est." tag next to a figure — no
 * paragraph, no `label`/`broker_check_note` text block. `source` still
 * prefers a broker-CONFIRMED `INT` charge over our own daily-accrual
 * formula; `all_time_since` is always shown next to the all-time figure so
 * "all-time" is never read as more complete than it actually is.
 *
 * Every figure is server-computed (src/margin_interest.py). Nothing here
 * re-derives a number.
 */

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <span className="flex items-baseline gap-1.5" title={note}>
      <span className="label-xs">{label}</span>
      <span className="font-mono text-[length:var(--fs-meta)] font-semibold tabular-nums text-ink">{value}</span>
    </span>
  );
}

function EstTag() {
  return (
    <Badge color="amber" size="xs">
      est.
    </Badge>
  );
}

export function MarginInterestStrip({
  account,
  accountError,
  variant = "page",
}: {
  account: AccountResponse | null;
  accountError?: string | null;
  variant?: "page" | "panel";
}) {
  const isPanel = variant === "panel";
  const wrapper = `${isPanel ? "mt-1 " : "mx-3 mt-1 "}flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 overflow-x-hidden rounded-lg border border-border bg-panel-alt px-3 py-1.5`;
  const plain = `${isPanel ? "" : "mx-3 mt-1.5 "}text-[length:var(--fs-meta)] text-dim`;

  if (!account) {
    return (
      <div className={plain}>
        {accountError ? `Margin interest unavailable: ${accountError}` : "Loading margin interest…"}
      </div>
    );
  }

  const mi = account.margin_interest;

  /* A missing object or a server-side fault says so in words — never falls
   * through to a reassuring "$0.00" or "no data". */
  if (!mi || mi.error) {
    return (
      <div className={plain}>
        {`Margin interest: not available — ${mi?.error || "the account could not be read"}`}
      </div>
    );
  }

  const c: MarginInterestCumulative | null = mi.cumulative;

  if (!c) {
    return <div className={plain}>Margin interest: cumulative view not available</div>;
  }

  if (c.source === "no_data") {
    return (
      <div className={plain}>
        Margin interest: no data yet — tracking starts today
      </div>
    );
  }

  const showTag = (usd: number) => c.is_estimate && usd !== 0;

  return (
    <div className={wrapper} aria-label="Margin interest">
      <Text className="uppercase tracking-wide">Margin interest</Text>
      <Stat label="This week" value={fmtMoney(c.this_week_usd)} />
      <span className="flex items-baseline gap-1">
        <Stat label={c.current_month_label} value={fmtMoney(c.current_month_usd)} />
        {showTag(c.current_month_usd) && <EstTag />}
      </span>
      {c.prior_months.map((m) => (
        <Stat key={m.label} label={m.label} value={fmtMoney(m.usd)} />
      ))}
      <span className="flex items-baseline gap-1">
        <Stat
          label="All-time"
          value={fmtMoney(c.all_time_usd)}
          note={`Counted from ${c.all_time_since}`}
        />
        {showTag(c.all_time_usd) && <EstTag />}
      </span>
      <span className="min-w-0 text-[length:var(--fs-meta)] text-dim">
        since {c.all_time_since}
      </span>
      {accountError && (
        <Badge color="amber" size="xs" className="ml-auto">
          stale
        </Badge>
      )}
    </div>
  );
}
