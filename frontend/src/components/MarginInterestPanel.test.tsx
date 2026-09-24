// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AccountResponse, MarginInterestEstimate } from "../api/client";
import { MarginInterestStrip } from "./MarginInterestPanel";

afterEach(() => {
  cleanup();
});

const ESTIMATE_LABEL =
  "ESTIMATE — paper trading's handling of margin interest is unconfirmed; not an observed broker charge";

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

describe("MarginInterestStrip", () => {
  it("renders the explicit zero state instead of nothing", () => {
    // THE POINT OF THIS COMPONENT. /account has returned this object since
    // 2026-09-01 and no component read it, so the cockpit displayed the
    // figure zero times in 17 days. Owner decision 2026-09-18: "every day,
    // even if it's zero, that way I know it's still working" — an absent
    // figure and a dead tracker are indistinguishable to a reader.
    render(
      <MarginInterestStrip
        account={account({
          debit_balance: 0,
          rate_pct: 6.25,
          daily_usd: 0,
          annual_usd: 0,
          label: null,
          broker_check_note: null,
          days_charged: 1,
          period_usd: 0,
          error: null,
        })}
      />,
    );
    expect(screen.getByLabelText("Margin interest")).toBeTruthy();
    expect(screen.getByText("$0.00")).toBeTruthy();
    expect(screen.getByText(/Nothing borrowed overnight/)).toBeTruthy();
    expect(screen.getByText("6.25%")).toBeTruthy();
    // A certain zero is NOT an estimate — nothing borrowed costs nothing at
    // any rate. The label is reserved for the figure that is genuinely a
    // projection, so it keeps biting where it has to.
    expect(screen.queryByText("ESTIMATE")).toBeNull();
  });

  it("renders every required figure for a carried debit balance, with the ESTIMATE label VISIBLE", () => {
    render(
      <MarginInterestStrip
        account={account({
          debit_balance: 5729,
          rate_pct: 6.25,
          daily_usd: 0.99,
          annual_usd: 358,
          label: ESTIMATE_LABEL,
          broker_check_note: "no INT activity on the account",
          days_charged: 1,
          period_usd: 0.99,
          error: null,
        })}
      />,
    );
    // The five things the owner asked to see: daily, annualised, the
    // balance it is charged on, the rate, and that it is an ESTIMATE.
    expect(screen.getByText("$0.99")).toBeTruthy();
    expect(screen.getByText("$358")).toBeTruthy();
    expect(screen.getByText("$5.7k")).toBeTruthy();
    expect(screen.getByText("6.25%")).toBeTruthy();
    // Standing desk rule: an estimate is never presented as a measurement,
    // and a label a reader has to hover to find has already failed at that.
    // Asserted as rendered TEXT, which a title-attribute tooltip would not
    // satisfy — that is the regression this pins.
    expect(screen.getByText("ESTIMATE")).toBeTruthy();
    expect(screen.getByText(ESTIMATE_LABEL)).toBeTruthy();
    expect(screen.getByText(/Broker check: no INT activity/)).toBeTruthy();
  });

  it("shows the 3-day weekend carry total on a Friday, not just the flat per-day figure", () => {
    // The defect this pins: /account used to return `days_charged`/
    // `period_usd` unset (defaulting to a flat 1-day figure) even on a
    // Friday, while the Telegram alert already named the same debit's
    // real 3-day (Fri+Sat+Sun) weekend carry. Same wording as
    // `src.margin_interest._closure_name`/`format_alert_line`.
    render(
      <MarginInterestStrip
        account={account({
          debit_balance: 5729,
          rate_pct: 6.25,
          daily_usd: 0.99,
          annual_usd: 358,
          label: ESTIMATE_LABEL,
          broker_check_note: null,
          days_charged: 3,
          period_usd: 2.97,
          error: null,
        })}
      />,
    );
    // The flat per-day figure is still shown...
    expect(screen.getByText("$0.99")).toBeTruthy();
    // ...but so is the real 3-day weekend total, so the per-day figure
    // cannot be misread as tonight's whole bill.
    expect(screen.getByText("3 days ≈ $2.97")).toBeTruthy();
    expect(screen.getByText("Over the weekend")).toBeTruthy();
  });

  it("says a fault is a fault and never renders it as a zero", () => {
    // "not available" and "$0.00" are different claims about the world.
    // Printing the reassuring one over a broken read is the exact failure
    // the owner asked to be able to see.
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
