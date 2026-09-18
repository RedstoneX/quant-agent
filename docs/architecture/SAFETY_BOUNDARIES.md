# Safety Boundaries — Accepted Contract

## Non-negotiable

1. Alpaca Paper only; live trading is not authorized.
2. Deterministic Python risk and broker-side protection are final authority.
3. AI Risk Manager is advisory/challenge logic, never a replacement for hard rules.
4. Safety uncertainty/failure fails closed.
5. Mission Control/API failure must have zero effect on trading or broker protection.
6. Read-only Mission Control cannot issue broker writes or override deterministic rejection.
7. Provider/model/prompt changes cannot bypass protected hard-risk behavior.
8. Meta Reflector changes remain human-approved while protected risk behavior stays outside automatic evolution.
9. Any future writable risk configuration requires explicit authorization, server validation, bounded ranges and auditability.

## Verified caveats that matter

- **Exit-path uncertainty (2026-09-16, item 60):** on SELL/REDUCE/COVER,
  uncertainty fails OPEN, not closed — owner-ratified 2026-08-27 for a dead
  or unparseable Risk Manager, and now the same direction for a hard-trigger
  recogniser that cannot run. Deterministic Python owns refusal: a completed
  "reason names no recognised trigger" still drops, and those exits are not
  sent to AI Risk, so a silent model cannot wave through a sale the owner
  already refused. AI Risk may add a refusal when it returns a parseable
  reject; its approval cannot override a deterministic drop. A dropped exit
  writes an append-only per-symbol reason on the named-trigger, AI-reject,
  noise-band, metric-contradiction, and proven-false holding-discipline
  gates. This is the documented exception to item 4 on this path only, not
  permission to fail-open entries or to retune the noise-band / stop-floor
  1.0s (item 70). Verified by `tests/test_exit_refusal_coherence.py`.
- **Kill switch** (2026-09-02): `risk.kill_switch_path` (default
  `data/KILL_SWITCH`) halts order flow via `path.exists()` in
  `AlpacaBroker` — no parsing, so it cannot fail open on a malformed file.
  It is the one deterministic check that ALSO blocks a risk-reducing order
  (an exit, a cover, a new protective stop): every other hard-block rule
  and circuit breaker deliberately exempts SELL/COVER
  (`RiskRuleEngine.check`'s first statement; `apply_gross_ceiling`'s
  BUY/SHORT-only filter) so a bad account state can never trap a position.
  A protective stop already resting at the broker from before the halt is
  untouched — only new order flow is refused. Verified by
  `tests/test_kill_switch.py`.
