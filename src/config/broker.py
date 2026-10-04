"""Broker and credential settings: API keys, Alpaca account, and the live-capital authorization lock.

Moved verbatim from src/config/__init__.py (pure move; bodies AST-identical).
"""

from pydantic import BaseModel, model_validator


class ApiKeysConfig(BaseModel):
    anthropic: str
    openai: str = ""
    deepseek: str = ""
    # OpenRouter (Stage 1 QAMC provider/model plumbing) — optional, only
    # required when an agent's explicit `provider: openrouter` is selected
    # (enforced in AppConfig._check_llm_provider_keys, not here, since that's
    # the layer that already knows which agents are configured for it).
    openrouter: str = ""
    # Google AI Studio direct (2026-08-31 owner decision: gemini-3.5-flash-lite
    # direct becomes the PRIMARY route for the eight specialist/review seats) —
    # optional, only required when an agent's explicit `provider: google` is
    # selected, or google is reachable as the configured cross-provider
    # failover target (both enforced in AppConfig._check_llm_provider_keys).
    google: str = ""
    fred: str
    alpaca_key: str
    alpaca_secret: str

    @model_validator(mode="after")
    def _check_required_keys(self):
        for field_name in ("alpaca_key", "alpaca_secret", "fred"):
            if not getattr(self, field_name):
                raise ValueError(f"Required API key '{field_name}' is empty — check your .env file")
        if not (
            self.anthropic or self.openai or self.deepseek
            or self.openrouter or self.google
        ):
            raise ValueError(
                "At least one of 'anthropic', 'openai', 'deepseek', 'openrouter', "
                "or 'google' API key must be set"
            )
        return self


# Alpaca's paper-trading host. `base_url` is declarative today — no code path
# reads it (the real switch is the `paper` flag below, which alpaca-py turns
# into an endpoint choice) — so the validator's job is to stop the two from
# disagreeing and giving a reader a false impression of which venue is in use.
_ALPACA_PAPER_HOST = "paper-api.alpaca.markets"

# The deliberate, reviewed code-level authorization for live capital. Flipping
# this is one of the TWO things that must happen for the desk to leave paper;
# the other is the live-capital pre-flight gate (board item 150,
# `src/live_capital_preflight.py`) passing every condition in its ACTIVATION
# scope. Neither alone is enough, on purpose:
#
#   * a settings.yaml edit alone still fails — this constant is False;
#   * flipping this constant alone still fails — the gate blocks and the error
#     names every unmet condition;
#   * a signed attestation file alone still fails — this constant is False.
#
# Before this existed the guard simply raised, which was safe but silent about
# WHY; the checklist lived in prose and nothing checked it. Do not flip this
# without the gate reporting PASS, and never as a drive-by edit.
LIVE_TRADING_AUTHORIZED = False


class AlpacaConfig(BaseModel):
    base_url: str
    paper: bool

    @model_validator(mode="after")
    def _enforce_paper_only(self):
        """Fail closed unless this is a paper account.

        "Alpaca **Paper only**; live trading is not authorized" is a hard
        boundary in CLAUDE.md, docs/STATE.md and AGENTS.md, but until now
        it lived entirely in prose: flipping `paper: false` in settings.yaml
        would have silently pointed the whole decision chain at a live
        brokerage account with no test, guard, or log to notice. A one-token
        config edit should not be able to do that.

        This is deliberately a hard failure with no env-var escape hatch. If
        live trading is ever authorized, it takes BOTH a reviewed code change
        flipping `LIVE_TRADING_AUTHORIZED` above AND the live-capital pre-flight
        gate (board item 150) reporting every activation-scope condition
        satisfied. This is the point where the paper lock would be lifted, so
        this is where the gate sits: there is no code path to a live account
        that does not run it, and a refusal names the conditions that failed.
        """
        if self.paper is not True:
            if not LIVE_TRADING_AUTHORIZED:
                raise ValueError(
                    "alpaca.paper must be true — live trading is not authorized "
                    "(see the hard boundaries in CLAUDE.md / docs/STATE.md). "
                    "Enabling live trading requires BOTH a reviewed change to "
                    "config.LIVE_TRADING_AUTHORIZED and a passing live-capital "
                    "pre-flight gate (src/live_capital_preflight.py), not a "
                    "settings.yaml edit."
                )
            # Authorized in code — the gate still has the last word, and names
            # which condition failed rather than refusing anonymously.
            from src.live_capital_preflight import (  # local: avoids import cycle
                LiveCapitalBlocked,
                assert_live_capital_authorized,
            )

            try:
                assert_live_capital_authorized()
            except LiveCapitalBlocked as exc:
                raise ValueError(
                    f"alpaca.paper is false and live trading is code-authorized, "
                    f"but the live-capital pre-flight gate refuses: {exc}"
                ) from exc
            return self
        host = self.base_url.strip().lower()
        if host and _ALPACA_PAPER_HOST not in host:
            raise ValueError(
                f"alpaca.base_url must point at {_ALPACA_PAPER_HOST} while "
                f"paper-only is in force; got {self.base_url!r}"
            )
        return self
