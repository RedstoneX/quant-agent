"""Congress (House + Senate) trading-disclosure smart-money provider.

Two independent, free, credentialless sources are cross-checked against
each other, matching the exact fail-open posture `SECForm4Provider` already
uses for SEC Form 4 (`src/data/smart_money.py`): a source going unreachable,
timing out or returning malformed data must never block a refresh or crash
the run, and this stream never gates a trade on its own (see
`SmartMoneyFinding`'s "conservative congressional contract" in
`src/models.py`, unchanged by this file).

Sources
-------
Primary   kadoa-org/congress-trading-monitor (GitHub, MIT licensed). Static
          JSON, no auth, no rate limit. Covers House Clerk PTRs, Senate eFD
          and OGE executive-branch filings, and carries a real
          ``filing_date`` per trade.
Secondary congresswatch.us (OpenSourcePatents LLC). No auth, ~8,000 records,
          House + Senate. Carries the official PTR link for provenance, but
          its live schema has NO filing/disclosure-date field at all (see
          `_estimate_disclosure_date` below), and at least one observed
          record has a future-dated transaction — every date is sanity-
          checked (`_sane_transaction_date`) before use, dropping rather
          than silently repairing an implausible one.

Both are single-operator, young projects with no track record: treat as
best-effort, never load-bearing, exactly like the free Form 4 discovery
path this seat already relies on.

Dedup and disagreement
-----------------------
The same real trade can appear in both feeds. Rows are grouped by
``(ticker, normalized filer name, transaction_date)`` — a "rough amount"
match is deliberately not part of the group key, because a genuine
disagreement about the dollar bracket is exactly the case this cross-check
exists to surface, not to hide by splitting into two rows. Within a group:

* one source only -> ``cross_source_agreement="single_source"``.
* both sources, same direction and overlapping amount bracket ->
  ``"agreement"``.
* both sources, but direction or amount bracket disagree -> ``"discrepancy"``,
  with `cross_source_note` stating what disagreed. The row is still kept
  (never dropped for disagreeing) and the analyst sees the flag.

Cluster/materiality reduction reuses the exact same window and helper as
SEC Form 4 (`src.data.smart_money_cluster.cluster_survivors`) — see that
module's docstring for why this is a shared function rather than two
independent implementations.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import requests

from src.data.smart_money import DEFAULT_USER_AGENT, SmartMoneySource, _symbol
from src.data.smart_money_cluster import cluster_survivors
from src.models import SmartMoneyObservation
from src.util.time import et_today

logger = logging.getLogger(__name__)

KADOA_TRADES_URL = (
    "https://raw.githubusercontent.com/kadoa-org/"
    "congress-trading-monitor/main/public/data/trades.json"
)
CONGRESSWATCH_TRADES_URL = "https://congresswatch.us/data/trades.json"

_AMOUNT_RE = re.compile(r"\$?([\d,]+)")
_NAME_PREFIXES = ("rep.", "rep", "sen.", "sen", "hon.", "hon", "dr.", "dr", "mr.", "mr", "ms.", "ms", "mrs.", "mrs")
# Any transaction more than this many years old is treated as implausible
# junk rather than real history — the STOCK Act (2012) is the practical
# floor for electronic congressional disclosure data of this kind.
_MAX_PLAUSIBLE_AGE_YEARS = 20


def _normalize_actor_name(raw: str) -> str:
    """Fold naming variants ("Rep. Kevin Hern" / "Kevin Hern") to one key."""
    cleaned = re.sub(r"[.,]", "", str(raw or "").strip().casefold())
    parts = cleaned.split()
    while parts and parts[0] in {p.rstrip(".") for p in _NAME_PREFIXES}:
        parts = parts[1:]
    return " ".join(parts)


# Real STOCK Act transaction-type values, as they actually appear across the
# two feeds and the systems they derive from. Verified 2026-09-04 against the
# House Ethics Committee's PTR instructions and the Senate Select Committee on
# Ethics' PTR instructions, which define exactly three reportable transaction
# kinds — purchase, sale, exchange — and the House PTR form's own short codes:
#
#   P            Purchase
#   S            Sale (full)
#   S (partial)  Partial sale (only part of a holding sold)
#   E            Exchange (rare; e.g. share swap in a merger)
#
# Senate eFD and the House Clerk's own web export render the same three kinds
# as full words, and the widely-mirrored House-Clerk-derived JSON schema both
# our sources ultimately descend from uses the snake_case forms
# ``purchase`` / ``sale_full`` / ``sale_partial`` / ``exchange``.
#
# This is an EXPLICIT allowlist on purpose. The previous implementation
# prefix-matched full words only, so a row carrying the form's short code fell
# through to "unknown" and was silently lost — for a feed whose entire point is
# buy/sell direction, that is real data loss, not a cosmetic gap. It is
# deliberately NOT a loose single-letter prefix test either: a bare
# ``startswith("s")`` would wrongly read "Stock Split" or "Stock Dividend" as a
# sale. Short codes match only as an exact whole token.
_BUY_TYPES = frozenset({"p", "purchase", "purchased", "buy"})
_SELL_TYPES = frozenset({
    "s", "s (partial)", "s (full)", "s(partial)", "s(full)",
    "sale", "sold", "sell",
    "sale (full)", "sale (partial)", "sale_full", "sale_partial",
    "sale full", "sale partial", "partial sale",
})
_EXCHANGE_TYPES = frozenset({"e", "exchange", "exchanged"})

#: Distinct raw transaction-type values that matched nothing above. Populated
#: (and warned about, once per distinct value) by `_direction` so a future
#: unrecognized upstream format is VISIBLE rather than silently swallowed as
#: "unknown". Tests clear this; production only ever reads it.
_UNRECOGNIZED_TRANSACTION_TYPES: set[str] = set()
_UNRECOGNIZED_LOCK = threading.Lock()


def _direction(transaction_type: str) -> Literal["buy", "sell", "exchange", "unknown"]:
    """Map a raw STOCK Act transaction-type value to a trade direction.

    Handles both the full-word forms and the House PTR form's short codes
    (``P``/``S``/``S (partial)``/``E``). Anything that still matches nothing is
    returned as "unknown" AND recorded/logged, never dropped quietly.
    """
    raw = str(transaction_type or "").strip()
    # Collapse internal whitespace so "S  (partial)" and "S (Partial)" fold to
    # the same key as the canonical form.
    label = " ".join(raw.lower().split())
    if not label:
        return "unknown"
    if label in _BUY_TYPES:
        return "buy"
    if label in _SELL_TYPES:
        return "sell"
    if label in _EXCHANGE_TYPES:
        return "exchange"
    # Full-word prefix fallback, preserved from the original implementation so
    # trailing qualifiers we have not enumerated (e.g. "purchase (partial)")
    # still resolve. Only ever applied to whole words, never to a short code.
    if label.startswith("purchase"):
        return "buy"
    if label.startswith("sale"):
        return "sell"
    if label.startswith("exchange"):
        return "exchange"
    with _UNRECOGNIZED_LOCK:
        first_time = raw not in _UNRECOGNIZED_TRANSACTION_TYPES
        _UNRECOGNIZED_TRANSACTION_TYPES.add(raw)
    if first_time:
        logger.warning(
            "congressional_trading: unrecognized transaction_type %r — row "
            "kept with direction='unknown'. If this is a real STOCK Act "
            "transaction code, add it to _BUY_TYPES/_SELL_TYPES/"
            "_EXCHANGE_TYPES in src/data/congressional_trading.py.",
            raw,
        )
    return "unknown"


def _amount_bracket(low, high, label: str) -> tuple[float | None, float | None]:
    """Prefer explicit low/high fields (kadoa); else parse the label string
    (both sources use identical STOCK Act bracket text, e.g.
    "$1,001 - $15,000")."""
    try:
        if low is not None:
            return float(low), (float(high) if high is not None else None)
    except (TypeError, ValueError):
        pass
    numbers = _AMOUNT_RE.findall(str(label or ""))
    if not numbers:
        return None, None
    parsed = [float(n.replace(",", "")) for n in numbers]
    lo = parsed[0]
    hi = parsed[1] if len(parsed) > 1 else None
    return lo, hi


def _sane_transaction_date(raw: str) -> date | None:
    """Reject (not repair) an implausible date rather than silently
    fabricating a corrected one — congresswatch.us has at least one observed
    record with a transaction dated months in the future."""
    try:
        parsed = date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None
    today = et_today()
    if parsed > today:
        return None
    if parsed < today - timedelta(days=365 * _MAX_PLAUSIBLE_AGE_YEARS):
        return None
    return parsed


def _brackets_overlap(a: tuple[float | None, float | None], b: tuple[float | None, float | None]) -> bool:
    a_lo, a_hi = a
    b_lo, b_hi = b
    if a_lo is None or b_lo is None:
        return False
    a_hi = a_hi if a_hi is not None else a_lo
    b_hi = b_hi if b_hi is not None else b_lo
    return a_lo <= b_hi and b_lo <= a_hi


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp, path)


class CongressionalTradingProvider:
    """Credentialless, dual-sourced Congress trading-disclosure provider.

    Implements the same `SmartMoneySource` protocol as `SECForm4Provider`
    (``refresh()`` does the network work and caches; ``fetch()`` is
    cache-only and applies the shared materiality/cluster reduction).
    """

    def __init__(
        self,
        *,
        kadoa_url: str = KADOA_TRADES_URL,
        congresswatch_url: str = CONGRESSWATCH_TRADES_URL,
        data_dir: str = "data/smart_money/congressional",
        user_agent: str = DEFAULT_USER_AGENT,
        request_timeout_s: float = 15.0,
        max_trades_per_source: int = 10_000,
        assumed_max_disclosure_lag_days: int = 45,
        # 180, not 30 — see `SmartMoneyConfig.congress_lookback_days` in
        # src/config.py for the full reasoning (45-day STOCK Act deadline,
        # filing-at-the-deadline behaviour in practice, and a comparable free
        # tool's documented 180-day default). Intentionally unrelated to
        # `SECForm4Provider`'s much tighter window, which tracks Form 4's
        # ~2-business-day deadline instead.
        lookback_days: int = 180,
        min_transaction_value_usd: float = 100_000,
        external_min_transaction_value_usd: float = 250_000,
        cluster_window_days: int = 2,
        min_cluster_owners: int = 2,
        max_observations: int = 40,
        session: requests.Session | None = None,
    ):
        self.kadoa_url = kadoa_url
        self.congresswatch_url = congresswatch_url
        self.data_dir = Path(data_dir)
        self.observations_path = self.data_dir / "observations.json"
        self.kadoa_cache_path = self.data_dir / "kadoa_raw.json"
        self.congresswatch_cache_path = self.data_dir / "congresswatch_raw.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent.strip() or DEFAULT_USER_AGENT
        self.request_timeout_s = max(1.0, float(request_timeout_s))
        self.max_trades_per_source = max(1, int(max_trades_per_source))
        self.assumed_max_disclosure_lag_days = max(1, int(assumed_max_disclosure_lag_days))
        self.lookback_days = max(1, int(lookback_days))
        self.min_transaction_value_usd = max(0.0, float(min_transaction_value_usd))
        self.external_min_transaction_value_usd = max(
            self.min_transaction_value_usd, float(external_min_transaction_value_usd),
        )
        self.cluster_window_days = max(1, int(cluster_window_days))
        self.min_cluster_owners = max(2, int(min_cluster_owners))
        self.max_observations = max(1, int(max_observations))
        self.session = session or requests.Session()
        self._cache_lock = threading.Lock()

    def _load_json(self, path: Path, fallback):
        try:
            return json.loads(path.read_text()) if path.exists() else fallback
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("Congressional-trading cache unreadable at %s: %s", path, exc)
            return fallback

    def _get_source(self, url: str, cache_path: Path) -> tuple[list, str | None]:
        """Fetch one source's raw JSON list, falling back to its last good
        cache on any failure. Never raises — this is exactly the posture
        `SECForm4Provider._listed_map` already uses for its own stale-cache
        fallback."""
        try:
            response = self.session.get(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json, */*",
                },
                timeout=self.request_timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload if isinstance(payload, list) else payload.get("trades", [])
            if not isinstance(rows, list):
                raise ValueError("unexpected_payload_shape")
            rows = rows[: self.max_trades_per_source]
            _atomic_json(cache_path, rows)
            return rows, None
        except Exception as exc:
            logger.warning("Congressional source unavailable (%s): %s", url, exc)
            cached = self._load_json(cache_path, None)
            if isinstance(cached, list):
                return cached, f"stale_cache:{type(exc).__name__}"
            return [], f"unavailable:{type(exc).__name__}"

    @staticmethod
    def _normalize_kadoa(row: dict) -> dict | None:
        ticker = _symbol(row.get("ticker", ""))
        transaction_date = _sane_transaction_date(row.get("transaction_date"))
        filing_date = _sane_transaction_date(row.get("filing_date"))
        actor = str(row.get("filer_name") or "").strip()
        if not ticker or transaction_date is None or filing_date is None or not actor:
            return None
        low, high = _amount_bracket(
            row.get("amount_range_low"), row.get("amount_range_high"),
            row.get("amount_range_label", ""),
        )
        chamber = str(row.get("chamber") or "").strip().lower()
        return {
            "source": "kadoa",
            "symbol": ticker,
            "actor": actor,
            "actor_key": _normalize_actor_name(actor),
            "actor_id": str(row.get("filer_id") or ""),
            "direction": _direction(row.get("transaction_type")),
            "amount_low": low,
            "amount_high": high,
            "amount_label": str(row.get("amount_range_label") or ""),
            "transaction_date": transaction_date,
            "disclosure_date": filing_date,
            "disclosure_date_estimated": False,
            "source_url": str(row.get("doc_url") or ""),
            "chamber": chamber,
        }

    @classmethod
    def _normalize_congresswatch(cls, row: dict) -> dict | None:
        ticker = _symbol(row.get("ticker", ""))
        transaction_date = _sane_transaction_date(row.get("transaction_date"))
        actor = str(row.get("member_name") or "").strip()
        if not ticker or transaction_date is None or not actor:
            return None
        low, high = _amount_bracket(None, None, row.get("amount", ""))
        # congresswatch.us carries no filing/disclosure-date field at all
        # (verified against the live feed, 2026-09-04) — see module
        # docstring. Estimate conservatively at the statutory ceiling rather
        # than assuming the trade was just disclosed.
        estimated = min(
            et_today(),
            transaction_date + timedelta(days=45),
        )
        chamber = str(row.get("chamber") or "").strip().lower()
        return {
            "source": "congresswatch",
            "symbol": ticker,
            "actor": actor,
            "actor_key": _normalize_actor_name(actor),
            "actor_id": str(row.get("bioguide_id") or ""),
            "direction": _direction(row.get("type")),
            "amount_low": low,
            "amount_high": high,
            "amount_label": str(row.get("amount") or ""),
            "transaction_date": transaction_date,
            "disclosure_date": estimated,
            "disclosure_date_estimated": True,
            "source_url": str(row.get("ptr_link") or ""),
            "chamber": chamber,
        }

    @staticmethod
    def _merge_group(rows: list[dict]) -> dict:
        """Merge one (ticker, actor, transaction_date) group into a single
        normalized record, flagging any disagreement rather than resolving
        it silently."""
        by_source = {row["source"]: row for row in rows}
        kadoa = by_source.get("kadoa")
        congresswatch = by_source.get("congresswatch")
        primary = kadoa or congresswatch
        assert primary is not None

        if kadoa and congresswatch:
            direction_agrees = kadoa["direction"] == congresswatch["direction"]
            amount_agrees = _brackets_overlap(
                (kadoa["amount_low"], kadoa["amount_high"]),
                (congresswatch["amount_low"], congresswatch["amount_high"]),
            )
            notes = []
            if not direction_agrees:
                notes.append(
                    f"direction disagreement: kadoa={kadoa['direction']} "
                    f"congresswatch={congresswatch['direction']}"
                )
            if not amount_agrees:
                notes.append(
                    f"amount bracket disagreement: kadoa={kadoa['amount_label']!r} "
                    f"congresswatch={congresswatch['amount_label']!r}"
                )
            agreement = "agreement" if (direction_agrees and amount_agrees) else "discrepancy"
            note = "; ".join(notes)
            # kadoa is primary: it carries a real filing_date and is the
            # more structured of the two feeds. Its direction/amount are
            # kept as canonical on a disagreement; the disagreement itself
            # is never hidden — it is recorded in cross_source_note.
            merged = dict(kadoa)
            merged["source_url"] = kadoa["source_url"] or congresswatch["source_url"]
            merged["amount_low"] = min(
                v for v in (kadoa["amount_low"], congresswatch["amount_low"]) if v is not None
            )
            merged["cross_source_agreement"] = agreement
            merged["cross_source_note"] = note
            return merged

        merged = dict(primary)
        merged["cross_source_agreement"] = "single_source"
        merged["cross_source_note"] = ""
        return merged

    def _dedupe(self, kadoa_rows: list[dict], congresswatch_rows: list[dict]) -> list[dict]:
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for row in kadoa_rows + congresswatch_rows:
            key = (row["symbol"], row["actor_key"], row["transaction_date"].isoformat())
            groups[key].append(row)
        return [self._merge_group(rows) for rows in groups.values()]

    def refresh(self) -> dict:
        """Network refresh with a JSON-safe status/result summary.

        Fail-open per source: either feed being unreachable, timed out or
        malformed degrades to that feed's last good cache (or an empty
        list, on a first-ever run) — it never prevents the other feed's
        data, or the rest of the smart-money pipeline, from proceeding.
        """
        errors: list[str] = []
        kadoa_raw, kadoa_error = self._get_source(self.kadoa_url, self.kadoa_cache_path)
        if kadoa_error:
            errors.append(f"kadoa:{kadoa_error}")
        congresswatch_raw, cw_error = self._get_source(
            self.congresswatch_url, self.congresswatch_cache_path,
        )
        if cw_error:
            errors.append(f"congresswatch:{cw_error}")

        kadoa_normalized = [
            row for raw in kadoa_raw
            if isinstance(raw, dict) and (row := self._normalize_kadoa(raw)) is not None
        ]
        congresswatch_normalized = [
            row for raw in congresswatch_raw
            if isinstance(raw, dict) and (row := self._normalize_congresswatch(raw)) is not None
        ]
        merged = self._dedupe(kadoa_normalized, congresswatch_normalized)

        # Retain enough history for the cluster window on top of the
        # lookback used at read time, same shape as SECForm4Provider.
        cutoff = et_today() - timedelta(days=self.lookback_days + self.cluster_window_days)
        kept = [row for row in merged if row["disclosure_date"] >= cutoff]

        serializable = [{
            **row,
            "transaction_date": row["transaction_date"].isoformat(),
            "disclosure_date": row["disclosure_date"].isoformat(),
        } for row in kept]
        with self._cache_lock:
            _atomic_json(self.observations_path, serializable)

        error = None
        if errors:
            error = ("provider_partial_error" if kept else "provider_error") + ":" + ",".join(errors)
        return {
            "status": (
                "provider_error" if error and not kept else
                "partial" if error else "ok"
            ),
            "kadoa_raw_count": len(kadoa_raw),
            "congresswatch_raw_count": len(congresswatch_raw),
            "merged_count": len(merged),
            "cached_observations": len(kept),
            "discrepancy_count": sum(
                row["cross_source_agreement"] == "discrepancy" for row in kept
            ),
            "error": error,
        }

    def fetch(self, symbols: list[str]) -> tuple[list[SmartMoneyObservation], str | None]:
        """Cache-only. Congressional data is confirmatory context for the
        already-configured trading universe only — unlike SEC Form 4's
        external-purchase admission lane, it never grows the universe (the
        seat's acceptance contract ties symbol admission to an exact SEC
        Form 4 open-market `P`, not to congressional disclosures)."""
        core = {_symbol(s) for s in symbols if str(s).strip()}
        raw_rows = self._load_json(self.observations_path, [])
        parsed: list[SmartMoneyObservation] = []
        invalid = 0
        for raw in raw_rows if isinstance(raw_rows, list) else []:
            try:
                symbol = _symbol(raw.get("symbol", ""))
                if symbol not in core:
                    continue
                transaction_date = date.fromisoformat(str(raw["transaction_date"]))
                disclosure_date = date.fromisoformat(str(raw["disclosure_date"]))
                age_days = max(0, (et_today() - disclosure_date).days)
                if age_days > self.lookback_days:
                    continue
                freshness = "fresh" if age_days <= 7 else (
                    "delayed" if age_days <= self.lookback_days else "stale"
                )
                lag_days = max(0, (disclosure_date - transaction_date).days)
                item = SmartMoneyObservation(
                    symbol=symbol,
                    stream="congressional",
                    actor=raw.get("actor", ""),
                    actor_cik=raw.get("actor_id", ""),
                    direction=raw.get("direction", "unknown"),
                    amount_range=raw.get("amount_label", ""),
                    transaction_date=transaction_date,
                    disclosure_date=disclosure_date,
                    known_at=datetime.combine(disclosure_date, datetime.min.time()),
                    source_url=raw.get("source_url") or "https://congresswatch.us/",
                    transaction_value_usd=raw.get("amount_low"),
                    in_core_universe=True,
                    in_trading_universe=True,
                    # Congressional disclosures never independently admit a
                    # new symbol or trigger the transient lane — see
                    # `smart_money_analyst.py`'s module docstring.
                    admission_eligible=False,
                    transient_admission_eligible=False,
                    lag_days=lag_days,
                    disclosure_age_days=age_days,
                    freshness=freshness,
                    economic_role="confirmatory",
                    cross_source_agreement=raw.get("cross_source_agreement", ""),
                    cross_source_note=raw.get("cross_source_note", ""),
                )
            except Exception:
                invalid += 1
                continue
            parsed.append(item)

        survivors = cluster_survivors(
            parsed,
            threshold_fn=lambda symbol: (
                self.min_transaction_value_usd
                if symbol in core else self.external_min_transaction_value_usd
            ),
            cluster_window_days=self.cluster_window_days,
            min_cluster_owners=self.min_cluster_owners,
        )
        ordered = sorted(
            survivors.values(),
            key=lambda item: (
                -(item.transaction_value_usd or 0),
                item.disclosure_age_days,
                item.actor,
            ),
        )[: self.max_observations]
        error = f"cache_partial_error:{invalid}_invalid_rows" if invalid else None
        return ordered, error


class CombinedSmartMoneyProvider:
    """Fans one `SmartMoneySource` call out to several, concatenating
    results. Each sub-provider's failure is isolated: one raising or timing
    out never prevents the others' evidence, or the run, from proceeding —
    same fail-open posture each sub-provider already applies internally.
    """

    def __init__(self, providers: list[SmartMoneySource]):
        self.providers = [p for p in providers if p is not None]

    def refresh(self) -> dict:
        # Keyed by index+class name, not class name alone: two providers of
        # the same class (or two test doubles that happen to share one)
        # must not collide and silently drop one result from `results`.
        results: dict[str, dict] = {}
        errors: list[str] = []
        for index, provider in enumerate(self.providers):
            name = f"{index}:{type(provider).__name__}"
            try:
                results[name] = provider.refresh()
                if results[name].get("error"):
                    errors.append(f"{name}:{results[name]['error']}")
            except Exception as exc:
                logger.warning("Smart-money sub-provider refresh failed (%s): %s", name, exc)
                results[name] = {"status": "provider_error", "error": str(exc)}
                errors.append(f"{name}:refresh_exception:{type(exc).__name__}")
        return {
            "status": (
                "ok" if not errors else
                "provider_error" if all(
                    r.get("status") == "provider_error" for r in results.values()
                ) else "partial"
            ),
            "providers": results,
            "error": "; ".join(errors) or None,
        }

    def fetch(self, symbols: list[str]) -> tuple[list[SmartMoneyObservation], str | None]:
        observations: list[SmartMoneyObservation] = []
        errors: list[str] = []
        for index, provider in enumerate(self.providers):
            name = f"{index}:{type(provider).__name__}"
            try:
                rows, error = provider.fetch(symbols)
                observations.extend(rows)
                if error:
                    errors.append(f"{name}:{error}")
            except Exception as exc:
                logger.warning("Smart-money sub-provider fetch failed (%s): %s", name, exc)
                errors.append(f"{name}:fetch_exception:{type(exc).__name__}")
        error = "; ".join(errors) or None
        return observations, error
