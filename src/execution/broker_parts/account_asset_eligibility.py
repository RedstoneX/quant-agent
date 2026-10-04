"""Asset shortability and fractionability reads, lifted verbatim out of `AccountReads`.

A mixin so `AccountReads` does not grow; it uses only `self` plus the broker logger.
"""
from __future__ import annotations

import logging

from src.execution.broker_parts.stop_place import _alpaca_symbol, _internal_symbol
from src.sentinel.guarded import record_guarded_pass

logger = logging.getLogger("src.execution.broker")


class AssetEligibilityReads:
    def get_shortability(self, symbol: str) -> dict:
        """D6 (Stage 3): the borrow gate. Alpaca's per-asset `shortable` and
        `easy_to_borrow` flags, cached for the life of this broker instance
        exactly like `get_transient_equity_eligibility` is (a read-only
        asset-directory fact that does not change intra-session).

        A short may open ONLY when BOTH flags are true. This is paper
        trading against IEX data: a hard-to-borrow name fills unrealistically
        in paper and its borrow cost is not modeled anywhere in this system,
        so restricting to easy-to-borrow names is what keeps measured paper
        results transferable to live capital. `reason` distinguishes the two
        ways a short can be refused ("not_shortable" vs "hard_to_borrow") so
        the caller can log which one fired.

        Fails CLOSED: an API error or an unreadable/unknown symbol reports
        shortable=False / easy_to_borrow=False — a short is refused, never
        guessed open.
        """
        canonical = _internal_symbol(_alpaca_symbol(symbol))
        cached = self._shortable_cache.get(canonical)
        if cached is not None:
            return cached

        alpaca_symbol = _alpaca_symbol(canonical)
        try:
            asset = self.client.get_asset(alpaca_symbol)
            record_guarded_pass(self, "account_reads.shortability", context={"symbol": canonical})
        except Exception as exc:
            record_guarded_pass(self, "account_reads.shortability", exc, log=logger, context={**{"symbol": canonical}, "effect": "reported not shortable"})
            result = {
                "shortable": False, "easy_to_borrow": False,
                "reason": "asset_lookup_failed", "symbol": canonical,
            }
            self._shortable_cache[canonical] = result
            return result

        def _field(name, default=None):
            if isinstance(asset, dict):
                return asset.get(name, default)
            return getattr(asset, name, default)

        shortable = bool(_field("shortable", False))
        easy_to_borrow = bool(_field("easy_to_borrow", False))
        if shortable and easy_to_borrow:
            reason = "eligible"
        elif not shortable and not easy_to_borrow:
            reason = "not_shortable"  # the more specific/common of the two
        elif not shortable:
            reason = "not_shortable"
        else:
            reason = "hard_to_borrow"
        result = {
            "shortable": shortable, "easy_to_borrow": easy_to_borrow,
            "reason": reason, "symbol": canonical,
        }
        self._shortable_cache[canonical] = result
        return result

    def get_fractionability(self, symbol: str) -> dict:
        """Spec §11.1: is this symbol tradeable in fractional quantities?

        Alpaca publishes a per-asset `fractionable` flag in the same
        asset-directory record `get_shortability` reads, and it is cached the
        same way for the same reason — it does not change intra-session.

        **Fails CLOSED, and that is the whole point.** An API error, an
        unknown symbol, an asset record with no `fractionable` field at all —
        every one of those reports `fractionable=False`, and the caller sizes
        in whole shares. Fractional-by-assumption is the failure this guard
        exists to prevent: a fractional order on a non-fractionable name is
        rejected outright by the broker, which turns an approved trade into
        no trade at all and hides the reason in an order-rejection log.

        `reason` distinguishes the three ways the answer can be no, so the
        caller can log which one fired rather than a bare False.
        """
        canonical = _internal_symbol(_alpaca_symbol(symbol))
        cached = self._fractionable_cache.get(canonical)
        if cached is not None:
            return cached

        alpaca_symbol = _alpaca_symbol(canonical)
        try:
            asset = self.client.get_asset(alpaca_symbol)
            record_guarded_pass(self, "account_reads.fractionability", context={"symbol": canonical})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(self, "account_reads.fractionability", exc, log=logger, context={**{"symbol": canonical}, "effect": "sized in whole shares"})
            result = {
                "fractionable": False, "reason": "asset_lookup_failed",
                "symbol": canonical,
            }
            self._fractionable_cache[canonical] = result
            return result

        def _field(name, default=None):
            if isinstance(asset, dict):
                return asset.get(name, default)
            return getattr(asset, name, default)

        raw = _field("fractionable", None)
        if raw is None:
            # The record came back but carries no flag — an older API shape,
            # a stub, a mock. "Absent" is not "true".
            result = {
                "fractionable": False, "reason": "fractionable_unknown",
                "symbol": canonical,
            }
        elif bool(raw):
            result = {
                "fractionable": True, "reason": "fractionable",
                "symbol": canonical,
            }
        else:
            result = {
                "fractionable": False, "reason": "not_fractionable",
                "symbol": canonical,
            }
        self._fractionable_cache[canonical] = result
        return result
