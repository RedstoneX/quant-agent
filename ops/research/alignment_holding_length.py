"""Derive the protective-stop ATR multiple from the HOLDING PERIOD the desk's
own alignment exit actually produces.

WHY: `src.config.RiskConfig.min_stop_atr_multiple` (2.5) is an unsourced
number. The prior sweep (`ops/research/min_stop_atr_sweep.py`, branch
`research/min-stop-measurement`, 2026-10-04) measured that the regret and
save curves never separate -- no knee, no crossover -- so the data does not
pick a multiple directly. What it DOES determine is a peak of useful stop-outs
PER HOLDING HORIZON. The horizon was the undeclared variable: this repo pins no
holding period anywhere in config.

Owner ruling 2026-09-30 ("exit on ALIGNMENT, never on a target") makes the
horizon ENDOGENOUS -- a position lasts as long as the alignment rule lets it.
So this script measures that distribution off committed price history and reads
the peak-usefulness multiple AT the measured holding lengths. No horizon is
picked here; every horizon fed to the sweep is a percentile of a measurement.

CONTROL FLOW REPRODUCED, read from `src.risk.alignment_exit` (2026-10-04):

  * `check_alignment_exit` is imported and CALLED, not reimplemented, so this
    cannot drift from the live rule.
  * It takes NO entry price and NO position cost. There is no above-entry
    early return in it (that early return lives in the entry-anchored
    `exit_guard` noise band, a different rule with a different anchor). The
    alignment verdict is therefore a property of (symbol, session) alone, so
    the exit session is computed ONCE per bar and reused for every entry.
  * Inputs supplied here: `closes` = that symbol's completed daily closes up to
    and including the session being judged, ascending; `atr` = Wilder 14 from
    `src.data.technical.atr_series`, the desk's own reading, at that session.
  * `thesis_invalid_if=None`: a thesis is model-written prose that exists only
    on live positions and is NOT in price history. The live code's documented
    behaviour for that case is to fall back to the CHART's own averages,
    `CHART_MA_PERIODS` = (20, 50, 200) SMAs. That is the branch exercised here,
    and it is stated rather than assumed.
  * `broken_structural_level=None`: the live caller passes a level only once
    `check_structural_protection` has CONFIRMED a break. Structural levels were
    measured on this same repo to have no out-of-sample edge, and reproducing
    that gate is a separate harness. Omitting it means the marks here are the
    three SMAs only, which makes the measured holds an UPPER bound on holding
    length (an extra mark can only add a way to exit sooner). Stated as a
    limitation, not hidden.
  * `is_short=False`: the desk is long-only in the fixtures.

EXIT RULE AS EXERCISED: price must close below the LOWEST of the SMAs it has
given up, by more than `ALIGNMENT_GIVE_BACK_ATR_MULTIPLE` (3.0) x ATR(14).

SWEEP SETTINGS are inherited verbatim from the prior script and are not
re-justified here: multiples 0.50..5.00 step 0.25, regret = stop hit then a
later close inside the horizon regains entry, saved = hits - regret.

CONTROL: a memoryless exit process with the SAME measured per-session exit
rate (geometric holds). If the alignment rule's implied multiple matches the
control's, the rule contributes nothing beyond its average exit frequency.

Hermetic: committed .json.gz fixtures only. No network, no broker, no writes.
"""
from __future__ import annotations

import gzip
import json
import math
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from src.data.technical import ATR_PERIOD, atr_series  # noqa: E402
from src.models.analysis import OHLCV  # noqa: E402
from src.risk.alignment_exit import (  # noqa: E402
    ALIGNMENT_GIVE_BACK_ATR_MULTIPLE,
    CHART_MA_PERIODS,
    check_alignment_exit,
)

LONG_FIXTURE = REPO / "ops/model_policy/fixtures/yf_daily_bars_2026-08-28.json.gz"
REHEARSAL_FIXTURE = REPO / "ops/rehearsal/recordings/market_bars.json.gz"

MULTIPLES = [round(0.5 + 0.25 * i, 2) for i in range(19)]  # 0.50 .. 5.00
CONTROL_SEED = 20261004
PERCENTILES = (25, 50, 75)  # reported spread, not a threshold


def _load() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    with gzip.open(REHEARSAL_FIXTURE) as fh:
        for sym, bars in json.load(fh)["bars"].items():
            out[sym] = bars
    with gzip.open(LONG_FIXTURE) as fh:
        for sym, bars in json.load(fh).items():
            out[sym] = bars  # longer history wins
    return out


