"""Expected-sessions dead-man step (moved verbatim from TradingPipeline)."""
from __future__ import annotations

import logging

from src.sentinel.guarded import record_guarded_pass
logger = logging.getLogger(__name__)


class ExpectedSessionsMissingSession:
    """Expected-sessions dead-man step (moved verbatim from TradingPipeline)."""

    def __init__(
        self,
        *,
        db,
    ) -> None:
        self._db = db

    def run(self) -> list[str]:
        """Best-effort internal dead-man's check: on a trading day, which of
        the market-day sessions that should have run by evening left NO
        agent_logs rows? Catches a session that silently never fired — the one
        failure mode push-on-completion observability structurally cannot see
        (a disabled timer, a stuck lock, ET-window math wrong on a half-day).

        Run from evening, which is already gated on `_is_trading_day`, so this
        never false-fires on a holiday. Does NOT cover total host death or
        evening itself not firing — that needs an EXTERNAL dead-man's switch
        (e.g. a healthchecks.io ping the wrapper hits on success). Best-effort:
        any failure returns [] so it can never break the evening push.
        """
        try:
            present = self._db.session_prefixes_logged_on()
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(self._db, "sessions.missing_session_check", exc, log=logger)
            return []
        # run_id prefix -> display name; morning's prefix is 'run'.
        expected = {"run": "morning", "midday": "midday", "close": "close"}
        missing = [name for prefix, name in expected.items() if prefix not in present]

        # RC5 (2026-07-16): "any run- row exists" cannot tell a completed
        # morning from one killed mid-flight — research rows land BEFORE the
        # kill, so 13 straight days of morning deaths passed this check and
        # the 🔴 banner never fired. Two sharper probes:
        if "morning" not in missing and "run" in present:
            # A legit PM-less completion (no_data, say) records a status
            # marker — skip both probes for it.
            try:
                from src import decision_checkpoint as _dc0
                legit_early_exit = _dc0.read_status("morning") is not None
            except Exception:  # noqa: BLE001
                legit_early_exit = False
            #  (a) research logged but the PM never ran → died during research.
            try:
                agents = self._db.agent_names_logged_on("run-")
                if (not legit_early_exit and agents
                        and "portfolio_manager" not in agents):
                    missing.append("morning (research ran, PM never did — killed mid-run?)")
            except Exception as exc:  # noqa: BLE001
                logger.warning("missing-session check: agent probe failed: %s", exc)
            #  (b) PM plan checkpointed but never consumed → killed before the
            #      RiskStage reviewed it (the observed 6/30-7/15 death mode).
            try:
                import json as _json
                from src import decision_checkpoint as _dc
                p = _dc.checkpoint_path("morning")
                if p.exists() and _json.loads(p.read_text()).get("consumed") is False:
                    missing.append(
                        "morning (PM plan never risk-reviewed — checkpoint unconsumed)"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("missing-session check: checkpoint probe failed: %s", exc)
        return missing
