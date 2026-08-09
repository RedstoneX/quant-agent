# QAMC — Plain-English Operator Summary

> [!note] Personal overview — non-governing
> This page is for the operator’s own orientation and explanation to family/friends. It is **not** architecture, an implementation instruction, or a source of technical authority.

**Quant Agent Mission Control (QAMC) is being built as an autonomous AI-assisted paper-trading system.** It watches the market, has several AI specialists independently analyze technical signals, news, macro conditions and company fundamentals, then has a Portfolio Manager combine those views and an AI Risk Manager challenge the proposed decisions.

The AI can analyze and recommend, but it **cannot bypass the hard safety rules**. Deterministic Python risk controls decide what is actually allowed to trade, and orders go only to **Alpaca Paper Trading** while the experiment is being developed and tested.

When the system is complete, it is intended to:
- run largely unattended on a small server;
- have multiple AI specialists debate each trading opportunity;
- turn their analysis into a portfolio decision and independent AI risk review;
- enforce hard limits for sizing, exposure, losses, stops and execution;
- place and manage paper trades automatically through Alpaca;
- record what every agent said, which AI model actually answered, what was proposed, what was executed, and what happened afterward;
- review its results and use structured reflection to propose improvements without allowing the AI to rewrite safety controls on its own;
- provide a browser/iPad **Mission Control** showing positions, orders, trades, agent agreement/disagreement, risk decisions, journal/history, performance and learning activity.

**The experiment’s real purpose:** determine whether inexpensive modern AI models add measurable trading value **out of sample**, beyond ordinary deterministic trading signals — not merely whether the AI can produce convincing explanations.

> [!important] Where we are now
> The underlying `quant-agent` engine already provides much of the autonomous trading, risk, persistence and reflection machinery. QAMC is still in early implementation: **Stage 0 audit/design is complete; Stage 0.5 is next.** Live-money trading is **not authorized**.
