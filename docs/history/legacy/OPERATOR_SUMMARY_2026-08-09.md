# 🤖 QAMC — What I’m Building, in Plain English

> [!note] 👤 For me, not the agents
> This is my simple explanation of the project so I can keep the big picture in sight and explain it to someone else. It is **not** a technical specification or an instruction to the trading system.

## 🎯 The 20-second explanation

**I’m building an autonomous AI-assisted paper-trading system that behaves more like a small virtual investment desk than a single trading bot.**

Several specialist AI agents independently study the same market from different angles. They are forced through structured reasoning rather than being allowed to jump straight to an answer. Their opinions can disagree. A Portfolio Manager then has to combine the evidence into an actual portfolio plan, a separate AI Risk Manager challenges that plan, and finally **hard computer-coded safety rules** decide what is actually allowed to trade.

For now it trades **fake money only through Alpaca Paper Trading** while we test the real question: **does the AI actually improve trading decisions, or does it merely sound intelligent?**

---

## ✨ The killer features

## 🧠 The “secret sauce” — a virtual investment committee

This is probably the most important idea in the whole project.

I am **not asking one chatbot “Should I buy this stock?”** The underlying `quant-agent` engine is built around specialized agents with defined jobs and structured outputs. In the main morning session, several of those specialists can analyze the market in parallel and then feed their evidence into the Portfolio Manager.

The useful part is that their views stay separate long enough for me to see **where the evidence agrees, where it conflicts, and what ultimately changed the decision**.

### 📈 Technical Agent — “What is the market actually doing?”
It studies the setup itself:

- trend and price action
- momentum
- volatility
- volume
- support and resistance
- how stale or fresh a signal is
- whether the potential reward is worth the risk

A key detail from the underlying engine: some important numbers, such as **risk/reward**, are calculated by Python rather than trusted to whatever number the AI happens to invent.

So this agent can say, in effect: **“The chart is attractive, here is my conviction, here is what would invalidate the idea, and here is the evidence.”**

### 📰 News Intelligence Agent — “What changed that the chart may not know yet?”
This is more than a headline summarizer. The upstream design separates news reasoning into layers:

- the persistent broader market narrative
- meaningful **state changes** — something genuinely changed rather than another copy of yesterday’s story
- ticker-specific alerts and catalysts

That gives the other agents a way to distinguish **important new information from background noise**.

### 🌎 Macro Agent — “What kind of market are we actually trading in?”
It looks above the individual ticker at the larger environment:

- market volatility
- interest rates and the yield curve
- monetary conditions
- inflation and labour conditions
- credit stress
- whether the regime is becoming more risk-on or risk-off
- which sectors the environment may favour or hurt

It also remembers the previous regime, so it can identify a **regime shift** rather than treating every day as a blank slate.

A beautiful chart in a hostile macro environment may deserve much less capital.

### 🏢 Earnings / Fundamentals Agent — “Does the company itself support this story?”
For companies with new filings, the engine can analyze actual **10-Q / 10-K information** rather than relying only on price and headlines.

It looks at things such as:

- revenue and margins
- cash flow
- strategic direction
- competitive position
- business risks
- whether management is executing
- whether the latest filing strengthens or weakens the investment thesis

So the system can catch the situation where **the chart says BUY but the company evidence says something is deteriorating underneath it**.

---

## 👔 The Portfolio Manager — where the agents become a decision

This is much more interesting than simply taking a vote.

The Portfolio Manager receives the specialists’ evidence and has to build an actual portfolio plan. But it also gets **memory**, so it does not wake up every morning with amnesia.

The upstream engine gives it context that includes things like:

- 📍 **today’s signals**
- 📖 the recent **portfolio narrative**
- 🔥 important recent high-conviction state changes
- 📊 how recent trades have actually performed
- 🛡️ the last several Risk Manager verdicts — so it can notice if Risk keeps cutting its sizing
- 🔁 its own recent decisions — helping expose flip-flopping
- 🧺 a preview of what the portfolio would look like if all the proposed buys were accepted, including concentration warnings

That means the PM is not merely asking **“Is NVDA attractive today?”** It is closer to:

