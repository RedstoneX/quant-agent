**188. The decision seats' last-resort route is now a small free model, and nobody has measured it at those seats — filed 2026-09-30.**

DONE WHEN:
  - [x] no seat has every reachable route on one provider, enforced mechanically against `config/settings.yaml` rather than by reading the config by eye
  - [x] DONE 2026-10-01: every decision seat persists, beside the responding model, whether its answer passed that seat's own acceptance gate and WHY it did not — the acceptance columns existed but carried one collapsed word per seat, so the portfolio manager, risk manager and position reviewer now each store the gate's own machine-readable reason (previously prose in a log line only). RECORDING ONLY; nothing reads it back. UNPROVEN: the production rows for these three seats are still all NULL, so the third criterion stays open until sessions produce rows — the database, not a paid benchmark, is what makes the substitute measurable (2026-09-30: only 3 free-model answers exist at these seats and no acceptance verdict is stored beside ANY model, so no rate is computable; 2026-10-01 re-read: still exactly 1 per seat, all three well-formed with every required field but n=1 proves nothing, so this recording is now the ONLY closing condition and the measurement box waits on it)
  - [ ] with that recording in place, the substitute is either measured at the three decision seats from the desk's own rows, or the seats are made to refuse rather than answer when only that route is left — decided on the measurement, not on a guess about how bad it is

detail: docs/board_notes/
