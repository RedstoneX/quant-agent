"""Daily record-only universe screen — STEP A of the owner's 2026-10-09 ruling.

Owner ruling 2026-10-09: a free, AI-free filter over all US stocks and plain
equity ETFs, run fresh every trading day, dropping only clearly dead names,
with every dropped name recorded with its reason. The "is anything
happening" cut-off is not designed yet, so this module only RECORDS: it runs
the existing eligibility checks of `src/universe_screen.py` (asset, history,
price, spread, volatility, size, sector, plain equity fund, takeover) over the
whole Alpaca active US-equity asset list and writes one durable record per
name. It sends nothing to any AI, admits nothing, removes nothing and never
touches `universe_state.json`; `universe_screen.enabled` still governs the
session path alone. No new threshold: every limit is `ScreenThresholds`.

Data and call budget:
  asset list   one Alpaca GET (`broker.list_assets`).
  daily bars   Alpaca multi-symbol `StockBarsRequest`, `universe_screen.
               bars_batch_size` symbols per request; the SDK pages at its
               10,000-bar maximum (alpaca.common.constants.DATA_V2_MAX_LIMIT,
               docs.alpaca.markets/reference/stockbars) and retries an HTTP
               429 itself. Every HTTP page is counted.
  size/sector  Yahoo company profile, one call per name that reaches it,
               CACHED: a profile read this ISO week is reused, an older one is
               re-read while time remains and otherwise reused with its date
               on the record. Calls counted.
  takeover     SEC issuer filing history, same cache rule. Calls counted.

Records: `<universe_screen.data_dir>/daily/<YYYY-MM-DD>.jsonl` (one line per
listed name: passed, dropped with its failing check(s), inconclusive on a
data outage, or "unreached: time limit") and `<YYYY-MM-DD>.summary.json`
(names listed, passed, dropped per reason, calls made, seconds taken).

Run: `python -m src.universe_daily` — a no-op unless today is a trading day
on the broker's calendar, the session has closed, and today has no record
yet, so any wake-up tick may call it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from src.universe_screen import (
    INCONCLUSIVE,
    ScreenSources,
    ScreenThresholds,
    _asset_symbol,
    _field,
    check_asset,
    check_bars,
    iso_week,
    screen_symbol,
)

logger = logging.getLogger(__name__)

UNREACHED = "unreached_time_limit"
UNREACHED_TEXT = "unreached: time limit"


# --------------------------------------------------------------------------
# Alpaca multi-symbol daily bars
# --------------------------------------------------------------------------


class AlpacaDailyBars:
    """Daily bars for many symbols per request, on the broker's existing
    Alpaca data client. `calls` counts HTTP pages actually sent."""

    def __init__(self, data_client, lookback_days: int, today: date):
        self.client = data_client
        self.lookback_days = int(lookback_days)
        self.today = today
        self.calls = 0
        original = data_client._one_request

        def _counted(*args, **kwargs):
            self.calls += 1
            return original(*args, **kwargs)

        data_client._one_request = _counted

    def __call__(self, symbols: list[str]) -> dict[str, list]:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        from src.execution.broker_parts.stop_place import _alpaca_symbol
        from src.models import OHLCV

        to_alpaca = {s: _alpaca_symbol(s) for s in symbols}
        raw = self.client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=sorted(set(to_alpaca.values())),
                timeframe=TimeFrame.Day,
                start=self.today - timedelta(days=self.lookback_days),
                end=self.today,
            )
        )
        data = raw.data if hasattr(raw, "data") else raw
        out: dict[str, list] = {}
        for symbol, alpaca_symbol in to_alpaca.items():
            parsed = []
            for bar in (data or {}).get(alpaca_symbol) or []:
                ts = getattr(bar, "timestamp", None)
                if ts is None:
                    continue
                try:
                    parsed.append(
                        OHLCV(
                            date=ts.date(),
                            open=float(bar.open or 0),
                            high=float(bar.high or 0),
                            low=float(bar.low or 0),
                            close=float(bar.close or 0),
                            volume=int(bar.volume or 0),
                        )
                    )
                except (TypeError, ValueError, AttributeError):
                    continue
            parsed.sort(key=lambda b: b.date)
            out[symbol] = parsed
        return out


# --------------------------------------------------------------------------
# Cache (profile and SEC filings change slowly)
# --------------------------------------------------------------------------


class ReadCache:
    """{kind: {symbol: {"week", "on", "value"}}} at `<dir>/daily_cache.json`."""

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "daily_cache.json"
        try:
            raw = json.loads(self.path.read_text()) if self.path.exists() else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("universe daily: cache unreadable at %s: %s", self.path, exc)
            raw = {}
        self.data = {k: dict(raw.get(k) or {}) for k in ("profile", "filings")}

    def get(self, kind: str, symbol: str) -> dict | None:
        return self.data[kind].get(symbol)

    def put(self, kind: str, symbol: str, value, today: date) -> None:
        self.data[kind][symbol] = {"week": iso_week(today), "on": today.isoformat(), "value": value}

    def save(self) -> None:
        _atomic_write(self.path, json.dumps(self.data, sort_keys=True))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# One daily run
# --------------------------------------------------------------------------


class _Unreached(BaseException):
    """A network read was due after the time limit. BaseException on purpose:
    `screen_symbol` turns any Exception from a source into a data-outage
    code, and running out of time is not an outage."""


@dataclass
class DailyRun:
    today: date
    records: dict[str, dict] = field(default_factory=dict)
    calls: dict[str, int] = field(
        default_factory=lambda: {"alpaca_assets": 0, "alpaca_bars": 0, "yahoo_profile": 0, "sec_filings": 0}
    )
    seconds: float = 0.0
    deadline_hit: bool = False

    def record(self, symbol: str, status: str, failures=(), measured=None, **extra) -> None:
        row = {"symbol": symbol, "status": status, "failures": list(failures), "measured": dict(measured or {})}
        if status == "unreached":
            row["reason"] = UNREACHED_TEXT
        row.update(extra)
        self.records[symbol] = row

    def summary(self) -> dict:
        statuses: dict[str, int] = {}
        dropped_any: dict[str, int] = {}
        dropped_first: dict[str, int] = {}
        for row in self.records.values():
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
            if row["status"] == "dropped":
                for code in row["failures"]:
                    dropped_any[code] = dropped_any.get(code, 0) + 1
                first = row["failures"][0]
                dropped_first[first] = dropped_first.get(first, 0) + 1
        return {
            "date": self.today.isoformat(),
            "names_listed": len(self.records),
            "passed": statuses.get("passed", 0),
            "dropped": statuses.get("dropped", 0),
            "inconclusive": statuses.get("inconclusive", 0),
            "unreached": statuses.get("unreached", 0),
            "dropped_by_first_reason": dict(sorted(dropped_first.items())),
            "dropped_by_any_reason": dict(sorted(dropped_any.items())),
            "calls": dict(self.calls),
            "seconds": round(self.seconds, 1),
            "deadline_hit": self.deadline_hit,
        }


def _status(failures: list[str]) -> str:
    if not failures:
        return "passed"
    if any(code in INCONCLUSIVE for code in failures):
        return "inconclusive"
    return "dropped"


def run_daily_record(
    assets: list,
    *,
    get_bars_batch: Callable[[list[str]], dict[str, list]],
    get_profile: Callable[[str], Any],
    get_filings: Callable[[str], Any],
    th: ScreenThresholds,
    today: date,
    deadline: float,
    batch_size: int,
    cache: ReadCache,
    clock: Callable[[], float] = time.monotonic,
) -> DailyRun:
    """Screen every listed name once; record each. Never mutates admission state."""
    started = clock()
    screen = _DailyScreen(
        run=DailyRun(today=today),
        th=th,
        cache=cache,
        deadline=deadline,
        clock=clock,
        get_profile=get_profile,
        get_filings=get_filings,
    )
    by_symbol: dict[str, Any] = {}
    for asset in assets:
        symbol = _asset_symbol(asset)
        if symbol:
            by_symbol[symbol] = asset
    needs_bars = screen.asset_phase(by_symbol)
    bars = screen.bars_phase(needs_bars, get_bars_batch, max(1, int(batch_size)))
    screen.detail_phase(by_symbol, bars)
    screen.run.seconds = clock() - started
    return screen.run


@dataclass
class _DailyScreen:
    run: DailyRun
    th: ScreenThresholds
    cache: ReadCache
    deadline: float
    clock: Callable[[], float]
    get_profile: Callable[[str], Any]
    get_filings: Callable[[str], Any]
    # The time limit is checked when a name STARTS: a name begun in time
    # finishes its reads; one begun late uses only what is cached.
    late: bool = False

    def asset_phase(self, by_symbol: dict[str, Any]) -> list[str]:
        """Asset checks: free, the list is already in hand."""
        needs_bars = []
        for symbol in sorted(by_symbol):
            failures = check_asset(symbol, by_symbol[symbol])
            if failures:
                self.run.record(symbol, _status(failures), failures)
            else:
                needs_bars.append(symbol)
        return needs_bars

    def bars_phase(self, symbols: list[str], get_bars_batch, step: int) -> dict[str, list]:
        """Bars, many symbols per Alpaca request; then the bar checks (no network)."""
        passed: dict[str, list] = {}
        for start in range(0, len(symbols), step):
            chunk = symbols[start : start + step]
            if self.clock() >= self.deadline:
                self.run.deadline_hit = True
                for symbol in chunk:
                    self.run.record(symbol, "unreached", [UNREACHED])
                continue
            try:
                got = get_bars_batch(chunk) or {}
            except Exception as exc:  # noqa: BLE001 -- recorded per name as an outage
                logger.warning("universe daily: bar batch failed (%d symbols): %s", len(chunk), exc)
                for symbol in chunk:
                    self.run.record(symbol, "inconclusive", ["market_data_unavailable"])
                continue
            for symbol in chunk:
                failures, measured = check_bars(got.get(symbol) or [], self.th)
                if failures:
                    self.run.record(symbol, _status(failures), failures, measured)
                else:
                    passed[symbol] = got[symbol]
        return passed

    def _age(self, symbol: str) -> tuple:
        entry = self.cache.get("profile", symbol)
        if entry is None:
            return (2, "", symbol)
        return (0 if entry.get("week") == iso_week(self.run.today) else 1, entry.get("on") or "", symbol)

    def _cached_read(self, kind: str, symbol: str, fetch, counter: str, keep_none: bool):
        entry = self.cache.get(kind, symbol)
        if entry is not None and (entry.get("week") == iso_week(self.run.today) or self.late):
            return entry["value"], entry.get("on")
        if self.late:
            raise _Unreached()
        self.run.calls[counter] += 1
        value = fetch(symbol)
        if value is not None or keep_none:
            self.cache.put(kind, symbol, value, self.run.today)
        return value, self.run.today.isoformat()

    def detail_phase(self, by_symbol: dict[str, Any], bars: dict[str, list]) -> None:
        """Profile and filings: cached this week first (no network), then the
        oldest reads, then names never read."""
        for symbol in sorted(bars, key=self._age):
            self.late = self.clock() >= self.deadline
            if self.late:
                self.run.deadline_hit = True
            read_on: dict[str, str | None] = {}

            def _profile(s, _r=read_on):
                value, _r["profile_read_on"] = self._cached_read("profile", s, self.get_profile, "yahoo_profile", False)
                return value

            def _filings(s, _r=read_on):
                value, _r["filings_read_on"] = self._cached_read("filings", s, self.get_filings, "sec_filings", True)
                return value

            sources = ScreenSources(
                get_asset=lambda s: None, get_bars=lambda s: [], get_profile=_profile, get_filings=_filings
            )
            try:
                result = screen_symbol(symbol, sources, self.th, asset=by_symbol[symbol], bars=bars[symbol])
            except _Unreached:
                self.run.record(symbol, "unreached", [UNREACHED])
                continue
            self.run.record(symbol, _status(result.failures), result.failures, result.measured, **read_on)


# --------------------------------------------------------------------------
# Durable record
# --------------------------------------------------------------------------


def record_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / "daily"


def already_recorded(data_dir: str | Path, today: date) -> bool:
    return (record_dir(data_dir) / f"{today.isoformat()}.summary.json").exists()


def write_record(data_dir: str | Path, run: DailyRun) -> dict:
    """Per-name lines first, totals last: the summary file marks a complete day."""
    folder = record_dir(data_dir)
    stamp = run.today.isoformat()
    lines = "".join(json.dumps(run.records[s], sort_keys=True, default=str) + "\n" for s in sorted(run.records))
    _atomic_write(folder / f"{stamp}.jsonl", lines)
    summary = run.summary()
    summary["written_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _atomic_write(folder / f"{stamp}.summary.json", json.dumps(summary, indent=1, sort_keys=True))
    logger.info(
        "UNIVERSE_DAILY %s %s",
        stamp,
        json.dumps(
            {
                k: summary[k]
                for k in ("names_listed", "passed", "dropped", "inconclusive", "unreached", "calls", "seconds")
            },
            sort_keys=True,
        ),
    )
    return summary


# --------------------------------------------------------------------------
# Entry point (timer wake-up)
# --------------------------------------------------------------------------


def _due_today(broker, data_dir, today: date) -> bool:
    """A trading day on the broker's calendar, after its close, not yet recorded."""
    if already_recorded(data_dir, today):
        logger.info("universe daily: %s already recorded", today)
        return False
    if not broker.is_trading_day(today):
        logger.info("universe daily: %s is not a trading day", today)
        return False
    close = broker.get_session_close(today)
    if close is None or datetime.now(timezone.utc) < close:
        logger.info("universe daily: session not closed yet (close=%s)", close)
        return False
    return True


