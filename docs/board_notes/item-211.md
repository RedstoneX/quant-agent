## item 211 — RETIRED 2026-10-01, both-edge paging deferred to the circuit's own self-clear window, repeat suppression made per-type, and every refusal now readable at /alerts/suppressed

Why the threshold is not a new number. The circuit already answers "how long
before this stops being a blip": `_auto_clear_transient_latch_locked` refuses
to retire a transient latch until `transient_latch_cooldown_minutes` of wall
clock have passed, and refuses again if the day's
`max_transient_latch_auto_clears_per_day` allowance is spent. Paging the owner
the instant the latch is set contradicts the circuit's own stated belief that
the fault may not be real yet. So the paging threshold IS that field. There was
no need to invent one, and inventing one would have been a barred arbitrary
number.

Why an episode is one trigger code on one ET day. The auto-clear allowance is
already counted per ET day against `llm_circuit_events` for exactly this
purpose — "a fault recurring this often is not transient". The episode
boundary reuses that unit rather than defining a second, differently-shaped
notion of "the same fault again".

What this does NOT do. It does not re-enable Telegram: the owner muted it
deliberately on 2026-09-30 and it stays muted. It does not make the desk
quieter about anything an operator must act on — every non-self-clearing
trigger still pages immediately, because there is no window it can expire
inside. It does not migrate the three existing per-symbol markers in
`src/coverage_watchdog.py` onto the new generic helper; that is a refactor of
working code and was left alone so this change cannot alter what they already
suppress. The suppression record IS now on the API, which was the item's last open
criterion: `GET /alerts/suppressed` returns the `suspend_alert_deferred`
rows from `llm_circuit_events` alongside the `suppressed_alerts` block from
BOTH watchdog state files, and reports "could not read it" separately from
"nothing was suppressed" rather than collapsing the two into an empty list.
It still does not migrate the three existing per-symbol markers in
`src/coverage_watchdog.py` onto the generic helper; that is a refactor of
working code and was left alone so this change cannot alter what they
already suppress. There is no dashboard tile — the criterion reads
"API/dashboard" and the API is what shipped.

Correction to the item text as filed. It said `scripts/check_deploy_drift.py`
claims through `coverage_watchdog.claim_typed_alert`. It does not: it keeps
its own `drift_alerted_for` marker in `deploy_drift.json`, which is still a
per-type, per-key, per-day claim but a separate implementation. What it was
genuinely missing is the recording half — a repeat it declined to send left
no trace at all — so `record_state` now writes that refusal through
`_record_suppressed_alert`, into the same state file, where the endpoint
reads it.

Where the live-risk line is drawn. A live-risk alert is one about a position
whose protection is gone or never arrived: a stop that failed to place, a
stop that failed to re-arm after a scale-in, an uncovered position, a broker
rejection. None of them is ever silenced: the cost circuit's deferral applies
only to `_SELF_CLEARING_HARD_TRIGGERS` (paid-provider faults), and the typed
claim helper always releases the FIRST occurrence for a symbol on an ET day,
holding only a byte-identical repeat of the same fault for the same symbol on
the same day — and recording even that. Both halves are proved in
`tests/test_alert_suppression_api.py` and `tests/test_cost_circuit.py`.