> “Given what all of my analysts are saying, what we already own, what has worked or failed recently, how Risk has been responding to me, and what the portfolio would look like afterward — what should I actually do?”

It then produces proposed positions rather than directly inventing broker orders.

### 🔢 Another important boundary: the AI does not get to invent everything
The Portfolio Manager can express **intent**, but deterministic code translates that intent into actual trade mechanics using live prices and rules.

The LLM is not supposed to freestyle things such as:

- exact share counts
- actual entry prices
- stop geometry
- whether the account has enough cash

That separation is one of the reasons I like this project as an experimental base.

---

## 🛡️ AI Risk Manager — “Now argue against the Portfolio Manager.”

After the Portfolio Manager proposes a plan, a **different AI** reviews it.

Its job is not to congratulate the PM. It can:

- ✅ approve a trade
- ❌ veto it
- 📉 scale the buying down
- ⚠️ flag risks the PM underweighted
- challenge poor risk/reward
- make symbol-by-symbol modifications

So there is an **AI proposer and a separate AI challenger**. That is much more useful experimentally than letting one model make a decision and then grade its own homework.

---

## 🔒 Then the AI discussion ends: hard risk rules take over

Even after both AI layers agree, ordinary Python has final authority.

The hard risk engine can block orders for things such as:

- insufficient cash
- oversized positions
- too much sector exposure
- too much total exposure
- daily-loss limits
- missing stop protection
- correlation/concentration problems

And the actual trade constructor turns approved intent into executable orders.

**The AI can persuade another AI. It cannot persuade the hard safety rules.**

---

## 🔄 It doesn’t stop thinking after the morning trade

Another thing I like about the underlying project is that the trading day has a **rhythm**, not just one morning prediction.

### 🚨 During the day — a non-AI circuit breaker
The system can check daily P&L repeatedly without spending an LLM call. If the loss cap is breached, deterministic safety logic can act.

### 🔎 Position Reviewer — “Has the thesis actually changed?”
Later in the day, another agent reviews positions already held.

The philosophy is intentionally different from a nervous day-trading bot constantly changing its mind. It is supposed to ask whether a **named thesis-breaking trigger** has occurred rather than selling merely because the price wobbled.

It can decide to:

- HOLD
- REDUCE
- SELL
- adjust protection / trail a stop

That gives the system a second look at existing positions without pretending every half-hour is a brand-new investment thesis.

### 🌙 Evening Analyst — “What actually happened, and were we right?”
After the market closes, the system reviews the day.

This is where it becomes particularly interesting as an experiment. The engine can grade things such as:

- whether buys were **correct, premature or wrong**
- whether the thesis was strengthening, intact, weakening or broken
- whether yesterday’s market outlook actually matched what happened
- which opportunities were missed
- what risks matter tomorrow

Importantly, some of that grading is tied to **actual realized market/P&L data**, rather than simply asking the AI to declare itself successful.

The following morning, parts of that history feed back into the Portfolio Manager.

---

## 🧬 Meta Reflector — learning over months, not just one bad trade

The underlying engine also has a longer-horizon **Meta Reflector** designed to review roughly a quarter of accumulated experience.

It can look for patterns such as:

- themes the agents caught versus missed
- recurring loss patterns
- which agents were useful or unhelpful
- signal hit rates
- repeated weaknesses in the prompts

It can then **propose improvements to agent prompts**.

The important word is *propose*. QAMC’s policy is deliberately conservative: learning remains inspectable and controlled, and the AI does **not** get permission to rewrite the deterministic safety system on its own.

---

## 🔁 The secret sauce in one picture

**market + filings + news**  
⬇️  
📈 Technical + 📰 News + 🌎 Macro + 🏢 Fundamentals  
⬇️  
🤔 **independent opinions + visible disagreement**  
⬇️  
👔 **Portfolio Manager + memory of what happened before**  
⬇️  
🛡️ **AI Risk Manager challenges / vetoes / resizes**  
⬇️  
🔒 **deterministic Python risk gate**  
⬇️  
🏦 **Alpaca Paper Trading**  
⬇️  
🔎 **Position Reviewer**  
⬇️  
📔 **Journal + Evening Analyst**  
⬇️  
🧬 **longer-term Meta reflection**