def _profile_reader():
    """Yahoo company profile in the shape `check_profile` reads, including the
    quote type and fund category the ETF check needs."""
    from src.data.market import MarketDataProvider
    from src.sector_reference import _canonicalize_sector, _get_sector

    market = MarketDataProvider()

    def _profile(symbol: str):
        raw = market.get_company_profile(symbol)
        if raw is None:
            return None
        sector = _canonicalize_sector(raw.get("sector_raw"))
        if sector == "Unknown":
            sector = _get_sector(symbol) or "Unknown"
        return {
            "market_cap_usd": raw.get("market_cap_usd"),
            "sector": sector,
            "quote_type": raw.get("quote_type"),
            "category": raw.get("category"),
        }

    return _profile


def _filings_reader(config, deadline: float):
    """SEC issuer filing history; built from `smart_money` config, passing
    every constructor field that config names, as the pipeline does."""
    import inspect

    from src.data.smart_money import SECForm4Provider

    sm = config.smart_money
    params = inspect.signature(SECForm4Provider).parameters
    sec = SECForm4Provider(**{name: getattr(sm, name) for name in params if hasattr(sm, name)})
    try:
        listed = sec.listed_map(deadline)
    except Exception as exc:  # noqa: BLE001 -- recorded per name as takeover_lookup_failed
        logger.warning("universe daily: SEC ticker map unavailable: %s", exc)
        listed = None

    def _filings(symbol: str):
        if listed is None:
            raise RuntimeError("SEC ticker map unavailable")
        return sec.recent_filings(symbol, deadline, listed=listed)

    return _filings


