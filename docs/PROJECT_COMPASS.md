# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🎯 What QAMC is

QAMC is an autonomous AI-assisted Alpaca Paper trading experiment that acts like a small virtual trading desk.

Specialists analyze the market, a Portfolio Manager synthesizes the evidence, AI Risk challenges the plan, and deterministic Python decides what is allowed to execute.

The experiment asks:

> **Does inexpensive modern AI add measurable out-of-sample trading value beyond ordinary deterministic signals?**

This is a **paper-only experiment**. Live-money trading is not authorized. The account itself is more capable than the system currently uses: it carries a 4x margin limit and 96 of its 101 symbols can be sold short. Margin is deliberately left unused; short selling is being built now.

## 🛂 What is allowed and what still needs approval

**Currently approved:**
- Paper trading with deterministic signals, AI analysis, and risk management
- Inverse ETF positions (`SH`, `SDS`, `PSQ`, `SQQQ`) to express bearish views
- Margin account operations (currently used unlevered)

**Still requires explicit owner approval:**
- Live capital deployment
- New paid external dependencies
- Secrets or credential redesign
- Material architecture changes outside current authority

## 📍 How bearish views work today

Right now, the system expresses a bearish market view by holding inverse ETFs. These are funds whose value moves in the opposite direction from their underlying indexes. The code handles their leverage correctly—a 3x fund like `SQQQ` is sized at three times its notional value, not treated as an ordinary position.

This approach works today. But it is a temporary solution. Real short selling of individual stocks is being built to replace it, which will give finer control and better transparency. Shorting is already enabled at the broker with easy-to-borrow terms on 96 stocks. The transition will happen in stages as the safety logic is validated.

## ✅ What is already built

The core system is solid:
- Real continuous integration gates the production code
- Governance corrections are in place
- Structural risk modeling is complete
- Risk measurement and sizing logic is complete, including correlation awareness and volatility-based stop widths
- Exit logic has been rewritten
- Execution fixes are complete
- An alarm system watches for deploy drift
- Mission Control, the trading cockpit, is live and read-only with a Research Intelligence Desk (agent research, disagreement markers, PM/Risk deltas, Smart Money evidence tracking) and a Smart Money seat for deliberation

## 🔨 What is in flight

- Short selling stages 2 and 3 (safety validation and live deployment)
- Cockpit trader-view improvements
- News feed reliability (Reuters and AP feeds are currently dead; dead feeds log a warning rather than blocking the system)
- Cost-circuit defects that impacted operations

## 🗺️ What comes next

The remaining milestones, in order:

1. **The desk actually deliberates** — seats nominate trades with evidence, disagree, and are adjudicated by the Portfolio Manager. This moves QAMC from a bot running technical analysis to something resembling a real trading desk with intellectual synergy.
2. **Bounded re-peg toward the moving NBBO** — executing tighter to the best bid and offer.
3. **Evidence symmetry and transparency** — short selling fully operational and auditable.
4. **Measurement** — a backtester and conviction calibration to answer "is any of this working" with evidence, not opinion.

## 👥 How the work is organized

- **`ubuntu` — engineering/operator.** Codex/Claude, Git/GitHub, development, tests, deployment.
- **`qamc` — runtime only.** Production checkout and QAMC Paper execution.

**Paper-beta engineering is end-to-end autonomous:** diagnose → implement → test → PR → merge → deploy → verify. There is no mandatory external code-review gate while on paper. Cheap fast models do bounded work; strong models do trading logic, safety, and architecture.

## 📊 Mission Control

The accepted cockpit is live and read-only. It uses Tremor/TanStack for UI, Lightweight Charts for price and trade visualization, and Dockview for the desktop workspace.
