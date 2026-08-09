# 🤖 QAMC — What I’m Building, in Plain English

> [!note] 👤 For me, not the agents
> This is my simple explanation of the project so I can keep the big picture in sight and explain it to someone else. It is **not** a technical specification or an instruction to the trading system.

## 🎯 The 20-second explanation

**I’m building an autonomous AI-assisted paper-trading system that acts a bit like a small virtual trading desk.**

Instead of one AI making a guess, several specialist AI agents look at the same market from different angles, form their own opinions, and expose where they agree or disagree. A Portfolio Manager then turns that evidence into a trading plan, an AI Risk Manager challenges the plan, and finally **hard computer-coded safety rules** decide what is actually allowed to happen.

For now, it trades **fake money only through Alpaca Paper Trading** while we test whether the AI is genuinely useful.

---

## ✨ The killer features

## 🧠 The “secret sauce” — a small virtual investment committee

This is probably the most important idea in the whole project.

I am **not asking one chatbot “Should I buy this stock?”** Each AI agent gets a specific job and is supposed to look at the opportunity through that lens. Their views remain visible instead of being blended into one mysterious AI answer.

### 📈 Technical Agent — “What is the market actually doing?”
Looks at the trading setup itself:

- price action and trend
- momentum and technical indicators
- support/resistance and market structure
- whether the setup looks strong, weak or conflicted

It may love a chart even when the news or macro picture is ugly — and that disagreement is useful information.

### 📰 News Agent — “What just happened that could move this?”
Looks for the narrative and catalysts around the opportunity:

- important company/market news
- recent events and catalysts
- whether headlines support or contradict the trade thesis
- whether something has changed that a chart alone would miss

This helps stop the system from treating price data as if it exists in a vacuum.

### 🌎 Macro Agent — “What kind of market are we trading in?”
Looks above the individual ticker at the larger environment:

- interest rates and economic conditions
- broad market tone
- risk-on / risk-off conditions
- whether the macro backdrop helps or hurts the proposed trade

A setup that looks attractive in isolation may be much less attractive in the wrong market regime.

### 🏢 Fundamental Agent — “Does the company story support the trade?”
Looks at company-level evidence such as:

- earnings and business performance
- filings and fundamentals
- company-specific strengths/weaknesses
- whether the underlying business evidence supports or fights the shorter-term trade

This is another independent perspective — not a replacement for the technical signal.

### 👔 Portfolio Manager — “Given all of that, what should we actually do?”
This is where the separate opinions become a proposed decision.

The Portfolio Manager sees the specialists’ evidence and disagreement and has to synthesize it into a coherent portfolio thesis:

- BUY / SELL / HOLD type decision
- how strong the opportunity appears
- which evidence matters most
- how the different agents’ views fit together
- whether the trade deserves capital at all

The important part is that I can later see **how it got from several competing opinions to the proposed trade**.

### 🛡️ AI Risk Manager — “Tell me why we should NOT do this.”
Then a different AI gets a deliberately different job: challenge the proposed decision rather than cheer it on.

It can:

- approve the idea
- reject it
- reduce/scale the proposed exposure
- raise warnings
- identify risks the Portfolio Manager may have underweighted

So there is an **AI proposer and an AI challenger**, not just one model grading its own homework.

### 🔒 Deterministic Risk Gate — “AI discussion is over; these are the rules.”
After all of the AI reasoning, ordinary Python code has final authority.

It checks hard limits such as:

- available cash
- position size
- portfolio exposure
- concentration
- daily-loss limits
- stops
- final order eligibility

The AI can persuade another AI. It **cannot persuade the hard safety rules**.

### 🔄 And the system keeps reviewing itself
The existing engine also has agents/processes for reviewing positions and what happened after the trading day:

- 🔎 **Position Reviewer** — revisits existing positions rather than treating every decision as brand new
- 🌙 **Evening Analyst / reflection** — looks back at the day and what worked or failed
- 🧬 **Meta Reflector** — over longer periods, analyzes the accumulated experience and can propose improvements to the AI prompts/approach

For now, those learning changes stay controlled and reviewable. They do **not** get permission to rewrite the deterministic safety layer on their own.

### 🔁 In one line, the decision chain is:

**market data → specialist opinions → visible disagreement → Portfolio Manager thesis → AI Risk challenge → hard risk gate → Alpaca paper order → journal → reflection/learning**

That combination — **specialized perspectives + disagreement + synthesis + independent challenge + deterministic final authority** — is the part I think could make this materially more interesting than a normal “AI trading bot.”

---

### 🪪 Agent Cards
Mission Control will turn those agent opinions into clear visual cards rather than walls of logs.

Each card can show things like:

- the agent’s opinion / bias
- conviction
- reasoning
- key evidence
- the factor it thinks matters most
- agreement or disagreement with the other agents

The dashboard should also show a **consensus / disagreement view**, so I can immediately see whether the virtual committee is strongly aligned or fighting with itself.

So I can see **why the system reached a decision**, not just the final BUY / SELL / HOLD.

### 📊 A real Mission Control dashboard
The system will have a browser/iPad dashboard where I can see the whole operation in one place:

- account value and cash
- open positions
- orders and trades
- what each AI agent is saying
- agent agreement/disagreement
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
- what each specialist thought
- where they agreed and disagreed
- what evidence changed the Portfolio Manager’s mind
- what the Portfolio Manager proposed
- what Risk approved, rejected or reduced
- what the deterministic rules ultimately allowed
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

Because the agents, models, decisions and outcomes are recorded, I should eventually be able to compare whether certain agents/models actually helped, hurt or simply produced convincing commentary.

If AI does not add value, the experiment should be able to show that too.

### 🧠 Reflection and learning — but controlled
The underlying engine can review trading results and reflect on what worked or failed.

Over time it can **propose improvements to its prompts/approach**, but it is deliberately not allowed to rewrite the hard safety system on its own.

### 🤖 Largely autonomous operation
The end goal is for it to run on a small Linux server and do most of its routine work without me babysitting it:

**watch → analyze → debate → decide → challenge → risk-check → paper trade → record → review → learn**

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

QAMC is adding the pieces that make it a much more understandable and measurable experiment — especially **better model/provider control, accurate AI attribution, Mission Control, Agent Cards, visible disagreement, TradingView charts, the journal, search and forensic inspection of decisions**.

---

> [!important] 🚧 Where it stands right now
> **Stage 0 is complete.** We audited the existing engine and froze the architecture before changing trading code.
>
> **Stage 0.5 is next:** a very small correctness fix so the journal/history records the **AI model that actually answered**, including when a fallback model was used.
>
> The full Mission Control dashboard, Agent Cards and journal are **planned features, not finished yet**.
>
> 💵 **No live-money trading is authorized.**