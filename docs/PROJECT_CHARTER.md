# Quant Agent Mission Control (QAMC) — Project Charter

## Objective

Build a small, understandable autonomous AI-assisted Alpaca paper-trading experiment that can answer:

> Can inexpensive modern AI models add measurable trading value beyond deterministic market signals, out of sample?

This is a private, noncommercial experiment and is separate from Quant Factory.

## Governing engineering principle

**Maximum existing functionality + minimum custom development.**

QAMC surrounds `yebof/quant-agent` with provider flexibility, experimental attribution, a browser-based Mission Control, and canonical-derived journal/search. It does not become a new trading engine or a second trading-memory system.

## Product layers

1. **quant-agent core** — authoritative specialist agents, Portfolio Manager, AI Risk Manager, deterministic risk/execution, Alpaca Paper, memory, reflection, and Meta Reflector.
2. **QAMC backend seam** — minimal provider/config/telemetry/correlation enhancements plus the accepted thin **read-only** Mission Control API. Any future write operations require a separate explicit authorization.
3. **QAMC Mission Control** — native React/Vite/Tailwind web UI using approved presentation/concept donors selectively and TradingView Lightweight Charts.
4. **Journal/Search** — native derived presentation/index over canonical quant-agent records; rebuildable and non-authoritative.
5. **Forensic observability** — native `agent_logs`, `run_id`/`decision_id`, and replay. AgentLens was evaluated and removed from the plan.

## Success definition

The experiment can run unattended on Alpaca Paper; expose what each AI agent, PM, AI Risk Manager, and deterministic gate did; show proposed-versus-executed differences; record actual model/provider/cost/outcomes; provide a useful Mission Control and daily journal/search experience; and remain safe and operationally independent if the UI/read-side stack fails.

Later model/scheduler/write controls are separate governed work and are not implied by the current read-only Mission Control authorization.