That loop — **specialists → disagreement → synthesis → challenge → hard safety → outcome → grading → learning** — is what makes this much more interesting to me than a normal “AI stock picker.”

---

## 🪪 Agent Cards — seeing the virtual committee think

Mission Control will turn those agent opinions into clear visual cards instead of walls of terminal logs.

Each card can show things like:

- the agent’s opinion / bias
- conviction
- reasoning
- key evidence
- the factor it thinks matters most
- agreement or disagreement with the other agents

The dashboard should also show a **consensus / disagreement view**, so I can immediately tell whether the virtual committee is strongly aligned or fighting with itself.

That means I can see **why the system reached a decision**, not just the final BUY / SELL / HOLD.

---

## 📊 Mission Control — the cockpit

The browser/iPad dashboard is intended to put the whole operation in one place:

- account value and cash
- open positions
- orders and trades
- what every AI agent is saying
- agent agreement/disagreement
- Portfolio Manager decisions
- Risk Manager approvals/rejections/resizing
- performance
- AI model usage and cost
- system health
- reflection and learning activity

Think of it as the **cockpit for the virtual trading desk**.

### 📉 TradingView-style charts
Mission Control will use **TradingView Lightweight Charts**, so I can inspect price action and trades visually rather than staring at database rows.

Entries, exits and other useful information can eventually be shown directly against the market chart.

---

## 📔 The automatic trading journal — the story of every trading day

This is one of the features I care about most.

Every trading day should build a journal showing:

- what the market looked like
- what opportunities the agents found
- what each specialist thought
- where they agreed and disagreed
- what prior memory/history mattered
- what evidence changed the Portfolio Manager’s mind
- what the Portfolio Manager proposed
- what Risk approved, rejected or reduced
- what the deterministic rules ultimately allowed
- what actually got executed
- how the trades turned out
- how the Evening Analyst graded them
- what the system thinks it learned afterward

So instead of trying to remember **“Why did it make that trade three weeks ago?”**, I should be able to open that day and reconstruct the whole story.

### 🔍 Inspect the AI decision trail
The underlying system already stores agent prompts/responses and related records. QAMC will make those records much easier to inspect.

I should be able to drill into a decision and see **which AI model actually answered, what it was asked, what it said, what was proposed, what Risk changed, what was executed, and what happened afterward**.

---

## 🧪 The real experiment: does any of this actually help?

This is not being built merely because an elaborate AI trading desk sounds cool.

The real question is:

> **Does inexpensive modern AI add measurable out-of-sample trading value beyond ordinary deterministic signals?**

Because the agents, models, decisions and outcomes are recorded, I should eventually be able to compare whether certain agents/models actually **helped, hurt, or simply produced convincing commentary**.

If AI adds no value, the experiment should be capable of showing that too.

---

## 🤖 The end goal: largely autonomous, but inspectable

The goal is for the system to run on a small Linux server and handle most of its routine work without me babysitting it:

**watch → analyze → disagree → synthesize → challenge → risk-check → paper trade → monitor → journal → grade → learn**

I should be able to check the whole operation from a browser or iPad rather than keeping a desktop trading application alive all day.

---

## 🧩 What already exists vs. what QAMC is adding

The good news is that I am **not building this trading engine from scratch**.

The open-source `quant-agent` project already provides a surprisingly large amount of the hard machinery: specialized agents, structured reasoning, daily scheduled operation, Alpaca execution, deterministic risk controls, position review, persistent records, evening grading/reflection and longer-term Meta reflection.

QAMC is adding the pieces that make it a much more understandable and measurable personal experiment — especially **better model/provider control, accurate AI attribution, Mission Control, Agent Cards, visible disagreement, TradingView charts, the journal, search and forensic inspection of decisions**.

---

> [!important] 🚧 Where it stands right now
> **Stage 0 is complete.** We audited the existing engine and froze the architecture before changing trading code.
>
> **Stage 0.5 is next:** a very small correctness fix so the history records the **AI model that actually answered**, including when a fallback model was used.
>
> The full Mission Control dashboard, Agent Cards and journal are **planned QAMC features, not finished yet**.
>
> 💵 **No live-money trading is authorized.**