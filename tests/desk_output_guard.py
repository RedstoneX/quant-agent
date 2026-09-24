"""Detector for REAL DESK OUTPUT committed into this PUBLIC repository.

Why this exists
---------------
Test fixtures in this repo were built by copying real output from the live
desk instead of inventing values: real tickers with the real entry price, the
real protective stop, the real target, real position-size reasoning, real
broker order ids, and production log lines copied verbatim. One published stop
price was resting at the broker at the time it was published.

The repository is public and stays public. The owner's instruction is narrow
and is the whole job of this module: **no FURTHER data of that sort gets
published.** Nothing here redacts or judges what is already committed; the
files that already carry desk output are named, one by one, in `ALLOWED`
below.

What "real desk output" means here
----------------------------------
A line of a committed file carries real desk output when it shows at least one
of four content signals. Each signal was chosen because it is something the
running desk MINTS and an author inventing a fixture would not produce by
accident. Each was measured against every tracked file in the repo before
being switched on, and each produced zero hits on legitimate synthetic
fixtures at the thresholds used here.

  BROKER-ORDER-ID    A UUID. The broker mints these; you cannot invent one.
  PRODUCTION-LOG     A line in the desk's own logger format
                     (`2026-09-18 14:31:55,689 [WARNING] src.data.news: ...`).
  DESK-DECISION      A `symbol` field sitting with four or more of the desk's
                     own emitted-decision fields (entry_price, stop_loss,
                     reference_target, thesis_invalid_if, ...) AND two or more
                     cent-precision prices that are not round numbers.
                     Hand-written fixtures in this repo price things at 150.0,
                     507.0, 140.0; the desk emits 219.51, 142.02, 159.52.
  DESK-PROSE         A sentence of forty words or more naming five or more
                     DIFFERENT real tradeable tickers. Nobody writing a
                     synthetic fixture writes a paragraph about five real
                     stocks; a portfolio-manager answer always does.

Three passes run over every file, so the payload is checked and not the
wrapper it arrived in:

  1. the raw text;
  2. the text with JSON string-escapes unwound (`\\"` -> `"`, `\\n` -> newline),
     which is how a model answer arrives when it has been stored inside
     another JSON document — every file in `ops/model_policy/results` does
     exactly that;
  3. for a single-line JSON document, the same content re-dumped one field per
     line, because whether a fixture is pretty-printed is a formatting choice
     and the verdict must not depend on it.

`.gz` files are decompressed first. Nothing keys off a filename: renaming a
fixture does not change its verdict.

What this does NOT catch is written down as plainly as what it does, in
`tests/test_no_real_desk_output.py` and in the pull request that added it. The
largest gap is a bare number: a real stop price asserted as a float literal
with no ticker beside it is invisible here.

Reporting a finding costs a file, a LINE NUMBER and a plain-language remedy,
because the person who trips this will usually not be the person who wrote it.

Related: `ops/model_policy/fixture_policy.py` already checks provenance
declarations on the benchmark fixtures in one directory, and gates whether a
fixture may be USED in an exam. This module is the other half — it gates
whether content may be COMMITTED, anywhere in the repo.
"""

from __future__ import annotations