- **Non-finite equity read** (2026-09-02): a NaN/inf current-equity read
  (Alpaca has been observed to return NaN `portfolio_value` on market-open
  glitches) forces the §11.2 gross-exposure ladder to its floor rung and
  alerts the owner (`_resolve_gross_ceiling`'s `bad_read` rung), instead of
  falling through to the standing cap the way a genuinely fresh account
  with no equity history does. Verified by
  `tests/test_gross_exposure_ladder.py`.
- `cash_sweep` `SWEEP_BUY` intentionally bypasses the shared hard-risk gate. It is deterministic, config-fixed and treated as cash-equivalent; its own bounds govern it.
- The shared deterministic gate runs before AI Risk and again after AI-applied modifications; AI cannot loosen a hard limit.
- AI Risk can widen a positive `stop_loss`; that widening IS re-audited (`src/pipeline.py::_apply_risk_modifications`, 2026-09-03): the edit is refused if it rests the stop inside the ATR noise band, using the constructor's own arithmetic. That refuses the *edit*, not the ticket. A computed or missing range ratio is not a ticket refusal and not an edit refusal — invented numeric reward:risk floors are retired (owner 2026-09-17) — and a breakout is not measured on reward:risk at all. This is a known narrow behavior, not permission to redesign risk during unrelated work.
- `alpaca.paper` is the effective paper/live selector; `alpaca.base_url` is not a second live-safety switch.
- The accepted Stage-2 API is separate-process, GET-only/read-only, uses independent SQLite `mode=ro` history reads, and has no trading-process dependency.
- The accepted Smart Money external-symbol lane is temporary and run-scoped.
  Only deterministic SEC Form 4 open-market purchase evidence may nominate a
  symbol, and broker common-equity eligibility, price, history, liquidity and
  known-sector checks must all pass. Admission bypasses only permanent-universe
  membership and the Technical prefilter; it never bypasses current Technical
  analysis, PM grounding, AI Risk, deterministic risk/funding rules, broker
  protection or Alpaca Paper authorization. The configured universe is not
  mutated, and any uncertainty fails closed.
- **Long scale-in cancel window** (2026-09-15): adding to a long that already
  has a resting protective sell requires cancelling that sell first (the
  broker will not work a BUY against a resting SELL stop on the same name).
  Between confirmed cancel and post-fill rearm the position is unprotected.
  Mitigations, not elimination: a write-ahead recovery row is persisted
  before cancel; cancel is confirmed against the broker's own order status
  before the BUY is sent (bounded REST polling — see the fill-confirmation
  boundary below); rearm sizes to the broker's full position, not the
  add's fill; trail / coverage repair / the coverage watchdog skip a name
  mid-sequence; a failed rearm pages the owner. This is the same cancel-to-
  free-shares shape that made the old whole-book daily breaker dangerous, but
  one symbol at a time with confirmed cancel and full-qty restore. Short adds
  stay blocked: scale-in is the long path. Missing short stops are repaired
  separately (item 73, closed). Verified by `tests/test_scale_in.py`.
- **The fill websocket cannot storm the account** (2026-09-18,
  `_STREAM_ATTEMPT_CEILING_PER_SESSION` / `_STREAM_ATTEMPT_CEILING_PER_DAY`
  in `src/execution/broker.py`): the installed `alpaca-py` (0.43.5) retries
  a failed `trade_updates` handshake from inside its OWN loop
  (`TradingStream._run_forever`), which sleeps a flat 10ms and has neither
  backoff nor an attempt limit. On 2026-09-15 that produced 32,896
  handshake attempts in one day, 32,666 of them rejected HTTP 429
  [measured, retained logs]. Alpaca's rate limit is per ACCOUNT (200
  requests per minute), so this competes with the order path. The desk now
  bounds the SDK's loop rather than its own: a failed handshake backs off
  on an equal-jitter curve, a 429 stands down for the full published
  minute-window instead of the transport cap, and reaching either the
  per-session or the per-day ceiling sets the SDK's `_should_run` false,
  pages the owner ONCE in plain words, and leaves every fill to the bounded
  REST path for the rest of the day. The budget is process-wide and
  day-keyed, not per-socket, because a per-socket counter resets on each
  new hub and bounds nothing in aggregate. Every constant is sourced in a
  comment beside it. Verified by `tests/test_order_fill_stream.py`.
- **Fills are confirmed by bounded REST polling whenever the socket cannot
  serve them** (`execution.fill_stream_enabled`, ON in
  `config/settings.yaml` since 2026-09-18; the history below is the
  2026-09-17 decision that switched it off and is kept because it names the
  two blockers): the
  `trade_updates` socket has never authenticated on this host since it was
  built on 2026-09-10, and two independent confirmed blockers mean no code
  or config change could make it (the process holds placeholder credentials
  that a local proxy substitutes on REST only, and the installed
  `alpaca-py` stream uses `websockets.legacy`, which has no proxy support;
  Alpaca also authenticates in-band by MESSAGE, not by handshake header,
  which a header-injecting gateway cannot supply). With the flag off the
  desk never opens the socket, never takes the account lease and never runs
  the reconnect loop; every fill wait takes the REST polling path with its
  existing timeouts, poll intervals and ceilings unchanged. The websocket
  code is dormant, not deleted — the credential decision is deferred, and
  flipping the flag restores the behaviour below exactly. The boundary that
  replaced the socket's silence: fill confirmation actually DEGRADING pages
  the owner (an order whose outcome the bounded window could not confirm,
  or a reconciliation finding the desk's record and the broker's
  disagreeing with no sale to explain it). The socket being off never
  pages. Verified by `tests/test_fill_stream_switch.py`.
