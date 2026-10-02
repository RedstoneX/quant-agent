"""Recorded sectors for a rehearsal: slow-moving reference data, captured once.

WHY THIS EXISTS
---------------
`broker.recorded_sector_lookup` already serves `_get_sector` from the market
recording's `sectors` table, but `market_bars.json.gz` was captured without one,
so every name's sector was an unrecorded input and the morning session voided.
A company's sector changes on the scale of years, so recording it once is
honest, unlike freezing a price. The table lives in its own small file so the
large bars blob is never rewritten.

RULES
-----
A symbol with no recorded sector stays ABSENT: the lookup then records it as a
missing recorded input and the run is voided. Nothing falls back to live and
nothing is replaced by "Unknown" or any other invented sector. The recording
the market capture itself holds wins over this file. A symbol yfinance returns
no sector for (an ETF, a delisting) is left out, never filled in.

Capture (online, unauthenticated, never run by a rehearsal):
    python -m ops.rehearsal.sector_recording            # every symbol in the bars recording
    python -m ops.rehearsal.sector_recording SYM ...
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_RECORDING = Path(__file__).resolve().parent / "recordings" / "sectors.json"


def load(path: Path | str = DEFAULT_RECORDING) -> dict[str, str]:
    """{SYMBOL: sector}; empty when no file exists. Blank sectors are dropped."""
    path = Path(path)
    if not path.exists():
        return {}
    table = json.loads(path.read_text()).get("sectors") or {}
    return {str(k).upper(): str(v) for k, v in table.items() if v and str(v).strip()}


def merge_into(recording: dict | None, path: Path | str = DEFAULT_RECORDING) -> dict:
    """The market recording with sectors filled from this file where it holds none."""
    merged = dict(recording or {})
    held = {str(k).upper(): v for k, v in (merged.get("sectors") or {}).items() if v}
    merged["sectors"] = {**load(path), **held}
    return merged


def capture(symbols, path: Path | str = DEFAULT_RECORDING) -> dict:
    """Ask yfinance ONCE for each symbol's sector and write the table to disk."""
    import yfinance as yf

    sectors: dict[str, str] = {}
    absent: list[str] = []
    for symbol in sorted({s.strip().upper() for s in symbols if s and s.strip()}):
        try:
            sector = (yf.Ticker(symbol).info or {}).get("sector")
        except Exception:  # noqa: BLE001 — no answer is recorded as absent
            sector = None
        if sector and str(sector).strip():
            sectors[symbol] = str(sector).strip()
        else:
            absent.append(symbol)
    result = {
        "captured_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "yfinance Ticker(symbol).info['sector'], as src.execution.broker._get_sector reads it",
        "symbols_with_no_sector": absent,
        "sectors": sectors,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    return result


def _main(argv: list[str]) -> int:
    symbols = argv
    if not symbols:
        from ops.rehearsal.market_recording import load as load_bars

        symbols = sorted((load_bars() or {}).get("bars") or {})
    result = capture(symbols)
    print(
        f"recorded {len(result['sectors'])} sectors to {DEFAULT_RECORDING}; "
        f"no sector for: {', '.join(result['symbols_with_no_sector']) or 'none'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
