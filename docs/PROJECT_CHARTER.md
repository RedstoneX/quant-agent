# Quant Agent Mission Control (QAMC) — Project Charter

## Objective
Build a small, understandable autonomous AI-assisted Alpaca paper-trading experiment that can answer:

> Can inexpensive modern AI models add measurable trading value beyond deterministic market signals, out of sample?

This is not a commercial product and is separate from Quant Factory.

## Governing engineering principle
**Maximum existing functionality + minimum custom development.**

QAMC surrounds quant-agent with better provider flexibility, a browser-based Mission Control, automated journal/search and optional AI observability. It does not become a new trading engine.

## Product layers
1. **quant-agent core** — authoritative agents, Portfolio Manager, AI Risk Manager, deterministic risk/execution, Alpaca Paper, memory, reflection and Meta Reflector.
2. **QAMC backend seam** — minimal provider/config/telemetry/correlation enhancements plus a thin read/control API.
3. **QAMC Mission Control** — native React/Vite/Tailwind web UI using approved donor components and TradingView Lightweight Charts.
4. **Journal/Search** — native derived presentation/index over canonical quant-agent records.
5. *(Retired 2026-08-09: an optional AgentLens observability sidecar was
   evaluated and **dropped** — DECISION #34. Forensic observability is served
   natively by `agent_logs`, `run_id` and `scripts/replay_decision.py`. QAMC
   has no external observability dependency.)*

## Success definition
The experiment can run unattended on Alpaca Paper, expose what each AI agent/PM/risk layer did, show actual execution differences, make model assignment easy, record cost/outcomes, provide a useful daily journal, and remain safe if the UI or observability stack fails.