- **UPDATE, 2026-09-18: "has never authenticated" above is now history, not
  the present state.** With the real credential delivered and the flag on, a
  connection attempt as the desk's own account authenticated on the FIRST
  try — the placeholder-credential root cause is proven fixed, not merely
  inferred. The broker's reply also flagged the in-band message format
  alpaca-py sends as deprecated; the desk now sends the format the broker
  actually asks for, with the old one kept only as a same-socket fallback.
  Both were fixed and shipped the same day. Full detail, including what is
  still unproven (the desk's own code completing the handshake end to end
  during a real trading session) and the account this socket is pinned to:
  `docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md`, "The websocket
  exception" section.
- **One `trade_updates` socket per Alpaca account** (2026-09-17, DORMANT
  while the flag above is off — this is the behaviour flipping it back on
  restores): Alpaca
  allows a single trading-stream connection per account. Morning, intra and
  other systemd jobs are separate processes; a process-local lock cannot
  stop them each opening a socket (auth storms / failed to authenticate at
  the open). One desk process owns the account slot via an advisory flock
  beside the DB. Other processes attach to that process's kept hub when they
  share it, otherwise they REST-poll. They never open a second socket.
  Protective fill waits use `wait_for_order_terminal`; a dead or unowned
  stream falls to REST for the remaining window, not a stacked second
  timeout and not a wait longer than the REST path. Frame-drain of already-
  seen statuses is a same-process attach aid, not ownership. Auth backoff
  is unchanged (alpaca-py reconnect max). Repegs stay off. Verified by
  `tests/test_order_fill_stream.py`.
- **Stop-level archive (2026-09-16):** `trades.stop_loss` on the opening
  BUY/SHORT row is written back when an in-code path changes the live stop
  (replace/trail funnel, coverage repair, scale-in rearm, ex-div shift,
  residual reprotect) and the broker accepted an order id. The entry bet is
  frozen as `initial_stop_loss`. Repair restores the live recorded (trailed)
  level, not the entry stop; a would-fire refuse is still a refuse, not a
  widen. A session/watchdog reconcile reports mismatches and does not copy
  the broker price into the archive. Do not treat a "traded through its stop"
  reading from `trades` as real while they disagree. Items 35 and 69 stay
  closed as archive illusions. Verified by `tests/test_stop_writeback.py`.
- `scripts/desk_reset.py` is the only operator tool that issues broker
  liquidations (`DELETE /v2/positions?cancel_orders=true`). It is outside the
  trading pipeline and outside the risk engine, so it carries its own
  fail-closed paper check rather than inheriting one: `alpaca.paper`, the
  configured `base_url`, the SDK client's **resolved** base URL, and the
  account's own `PA` account-number prefix must all say paper, and a read-only
  probe of the live host must **not** authenticate. Any single failure aborts.
  Note the qualifier above — `alpaca.base_url` is not a live-safety switch for
  the *pipeline*; inside this tool it is one of four independent signals, none
  of which is trusted alone. There is deliberately no override flag: a
  liquidation against a live account should require a reviewed code change,
  the same bar as `AppConfig._enforce_paper_only`. The tool never drops a
  table and never deletes a file; unknown tables are kept, not emptied.

Detailed verification remains in Git history. The last pre-ultra-lean working-tree snapshot is commit `02e20e6ac1c5c7e65b7f512f76c568328c990e3c`. Current authorization is always controlled by `docs/STATE.md` + `docs/WORK.md`.
