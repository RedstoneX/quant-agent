// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AccountResponse, MarginInterestCumulative, MarginInterestEstimate } from "../api/client";
import { MarginInterestStrip } from "./MarginInterestPanel";

afterEach(() => {
  cleanup();
});

function account(margin: MarginInterestEstimate | null): AccountResponse {
  return {
    cash: 1000,
    portfolio_value: 10000,
    last_equity: 10000,
    daily_pnl: 0,
    daily_pnl_pct: 0,
    total_pnl: null,
    total_pnl_pct: null,
    total_pnl_since: null,
    paper: true,
    history: [],
    liquidity: null,
    exposure: null,
    risk_limits: null,
    margin_interest: margin,
    error: null,
  };
}

function mi(cumulative: MarginInterestCumulative | null): MarginInterestEstimate {
  return {
    debit_balance: 5729,
    rate_pct: 6.25,
    daily_usd: 0.99,
    annual_usd: 358,
    label: null,
    broker_check_note: null,
    days_charged: 1,
    period_usd: 0.99,
    error: null,
    cumulative,
  };
}

describe("MarginInterestStrip", () => {
  it("renders the cumulative buckets — this week, current month, prior months, all-time", () => {
    // Owner ask 2026-09-24: this week / current month / up to 5 more
    // recent nonzero months / all-time, replacing the old per-day figure.
    render(
      <MarginInterestStrip
        account={account(mi({
          this_week_usd: 3.48,
          current_month_usd: 12.34,
          current_month_label: "September 2026",
          prior_months: [{ label: "August 2026", usd: 27.5 }],
          all_time_usd: 39.84,
          all_time_since: "2026-08-01",
          is_estimate: true,
          source: "estimate",
        }))}
      />,
    );
    expect(screen.getByLabelText("Margin interest")).toBeTruthy();
    expect(screen.getByText("$3.48")).toBeTruthy();
    expect(screen.getByText("$12.34")).toBeTruthy();
    expect(screen.getByText("$27.50")).toBeTruthy();
    expect(screen.getByText("$39.84")).toBeTruthy();
    expect(screen.getByText("August 2026")).toBeTruthy();
    expect(screen.getByText(/since 2026-08-01/)).toBeTruthy();
    // The estimate marker is the SMALL tag, never the old caveat paragraph.
    expect(screen.getAllByText("est.").length).toBeGreaterThan(0);
    expect(screen.queryByText(/paper trading's handling of margin interest is unconfirmed/)).toBeNull();
  });

  it("never removed the per-day/per-year figures — they no longer render at all", () => {
    render(
      <MarginInterestStrip
        account={account(mi({
          this_week_usd: 1,
          current_month_usd: 1,
          current_month_label: "September 2026",
          prior_months: [],
          all_time_usd: 1,
          all_time_since: "2026-09-24",
          is_estimate: true,
          source: "estimate",
        }))}
      />,
    );
    expect(screen.queryByText(/day$/)).toBeNull();
    expect(screen.queryByText(/\/yr/)).toBeNull();
    expect(screen.queryByText("ESTIMATE")).toBeNull();
  });

  it("carries no est. tag when every dollar is broker-confirmed", () => {
    render(
      <MarginInterestStrip
        account={account(mi({
          this_week_usd: 1.5,
          current_month_usd: 1.5,
          current_month_label: "September 2026",
          prior_months: [],
          all_time_usd: 24.5,
          all_time_since: "2026-01-01",
          is_estimate: false,
          source: "broker_actual",
        }))}
      />,
    );
    expect(screen.queryByText("est.")).toBeNull();
  });

  it("shows a certain zero without an est. tag", () => {
    render(
      <MarginInterestStrip
        account={account(mi({
          this_week_usd: 0,
          current_month_usd: 0,
          current_month_label: "September 2026",
          prior_months: [],
          all_time_usd: 0,
          all_time_since: "2026-09-24",
          is_estimate: true,
          source: "estimate",
        }))}
      />,
    );
    expect(screen.getAllByText("$0.00").length).toBeGreaterThan(0);
    expect(screen.queryByText("est.")).toBeNull();
  });

  it("says 'no data yet' as its own honest state, not a fabricated zero", () => {
    render(
      <MarginInterestStrip
        account={account(mi({
          this_week_usd: 0,
          current_month_usd: 0,
          current_month_label: "September 2026",
          prior_months: [],
          all_time_usd: 0,
          all_time_since: "2026-09-24",
          is_estimate: true,
          source: "no_data",
        }))}
      />,
    );
    expect(screen.getByText(/no data yet/)).toBeTruthy();
    expect(screen.queryByText("$0.00")).toBeNull();
  });

  it("says a fault is a fault and never renders it as a zero", () => {
    render(
      <MarginInterestStrip
        account={account({
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
        })}
      />,
    );
    expect(screen.getByText(/not available — no borrowing rate is configured/)).toBeTruthy();
    expect(screen.queryByText("$0.00")).toBeNull();
  });

  it("says so when the account object carries no margin figure at all", () => {
    render(<MarginInterestStrip account={account(null)} />);
    expect(screen.getByText(/Margin interest: not available/)).toBeTruthy();
    expect(screen.queryByText("$0.00")).toBeNull();
  });
});
