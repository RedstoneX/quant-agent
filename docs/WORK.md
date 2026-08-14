# QAMC Current Work

Status: **PAPER SOAK ACTIVATION**

## Goal

Enable the existing scheduled **Alpaca Paper** timers, verify the schedule and runtime health, then begin observing real paper-trading behaviour. Do not insert another intelligence/model/dashboard tranche before soak start unless activation itself exposes a genuine blocker.

The operator has already authorized the paper soak.

## Commissioning is closed

The final `qamc` live commissioning run against current `main` passed on 2026-08-14:

- **37 passed / 0 failed / 0 warned / 1 skipped**
- `COMMISSIONING ACCEPTANCE: PASS`
- `EXIT=0`

The single SKIP is the intentionally `dev`-only off-account credential-isolation check. The earlier green `dev` run already proves that boundary. Full commissioning is therefore accepted by union of the two account runs.

Also closed:

- stages 0–5 accepted/deployed;
- Mission Control private/read-only and sufficient for initial soak observability;
- OneCLI credential path green;
- Alpaca Paper account/data/quote/calendar green;
- FRED green;
- both accepted OpenRouter policy models complete live calls;
- runtime DB/paper/broker health green;
- all seven trading timers confirmed disabled before activation;
- PR #33 timer-state parser fix merged and verified;
- deterministic risk/execution semantics unchanged.

## Immediate work

Activate the existing QAMC trading timers under the `qamc` user and verify:

1. the intended timer units are enabled and scheduled;
2. Alpaca remains Paper-only;
3. Mission Control and broker health remain green;
4. no unrelated service or public exposure is introduced.

Activation is routine deployment under the already-recorded operator authorization, not a new architecture/product gate.

## After activation

Use actual soak evidence to prioritize the next work:

- positions selected and rejected;
- specialist disagreement and evidence quality;
- PM/RM reasoning and vetoes;
- deterministic blocks;
- order/fill behaviour;
- cost/latency/model attribution;
- missed opportunities and performance;
- Mission Control usability during real sessions.

Do **not** prioritize speculative polish over observed failures or learning from the soak.

## Hard boundaries

- Alpaca **Paper only**; no live trading.
- Preserve Specialist Agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution.
- No deterministic risk/execution semantic redesign without a new contract.
- No broker-write Mission Control controls.
- No public services.
- Preserve `dev` / `qamc` / `ubuntu` isolation.
- Keep upstream OneCLI as the credential layer.
- No secrets in Git/chat/logs/screenshots/client evidence.
- No silent model fallback or unrecorded model choice.
- Claude does not merge its own PR or push implementation directly to `main`.