import gzip
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Files that ALREADY carry real desk output.
#
# This is an enumerated, per-file list with a reason on each line. It is
# deliberately NOT a directory or glob exclusion: a new file dropped beside an
# allow-listed one is still checked. `tests/test_no_real_desk_output.py`
# asserts that every path here still exists and still trips the detector, so
# an entry cannot outlive the problem it excuses.
#
# Adding to this list is not a routine act. The intended response to a new
# finding is to invent the value, not to append a line here.
# ---------------------------------------------------------------------------
# Each entry is `path: (one-line reason, ceiling)`. The ceiling is the number of
# findings the file carried when it was allow-listed. It is what stops the list
# becoming a licence: appending a NEW real stop price to an already-listed file
# pushes its count past the ceiling and fails the build, naming the new line.
# The pattern is the repo's own — `src/number_sources.py` uses the same
# count-that-cannot-silently-rise for unscoped numeric sites.
ALLOWED: dict[str, tuple[str, int]] = {
    # --- tests/fixtures: desk output copied into the test suite ---
    "tests/fixtures/constructor_drop_paths_archive.json":
        ("frozen archive of ~40 real tickers with their real entry/stop pairs (board item 10)", 43),
    "tests/fixtures/holding_why_rsg_20260917.json":
        ("a real RSG holding with its real stop and the real broker order id of its fill", 6),
    "tests/fixtures/log_health_production_excerpt.txt":
        ("production log lines copied verbatim off the desk host, as its own header states", 46),
    "tests/fixtures/pm_response_11_targets_20260817.txt":
        ("a verbatim portfolio-manager answer: real book, real targets, real sizing reasoning", 7),
    "tests/fixtures/pm_response_17_targets_20260820.txt":
        ("a second verbatim portfolio-manager answer from a different session", 3),
    "tests/fixtures/tech_answer_20260917_intra_check_26f52bf2_first.txt":
        ("a verbatim tech-analyst answer with real entries, stops and support/resistance levels", 5),
    "tests/fixtures/tech_answer_20260917_intra_check_26f52bf2_retry.txt":
        ("the retry of that same real intraday check", 4),

    # --- tests and scripts built around the real 2026-08-28 ONDS/CCJ stop-out ---
    "tests/test_stop_out_reconciliation.py":
        ("reconstructs the real ONDS/CCJ stop-out, quoting both real broker order ids and fills", 12),
    "tests/test_broker.py":
        ("reuses the same real broker order ids as list_filled_sell_orders test input", 4),
    "scripts/backfill_stop_out_fills.py":
        ("one-off backfill whose docstring names the two real broker order ids it repaired", 2),

    # --- ops/model_policy fixtures: benchmark inputs taken off a real day ---
    "ops/model_policy/fixtures/run_64290730_pm_input.json":
        ("a real portfolio-manager input snapshot; already quarantined by fixture_policy", 63),
    "ops/model_policy/fixtures/run_bba4d4f3_pm_input.json":
        ("a second real portfolio-manager input snapshot; already quarantined by fixture_policy", 62),
    "ops/model_policy/fixtures/pm_public_day_pm_input.json":
        ("a PM input rebuilt for a public day, but it still carries the real candidate book", 1),

    # --- docs that record what the desk actually did ---
    "docs/INCIDENT_HISTORY.md":
        ("the incident record; naming the real trade is the point of an incident record", 1),
    "docs/AGENT_ROLE_AUDIT.md":
        ("audit findings quoted from real runs, kept as the evidence trail for those findings", 1),

    # --- prompt templates whose worked examples came from real sessions ---
    "config/prompts/news_analyst.md":
        ("its worked example is a real news-analyst briefing, cited to the model as doctrine", 1),
}

# The model-benchmark results. Every one of these replays a real desk input
# through a candidate model and stores the answer verbatim, so they carry real
# tickers at real prices. They are enumerated per file with their own ceiling,
# not glob-excluded: a new result file dropped into this directory is checked
# like anything else, and an existing one cannot grow new findings.
_BENCHMARK_REASON = "model-benchmark answer replayed over a real desk input, stored verbatim"
_BENCHMARK_CEILINGS: dict[str, int] = {
    "2026-09-14-analyst-iso-deepseek_deepseek-v4-flash-0731-ta.json": 1,
    "2026-09-14-analyst-iso-deepseek_deepseek-v4_1-flash-ta.json": 1,
    "2026-09-14-analyst-iso-google_gemini-2_5-flash-lite-ta.json": 1,
    "2026-09-14-analyst-t1-google-direct_gemini-3_5-flash-lite.json": 2,
    "2026-09-14-analyst-t2-deepseek_deepseek-v4_1-flash.json": 1,
    "2026-09-14-analyst-t2-google_gemini-3_5-flash-lite.json": 2,
    "2026-09-14-analyst-t2-meta_muse-spark-1_3.json": 2,
    "2026-09-14-analyst-t2-openai_gpt-5_6-luna.json": 1,
    "2026-09-14-analyst-t2-z-ai_glm-5_3-flash.json": 1,
    "2026-09-14-analyst-t2-z-ai_glm-5_3.json": 1,
    "2026-09-15-pm-public-day-anthropic_claude-opus-5.json": 2,
    "2026-09-15-pm-public-day-deepseek_deepseek-v4_1-flash.json": 2,
    "2026-09-15-pm-public-day-google-direct_gemini-3_5-flash-lite.json": 1,
    "2026-09-15-pm-public-day-meta_muse-spark-1_3.json": 2,
    "2026-09-15-pm-public-day-moonshotai_kimi-k3.json": 4,
    "2026-09-15-pm-public-day-z-ai_glm-5_3-flash.json": 4,
    "2026-09-15-pm-public-day-z-ai_glm-5_3.json": 2,
    "PM_PROMPT_run64290730_rendered.txt": 1,
    "gemini35-fullsweep-2026-08-31.json": 3,
    "merged.json": 22,
    "pm-agreement-2026-09-01.json": 12,
    "pm-deep-candidates-2026-09-01.json": 8,
    "pm-scale-cheap-2026-08-31.json": 3,
    "pm-selection-postfix-2026-09-02.json": 5,
    "pm-selection-postfix-run2-2026-09-02.json": 9,
    "rerun-capfix-flash.json": 2,
    "rm-rerun-2026-08-14.json": 11,
    "sweep-a.json": 2,
    "sweep-b.json": 17,
    "sweep-tech-full.json": 2,
    "zz-pm-gpt55-qualified-final-2026-08-25.json": 2,
    "zz-pm-luna-disqualified-final-2026-08-25.json": 1,
}

