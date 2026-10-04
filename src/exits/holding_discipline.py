"""The holding-discipline fact-check for an exit: every claim the exit reason makes, checked against the recorded state.

Lifted verbatim out of `ExitEngineMixin` (src/pipeline_exits.py) as a
standalone class: every collaborator is an explicit keyword-only
constructor argument, so it is built and exercised without a pipeline.
The mixin keeps a thin method of the same name that builds this object
and calls it, so every existing caller and patch target is unchanged.
"""

import logging
import json as _json

#: Logs under `src.pipeline`, as the bodies did before the move;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class HoldingDiscipline:
    """The holding-discipline fact-check for an exit: every claim the exit reason makes, checked against the recorded state."""

    def __init__(self, *,
                 build_active_state_changes,
                 carry_forward_macro,
                 structural_protection_for_holding,
                 db) -> None:
        self._build_active_state_changes = build_active_state_changes
        self._carry_forward_macro = carry_forward_macro
        self._structural_protection_for_holding = structural_protection_for_holding
        self.db = db

    def _holding_discipline_check_for_exit(
        self,
        *,
        symbol: str,
        action: str,
        reason: str,
        positions,
        run_id: str,
        position_history: dict | None = None,
        exit_trigger=None,
    ):
        """Fact-check ONE midday/close exit's hard-trigger claim, using the
        same deterministic checker the morning Portfolio-Manager path uses.

        2026-09-11. `_reason_cites_hard_trigger` is a SUBSTRING MATCH and
        has never been anything else: it forces the reason to make a CLAIM
        ("a regime shift happened"), and until this landed nothing on the
        midday/close surface ever asked whether the claim was TRUE.
        `src/risk/exit_guard.holding_discipline_claim_check` — built for
        exactly that question, and live on the morning PM path since
        2026-09-03/04 (`RiskStage.run`, "Holding-discipline compliance") —
        was imported from that one call site and nowhere else, so the
        desk's two BUSIEST exit surfaces ran on the words alone.

        This method only ASSEMBLES the inputs; the verdict semantics are
        the checker's and are deliberately not re-decided here. The caller
        drops the exit on `check.blocks` (PROVABLY FALSE) and lets an
        UNVERIFIABLE verdict through — absence of proof is not proof, and
        blocking an exit we merely cannot check would strand the desk in a
        losing position, which is strictly worse than the gap being closed.

        Inputs this path can supply, and how:
          - `protected`: YES, in full. `_structural_protection_for_holding`
            already lives on this class (it is the same method RiskStage
            calls) and its entry context — `thesis_invalid_if`,
            `entry_price`, `stop_loss` — comes from
            `_build_position_history`, the same DB-backed builder the
            morning path reads through `ctx.position_history`. No LLM and
            no morning-only state is involved in either.
          - `active_state_changes`: YES, identical. `_build_active_state_changes`
            is a plain news-store read on this class; RiskStage calls the
            very same method.
          - `macro_regime_today` / `macro_status`: PARTIALLY, and honestly
            so. No macro analyst runs at midday or close, so there is no
            fresh read to pass. `_carry_forward_macro` may return this
            MORNING's stored read (`carried_from_morning`, same session)
            or a GOOD prior-day regime (`remembered` until a real
            regime/print change). Only a same-session payload may falsify
            an exit claim — holding-discipline reads `.same_session`, not
            payload truthiness. When payload is None or not same-session,
            `macro_status` is passed as None and the checker's own
            UNVERIFIABLE branch handles it. Nothing is defaulted,
            substituted or invented to fill the gap: an absent or
            cross-day macro read makes a regime claim unverifiable, never
            false.

        Returns the `HoldingDisciplineClaimCheck`, or None when there is
        nothing for it to adjudicate (see the short-circuit below).
        """
        # Local imports: `pipeline_stages` imports this module, so the
        # `_macro_regime` reader (reused rather than reimplemented — it is
        # the same "MacroAnalysis or carried-forward dict" reader RiskStage
        # feeds the checker with) can only be pulled in at call time.
        from src.pipeline_stages import _macro_regime
        from src.risk.exit_guard import (
            claims_bearish_state_change,
            claims_regime_flip,
            claims_thesis_invalidation,
            holding_discipline_claim_check,
        )
        from src.agents.portfolio_manager import (
            PortfolioManagerAgent as _PortfolioManagerAgent,
        )
        from src.risk.exit_trigger import ExitTrigger, normalize_trigger

        if str(action).upper() not in ("SELL", "REDUCE", "COVER"):
            return None
        # (b)/(c): the two claims `holding_discipline_claim_check` can
        # actually adjudicate. With neither present it returns "ok"
        # regardless of everything else it is passed, so its verdict is not
        # what the thesis branch below is here for.
        # 2026-09-18: read the claim from `PositionAction.exit_trigger`
        # when the seat filled it, and from the prose only as a fallback.
        # The prose-only version of this line is why the two real
        # 2026-09-16 exits were never adjudicated at all: their entire
        # reason was the words "adverse news", which neither regex
        # recognises, so this short-circuited to None and no fact-check of
        # any kind ran. See `src/risk/exit_trigger.py`.
        _structured = normalize_trigger(exit_trigger)
        adjudicable_claim = (
            claims_regime_flip(reason)
            or claims_bearish_state_change(reason)
            or _structured in (
                ExitTrigger.REGIME_SHIFT, ExitTrigger.BEARISH_STATE_CHANGE,
                ExitTrigger.ADVERSE_NEWS,
            )
        )
        # (a) thesis invalidation. Until 2026-09-14 this fell through the
        # short-circuit above and the structural check was NEVER consulted
        # on it — on the one exit class where "did the level backing this
        # stop actually break?" is the whole question, and the exit class
        # for which the ATR noise band is least redundant: most
        # hard-trigger keywords ALSO match `EXTERNAL_INFORMATION_PATTERNS`
        # and so skip the band outright, while the thesis-invalidation
        # wordings never have. The desk already computes the answer; it
        # simply was not asked here. docs/WORK.md item 60.
        #
        # NO COUNT IS WRITTEN HERE ON PURPOSE (2026-09-30). This comment
        # used to read "21 of the 26 hard-trigger keywords", and the 26 was
        # already wrong before this change — the tuple held 23 — so the
        # sentence reasoned from a number that had outlived its derivation.
        # The figures are now RECOMPUTED FROM THE CODE, every run, by
        # `tests/test_exit_trigger_canonical_names.py::
        # test_clamp_bypass_divergence_is_pinned_per_trigger`, which also
        # pins WHICH keywords diverge. A digit in prose here can only go
        # stale again.
        #
        # This branch is STRICTLY ADDITIVE and is designed so that it
        # cannot change which exits execute:
        #   - the read is taken with `persist=False`, so it can never
        #     become the prior-day half of a future confirmation and so can
        #     never lift `protected` a session earlier than it does today;
        #   - its verdict is recorded and logged, and is NOT fed to
        #     `holding_discipline_claim_check` (which still leaves (a)
        #     unjudged) and NOT returned to the caller as a verdict;
        #   - on a thesis-only reason this method still returns None,
        #     exactly as it did before, so the caller's block/allow path is
        #     byte-for-byte the behaviour it had.
        # A "the level did break" answer is corroboration for the evening
        # grade and the audit trail; an "intact" or "cannot tell" answer
        # changes nothing at all. Tightening the sell path on an intact
        # level was considered and deliberately NOT done here: it is a
        # separate, ratifiable decision, not a side effect of wiring up a
        # check that should always have been consulted.
        thesis_claim = (
            claims_thesis_invalidation(reason)
            or _structured is ExitTrigger.THESIS_INVALID
        )
        if not (adjudicable_claim or thesis_claim):
            return None

        symbol_u = (symbol or "").strip().upper()
        if position_history is None:
            position_history = {}
        hist = position_history.get(symbol) or position_history.get(symbol_u) or {}
        pos = next(
            (p for p in (positions or []) if (p.symbol or "").upper() == symbol_u),
            None,
        )
        protection = self._structural_protection_for_holding(
            symbol=symbol_u,
            thesis_invalid_if=hist.get("thesis_invalid_if"),
            entry_price=hist.get("entry_price"),
            # The entry SESSION, for the noise-band fallback's running-extreme
            # anchor; entry price alone cannot locate the extreme. Absent, the
            # callee looks it up, and failing that the band stays entry-anchored.
            entry_date=hist.get("entry_date"),
            stop_loss=hist.get("stop_loss"),
            is_short=bool(pos is not None and pos.qty < 0),
            run_id=run_id,
            # Read-only unless a (b)/(c) claim is present, i.e. unless this
            # call site would have run anyway. See `persist`'s docstring.
            persist=adjudicable_claim,
        )
        logger.info(
            "Holding-discipline structural protection for %s: protected=%s "
            "basis=%s — %s",
            symbol_u, protection.protected, protection.basis, protection.detail,
        )

        if thesis_claim:
            # Durable, per-symbol, machine-readable record of what the
            # structural check actually said about a thesis-invalidation
            # exit — the answer this surface used to discard. Written as
            # append-only specialist evidence rather than into
            # `intraday_evaluations`, whose (symbol, run_id) upsert would
            # let this observation overwrite, or be overwritten by, a real
            # gate's verdict for the same symbol and run.
            corroborated = not protection.protected
            logger.info(
                "Thesis-invalidation exit %s %s: structural check says "
                "%s (basis=%s). Recorded, not acted on — this observation "
                "neither blocks nor releases the exit. %s",
                action, symbol_u,
                "the backing level HAS broken (exit corroborated)"
                if corroborated else
                "the backing level is INTACT (exit not corroborated)",
                protection.basis, protection.detail,
            )
            try:
                self.db.insert_specialist_evidence(
                    run_id=run_id, agent_name="risk_manager",
                    kind="thesis_invalidation_structural_check",
                    scope="symbol", symbol=symbol_u,
                    evidence_json=_json.dumps({
                        "action": str(action).upper(),
                        "protected": bool(protection.protected),
                        "raw_broken": bool(protection.raw_broken),
                        "basis": protection.basis,
                        "detail": str(protection.detail)[:400],
                        "corroborates_exit": corroborated,
                        "reason": str(reason)[:400],
                        "advisory_only": True,
                    }),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "thesis-invalidation structural check: evidence write "
                    "failed for %s (%s) — the check still ran and is in "
                    "the log above", symbol_u, e,
                )

        if not adjudicable_claim:
            # Nothing for `holding_discipline_claim_check` to adjudicate:
            # it would return "ok" for any (a)-only reason. Same None the
            # caller received before this branch existed.
            return None

        # This morning's macro read, or nothing. `_carry_forward_macro` is
        # already the producer of the `carried_from_morning` status
        # elsewhere in this class (see the intraday-scan data_status block),
        # so the label is reused rather than a second one invented.
        # Cross-day remembered regime is usable for the PM but is NOT
        # proof about today — only a same-session payload may falsify an
        # exit claim.
        carried_macro = self._carry_forward_macro()
        if carried_macro.same_session and carried_macro.payload is not None:
            macro_regime_today = _macro_regime(carried_macro.payload)
            macro_status = (
                carried_macro.status
                if carried_macro.status == "carried_from_morning"
                else "carried_from_morning"
            )
        else:
            macro_regime_today = None
            macro_status = None

        try:
            active_state_changes = self._build_active_state_changes()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "holding discipline: state-change lookup failed (%s) — "
                "bearish-state-change claims go unverified for %s",
                e, symbol_u,
            )
            active_state_changes = ""

        return holding_discipline_claim_check(
            action=action,
            reason=reason,
            symbol=symbol_u,
            protected=protection.protected,
            macro_regime_today=macro_regime_today,
            macro_status=macro_status,
            state_change_parser=_PortfolioManagerAgent._state_change_symbols_by_date,
            active_state_changes=active_state_changes,
            exit_trigger=exit_trigger,
        )
