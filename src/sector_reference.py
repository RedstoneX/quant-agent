"""L0 reference data: sector resolution (owns the sector cluster).

Moved out of src/execution/broker.py so that src/risk/rules.py need not import
upward into an execution adapter. Function bodies are byte-for-byte the originals.
src/execution/broker.py keeps every name reachable through a write-through
module mirror (one __getattr__ / one __setattr__), so tests that patch
`src.execution.broker._get_sector` rebind the object the moved code calls.

Imports nothing from src/execution/ or any adapter.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

import yfinance as yf

from src.models import _ALLOWED_SECTORS, _SECTOR_ALIASES
from src.sentinel.counted import record_swallowed

# Same logger name the code had inside broker.py, so log capture is unchanged.
logger = logging.getLogger("src.execution.broker")

# Index ETFs that have no single sector — bucket them as "Broad".
_INDEX_ETFS = {"SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "IVV"}

# Sector / thematic ETFs → their canonical sector bucket.
#
# WHY (2026-07-16 audit): yfinance's `.info` carries no `sector` key for ETFs,
# so _get_sector fell through to "Unknown" for every one of them. Two silent
# failures followed: (1) `max_sector_pct` is gated on `new_sector != "Unknown"`
# (risk/rules.py), so a BUY of XLV/SMH/... skipped the sector cap ENTIRELY;
# (2) a held ETF carries sector="Unknown", so it contributed $0 to the sector
# bucket of a same-sector single name — a book that is 30% XLV would let an
# LLY BUY through as if Healthcare exposure were zero. Both directions of the
# cap were dead for these symbols despite the universe being ~20% ETFs.
#
# Deterministic table, consulted BEFORE the network fetch: an ETF's sector is
# a fact about the product, not something to rediscover per process.
_ETF_SECTORS = {
    # SPDR sector suite
    "XLF": "Financial Services",
    "XLE": "Energy",
    "XLV": "Healthcare",
    "XLI": "Industrials",
    "XLP": "Consumer Defensive",
    "XLY": "Consumer Cyclical",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLB": "Basic Materials",
    "XLK": "Technology",
    "XLC": "Communication Services",
    # Semiconductor / AI thematics
    "SMH": "Technology",
    "SOXX": "Technology",
    "DRAM": "Technology",
    "CHPX": "Technology",
    # Inverse / leveraged index ETFs track a BROAD index — they have no sector
    # of their own. (Their leverage is handled separately by the signed/gross
    # multipliers in risk/rules.py.)
    "SH": "Broad",
    "SDS": "Broad",
    "PSQ": "Broad",
    "SQQQ": "Broad",
}

_SECTOR_LOOKUP_TIMEOUT_S = 10  # per-symbol ceiling on yfinance .info hang in _get_sector

# Cache sector lookups to avoid repeated API calls
_sector_cache: dict[str, str] = {}
_sector_lock = threading.Lock()

# WHY (2026-09-01 audit): a symbol whose sector never resolves reads
# identically to one with no exception at all — both come back "Unknown"
# from `_get_sector` with no further detail. That is fine for the two
# existing consumers (they only needed a sector string), but it is not
# enough for an owner-facing alert: "the network is having a bad day, this
# will self-heal" and "this instrument has no sector to find" are different
# situations and should not read the same. Best-effort, advisory only —
# NOT part of the caching contract above (an unresolved symbol is still
# never cached; see `_get_sector`'s docstring), keyed the same way as
# `_sector_cache`, and simply absent/stale when `_get_sector` itself is
# mocked out wholesale (tests) — `_sector_resolution_status_for` defaults
# to "unknown_reason" rather than guessing.
_sector_resolution_status: dict[str, str] = {}


def _sector_resolution_status_for(symbol: str) -> str:
    """Best-effort reason the last `_get_sector(symbol)` call in THIS
    process came back "Unknown". One of:

      "resolved"       - moot; the symbol has a real sector.
      "lookup_failed"  - network error, timeout, or an empty response with
                          no error — yfinance returning nothing for a real
                          symbol is usually transient (see
                          test_sector_canonicalization.py). Will retry.
      "no_sector"      - the fetch itself succeeded and returned real data,
                          just no `sector` field — this symbol may
                          genuinely be unclassifiable (e.g. an ETF outside
                          `_ETF_SECTORS`), not a network problem.
      "unknown_reason" - no attempt recorded yet for this symbol in this
                          process (fresh process, or a test/caller mocked
                          `_get_sector` directly instead of going through
                          the real fetch below).
    """
    with _sector_lock:
        return _sector_resolution_status.get(symbol, "unknown_reason")


def _canonicalize_sector(raw: str | None) -> str:
    """Normalize yfinance / LLM sector strings to the 12-value canonical enum.

    Returns "Unknown" for anything that can't be mapped — callers must decide
    whether to skip or fall back. The MacroAnalysis pydantic model uses the
    same alias table to self-heal LLM output.
    """
    if not raw:
        return "Unknown"
    s = str(raw).strip()
    if s in _ALLOWED_SECTORS:
        return s
    canon = _SECTOR_ALIASES.get(s.lower())
    if canon in _ALLOWED_SECTORS:
        return canon
    return "Unknown"


def _get_sector(symbol: str) -> str:
    """Look up sector for a symbol using yfinance. Thread-safe, cached per process.

    Output is canonicalized to the 12-value MacroSectorGuidance enum (or "Unknown"
    for un-classifiable names), so macro sector_guidance and position.sector share
    a namespace.

    Caching policy: only KNOWN sectors are cached. "Unknown" is returned but
    NOT cached, so a transient yfinance outage gets re-diagnosed on every
    call instead of freezing a stale verdict. Codex r11 P1: a one-shot
    lookup miss in --mode live used to leave the symbol cap-exempt until
    process restart. Re-querying yfinance on every call for an unresolved
    symbol is a small overhead vs. silently disabling a hard risk rule.

    2026-09-01 audit: "Unknown" used to mean EXEMPT from
    `RiskRuleEngine.check`'s sector cap (rule 5 skipped the check outright).
    80 of 101 universe symbols depend on this lookup with no offline
    fallback, so a network blip silently switched the sector cap off for
    most of the book. The gate now pools "Unknown" like any other sector
    (`sector_side_gross(..., include_unknown=True)`) and checks it against
    the same soft/hard cap pair instead of skipping it — see
    `_sector_resolution_status_for` below for WHY a given call came back
    "Unknown", which the gate surfaces as an owner-visible alert.
    """
    # _sector_lock guards ONLY the cache dict — never a network call.
    # audit F3: the old code held _sector_lock for the entire function
    # including the yfinance fetch, so one stuck symbol froze every
    # sector lookup process-wide (risk/position sizing all serialize
    # through _get_sector).
    with _sector_lock:
        cached = _sector_cache.get(symbol)
    if cached is not None:
        return cached
    if symbol.upper() in _INDEX_ETFS:
        with _sector_lock:
            _sector_cache[symbol] = "Broad"
        return "Broad"
    # Sector/thematic ETFs: yfinance .info has no `sector` for ETFs, so
    # without this table they resolve to "Unknown" and silently switch the
    # sector cap OFF (see _ETF_SECTORS). Deterministic, offline, before the fetch.
    etf_sector = _ETF_SECTORS.get(symbol.upper())
    if etf_sector is not None:
        with _sector_lock:
            _sector_cache[symbol] = etf_sector
        return etf_sector

    # Set from inside the worker thread when the fetch itself raises — read
    # back on the calling thread only after `.result()` returns (timeout
    # aside, where we already know the answer without consulting this).
    # Best-effort/advisory like the status table it feeds; not a
    # correctness dependency of the timeout/lock guarantees below.
    fetch_error = {"raised": False}

    def _fetch():
        try:
            return yf.Ticker(symbol).info or {}
        except Exception as e:
            record_swallowed("sector_reference._fetch", e, log=logger, symbol=symbol)
            fetch_error["raised"] = True
            return {}

    # yfinance .info has no hard upper bound — a stuck socket can hang
    # for far longer than _SECTOR_LOOKUP_TIMEOUT_S. audit F3: do NOT use
    # `with ThreadPoolExecutor(...)`; its __exit__ calls
    # shutdown(wait=True), which re-blocks on the hung worker after the
    # .result() timeout fires, making the ceiling illusory.
    # shutdown(wait=False, cancel_futures=True) returns immediately. A
    # still-running fetch leaks one worker thread — accepted vs. the
    # prior behaviour of stalling the whole session.
    ex = ThreadPoolExecutor(max_workers=1)
    timed_out = False
    try:
        info = ex.submit(_fetch).result(timeout=_SECTOR_LOOKUP_TIMEOUT_S)
    except FuturesTimeout:
        logger.warning("yfinance sector lookup timed out for %s", symbol)
        info = {}
        timed_out = True
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    raw = info.get("sector", "") if isinstance(info, dict) else ""
    canonical = _canonicalize_sector(raw)
    if canonical != "Unknown":
        with _sector_lock:
            _sector_cache[symbol] = canonical
        return canonical

    # Unresolved. Record WHY — never cached (see docstring above), same as
    # the "Unknown" return itself, so a self-heal on the next call is
    # re-diagnosed fresh rather than repeating a stale verdict.
    # `not info` (fetch technically completed, no exception, but returned
    # nothing) is bucketed with "lookup_failed": per
    # test_sector_canonicalization.py's own finding, an empty response for
    # a real symbol is usually transient, not proof the symbol lacks a
    # sector. "no_sector" is reserved for a fetch that came back with real
    # data and simply had no `sector` field in it.
    status = "lookup_failed" if (timed_out or fetch_error["raised"] or not info) else "no_sector"
    with _sector_lock:
        _sector_resolution_status[symbol] = status
    return canonical
