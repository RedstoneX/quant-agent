# QAMC Current Work

Status: **TRADING-UTILITY RECOVERY — PAPER EXPERIMENT ACTIVE**

The finish-line rollout is complete and production remains on the accepted Paper-only target. The current priority is no longer passive soak observation: it is to determine why QAMC performs substantial analysis but rarely converts legitimate opportunities into meaningful market exposure, and to correct the causes that are justified by evidence.

## Goal

Using natural Alpaca Paper evidence, make QAMC reliably:

**find opportunity → evaluate it → make a defensible bullish, bearish or neutral decision → execute when eligible → manage/exit the position → measure the result.**

Success is **not** “more trades.” Do not force activity, weaken safety, or tune to hindsight. When QAMC does not trade, the reason must be specific and defensible.

## Authorized work

Claude has broad autonomy inside the accepted architecture to investigate and improve the full path:

**discovery → Specialists → Portfolio Manager → AI Risk Manager → deterministic gate → SGOV funding → broker execution → position/exit management → reflection.**

Start from real production/paper evidence. Quantify where candidates disappear or are vetoed. Fix evidence-supported causes such as discovery/ranking quality, stale or incomplete evidence, prompt/decision paralysis, configuration that is unintentionally too restrictive, PM/Risk interaction problems, funding/execution defects, and position-management defects.

Do not preserve a prior choice merely because it already exists if it is preventing the product outcome. Material architecture or safety changes still require operator approval.

## Required evidence

At each meaningful checkpoint, keep the durable record concise:

**Finding → evidence → decision → change → verification → remaining uncertainty.**

Use Git commits/PR history for detail; do not create another governance/status system.

Before declaring the recovery successful, demonstrate from natural paper-market evidence that QAMC can detect and act on worthwhile opportunities in both supported bullish and bearish conditions, while also producing defensible no-trade decisions when appropriate.

## Secondary product debt

Mission Control has serious semantic/usability issues, including candidate/run attribution and misleading liquidity presentation. Fix read-side correctness required to diagnose trading behavior, but defer broad dashboard redesign until the trading-utility root causes are understood.

## Hard boundaries

- Alpaca **Paper only**.
- No margin, options or direct stock shorting.
- Bearish expression remains through approved inverse ETFs.
- Deterministic Python/broker protections remain final safety authority.
- Do not force/manufacture trades or weaken safeguards merely to increase activity.
- No new daemon/service/database/proxy/security/credential architecture without explicit approval.
- Preserve `dev` / `qamc` / `ubuntu` isolation and OneCLI secret handling.
- Mission Control remains private/read-only; Telegram remains output-only.
- Claude does not merge or deploy its own work; external review remains required.
