# Future ideas — not scheduled, not authorized

Owner ideas parked for later. Nothing here is work. Nothing here goes into WORK.md until the owner schedules it.

## Options desk cloned from QAMC

**Recorded 2026-09-14. Owner idea.**

**Verdict: a new desk, not a setting.** The plumbing is bounded. The trading logic is most of the work.

### Broker facts (Alpaca docs, fetched 2026-09-14)

- Paper accounts have options enabled by default.
- Levels: 1 = covered calls / cash-secured puts; 2 = + buying calls and puts; 3 = + spreads.
- Orders: market, limit, stop, stop-limit. Stops single-leg only. Time-in-force day or gtc only.
- Whole contracts only. No extended hours.
- In-the-money contracts auto-exercise at expiry; assignment is visible by REST polling only, no websocket.
- Data: free "indicative" feed (quotes modified, trades delayed 15 min); real OPRA feed is paid. Chain snapshots return Greeks and implied volatility on both.
- Historical options data only goes back to February 2024.

### Plumbing (mechanical)

- Options data client: chains, snapshots, contract symbols. Today the code uses stock data clients only.
- Order path: contract quantities, x100 multiplier, no fractional, no overnight-session orders.
- Positions, P&L and journal: premium, expiry dates, exercise and assignment handling.
- Measured 2026-09-14: 47 source files touch share quantity, fractional or stop logic.

### Hard part (judgement)

- Every agent, gate and grader reasons in stock price levels. Options add strike, expiry, implied volatility and time decay.
- R/R and sizing change shape: a bought option's maximum loss is the premium, not stop distance.
- Exits change: time decay works against the horizon; broker stops on option prices are unproven for this desk.
- Validation rig and benchmark need new options fixtures; history is short (Feb 2024).
- Real quotes need the paid OPRA feed — a paid-dependency decision for the owner.

### Suggested cheapest first step (unratified)

- Keep QAMC's stock analysis unchanged. Add one translation layer: stock thesis -> buy a call or put (Level 2 only).
- No spreads, no selling options, paper only, until that layer has its own evidence.
