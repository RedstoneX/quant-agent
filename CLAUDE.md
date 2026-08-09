# QAMC — Claude Code Contract

QAMC is a private Alpaca paper-trading experiment built around `yebof/quant-agent`.

## Start

Read `docs/STATE.md` first.

For substantial work:
- use `/qamc-discover` when discovery/architecture challenge is authorized;
- use `/qamc-build` only when `docs/STATE.md` and `docs/WORK.md` explicitly authorize implementation.

Load only what the task needs. Do **not** preload the whole repository, `docs/PROJECT_COMPASS.md`, `docs/history/`, or `docs/reference/`.

Current machine-facing authority is intentionally small:
- `docs/OUTCOME.md` — desired result and hard product constraints;
- `docs/STATE.md` — accepted state and current authorization;
- `docs/WORK.md` — the one current work/handoff contract;
- `docs/ROADMAP.md` — sequencing only, when relevant;
- relevant accepted contracts under `docs/architecture/`.

## Hard boundaries

- Alpaca **Paper only**; live trading is not authorized.
- `yebof/quant-agent` remains the authoritative trading engine.
- Specialist agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution remains the decision chain.
- Deterministic Python and broker protections own final safety/execution eligibility; uncertainty fails closed.
- Mission Control/API/journal/search/UI remain non-critical to trading and read-only unless a future accepted work contract explicitly says otherwise.
- No secrets or fake production trading state in API/UI/client artifacts.
- Derived UI/search/journal state is rebuildable and non-authoritative.
- Avoid unnecessary infrastructure and gratuitous trading-core rewrites; preserve upstream mergeability.

Path-scoped rules add detail only when relevant files are touched.

## Decision discipline

- Repository fact → investigate it.
- Routine engineering choice → decide it.
- Genuine operator product/value trade-off → ask one question at a time.
- Material conflict with accepted architecture/safety/scope → record it in `docs/WORK.md` for external reconciliation.

Do not turn the operator into the technical architect.

## Git and continuity

Git is durable QAMC memory. Project auto-memory is disabled. A new Claude session rehydrates from the repository, not from old chat transcripts. A usage reset is not a context reset.

Use dedicated branches. Never force-push, push implementation directly to `main`, or merge a PR from Claude Code. Discovery/implementation handoffs must be committed and pushed before external review.

Do not create new handoff/status/governance documents. `STATE.md` owns current authorization, `WORK.md` owns the current handoff, `ROADMAP.md` owns sequencing, and `PROJECT_COMPASS.md` is a human-only projection.
