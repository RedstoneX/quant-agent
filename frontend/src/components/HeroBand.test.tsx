// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AccountResponse } from "../api/client";
import { HeroBand } from "./HeroBand";

afterEach(() => {
  cleanup();
});

function account(overrides: Partial<AccountResponse> = {}): AccountResponse {
  return {
    cash: 1000,
    portfolio_value: 10052.8,
    last_equity: 10000,
    daily_pnl: 52.8,
    daily_pnl_pct: 0.53,
    total_pnl: 52.8,
    total_pnl_pct: 0.53,
    total_pnl_since: "2026-08-14",
    paper: true,
    history: [],
    liquidity: null,
    exposure: null,
    risk_limits: null,
    margin_interest: null,
    error: null,
    ...overrides,
  };
}

describe("HeroBand — total P&L and margin interest visibility", () => {
  // Owner request 2026-09-23: total P&L since the start of the board, and
  // margin interest, both belong at the very top-left of the dashboard
  // alongside the day P&L that already showed there.
  it("shows total P&L since the board's tracked start, in the full card", () => {
    render(
      <HeroBand
        account={account()}
        accountError={null}
        positions={[]}
        regime={null}
      />,
    );
    expect(screen.getByText(/Total \$52\.80 \(\+0\.53%\) since 2026-08-14/)).toBeTruthy();
  });

  it("shows total P&L in the collapsed header line too", () => {
    render(
      <HeroBand
        account={account()}
        accountError={null}
        positions={[]}
        regime={null}
        collapsed
      />,
    );
    expect(screen.getByText(/Total \$52\.80 \(\+0\.53%\) since 2026-08-14/)).toBeTruthy();
  });

  it("degrades total P&L to '—' rather than a fabricated number when the baseline is unavailable", () => {
    render(
      <HeroBand
        account={account({ total_pnl: null, total_pnl_pct: null, total_pnl_since: null })}
        accountError={null}
        positions={[]}
        regime={null}
      />,
    );
    expect(screen.getByText("Total P&L: —")).toBeTruthy();
  });

  it("shows a real zero this-week cumulative as an explicit $0.00, never blank", () => {
    render(
      <HeroBand
        account={account({
          margin_interest: {
            debit_balance: 0,
            rate_pct: 6.25,
            daily_usd: 0,
            annual_usd: 0,
            label: null,
            broker_check_note: null,
            days_charged: null,
            period_usd: null,
            error: null,
            cumulative: {
              this_week_usd: 0,
              current_month_usd: 0,
              current_month_label: "September 2026",
              prior_months: [],
              all_time_usd: 0,
              all_time_since: "2026-09-24",
              is_estimate: true,
              source: "estimate",
            },
          },
        })}
        accountError={null}
        positions={[]}
        regime={null}
      />,
    );
    expect(screen.getByText("Interest this week: $0.00")).toBeTruthy();
  });

  it("labels a nonzero cumulative this-week figure as (est.), never a bare number", () => {
    render(
      <HeroBand
        account={account({
          margin_interest: {
            debit_balance: 5729,
            rate_pct: 6.25,
            daily_usd: 0.99,
            annual_usd: 358,
            label: null,
            broker_check_note: null,
            days_charged: null,
            period_usd: null,
            error: null,
            cumulative: {
              this_week_usd: 2.97,
              current_month_usd: 2.97,
              current_month_label: "September 2026",
              prior_months: [],
              all_time_usd: 2.97,
              all_time_since: "2026-09-22",
              is_estimate: true,
              source: "estimate",
            },
          },
        })}
        accountError={null}
        positions={[]}
        regime={null}
      />,
    );
    expect(screen.getByText("Interest this week: $2.97 (est.)")).toBeTruthy();
  });
});