_BENCHMARK_RESULTS_DIR = "ops/model_policy/results"


def allow_list() -> dict[str, tuple[str, int]]:
    """The full enumerated allow-list: path -> (reason, finding ceiling)."""
    out = dict(ALLOWED)
    for name, ceiling in _BENCHMARK_CEILINGS.items():
        out[f"{_BENCHMARK_RESULTS_DIR}/{name}"] = (_BENCHMARK_REASON, ceiling)
    return out


# ---------------------------------------------------------------------------
# Signal 1 — broker order identifier
# ---------------------------------------------------------------------------
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# A UUID nobody could mistake for a real one. Keep this generous: the remedy we
# print tells authors to use exactly PLACEHOLDER_UUID, and a fixture author who
# reaches for 1111... or deadbeef... meant the same thing.
PLACEHOLDER_UUID = "00000000-0000-4000-8000-000000000000"
_OBVIOUS_PLACEHOLDER_PREFIX = ("00000000-", "11111111-", "12345678-", "deadbeef-",
                               "aaaaaaaa-", "ffffffff-", "abcdabcd-")


def _is_placeholder_uuid(value: str) -> bool:
    low = value.lower()
    if low.startswith(_OBVIOUS_PLACEHOLDER_PREFIX):
        return True
    return len(set(low.replace("-", ""))) <= 2


# A UUID on its own is not evidence of anything — SEC filings carry EDGAR
# document ids, front-end bundles carry build ids. What makes a UUID a BROKER
# order id is the company it keeps, so one of these words must appear within a
# few lines of it. Measured: this drops the EDGAR ids in
# ops/model_policy/fixtures/sec_*.gz and keeps every real order id in the repo.
# The lookbehind is letters-only on purpose: it lets `broker_order_id` and
# `_order_id` match while keeping `border`, `recorder` and `reorder` out.
_ORDER_CONTEXT = re.compile(
    r"(?<![A-Za-z])("
    r"order|broker|fill|alpaca|execut|trade|position|stop_limit|stop-limit|qty|side"
    r")",
    re.IGNORECASE,
)
_UUID_CONTEXT_LINES = 4

def _uuid_is_markup_id(line: str, start: int, end: int) -> bool:
    """True when this UUID is a document id belonging to somebody else's format.

    Third-party snapshots in this repo are full of UUIDs that have nothing to
    do with the broker: CDN asset URLs in the RSS snapshot, EDGAR's
    `<!--r:…,g:…,d:…-->` stamp on every filing, and `<guid>…</guid>` on every
    RSS item. Each is recognised by what sits immediately around the value, so
    the test is local — checking the whole line would exempt a single-line JSON
    document that happens to contain a URL somewhere else in it.
    """
    left = line[max(0, start - 60):start]
    right = line[end:end + 20]
    if "://" in left or "<!--" in left:
        return True
    return left.rstrip().endswith(">") and right.lstrip().startswith("<")


