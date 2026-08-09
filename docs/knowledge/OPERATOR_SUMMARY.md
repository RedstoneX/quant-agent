# 🤖 QAMC — What I’m Building, in Plain English

> [!note] 👤 For me, not the agents
> This is my simple explanation of the project so I can keep the big picture in sight and explain it to someone else. It is **not** a technical specification or an instruction to the trading system.

## 🎯 The 20-second explanation

**I’m building an autonomous AI-assisted paper-trading system that acts a bit like a small virtual trading desk.**

Instead of one AI making a guess, several specialist AI agents look at the market from different angles, debate the opportunity, a Portfolio Manager forms a plan, an AI Risk Manager challenges it, and then **hard computer-coded safety rules** decide what is actually allowed to happen.

For now, it trades **fake money only through Alpaca Paper Trading** while we test whether the AI is genuinely useful.

---

## ✨ The killer features

### 🧠 A team of AI specialists
Different agents have different jobs — for example:

- 📈 **Technical Agent** — charts, price action and indicators
- 📰 **News Agent** — what is happening around a company or market
- 🌎 **Macro Agent** — rates, economy and broader market conditions
- 🏢 **Fundamental Agent** — earnings, filings and company fundamentals
- 👔 **Portfolio Manager** — combines the evidence into an actual trading thesis
- 🛡️ **AI Risk Manager** — independently challenges the proposed trade

The point is not just “ask ChatGPT whether to buy.” It is a **structured decision process with disagreement visible**.

### 🪪 Agent Cards
Mission Control will show each agent as a clear card with things like:

- its opinion / bias
- conviction
- reasoning
- key evidence
- agreement or disagreement with the other agents

So I can see **why the system reached a decision**, not just the final BUY / SELL / HOLD.

### 📊 A real Mission Control dashboard
The system will have a browser/iPad dashboard where I can see the whole operation in one place:

- account value and cash
- open positions
- orders and trades
- what each AI agent is saying
- Portfolio Manager decisions
- Risk Manager approvals/rejections
- performance
- model usage and cost
- system health
- learning/reflection activity

Think of it as the **cockpit for the trading system**.

### 📉 TradingView-style charts
The dashboard will use **TradingView Lightweight Charts**, so I can inspect price action and trades visually instead of staring at database rows or terminal output.

Trades, entries/exits and other useful information can eventually be shown directly against the market chart.

### 📔 An automatic trading journal
This is one of the features I care about most.

Every trading day should build a journal showing:

- what the market looked like
- what opportunities the agents found
- what each agent thought
- where they agreed and disagreed
- what the Portfolio Manager proposed
- what Risk approved, rejected or reduced
- what actually got executed
- how the trades turned out
- what the system learned afterward

So instead of trying to remember **“Why did it make that trade three weeks ago?”**, I should be able to open that day and reconstruct the whole story.

### 🔍 Inspect the AI decision trail
The system already records the agents’ prompts and responses. QAMC will make those records much easier to inspect.

I should be able to drill into a trading decision and see **which AI model answered, what it was asked, what it said, what was proposed, what was actually executed and what happened afterward**.

### 🧪 It can test whether AI actually adds value
This is not being built merely because AI trading sounds interesting.

The real experiment is:

> **Does inexpensive modern AI improve trading results out-of-sample compared with ordinary deterministic signals?**

If it does not, the experiment should be able to show that too.

### 🧠 Reflection and learning — but controlled
The underlying engine can review trading results and reflect on what worked or failed.

Over time it can **propose improvements to its prompts/approach**, but it is deliberately not allowed to rewrite the hard safety system on its own.

### 🤖 Largely autonomous operation
The end goal is for it to run on a small Linux server and do most of its routine work without me babysitting it:

**watch → analyze → debate → decide → risk-check → paper trade → record → review → learn**

I should be able to check it from a browser or iPad rather than keeping a desktop trading program alive all day.

---

## 🛡️ The important safety idea

The AI gets to **think**. It does **not** get unlimited authority to trade.

Deterministic Python rules remain the final gate for things such as:

- position size
- available cash
- exposure
- concentration
- daily-loss limits
- stops
- whether an order is actually eligible to execute

If the risk system fails, the design is to **fail closed rather than trade anyway**.

And during development, **live-money trading is not authorized**.

---

## 🧩 What already exists vs. what I’m adding

The good news is that I am **not building the trading engine from scratch**.

The open-source `quant-agent` project already provides much of the hard machinery: scheduled autonomous operation, Alpaca paper execution, AI agents, risk controls, persistent records, reflection and longer-term learning.

QAMC is adding the pieces that make it a much more understandable and usable experiment — especially **better model/provider control, accurate AI attribution, Mission Control, Agent Cards, TradingView charts, the journal, search and forensic inspection of decisions**.

---

> [!important] 🚧 Where it stands right now
> **Stage 0 is complete.** We audited the existing engine and froze the architecture before changing trading code.
>
> **Stage 0.5 is next:** a very small correctness fix so the journal/history records the **AI model that actually answered**, including when a fallback model was used.
>
> The full Mission Control dashboard, Agent Cards and journal are **planned features, not finished yet**.
>
> 💵 **No live-money trading is authorized.**