def _atrs(bars: list[dict]) -> list[float | None]:
    rows = [
        OHLCV(
            date=b["date"], open=b["open"], high=b["high"],
            low=b["low"], close=b["close"], volume=int(b["volume"]),
        )
        for b in bars
    ]
    series = atr_series(rows, ATR_PERIOD)
    pad = len(bars) - len(series)
    return [None] * pad + [float(v) for v in series]


def exit_flags(bars: list[dict], atrs: list[float | None]) -> list[bool]:
    """True on each session where the LIVE alignment rule says EXIT."""
    closes = [float(b["close"]) for b in bars]
    flags: list[bool] = []
    for i in range(len(bars)):
        v = check_alignment_exit(
            thesis_invalid_if=None,
            closes=closes[: i + 1],
            atr=atrs[i],
            broken_structural_level=None,
            is_short=False,
        )
        flags.append(v.status == "EXIT")
    return flags


def holds_from_flags(flags: list[bool]) -> tuple[list[int], int]:
    """(observed holding lengths, censored count) over every entry session.

    An entry at session i exits on the first t > i whose flag is True; the
    hold is t - i sessions. Entries with no exit before the data ends are
    RIGHT-CENSORED and counted separately rather than dropped or truncated.
    """
    n = len(flags)
    nxt = [-1] * n
    last = -1
    for i in range(n - 1, -1, -1):
        nxt[i] = last
        if flags[i]:
            last = i
    holds = [nxt[i] - i for i in range(n) if nxt[i] > i]
    return holds, sum(1 for i in range(n) if nxt[i] <= i)


