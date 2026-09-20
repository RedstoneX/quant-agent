"""Universe expansion and pruning — THE admission screen.

Built 2026-09-19 from the design agreed with the owner on 2026-09-01
(docs/INCIDENT_HISTORY.md, "Universe expansion and pruning — DESIGN AGREED
WITH THE OWNER 2026-09-01"). Owner, restating it 2026-09-19: add stocks that
pass standard desk filters — "a certain amount of liquidity, a certain
minimum price ... filter out the garbage, the penny stocks, the highly
speculative" — and remove the ones that stop passing.

ONE SCREEN. Every path that can put a symbol outside the hand-typed
`trading.universe` in front of the desk calls `screen_symbol` (or its batch
form in `run_screen`): the weekly broker-asset-list screen, the SEC Form 4
smart-money side door and the Phase 9 nomination side door. Behind
`universe_screen.enabled`, default OFF; with it off every caller keeps its
pre-existing behaviour.

THE CRITERIA, and where each threshold comes from (every number is in
`config/number_ledger.yaml`; nothing below is chosen here):

  asset       broker says the symbol exists, is `active`, `tradable`, a US
              equity on a listed exchange, and is not a warrant, unit,
              right, preferred, depositary receipt or fund. Delisted /
              inactive / untradable is PERMANENT: an admitted name is
              removed at once, no second chance.
  borrow      broker `shortable` AND easy to borrow — half the point is
              bearish trades. Read from `borrow_status`, falling back to
              the deprecated `easy_to_borrow` flag (Alpaca sunsets it
              2026-09-22; https://docs.alpaca.markets/reference/get-v2-assets-1).
  history     a year of daily bars: the first bar is at least a calendar
              year before the last, AND there are enough bars for the
              200-session average plus the 10-session slope the analyst
              reads (`technical.LONGEST_INDICATOR_WINDOW` +
              `context._SLOPE_LOOKBACK`).
  price       last close >= `min_price_usd` ($5, SEC Rule 3a51-1(d): a
              security priced at five dollars or more is not a penny stock).
  spread      the Corwin & Schultz (J. Finance 2012) high-low spread
              estimate over that year of bars. Half of it — the cost of
              crossing from mid to the far side — must not exceed the
              desk's own entry-slippage belt (`execution.
              max_entry_slippage_bps`): a name whose ordinary half-spread
              is already past the belt is one the execution stage would
              refuse to cross. Replaces the $10M dollar-volume floor, as the
              design required. Estimated from bars, not quotes, because
              this account sees IEX quotes only and those are routinely
              absurd (src/pipeline_stages.py, the CCJ 15%-spread note).
  volatility  ATR(14) / price <= STOP_SANITY_FLOOR_FRACTION /
              `risk.min_stop_atr_multiple`. Past it, the desk's own
              minimum unbacked stop (k x ATR under price) sits below half
              the price, which the midday stop-sanity rule refuses as a
              typo — the name fails by construction, so it is screened at
              the door.
  size        market capitalisation >= `min_market_cap_usd` ($30M, the
              Russell US indexes' eligibility floor).
  sector      resolves to a real sector — the sector cap needs one. Kept
              from the pre-existing side-door gate.
  takeover    no pending acquisition: the issuer's SEC filing history
              holds no merger proxy / tender-offer filing that a later
              8-K Item 1.02 (termination of a material definitive
              agreement) has not followed.

NOT criteria, on the owner's ruling: no earnings-date requirement, no
maximum price.

A screen that could not READ something (a data outage) is INCONCLUSIVE: it
admits nothing and counts neither for nor against an admitted name.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

#: The midday stop-sanity floor, restated so the volatility ceiling can be
#: derived from it: a trailing stop below this fraction of price is refused
#: as a model typo (`TradingPipeline._midday_execute_llm_actions`). A
#: `derived` ledger row pins this to that inline factor, so the two cannot
#: drift apart silently.
STOP_SANITY_FLOOR_FRACTION = 0.5

#: Calendar days of daily bars requested per symbol. A fetch size, not a
#: threshold: it only has to exceed the one-year history test with room for
#: holidays and a missing first week. Ledger: not-trade-governing.
HISTORY_FETCH_DAYS = 400

#: Exchanges a listed US common stock trades on, as Alpaca spells them
#: (lower-cased; the SDK's enum text is `assetexchange.<name>`). Same set as
#: the pre-existing side-door gate in `AlpacaBroker.
#: get_transient_equity_eligibility`. OTC is deliberately absent.
LISTED_EXCHANGES = frozenset({"nyse", "nasdaq", "amex", "arca", "bats"})

#: Ticker suffixes that mark a warrant, unit or right (design: "exclude
#: warrants, units and rights"; a delisted warrant once reached the data
#: layer and caused a recursion fault). Same list as the pre-existing gate.
NON_EQUITY_SUFFIXES = (".WS", ".WSA", ".WSB", ".U", ".UN", ".RT", "-WS", "-U", "-RT")

#: Security-name words that mark something other than a common share. Word
#: boundaries matter: the pre-existing gate's `" unit"` substring also
#: matches "First United", which this does not.
_NON_COMMON_NAME = re.compile(
    r"\b(warrants?|units?|rights?|preferred|depositary|american deposit\w*|"
    r"etf|exchange traded fund|fund shares|notes?|debentures?)\b",
    re.IGNORECASE,
)

#: SEC submission types that mean the issuer is the subject of a pending
#: acquisition: merger proxies and information statements (Schedule 14A /
#: 14C "M" variants), the target's tender-offer response (SC 14D9 and its
#: pre-commencement SC14D9C), a third-party tender offer (SC TO-T, SC TO-C)
#: and a going-private transaction (SC 13E3). SEC lists these as proxy and
#: Williams Act submission types:
#: https://www.sec.gov/submit-filings/filer-support-resources/how-do-i-guides/understand-edgarlink-online-submission-types
#: Spellings checked against EDGAR full-text search on 2026-09-19 (each
#: returned live filings in 2026-06..09).
TAKEOVER_FORMS = frozenset({
    "PREM14A", "DEFM14A", "PREM14C", "DEFM14C",
    "SC 14D9", "SC14D9C", "SC TO-T", "SC TO-C", "SC 13E3",
})

#: 8-K item "Termination of a Material Definitive Agreement"
#: (https://www.sec.gov/files/form8-k.pdf). A later 1.02 is read as the deal
#: having died. It can also be an unrelated agreement — that errs toward
#: admitting a name, and is recorded as a known limit.
TERMINATION_ITEM = "1.02"

#: Failure codes that mean "could not read", never "failed the screen".
INCONCLUSIVE = frozenset({
    "asset_lookup_failed", "market_data_unavailable", "profile_unavailable",
    "takeover_lookup_failed", "screen_deadline",
})

#: Failure codes that remove an admitted name immediately.
PERMANENT = frozenset({"asset_not_found", "asset_inactive", "asset_not_tradable"})

#: Plain words for every code, for the owner's morning message.
PLAIN_REASON = {
    "asset_not_found": "the broker no longer lists it (delisted)",
    "asset_inactive": "the broker marks it inactive (delisted)",
    "asset_not_tradable": "the broker marks it not tradable (halted)",
    "asset_lookup_failed": "the broker could not be read",
    "not_us_equity": "not a US stock",
    "unsupported_exchange": "not on a main US exchange",
    "not_common_stock": "a warrant, unit, right, preferred, receipt or fund",
    "not_shortable": "cannot be sold short",
    "hard_to_borrow": "hard to borrow for a short",
    "no_price_history": "no price history",
    "insufficient_history": "less than a year of price history",
    "price_below_minimum": "a penny stock (under $5)",
    "spread_too_wide": "too costly to trade (wide bid-ask spread)",
    "spread_unmeasurable": "its trading cost could not be measured",
    "volatility_above_ceiling": "too wild to place a sane stop",
    "company_too_small": "company too small (under $30M)",
    "market_cap_unknown": "company size unknown",
    "unresolved_sector": "no sector",
    "pending_takeover": "being taken over",
    "takeover_status_unknown": "no SEC filing record to check for a takeover",
    "market_data_unavailable": "price data could not be read",
    "profile_unavailable": "company data could not be read",
    "takeover_lookup_failed": "SEC filings could not be read",
    "screen_deadline": "ran out of time",
    "passed": "passes every check",
}


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ScreenThresholds:
    min_price_usd: float
    min_market_cap_usd: float
    max_half_spread_bps: float
    max_atr_fraction: float
    min_history_bars: int

    @classmethod
    def from_config(cls, config) -> "ScreenThresholds":
        from src.data.context import _SLOPE_LOOKBACK
        from src.data.technical import LONGEST_INDICATOR_WINDOW

        screen = config.universe_screen
        return cls(
            min_price_usd=float(screen.min_price_usd),
            min_market_cap_usd=float(screen.min_market_cap_usd),
            max_half_spread_bps=float(config.execution.max_entry_slippage_bps),
            max_atr_fraction=(
                STOP_SANITY_FLOOR_FRACTION / float(config.risk.min_stop_atr_multiple)
            ),
            min_history_bars=int(LONGEST_INDICATOR_WINDOW + _SLOPE_LOOKBACK),
        )


# --------------------------------------------------------------------------
# Measures
# --------------------------------------------------------------------------

def corwin_schultz_spread(bars: list) -> float | None:
    """Mean Corwin–Schultz (2012) bid-ask spread estimate, as a fraction.

    Corwin & Schultz, "A Simple Way to Estimate Bid-Ask Spreads from Daily
    High and Low Prices", J. Finance 67(2), 719-760
    (https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1106193). For each
    pair of consecutive days: beta = ln(H1/L1)^2 + ln(H2/L2)^2,
    gamma = ln(max(H1,H2)/min(L1,L2))^2,
    alpha = (sqrt(2 beta) - sqrt(beta)) / (3 - 2 sqrt 2) - sqrt(gamma / (3 - 2 sqrt 2)),
    S = 2 (e^alpha - 1) / (1 + e^alpha). Day 2's range is shifted by any
    overnight gap past day 1's close, as in the authors' procedure
    (replication of their SAS code:
    https://github.com/ioannisrpt/Corwin_Schultz_2012).

    Negative estimates: the replication offers two treatments — floor each
    two-day estimate at zero and then average (its SPREAD_0), or average the
    raw estimates and floor the AVERAGE at zero (its XSPREAD_0). This uses
    the second. Measured 2026-09-19 on a year of daily bars, the first
    reads AAPL at 25.6 bps half-spread, TSLA 44.4, AMD 51.4 — volatility,
    not trading cost, because flooring each day discards every negative
    that would cancel a positive; the second reads them 0.2, 5.7, 0.0 and
    still reads an illiquid name (ANL) at 64 bps. Averaged over every pair
    in `bars` (the screening year). None when no pair is usable.
    """
    const = 3.0 - 2.0 * math.sqrt(2.0)
    estimates: list[float] = []
    for prev, cur in zip(bars, bars[1:]):
        try:
            h1, l1, c1 = float(prev.high), float(prev.low), float(prev.close)
            h2, l2 = float(cur.high), float(cur.low)
        except (AttributeError, TypeError, ValueError):
            continue
        if min(h1, l1, h2, l2, c1) <= 0 or h1 < l1 or h2 < l2:
            continue
        if c1 < l2:            # gapped up overnight
            h2, l2 = h2 - (l2 - c1), c1
        elif c1 > h2:          # gapped down overnight
            h2, l2 = c1, l2 + (c1 - h2)
        beta = math.log(h1 / l1) ** 2 + math.log(h2 / l2) ** 2
        gamma = math.log(max(h1, h2) / min(l1, l2)) ** 2
        alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / const - math.sqrt(gamma / const)
        estimates.append(2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha)))
    if not estimates:
        return None
    return max(sum(estimates) / len(estimates), 0.0)


def _one_year_before(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # 29 Feb
        return day.replace(year=day.year - 1, day=day.day - 1)


def _field(record, name, default=None):
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _enum_text(value) -> str:
    """Lower-cased enum text with any SDK prefix (`assetexchange.nyse`) removed."""
    text = str(getattr(value, "value", value) or "").strip().lower()
    for prefix in ("assetexchange.", "assetclass.", "assetstatus."):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def is_non_common_security(symbol: str, name: str) -> bool:
    upper = str(symbol or "").upper()
    return upper.endswith(NON_EQUITY_SUFFIXES) or bool(_NON_COMMON_NAME.search(str(name or "")))


# --------------------------------------------------------------------------
# The criteria
# --------------------------------------------------------------------------

def check_asset(symbol: str, asset) -> list[str]:
    """Broker asset-directory checks. `asset` None = the broker says it does not exist."""
    if asset is None:
        return ["asset_not_found"]
    if _enum_text(_field(asset, "status")) != "active":
        return ["asset_inactive"]
    if not bool(_field(asset, "tradable", False)):
        return ["asset_not_tradable"]
    failures = []
    if _enum_text(_field(asset, "class", _field(asset, "asset_class"))) != "us_equity":
        failures.append("not_us_equity")
    if _enum_text(_field(asset, "exchange")) not in LISTED_EXCHANGES:
        failures.append("unsupported_exchange")
    if is_non_common_security(_field(asset, "symbol", symbol) or symbol, _field(asset, "name", "")):
        failures.append("not_common_stock")
    return failures


def check_borrow(asset) -> list[str]:
    if not bool(_field(asset, "shortable", False)):
        return ["not_shortable"]
    status = str(_field(asset, "borrow_status", "") or "").strip().lower()
    if status:
        easy = status == "easy_to_borrow"
    else:
        easy = bool(_field(asset, "easy_to_borrow", False))
    return [] if easy else ["hard_to_borrow"]


def check_bars(bars: list, th: ScreenThresholds) -> tuple[list[str], dict]:
    """History, price, spread and volatility — everything read off the bars."""
    from src.data.technical import atr_series

    measured: dict[str, Any] = {}
    if not bars:
        return ["no_price_history"], measured
    first, last = bars[0], bars[-1]
    measured["history_bars"] = len(bars)
    measured["first_bar"] = str(getattr(first, "date", ""))
    failures: list[str] = []
    try:
        long_enough = getattr(first, "date") <= _one_year_before(getattr(last, "date"))
    except (AttributeError, TypeError):
        long_enough = False
    if not long_enough or len(bars) < th.min_history_bars:
        failures.append("insufficient_history")
    try:
        price = float(last.close)
    except (AttributeError, TypeError, ValueError):
        return failures + ["no_price_history"], measured
    measured["last_price"] = round(price, 4)
    if price < th.min_price_usd:
        failures.append("price_below_minimum")
    # The screening year only — older bars are not today's trading cost.
    try:
        year_start = _one_year_before(getattr(last, "date"))
        recent = [b for b in bars if getattr(b, "date") >= year_start]
    except (AttributeError, TypeError):
        recent = list(bars)
    spread = corwin_schultz_spread(recent)
    if spread is None:
        failures.append("spread_unmeasurable")
    else:
        half_bps = spread / 2 * 10_000
        measured["half_spread_bps"] = round(half_bps, 1)
        if half_bps > th.max_half_spread_bps:
            failures.append("spread_too_wide")
    atr = atr_series(bars)
    if len(atr) and price > 0:
        atr_fraction = float(atr[-1]) / price
        measured["atr_pct"] = round(atr_fraction * 100, 2)
        if atr_fraction > th.max_atr_fraction:
            failures.append("volatility_above_ceiling")
    return failures, measured


def check_profile(profile, th: ScreenThresholds) -> tuple[list[str], dict]:
    if profile is None:
        return ["profile_unavailable"], {}
    failures = []
    measured = {}
    cap = profile.get("market_cap_usd") if isinstance(profile, dict) else None
    if not isinstance(cap, (int, float)) or cap <= 0:
        failures.append("market_cap_unknown")
    else:
        measured["market_cap_usd"] = float(cap)
        if cap < th.min_market_cap_usd:
            failures.append("company_too_small")
    sector = (profile.get("sector") if isinstance(profile, dict) else None) or "Unknown"
    measured["sector"] = sector
    if sector == "Unknown":
        failures.append("unresolved_sector")
    return failures, measured


def pending_takeover(filings) -> tuple[bool, str | None]:
    """(pending, latest takeover filing date) from (form, date, items) rows."""
    latest_deal = None
    latest_termination = None
    for form, filed, items in filings or []:
        base = str(form or "").strip().upper()
        if base.endswith("/A"):
            base = base[:-2]
        day = str(filed or "")[:10]
        if base in TAKEOVER_FORMS:
            latest_deal = max(latest_deal or day, day)
        elif base == "8-K" and TERMINATION_ITEM in {
            part.strip() for part in str(items or "").split(",")
        }:
            latest_termination = max(latest_termination or day, day)
    if latest_deal is None:
        return False, None
    return (latest_termination is None or latest_termination < latest_deal), latest_deal


def check_takeover(filings) -> tuple[list[str], dict]:
    if filings is None:
        return ["takeover_status_unknown"], {}
    pending, filed = pending_takeover(filings)
    if pending:
        return ["pending_takeover"], {"takeover_filing": filed}
    return [], {}


# --------------------------------------------------------------------------
# One symbol
# --------------------------------------------------------------------------

@dataclass
class ScreenResult:
    symbol: str
    failures: list[str] = field(default_factory=list)
    measured: dict = field(default_factory=dict)

    @property
    def inconclusive(self) -> bool:
        return any(code in INCONCLUSIVE for code in self.failures)

    @property
    def permanent(self) -> bool:
        return any(code in PERMANENT for code in self.failures)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def reason(self) -> str:
        return self.failures[0] if self.failures else "passed"

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "failures": list(self.failures),
                "measured": dict(self.measured)}


@dataclass
class ScreenSources:
    """Where the screen reads from. Each callable may raise for "could not read".

    get_asset(symbol) -> record | None   (None = broker says it does not exist)
    get_bars(symbol) -> list[OHLCV]
    get_profile(symbol) -> {"market_cap_usd", "sector"} | None
    get_filings(symbol) -> [(form, filing_date, items)] | None (None = no SEC issuer)
    """
    get_asset: Callable[[str], Any]
    get_bars: Callable[[str], list]
    get_profile: Callable[[str], dict | None]
    get_filings: Callable[[str], list | None]


def screen_symbol(
    symbol: str,
    sources: ScreenSources,
    th: ScreenThresholds,
    *,
    asset: Any = ...,
    bars: list | None = None,
) -> ScreenResult:
    """Run every criterion on one symbol, cheapest first; stop at the first
    failing stage so a name that fails for free costs no network reads."""
    result = ScreenResult(symbol=symbol)
    if asset is ...:
        try:
            asset = sources.get_asset(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("universe screen: asset lookup failed for %s: %s", symbol, exc)
            result.failures = ["asset_lookup_failed"]
            return result
    result.failures = check_asset(symbol, asset)
    if not result.failures:
        result.failures = check_borrow(asset)
    if result.failures:
        return result
    if bars is None:
        try:
            bars = sources.get_bars(symbol) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("universe screen: bars failed for %s: %s", symbol, exc)
            result.failures = ["market_data_unavailable"]
            return result
    failures, measured = check_bars(bars, th)
    result.measured.update(measured)
    if failures:
        result.failures = failures
        return result
    try:
        profile = sources.get_profile(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("universe screen: profile failed for %s: %s", symbol, exc)
        profile = None
    failures, measured = check_profile(profile, th)
    result.measured.update(measured)
    if failures:
        result.failures = failures
        return result
    try:
        filings = sources.get_filings(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("universe screen: SEC filings failed for %s: %s", symbol, exc)
        result.failures = ["takeover_lookup_failed"]
        return result
    failures, measured = check_takeover(filings)
    result.measured.update(measured)
    result.failures = failures
    return result


# --------------------------------------------------------------------------
# Persisted state and the prune state machine
# --------------------------------------------------------------------------

def iso_week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def empty_state() -> dict:
    return {"admitted": {}, "screened": {}, "removed": {}, "events": []}


class UniverseStore:
    """`<data_dir>/universe_state.json` — the admitted set and its history.

    admitted[SYM]  = {admitted_on, status: active|flagged, flagged_on,
                      last_screen_week, last_screened_on, failures,
                      measured, last_offered_on}
    screened[SYM]  = {week, failures}   (candidates that did not pass)
    removed[SYM]   = {removed_on, reason}
    events         = changes not yet shown in a morning message
    """

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "universe_state.json"

    def load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text()) if self.path.exists() else None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("universe state unreadable at %s: %s", self.path, exc)
            raw = None
        state = empty_state()
        if isinstance(raw, dict):
            for key in state:
                if isinstance(raw.get(key), type(state[key])):
                    state[key] = raw[key]
        return state

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".universe.")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _event(state: dict, action: str, symbol: str, reasons: list[str], today: date, measured=None):
    event = {
        "date": today.isoformat(),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action": action,
        "symbol": symbol,
        "reasons": list(reasons),
    }
    if measured:
        event["measured"] = dict(measured)
    state["events"].append(event)
    logger.info(
        "UNIVERSE_CHANGE %s %s: %s", action.upper(), symbol,
        ", ".join(reasons) or "passed",
    )
    return event


def apply_result(
    state: dict,
    result: ScreenResult,
    *,
    today: date,
    held: set[str],
) -> dict | None:
    """Fold one screen result into the state. Returns the event, if any.

    Admitted name: pass clears a flag; a PERMANENT failure removes it at once;
    any other failure flags it the first week and removes it the second
    consecutive week. A held name is never removed — the removal is deferred
    and reported, because removing it would cut a live position off from
    analysis while its stop still sits at the broker.
    Candidate: pass admits it; a failure is cached for the week.
    Inconclusive (a read failed): nothing changes, and it is re-read next run.
    """
    symbol = result.symbol
    week = iso_week(today)
    admitted = state["admitted"].get(symbol)
    if result.inconclusive:
        return None
    if admitted is None:
        if result.passed:
            state["admitted"][symbol] = {
                "admitted_on": today.isoformat(), "status": "active",
                "flagged_on": None, "last_screen_week": week,
                "last_screened_on": today.isoformat(), "failures": [],
                "measured": dict(result.measured), "last_offered_on": None,
            }
            state["screened"].pop(symbol, None)
            state["removed"].pop(symbol, None)
            return _event(state, "added", symbol, ["passed"], today, result.measured)
        state["screened"][symbol] = {"week": week, "failures": list(result.failures)}
        return None

    already_this_week = admitted.get("last_screen_week") == week
    if already_this_week and not result.permanent:
        return None
    admitted.update(
        last_screen_week=week, last_screened_on=today.isoformat(),
        failures=list(result.failures), measured=dict(result.measured),
    )
    if result.passed:
        if admitted.get("status") == "flagged":
            admitted.update(status="active", flagged_on=None)
            return _event(state, "cleared", symbol, ["passed"], today)
        return None
    remove = result.permanent or admitted.get("status") == "flagged"
    if not remove:
        admitted.update(status="flagged", flagged_on=today.isoformat())
        return _event(state, "flagged", symbol, result.failures, today, result.measured)
    if symbol in held:
        admitted.update(status="flagged", flagged_on=admitted.get("flagged_on") or today.isoformat())
        return _event(state, "removal_deferred_held", symbol, result.failures, today)
    del state["admitted"][symbol]
    state["removed"][symbol] = {"removed_on": today.isoformat(), "reason": result.reason}
    return _event(state, "removed", symbol, result.failures, today)


# --------------------------------------------------------------------------
# The weekly screen
# --------------------------------------------------------------------------

@dataclass
class ScreenRun:
    candidates: int = 0
    screened: int = 0
    passed: int = 0
    inconclusive: int = 0
    deadline_hit: bool = False
    events: list[dict] = field(default_factory=list)
    failures_by_reason: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "candidates": self.candidates, "screened": self.screened,
            "passed": self.passed, "inconclusive": self.inconclusive,
            "deadline_hit": self.deadline_hit, "events": list(self.events),
            "failures_by_reason": dict(sorted(self.failures_by_reason.items())),
        }


def _asset_symbol(asset) -> str:
    raw = str(_field(asset, "symbol", "") or "").strip().upper()
    # Alpaca spells a class share BRK.B; the desk (and yfinance) BRK-B.
    return re.sub(r"^([A-Z]+)\.([A-Z])$", r"\1-\2", raw)


def run_screen(
    state: dict,
    *,
    assets: list,
    sources: ScreenSources,
    get_bars_batch: Callable[[list[str]], dict[str, list]],
    th: ScreenThresholds,
    today: date,
    held: Iterable[str],
    configured: Iterable[str],
    deadline: float,
    batch_size: int,
    confirm_missing_asset: Callable[[str], Any] | None = None,
) -> ScreenRun:
    """One incremental pass. Safe to run every evening: each symbol is
    screened at most once per ISO week, admitted names first, then the
    candidates never screened, then the oldest-screened. Delisted/halted
    admitted names are checked on EVERY pass (immediate removal)."""
    run = ScreenRun()
    held_set = {str(s).strip().upper() for s in held}
    configured_set = {str(s).strip().upper() for s in configured}
    week = iso_week(today)
    by_symbol = {_asset_symbol(a): a for a in assets if _asset_symbol(a)}

    def _record(result: ScreenResult) -> None:
        run.screened += 1
        if result.inconclusive:
            run.inconclusive += 1
        elif result.passed:
            run.passed += 1
        for code in result.failures[:1]:
            run.failures_by_reason[code] = run.failures_by_reason.get(code, 0) + 1
        event = apply_result(state, result, today=today, held=held_set)
        if event is not None:
            run.events.append(event)

    # 1. Delisted / halted admitted names: every pass, no weekly wait.
    for symbol in sorted(state["admitted"]):
        asset = by_symbol.get(symbol)
        if asset is None and confirm_missing_asset is None:
            # Absent from a LIST is not proof of delisting; without a
            # per-symbol confirmation nothing is removed.
            continue
        if asset is None:
            try:
                asset = confirm_missing_asset(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("universe screen: could not confirm %s: %s", symbol, exc)
                continue
            if asset is not None:
                by_symbol[symbol] = asset
        failures = check_asset(symbol, asset)
        if any(code in PERMANENT for code in failures):
            _record(ScreenResult(symbol=symbol, failures=failures))

    # 2. Who is due this week.
    due_admitted = [
        s for s in sorted(state["admitted"])
        if state["admitted"][s].get("last_screen_week") != week and s in by_symbol
    ]
    candidates = [
        s for s in sorted(by_symbol)
        if s not in state["admitted"] and s not in configured_set
    ]
    run.candidates = len(candidates)
    fresh = [s for s in candidates if s not in state["screened"]]
    stale = sorted(
        (s for s in candidates if s in state["screened"]
         and state["screened"][s].get("week") != week),
        key=lambda s: (state["screened"][s].get("week") or "", s),
    )

    # 3. Asset-level checks are free (the list is already in hand): settle
    # every candidate that fails them without a single market-data read.
    needs_bars: list[str] = list(due_admitted)
    for symbol in fresh + stale:
        asset = by_symbol[symbol]
        failures = check_asset(symbol, asset) or check_borrow(asset)
        if failures:
            _record(ScreenResult(symbol=symbol, failures=failures))
        else:
            needs_bars.append(symbol)

    # 4. Bars in batches, then the per-symbol reads, until the deadline.
    for start in range(0, len(needs_bars), max(1, int(batch_size))):
        if time.monotonic() >= deadline:
            run.deadline_hit = True
            break
        chunk = needs_bars[start:start + max(1, int(batch_size))]
        try:
            bars_by_symbol = get_bars_batch(chunk) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("universe screen: bar batch failed (%d symbols): %s", len(chunk), exc)
            for symbol in chunk:
                _record(ScreenResult(symbol=symbol, failures=["market_data_unavailable"]))
            continue
        for symbol in chunk:
            if time.monotonic() >= deadline:
                run.deadline_hit = True
                break
            result = screen_symbol(
                symbol, sources, th,
                asset=by_symbol[symbol], bars=bars_by_symbol.get(symbol) or [],
            )
            _record(result)
        if run.deadline_hit:
            break
    if run.deadline_hit:
        logger.info(
            "universe screen: deadline reached after %d symbols; the rest resume next run",
            run.screened,
        )
    return run


# --------------------------------------------------------------------------
# Per-run selection (the cap on what reaches the portfolio manager)
# --------------------------------------------------------------------------

def select_for_run(state: dict, *, held: Iterable[str], cap: int, today: date) -> dict[str, dict]:
    """Admitted names that join THIS run's research surface.

    Every held admitted name (coverage, not discovery — never capped), plus
    at most `cap` others, least-recently-offered first so the whole admitted
    set is rotated through rather than the same few names every morning.
    Stamps `last_offered_on` on the ones chosen.
    """
    held_set = {str(s).strip().upper() for s in held}
    admitted = state.get("admitted", {})
    chosen: dict[str, dict] = {}
    for symbol in sorted(admitted):
        if symbol in held_set:
            chosen[symbol] = admitted[symbol]
    others = sorted(
        (s for s in admitted if s not in held_set),
        key=lambda s: (admitted[s].get("last_offered_on") or "", admitted[s].get("admitted_on") or "", s),
    )
    for symbol in others[:max(0, int(cap))]:
        chosen[symbol] = admitted[symbol]
    for symbol in chosen:
        admitted[symbol]["last_offered_on"] = today.isoformat()
    return {
        symbol: {
            "temporary": True,
            "reason": "universe_screen_admission",
            "admitted_on": record.get("admitted_on"),
            "status": record.get("status"),
            "held": symbol in held_set,
            **{k: v for k, v in (record.get("measured") or {}).items()},
        }
        for symbol, record in chosen.items()
    }


# --------------------------------------------------------------------------
# The owner's words
# --------------------------------------------------------------------------

def plain_reasons(codes: Iterable[str]) -> str:
    return "; ".join(PLAIN_REASON.get(code, code.replace("_", " ")) for code in codes)


def describe_event(event: dict) -> str:
    symbol = event.get("symbol", "?")
    action = event.get("action")
    why = plain_reasons(event.get("reasons") or [])
    if action == "added":
        return f"{symbol} added — passes every check"
    if action == "flagged":
        return f"{symbol} flagged (removed if it fails again next week) — {why}"
    if action == "removed":
        return f"{symbol} removed — {why}"
    if action == "cleared":
        return f"{symbol} flag cleared — passes again"
    if action == "removal_deferred_held":
        return f"{symbol} should be removed but is HELD, so kept and watched — {why}"
    return f"{symbol} {action} — {why}"