# ---------------------------------------------------------------------------
# Signal 2 — production log line, in the desk's own logger format
# ---------------------------------------------------------------------------
_PRODUCTION_LOG = re.compile(
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} "
    r"\[(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\] "
    r"[A-Za-z_][A-Za-z0-9_.]*:"
)


# ---------------------------------------------------------------------------
# Signal 3 — a desk decision record
# ---------------------------------------------------------------------------
_SYMBOL_FIELD = re.compile(r"""['"]symbol['"]\s*[:=]\s*['"]([A-Z][A-Z0-9.\-]{0,5})['"]""")

# Fields the desk's own agents emit. A record carrying four of them is a
# serialised TechAnalysisResult / decision row, not a hand-built stub.
_DECISION_FIELDS = (
    "entry_price", "stop_loss", "reference_target", "thesis_invalid_if",
    "reasoning_chain", "support_levels", "resistance_levels", "setup_type",
    "expected_horizon_sessions", "conviction", "signal_weight",
    "broker_order_id", "fill_price", "avg_entry_price", "rating",
)
_DECISION_FIELD_RE = {
    f: re.compile(r"(?<![A-Za-z0-9_])" + f + r"(?![A-Za-z0-9_])") for f in _DECISION_FIELDS
}
DECISION_FIELDS_REQUIRED = 4
DECISION_PRICES_REQUIRED = 2
_RECORD_WINDOW_LINES = 16

_CENT_PRECISION = re.compile(r"(?<![\d.])(\d{1,6}\.\d{2})(?![\d])")
# A hand-written fixture prices things in whole dollars, halves or quarters.
_INVENTED_ENDINGS = (".00", ".25", ".50", ".75")


def _measured_prices(window: str) -> set[str]:
    """Cent-precision values that do not look hand-chosen."""
    return {
        m.group(1) for m in _CENT_PRECISION.finditer(window)
        if not m.group(1).endswith(_INVENTED_ENDINGS)
    }


# ---------------------------------------------------------------------------
# Signal 4 — verbatim desk prose about the real book
# ---------------------------------------------------------------------------
# Real US-listed symbols. This list is NOT exhaustive and is not meant to be:
# it exists so that an all-caps English word in a design document is not read
# as a stock. A ticker missing from this list simply is not counted by the
# prose signal — the other three signals do not use it at all.
REAL_TICKERS: frozenset[str] = frozenset("""
AAPL ABBV ABT ACN ADBE AMAT AMD AMGN AMT AMZN AVGO AXP BA BAC BK BKNG BLK BMY
BRK C CAT CB CCJ CEG CHTR CL CMCSA COF COP COST CRM CSCO CVS CVX DE DHR DIS DLR
DOW DUK ELV EMR ENPH EOG EPD EQIX EQNR ETN EXC F FCX FDX GD GE GEV GILD GM GOOG
GOOGL GS HAL HD HON IBM INTC JNJ JPM KHC KLAC KMI KO LIN LLY LMT LOW LRCX MA MCD
MDLZ MDT MET META MMM MO MP MRK MRVL MS MSFT MU NEE NET NFLX NKE NOC NUE NVDA
NXPI OKLO ONDS ORCL OXY PANW PEP PFE PG PLD PLTR PM PSX PWR PYPL QCOM RSG RTX
SBUX SCHW SLB SNDK SO SPG T TGT TJX TMO TMUS TSLA TSM TXN UNH UNP UPS USB V VLO
VST VZ WDC WFC WMB WMT XOM ZS
ARKK DIA EEM EFA GLD HYG IEF IWM IYR KRE LQD QQQ RSP SGOV SLV SMH SPY SQQQ TLT
USO VEA VNQ VOO VTI VWO XBI XLB XLC XLE XLF XLI XLK XLP XLRE XLU XLV XLY XOP
""".split())

_UPPER_TOKEN = re.compile(r"(?<![A-Za-z0-9_$./\\-])([A-Z]{1,5})(?![A-Za-z0-9_])")
PROSE_MIN_WORDS = 40
PROSE_MIN_SENTENCES = 2
PROSE_MIN_TICKERS = 5
_MAX_SCANNED_LINE = 6000  # a longer "line" is a minified bundle, not prose


# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One place a file looks like it carries real desk output."""

    path: str
    line: int
    signal: str
    detail: str
    excerpt: str

    def remedy(self) -> str:
        return REMEDIES[self.signal]

    def render(self) -> str:
        return (
            f"{self.path}:{self.line}: [{self.signal}] {self.detail}\n"
            f"    | {self.excerpt}\n"
            f"    FIX: {self.remedy()}"
        )


REMEDIES: dict[str, str] = {
    "broker-order-id": (
        "Replace the identifier with a placeholder such as "
        f"{PLACEHOLDER_UUID}. A broker order id is minted by the broker, so a "
        "real one in a public file says which order the desk actually placed. "
        "No test needs the real value; it only needs a consistent string."
    ),
    "production-log": (
        "Do not paste log lines off the desk host. Write the log lines the test "
        "needs by hand, with invented timestamps, invented tickers and invented "
        "counts. Production log lines carry live positions and broker payloads."
    ),
    "desk-decision": (
        "This looks like an answer the desk's own agents produced about a real "
        "position: a real ticker with a real entry, stop and target. Invent the "
        "numbers instead. Price things in whole dollars or halves (150.0, 507.5) "
        "so it is obvious to a reader that nothing here is a live level. A stop "
        "price published here is a stop that may be resting at the broker now."
    ),
    "desk-prose": (
        "This reads like a verbatim model answer about the real book — a "
        "paragraph naming five or more real stocks. Cut it down to the one or "
        "two sentences the test actually asserts on, and use invented tickers or "
        "a single real one. Do not paste a whole portfolio-manager answer."
    ),
}


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _log_line_carries_payload(line: str) -> bool:
    """True when a production-format log line carries live detail.

    One such line inside a comment or docstring is how the log FORMAT gets
    documented — `src/log_health.py` does exactly that, and its example message
    body is the word `text`. A line carrying a real ticker, a dollar figure or
    an identifier is not documentation; it is a paste.
    """
    body = line.split(": ", 1)[-1]
    if _UUID.search(body) or "$" in body:
        return True
    return any(t in REAL_TICKERS for t in _UPPER_TOKEN.findall(body))


def _scan_pass(text: str, path: str, *, pass_name: str) -> list[Finding]:
    findings: list[Finding] = []
    lines = text.splitlines()
    production_log_lines = [n for n, l in enumerate(lines, 1) if _PRODUCTION_LOG.search(l)]

    def excerpt(s: str) -> str:
        s = s.strip()
        return s[:160] + (" ..." if len(s) > 160 else "")

    for i, line in enumerate(lines, start=1):
        # The identifier and log-format signals read a short neighbourhood, so
        # they work on a line of any length and must run on one: a fixture
        # dumped as single-line JSON is one 64,000-character line. Only the
        # prose and decision-record signals, which read a line as a unit, are
        # capped — past the cap a "line" is a minified bundle, not writing.
        long_line = len(line) > _MAX_SCANNED_LINE

        for m in _UUID.finditer(line):
            if _is_placeholder_uuid(m.group(0)):
                continue
            if _uuid_is_markup_id(line, m.start(), m.end()):
                continue
            # On a pretty-printed file the key sits on a neighbouring line; on a
            # single-line JSON document it sits a few characters away and the
            # surrounding lines are the whole file, which would prove nothing.
            if len(line) > 2000:
                near = line[max(0, m.start() - 200): m.end() + 200]
            else:
                near = "\n".join(
                    lines[max(0, i - 1 - _UUID_CONTEXT_LINES): i + _UUID_CONTEXT_LINES]
                )
            if not _ORDER_CONTEXT.search(near):
                continue
            findings.append(Finding(
                path, i, "broker-order-id",
                f"a real-looking broker/order identifier ({m.group(0)}){pass_name}",
                excerpt(line),
            ))
            break

        if _PRODUCTION_LOG.search(line) and (
            len(production_log_lines) > 1 or _log_line_carries_payload(line)
        ):
            findings.append(Finding(
                path, i, "production-log",
                f"a line in the desk's production logger format{pass_name}",
                excerpt(line),
            ))

        sym = None if long_line else _SYMBOL_FIELD.search(line)
        if sym:
            window = "\n".join(lines[i - 1: i - 1 + _RECORD_WINDOW_LINES])
            present = [f for f, r in _DECISION_FIELD_RE.items() if r.search(window)]
            prices = _measured_prices(window)
            if len(present) >= DECISION_FIELDS_REQUIRED and len(prices) >= DECISION_PRICES_REQUIRED:
                findings.append(Finding(
                    path, i, "desk-decision",
                    f"{sym.group(1)} carries {len(present)} desk decision fields "
                    f"and prices that look measured, not invented "
                    f"({', '.join(sorted(prices)[:3])}){pass_name}",
                    excerpt(line),
                ))

        words = [] if long_line else line.split()
        if len(words) >= PROSE_MIN_WORDS and (line.count(".") + line.count("!")) >= PROSE_MIN_SENTENCES:
            tickers = {t for t in _UPPER_TOKEN.findall(line) if t in REAL_TICKERS}
            if len(tickers) >= PROSE_MIN_TICKERS:
                findings.append(Finding(
                    path, i, "desk-prose",
                    f"a {len(words)}-word passage naming {len(tickers)} real tickers "
                    f"({', '.join(sorted(tickers)[:6])}){pass_name}",
                    excerpt(line),
                ))

    return findings