def pct(xs: list[int], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def _outcomes(bars, atrs, mult, horizon, rng=None):
    """Prior script's measure, unchanged: (entries, hits, regret, saved)."""
    entries = hits = regret = 0
    n = len(bars)
    for i in range(n - horizon):
        atr = atrs[i] if rng is None else rng.choice(atrs[ATR_PERIOD:])
        if not atr or atr <= 0:
            continue
        entry = bars[i]["close"]
        if entry <= 0:
            continue
        stop = entry - mult * atr
        if stop <= 0:
            continue
        entries += 1
        hit_at = None
        for t in range(i + 1, i + 1 + horizon):
            if bars[t]["low"] <= stop:
                hit_at = t
                break
        if hit_at is None:
            continue
        hits += 1
        if any(bars[t]["close"] >= entry for t in range(hit_at + 1, i + 1 + horizon)):
            regret += 1
    return entries, hits, regret, hits - regret


def peak_multiple(data, atrs, horizon):
    """The multiple maximising saved stop-outs per 100 entries at `horizon`."""
    rows = []
    for mult in MULTIPLES:
        e = s = 0
        for sym in sorted(data):
            a, _h, _r, d = _outcomes(data[sym], atrs[sym], mult, horizon)
            e += a
            s += d
        if e:
            rows.append((mult, 100.0 * s / e, e))
    if not rows:
        return None, None, 0
    best = max(rows, key=lambda r: r[1])
    # every multiple within 1% (relative) of the peak -- a plateau, not a point
    near = [r[0] for r in rows if r[1] >= best[1] * 0.99]
    return best, (min(near), max(near)), rows[0][2]


def main() -> None:
    data = _load()
    syms = sorted(data)
    first = min(b[0]["date"] for b in data.values())
    last = max(b[-1]["date"] for b in data.values())
    total = sum(len(b) for b in data.values())
    print(f"sample: {len(syms)} symbols, {total} daily bars, {first} .. {last}")
    print(f"alignment rule: live check_alignment_exit, give-back "
          f"{ALIGNMENT_GIVE_BACK_ATR_MULTIPLE} ATR below the last lost mark; "
          f"marks = SMA{list(CHART_MA_PERIODS)} (no thesis, no structural level)")
    print()

    atrs = {s: _atrs(b) for s, b in data.items()}
    all_holds: list[int] = []
    censored = 0
    exit_days = 0
    usable_days = 0
    for s in syms:
        f = exit_flags(data[s], atrs[s])
        h, c = holds_from_flags(f)
        all_holds += h
        censored += c
        exit_days += sum(f)
        usable_days += len(f)

    print("=== measured holding length under the live alignment exit ===")
    print(f"entries measured (every session as an entry): "
          f"{len(all_holds) + censored}")
    print(f"exited before data ends: {len(all_holds)}   "
          f"right-censored (never exited): {censored} "
          f"({100.0*censored/(len(all_holds)+censored):.2f}%)")
    print(f"exit sessions: {exit_days} of {usable_days} "
          f"({100.0*exit_days/usable_days:.3f}% of sessions)")
    for p in PERCENTILES:
        print(f"  p{p}: {pct(all_holds, p):.1f} sessions")
    print(f"  mean {sum(all_holds)/len(all_holds):.1f}   "
          f"min {min(all_holds)}   max {max(all_holds)}")
    print()

    rate = exit_days / usable_days
    rng = random.Random(CONTROL_SEED)
    ctrl = [1 + int(math.floor(math.log(rng.random()) / math.log(1 - rate)))
            for _ in range(len(all_holds))]
    print("=== control: memoryless exit at the SAME measured rate ===")
    for p in PERCENTILES:
        print(f"  p{p}: {pct(ctrl, p):.1f} sessions")
    print(f"  mean {sum(ctrl)/len(ctrl):.1f}")
    print()

    print("=== peak-usefulness multiple AT each measured holding length ===")
    print(f"{'source':>10} {'horizon':>8} {'peak D':>7} {'saved/100':>10} "
          f"{'plateau(within 1%)':>20}")
    for label, xs in (("alignment", all_holds), ("control", ctrl)):
        for p in PERCENTILES:
            hz = int(round(pct(xs, p)))
            if hz < 2:
                print(f"{label:>10} {hz:8d}  horizon below 2 sessions, skipped")
                continue
            best, plateau, _e = peak_multiple(data, atrs, hz)
            print(f"{label:>10} {hz:8d} {best[0]:7.2f} {best[1]:10.2f} "
                  f"{plateau[0]:9.2f}-{plateau[1]:.2f}")


if __name__ == "__main__":
    main()


# =====================================================================
# FINDINGS, run 2026-10-04 (output of `main()` above, reproducible)
# Sample: 38 symbols, 17,208 committed daily bars, 2021-09-27..2026-09-30.
#
# 1. HOLDING LENGTH UNDER THE LIVE ALIGNMENT EXIT, every session treated as
#    an entry: p25 29 sessions, median 85, p75 211, mean 150, max 812.
#    397 of 17,208 sessions (2.31%) fire an exit. 6,668 of 17,208 entries
#    (38.75%) never exit before the data ends and are reported as censored,
#    which means the median is an UNDERSTATEMENT of the true hold.
#
# 2. THE DISTRIBUTION IS NOT AN ARTEFACT OF EXIT FREQUENCY. A memoryless
#    control at the same measured 2.31% per-session rate gives p25 13,
#    median 30, p75 60. The alignment rule's holds are roughly three times
#    longer at the quartiles, i.e. its exits are CLUSTERED, not random.
#
# 3. IMPLIED MULTIPLE, read off the peak of saved stop-outs per 100 entries
#    at each measured holding length: p25 hold (29 sessions) -> 2.25
#    (plateau 2.00-2.50); median hold (85) -> 4.00 (plateau 3.75-4.50);
#    p75 hold (211) -> 5.00, which is the TOP OF THE SWEEP, so the true peak
#    there is >= 5.00 and was not bracketed.
#
# 4. THE HONEST ANSWER IS A RANGE, NOT A NUMBER: the desk's own exit doctrine
#    implies roughly 2.25 to >=5.00 ATR, centred near 4.0. The live 2.5 is
#    not absurd but sits at the BOTTOM QUARTILE of what the doctrine implies;
#    a stop set for a 29-session hold is being asked to survive a median
#    85-session one.
#
# 5. LIMITATIONS, each of which biases the measured hold in a known direction:
#    - no structural mark is supplied (see the control-flow note), and an
#      extra mark can only create earlier exits, so the measured holds are an
#      UPPER bound and the implied multiple an upper bound with them;
#    - 38.75% censoring pushes the measured median DOWN, the opposite way;
#    - every session is treated as an entry, whereas the live desk enters only
#      on a five-seat conviction, which is not in price history and cannot be
#      reproduced from it;
#    - 38 symbols over one five-year window is one regime, not many.
#
# 6. NOT CLAIMED: that 2.5 should be changed to any particular value. The
#    sweep's own prior finding stands -- the regret and save curves never
#    separate -- so this run narrows the number to a wide range, it does not
#    settle it to a point. Changing the live value is a separate decision.