def _data_client(broker, key: str, secret: str):
    """The broker's existing Alpaca historical-data client, made the way
    `get_bars` makes it when it does not exist yet."""
    if broker._data_client is None:
        from alpaca.data.historical.stock import StockHistoricalDataClient

        from src.execution.broker_parts.http_timeout import _install_http_timeout

        broker._data_client = StockHistoricalDataClient(key, secret)
        _install_http_timeout(broker._data_client)
    return broker._data_client


def main(argv: list[str] | None = None) -> int:
    import argparse

    from src.api.deps import get_alpaca_credentials, get_alpaca_paper
    from src.config import load_config
    from src.execution.broker import AlpacaBroker
    from src.universe_screen import HISTORY_FETCH_DAYS
    from src.util.time import et_today

    parser = argparse.ArgumentParser(description="Daily record-only universe screen")
    parser.add_argument("--config", default="config/settings.yaml")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(Path(parser.parse_args(argv).config))
    cfg = config.universe_screen
    today = et_today()
    key, secret = get_alpaca_credentials()
    broker = AlpacaBroker(api_key=key, secret_key=secret, paper=get_alpaca_paper())
    if not _due_today(broker, cfg.data_dir, today):
        return 0
    deadline = time.monotonic() + float(cfg.screen_deadline_s)
    bars = AlpacaDailyBars(_data_client(broker, key, secret), HISTORY_FETCH_DAYS, today)
    cache = ReadCache(cfg.data_dir)
    run = run_daily_record(
        broker.list_assets(),
        get_bars_batch=bars,
        get_profile=_profile_reader(),
        get_filings=_filings_reader(config, deadline),
        th=ScreenThresholds.from_config(config),
        today=today,
        deadline=deadline,
        batch_size=int(cfg.bars_batch_size),
        cache=cache,
    )
    run.calls["alpaca_assets"] = 1
    run.calls["alpaca_bars"] = bars.calls
    cache.save()
    write_record(cfg.data_dir, run)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