def scan_text(text: str, path: str = "<memory>") -> list[Finding]:
    """Every finding in `text`, raw and with JSON escapes unwound.

    The second pass matters: a model answer stored inside another JSON document
    arrives as one physical line of `\\"symbol\\": \\"NVDA\\"`. Without it, the
    whole `ops/model_policy/results` class of files reads as clean.
    """
    findings = _scan_pass(text, path, pass_name="")
    seen = {(f.signal, f.excerpt) for f in findings}

    def add(more: list[Finding]) -> None:
        for f in more:
            if (f.signal, f.excerpt) not in seen:
                seen.add((f.signal, f.excerpt))
                findings.append(f)

    if '\\"' in text or "\\n" in text:
        add(_scan_pass(
            text.replace("\\n", "\n").replace('\\"', '"'),
            path, pass_name=" (inside an escaped JSON string)",
        ))

    # Whether a JSON fixture is pretty-printed is a formatting choice, and the
    # line-shaped signals must not depend on it: `holding_why_rsg_...json` is
    # one 64,000-character line. Re-dumping through the parser puts every field
    # on its own line, so the same record reads identically either way.
    stripped = text.lstrip()
    # Only worth doing when the file is not already one field per line: a
    # pretty-printed document re-dumps to itself and the pass costs real time.
    if stripped[:1] in "{[" and any(len(l) > 2000 for l in text.splitlines()):
        try:
            add(_scan_pass(
                json.dumps(json.loads(text), indent=1),
                path, pass_name=" (JSON re-indented for scanning)",
            ))
        except (ValueError, RecursionError):
            pass
    return findings


