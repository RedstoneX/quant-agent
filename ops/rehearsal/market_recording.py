"""Recorded market data, so a rehearsal replays bars instead of downloading them.

Board item 202. The harness always claimed to be offline (`no_network` in
`ops/rehearsal/isolation.py`) and always replaced the market-data provider with
one that returns nothing (`blocked_market_data` in `ops/rehearsal/broker.py`).
Neither held: yfinance does not use `requests` or the Python socket layer — it
ships its own transport on `curl_cffi`, which is libcurl in C and never touches
`socket.socket.connect`. So every rehearsal quietly downloaded live prices
(~196s of fetching, measured 2026-09-30 on `origin/main`), which makes a replay
neither reproducible nor offline.

Closing the hole alone is not enough: with nothing to serve, every technical
read is empty, the evidence gate correctly refuses the decision and the
rehearsal never reaches the seat it exists to exercise. So the bars are
RECORDED once, deliberately and online, by `capture()` below — run as an
operator command, never from inside a rehearsal — and served from disk
afterwards.

No invented data: a symbol absent from the recording is served exactly what a
yfinance outage serves (empty), is named in the rehearsal's `unavailable` list,
and shows up in the report as degradation rather than being silently filled in.
"""

from __future__ import annotations

import argparse
import gzip
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path

DEFAULT_RECORDING = Path(__file__).resolve().parent / "recordings" / "market_bars.json.gz"


def _plain(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _row(bar) -> dict:
    raw = asdict(bar) if is_dataclass(bar) else dict(vars(bar))
    return {k: _plain(v) for k, v in raw.items()}


def _revive(row: dict):
    from src.models import OHLCV

    fields = dict(row)
    value = fields.get("date")
    if isinstance(value, str):
        fields["date"] = date.fromisoformat(value[:10])
    return OHLCV(**fields)


def load(path: Path | str = DEFAULT_RECORDING) -> dict | None:
    """The recording, or None when none has been captured on this box."""
    path = Path(path)
    if not path.exists():
        return None
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as handle:
        return json.load(handle)


def capture(symbols, path: Path | str = DEFAULT_RECORDING, lookback_days: int = 400) -> dict:
    """Download bars ONCE and write them to disk. Online by design.

    Never called from a rehearsal — `run_rehearsal` only ever reads. Bars come
    from the broker (Alpaca, read-only), so this needs the Alpaca credentials
    every standalone script already uses.
    """
    from src.backtest.data import broker_backed_provider

    provider = broker_backed_provider()  # Alpaca daily bars, read-only (owner 2026-10-09)
    bars: dict[str, list] = {}
    empty: list[str] = []
    # Sectors are recorded too (board item 202): `broker._get_sector` reads
    # `yf.Ticker(symbol).info`, which is a SECOND live fetch the bars
    # recording did not cover, and a rehearsal that cannot serve it either
    # reaches the network or degrades every name to "Unknown".
    sectors: dict[str, str] = {}
    for symbol in sorted({s.strip().upper() for s in symbols if s and s.strip()}):
        try:
            import yfinance as yf
            sector = (yf.Ticker(symbol).info or {}).get("sector")
        except Exception:  # noqa: BLE001 — a missing sector is recorded as absent
            sector = None
        if sector:
            sectors[symbol] = str(sector)
        series = provider.get_ohlcv(symbol, lookback_days=lookback_days) or []
        if series:
            bars[symbol] = [_row(b) for b in series]
        else:
            empty.append(symbol)
    recording = {
        "captured_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "lookback_days": lookback_days,
        "source": "Alpaca daily bars via src.data.market.MarketDataProvider.get_ohlcv",
        "symbols_with_no_data": empty,
        "bars": bars,
        "sectors": sectors,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt") as handle:
        json.dump(recording, handle)
    return recording


def recorded_market_data(record: list[str], recording: dict | None):
    """A market-data provider that reads the recording and never the network.

    `record` collects what could not be served, so the report says which
    symbols the rehearsal was blind to instead of the operator assuming full
    coverage.
    """
    from src.data.market import MarketDataProvider

    bars = dict((recording or {}).get("bars") or {})
    captured = (recording or {}).get("captured_utc")

    class RecordedMarketData(MarketDataProvider):
        recording_captured_utc = captured
        recorded_symbols = sorted(bars)

        def __init__(self):
            super().__init__()

        def get_ohlcv(self, symbol: str, lookback_days: int = 120):
            rows = bars.get((symbol or "").upper())
            if not rows:
                note = f"recorded daily bars for {symbol}"
                if note not in record:
                    record.append(note)
                return []
            series = [_revive(r) for r in rows]
            if lookback_days and len(series) > lookback_days:
                series = series[-lookback_days:]
            return series

        def get_valuation_metrics(self, symbol: str):
            note = f"valuation metrics for {symbol} (not recorded; rehearsals are offline)"
            if note not in record:
                record.append(note)
            return {}

        def get_upcoming_ex_dividend(self, symbol: str):
            return {}

    return RecordedMarketData()


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+", help="tickers to record")
    parser.add_argument("--out", default=str(DEFAULT_RECORDING))
    parser.add_argument("--lookback-days", type=int, default=400)
    args = parser.parse_args()
    result = capture(args.symbols, args.out, args.lookback_days)
    print(
        f"recorded {len(result['bars'])} symbols to {args.out} "
        f"(no data for: {', '.join(result['symbols_with_no_data']) or 'none'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
