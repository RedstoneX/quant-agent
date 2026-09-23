import { Badge, Text } from "@tremor/react";
import { AccountResponse, MarginInterestEstimate } from "../api/client";
import { fmtMoney, fmtMoneyCompact } from "../lib/format";

/** A single "$X/day [ESTIMATE]" label for margin interest, for compact
 * chrome (HeroBand's top-left card and its collapsed header line) that
 * cannot afford the full strip below. Reads the SAME `margin_interest`
 * object this file's own strip does and applies the SAME rules — a `null`
 * or errored estimate degrades to "not available" text, never a fabricated
 * "$0.00", and a real zero (owner policy: show it every day) renders as an
 * explicit "$0.00/day", never dropped. The ESTIMATE caveat travels in
 * `note`, meant for a `title` tooltip in the compact spot it has no room to
 * print inline — see MarginInterestStrip above for where it prints in
 * full. */
export function marginInterestDailyLabel(
  mi: MarginInterestEstimate | null | undefined,
): { text: string; note: string } {
  if (!mi || mi.error) {
    return {
      text: "Interest: —",
      note: `Margin interest not available — ${mi?.error || "the account could not be read"}`,
    };
  }
  const isZero = mi.daily_usd === 0;
  const rateNote = mi.rate_pct === null ? "" : ` at ${mi.rate_pct.toFixed(2)}% annual rate`;
  if (isZero) {
    return {
      text: "Interest: $0.00/day",
      note: `Nothing borrowed overnight, so nothing is owed${rateNote}.`,
    };
  }
  return {
    text: `Interest: ${fmtMoney(mi.daily_usd)}/day (ESTIMATE)`,
    note: `${mi.label || "Margin interest ESTIMATE"} — borrowed ${fmtMoneyCompact(mi.debit_balance)}${rateNote}. Paper trading's own handling of margin interest is unconfirmed.`,
  };
}

/* Margin interest — the price of money the desk BORROWED, shown as its own
 * strip.
 *
 * Why this file exists at all: /account has returned a `margin_interest`
 * object since 2026-09-01 and no component ever read it, so the cockpit
 * computed the figure every poll and displayed it nowhere. Owner decision
 * 2026-09-18, verbatim: "Yes, every day, even if it's zero, that way I know
 * it's still working." An absent figure and a dead tracker look identical
 * to a reader, so the zero state below is not a courtesy — it is the point.
 *
 * Why it is NOT folded into LiquidityStrip: liquidity is what the desk can
 * deploy. This is what it owes. Same reason it was moved out of the
 * Telegram running-cost block in the same change.
 *
 * Styling is deliberately LiquidityPanel.tsx's, down to the shared `Stat`
 * shape and the bordered `bg-panel-alt` row — this is one more piece of
 * account chrome, not a new visual language. It stays a single wrapping row
 * for the same reason that file records for its own six-tiles-to-one-row
 * cut: account chrome must not eat panel real estate.
 *
 * Every figure is server-computed (src/margin_interest.py). Nothing here
 * re-derives a number — the defect that made the exposure gauge and its own
 * ceiling disagree by 13.6 points came from exactly that. */

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <span className="flex items-baseline gap-1.5" title={note}>
      <span className="label-xs">{label}</span>
      <span className="font-mono text-[length:var(--fs-meta)] font-semibold tabular-nums text-ink">{value}</span>
    </span>
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

  /* A missing object or a server-side fault says so in words. It must never
   * fall through to the zero state below: "not available" and "$0.00" are
   * different claims about the world, and printing the reassuring one over a
   * broken read is the exact failure the owner asked to be able to see. */
  if (!mi || mi.error) {
    return (
      <div className={plain}>
        {`Margin interest: not available — ${mi?.error || "the account could not be read"}`}
      </div>
    );
  }

  /* `error` is null and the dollar figures are present, so a 0 here is a
   * real, measured zero rather than a missing number. Checked against
   * `daily_usd` specifically because that is the figure being reported. */
  const isZero = mi.daily_usd === 0;

  return (
    <div className={wrapper} aria-label="Margin interest">
      <Text className="uppercase tracking-wide">Margin interest</Text>
      {isZero ? (
        <>
          <Stat label="Per day" value="$0.00" />
          <span className="text-[length:var(--fs-meta)] text-dim">
            Nothing borrowed overnight, so nothing is owed
          </span>
          <Stat
            label="Rate"
            value={mi.rate_pct === null ? "—" : `${mi.rate_pct.toFixed(2)}%`}
            note="Annual borrowing rate the desk is charged on an overnight debit balance"
          />
        </>
      ) : (
        <>
          {/* Full cents, not the compact form the other figures use: the
            * daily charge is a sub-dollar number on a book this size, and
            * fmtMoneyCompact would round $0.99 to "$1" — the one figure
            * here where the rounding eats the whole number. */}
          <Stat label="Per day" value={fmtMoney(mi.daily_usd)} />
          <Stat
            label="Per year"
            value={fmtMoneyCompact(mi.annual_usd)}
            note="What the same daily charge comes to over a full year at this balance and rate"
          />
          <Stat
            label="Borrowed"
            value={fmtMoneyCompact(mi.debit_balance)}
            note="The overnight debit balance the charge is worked out on"
          />
          <Stat
            label="Rate"
            value={mi.rate_pct === null ? "—" : `${mi.rate_pct.toFixed(2)}%`}
            note="Annual borrowing rate the desk is charged on an overnight debit balance"
          />
          {/* The ESTIMATE framing is DISPLAYED, never shrunk into a tooltip.
            * Standing desk rule: an estimate is never presented as a
            * measurement, and a label a reader has to hover to find has
            * already failed at that. The zero state above carries no such
            * badge on purpose — nothing borrowed costs nothing at any rate,
            * which is measured, and a label worn everywhere stops biting
            * where it has to. */}
          <Badge color="amber" size="xs">
            ESTIMATE
          </Badge>
          {mi.label && (
            <span className="min-w-0 text-[length:var(--fs-meta)] text-dim">{mi.label}</span>
          )}
        </>
      )}
      {mi.broker_check_note && (
        <span className="min-w-0 text-[length:var(--fs-meta)] text-dim">
          {`Broker check: ${mi.broker_check_note}`}
        </span>
      )}
      {accountError && (
        <Badge color="amber" size="xs" className="ml-auto">
          stale
        </Badge>
      )}
    </div>
  );
}