def read_text(path: Path) -> str | None:
    """File contents as text, decompressing `.gz`. None if it is not text."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError):
            return None
    if b"\0" in raw[:4096]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


# Scanning is proportionate to file size, and this repo commits third-party
# bulk snapshots that dwarf everything else — the 10-Q corpus decompresses to
# 148 MB of SEC HTML and alone costs more than the rest of the repo together.
# Past this cap only the first SCAN_BYTE_CAP characters are scanned. The cap is
# not a quiet hiding place: `test_no_real_desk_output.py` fails on any tracked
# file over the cap that is not named in LARGE_BLOBS with a reason, so putting
# desk output past byte 4,000,000 of a new giant file takes a reviewed entry.
SCAN_BYTE_CAP = 4_000_000

LARGE_BLOBS: dict[str, str] = {
    "ops/model_policy/fixtures/sec_10q10k_pm_public_day_2026-09-14.json.gz":
        "SEC 10-Q/10-K HTML corpus fetched from data.sec.gov; public filings, not desk output",
    "ops/model_policy/fixtures/yf_daily_bars_pm_public_day_2026-09-14.json.gz":
        "Yahoo daily OHLCV bars for the screen universe; public market data, not desk output",
}


# `test_no_real_desk_output.py` has to hold strings shaped exactly like real
# desk output — a real-looking ticker, cent-precision prices, a broker order
# id, a production log line — or it cannot prove the four signals actually
# fire. Those strings are invented for that one purpose; none of them ever
# came off the desk. Scanning that file for desk output means scanning the
# detector's own test specimens, which is not what this module is for.
#
# This is an exact single-path exclusion, not a directory or a glob:
# `test_the_specimen_exclusion_is_exactly_this_one_file` pins the set below to
# exactly this path, so it cannot quietly grow into a hiding place. A new file
# dropped anywhere else, including beside this one, is scanned like any other.
SPECIMEN_FILES = frozenset({
    "tests/test_no_real_desk_output.py",
})


def tracked_files(root: Path = PROJECT_ROOT) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True,
    ).stdout.decode("utf-8", "replace")
    return [p for p in out.split("\0") if p and p not in SPECIMEN_FILES]


@dataclass(frozen=True)
class Audit:
    """The whole-repository verdict, in the four shapes a reviewer needs."""

    #: Findings in files that are not on the allow-list at all.
    new_files: list[Finding]
    #: Allow-listed files whose finding count has RISEN: path -> (ceiling, now).
    over_ceiling: dict[str, tuple[int, int]]
    #: Findings in those risen files, so the failure can name a line.
    over_ceiling_findings: list[Finding]
    #: Allow-list entries that no longer earn their place: path -> why.
    stale_entries: dict[str, str]
    #: Tracked files larger than the scan cap that nobody has accounted for.
    oversize_unlisted: list[str]

    def ok(self) -> bool:
        return not (
            self.new_files or self.over_ceiling
            or self.stale_entries or self.oversize_unlisted
        )


def audit_repo(root: Path = PROJECT_ROOT) -> Audit:
    allowed = allow_list()
    tracked = set(tracked_files(root))
    new_files: list[Finding] = []
    over: dict[str, tuple[int, int]] = {}
    over_findings: list[Finding] = []
    counts: dict[str, int] = {}
    oversize_unlisted: list[str] = []

    for rel in sorted(tracked):
        text = read_text(root / rel)
        if text is None:
            continue
        if len(text) > SCAN_BYTE_CAP and rel not in LARGE_BLOBS:
            oversize_unlisted.append(rel)
        found = scan_text(text[:SCAN_BYTE_CAP], rel)
        counts[rel] = len(found)
        if not found:
            continue
        if rel not in allowed:
            new_files.extend(found)
            continue
        ceiling = allowed[rel][1]
        if len(found) > ceiling:
            over[rel] = (ceiling, len(found))
            over_findings.extend(found)

    stale: dict[str, str] = {}
    for rel in allowed:
        if rel not in tracked:
            stale[rel] = "the file is no longer tracked — delete this allow-list entry"
        elif counts.get(rel, 0) == 0:
            stale[rel] = (
                "the file no longer trips the guard (redacted?) — delete this "
                "allow-list entry so the file is protected again"
            )
    for rel in LARGE_BLOBS:
        if rel not in tracked:
            stale[rel] = "the file is no longer tracked — delete this LARGE_BLOBS entry"

    return Audit(new_files, over, over_findings, stale, oversize_unlisted)


def scan_repo(root: Path = PROJECT_ROOT, *, skip_allowed: bool = True) -> list[Finding]:
    """Every finding, optionally excluding allow-listed files. For the CLI."""
    allowed = allow_list()
    findings: list[Finding] = []
    for rel in tracked_files(root):
        if skip_allowed and rel in allowed:
            continue
        text = read_text(root / rel)
        if text is None:
            continue
        findings.extend(scan_text(text[:SCAN_BYTE_CAP], rel))
    return findings


if __name__ == "__main__":  # manual sweep: python -m tests.desk_output_guard
    import sys

    hits = scan_repo(skip_allowed="--all" not in sys.argv)
    for f in hits:
        print(f.render())
    print(f"\n{len(hits)} finding(s) in {len({f.path for f in hits})} file(s)")
    sys.exit(1 if hits else 0)