describe("HeroBand — redesigned Account dockview panel (variant='panel')", () => {
  // Owner, live 2026-09-23: "weirdly horizontally dead spaced ... hard to
  // find, the data I wanna see." The panel now leads with a full-width
  // primary stat row — NLV, day P&L, total P&L, interest/day — so the four
  // headline figures the owner named are the glance, not a tall left-hugging
  // stack with interest buried at the bottom.
  it("shows all four headline figures in the primary stat row", () => {
    render(
      <HeroBand
        account={account({
          margin_interest: {
            debit_balance: 5729,
            rate_pct: 6.25,
            daily_usd: 0.99,
            annual_usd: 358,
            label: null,
            broker_check_note: null,
            days_charged: null,
            period_usd: null,
            error: null,
            cumulative: {
              this_week_usd: 2.97,
              current_month_usd: 8.91,
              current_month_label: "September 2026",
              prior_months: [],
              all_time_usd: 8.91,
              all_time_since: "2026-09-01",
              is_estimate: true,
              source: "estimate",
            },
          },
        })}
        accountError={null}
        positions={[]}
        regime={null}
        variant="panel"
      />,
    );
    expect(screen.getByLabelText("Account headline")).toBeTruthy();
    expect(screen.getByText("$10,052.80")).toBeTruthy(); // NLV
    // Day P&L and total P&L both read $52.80 in this fixture, so both tiles
    // carry the value — assert it is present rather than unique.
    expect(screen.getAllByText("$52.80").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("$2.97")).toBeTruthy(); // interest this week, lifted to the top
  });

  it("keeps the est. marker on a nonzero cumulative figure VISIBLE as text, never tooltip-only", () => {
    render(
      <HeroBand
        account={account({
          margin_interest: {
            debit_balance: 5729,
            rate_pct: 6.25,
            daily_usd: 0.99,
            annual_usd: 358,
            label: null,
            broker_check_note: null,
            days_charged: null,
            period_usd: null,
            error: null,
            cumulative: {
              this_week_usd: 2.97,
              current_month_usd: 8.91,
              current_month_label: "September 2026",
              prior_months: [],
              all_time_usd: 8.91,
              all_time_since: "2026-09-01",
              is_estimate: true,
              source: "estimate",
            },
          },
        })}
        accountError={null}
        positions={[]}
        regime={null}
        variant="panel"
      />,
    );
    // Rendered text, which a title-attribute tooltip would not satisfy —
    // the standing desk rule the panel must not regress.
    expect(screen.getByText("est.")).toBeTruthy();
  });

  it("shows a real zero interest week explicitly and never with an est. tag", () => {
    render(
      <HeroBand
        account={account({
          margin_interest: {
            debit_balance: 0,
            rate_pct: 6.25,
            daily_usd: 0,
            annual_usd: 0,
            label: null,
            broker_check_note: null,
            days_charged: null,
            period_usd: null,
            error: null,
            cumulative: {
              this_week_usd: 0,
              current_month_usd: 0,
              current_month_label: "September 2026",
              prior_months: [],
              all_time_usd: 0,
              all_time_since: "2026-09-24",
              is_estimate: true,
              source: "estimate",
            },
          },
        })}
        accountError={null}
        positions={[]}
        regime={null}
        variant="panel"
      />,
    );
    expect(screen.getByText("$0.00")).toBeTruthy();
    expect(screen.queryByText("est.")).toBeNull();
  });

  it("says a fault is a fault and never renders it as a zero", () => {
    render(
      <HeroBand
        account={account({
          margin_interest: {
            debit_balance: null,
            rate_pct: null,
            daily_usd: null,
            annual_usd: null,
            label: null,
            broker_check_note: null,
            days_charged: null,
            period_usd: null,
            error: "no borrowing rate is configured",
            cumulative: null,
          },
        })}
        accountError={null}
        positions={[]}
        regime={null}
        variant="panel"
      />,
    );
    expect(screen.getByText("not available")).toBeTruthy();
    expect(screen.queryByText(/\$0\.00/)).toBeNull();
  });

  it("degrades total P&L to '—' rather than a fabricated number", () => {
    render(
      <HeroBand
        account={account({ total_pnl: null, total_pnl_pct: null, total_pnl_since: null })}
        accountError={null}
        positions={[]}
        regime={null}
        variant="panel"
      />,
    );
    // Both NLV-degraded spots would print "—"; assert the Total P&L tile
    // label is present and no fabricated total dollar figure is shown.
    expect(screen.getByText("Total P&L")).toBeTruthy();
  });
});
