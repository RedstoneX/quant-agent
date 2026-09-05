"""SEC EDGAR earnings data provider.

Downloads 10-Q and 10-K filings, extracts text, and tracks what's been fetched
via a local manifest so filings are only downloaded once.
"""

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from src.risk.rules import EARNINGS_STANCE_MAX_AGE_DAYS
from src.util.time import et_now, et_today
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

#: What a financial statement looks like, as text: dollar-prefixed amounts
#: ($1,234), bare comma-separated thousands (1,234,567), and parenthesized
#: negatives ((123)). Dense in income statements, balance sheets and cash
#: flow statements; near-absent in cover pages, tables of contents, XBRL
#: taxonomy boilerplate and the auditor's opinion letter.
#:
#: Defined once because two places need the SAME definition: the test for
#: whether extracted sections are worth keeping, and the search for the
#: densest region to fall back to. When those two disagreed, structured
#: extraction could pass a bar the fallback would have failed it on.
_FINANCIAL_FIGURE_RE = re.compile(
    r"\$[\d,]+(?:\.\d+)?|\d{1,3}(?:,\d{3})+|\(\d{1,3}(?:,\d{3})*\)"
)

SEC_BASE = "https://data.sec.gov"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
USER_AGENT = "quant-agent research@example.com"  # SEC requires contact info
REQUEST_DELAY = 0.12  # SEC rate limit: 10 req/s

# ETFs don't have SEC 10-Q/10-K filings — skip them at the entry point to
# avoid wasting CIK lookups + retry budget on something that will always
# fail. Keep this list in sync with `config/settings.yaml:trading.universe`
# whenever a new ETF is added there.
ETFS = {"SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLV", "XLI", "XLP",
        "XLY", "XLU", "XLRE", "XLB", "SMH", "SOXX", "DRAM", "CHPX",
        "SH", "SDS", "PSQ", "SQQQ"}


@dataclass
class FilingInfo:
    symbol: str
    form_type: str  # "10-Q" or "10-K"
    filing_date: str
    accession_number: str
    primary_doc: str  # filename of main document


@dataclass
class EarningsReport:
    symbol: str
    form_type: str
    filing_date: str
    filing_path: str  # local path to raw HTML
    analysis_path: str | None  # local path to analysis markdown
    text_excerpt: str  # extracted text for LLM (truncated)
    is_new: bool  # True if just downloaded this run
    # Parsed (not text-block) SEC XBRL values for the small set of fields
    # that map ONE-TO-ONE onto an `EarningsAnalysis` field — see
    # `EarningsDataProvider._xbrl_comparable_values` for which fields and
    # why only those. Used by `_classify_earnings_status`
    # (src/pipeline_stages.py) to cross-check the analyst's own reported
    # figures against the real filed numbers, catching a confident but
    # FABRICATED figure that isn't empty and isn't self-flagged. Empty dict
    # (not None) both when XBRL had nothing for this filer/period (fails
    # open, same as the text block) and for the cached-analysis path below,
    # which never re-fetches XBRL — either way there is nothing to cross-
    # check against, which the mismatch check already treats as "not a
    # mismatch" for an absent value.
    xbrl_facts: dict = field(default_factory=dict)


class EarningsDataProvider:
    def __init__(self, data_dir: str = "data/earnings", lookback_days: int = 45):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.data_dir / "manifest.json"
        self._manifest_lock = threading.Lock()
        self.manifest = self._load_manifest()
        self.lookback_days = lookback_days
        self._ticker_to_cik: dict[str, str] | None = None

    def _load_manifest(self) -> dict:
        if self.manifest_path.exists():
            try:
                return json.loads(self.manifest_path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Corrupt manifest, starting fresh: %s", e)
        return {}

    def save_manifest(self):
        with self._manifest_lock:
            tmp = self.manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.manifest, indent=2))
            os.replace(str(tmp), str(self.manifest_path))

    def prune(self, keep_days: int = 1000) -> int:
        """Delete raw filing HTML (``{FORM}_{YYYY-MM-DD}.html``) older than
        keep_days. The file-stores had no prune (design-review finding) — raw
        10-K/10-Q HTML (often multiple MB each) accreted per symbol per quarter
        forever.

        Conservative + safe: raw HTML is read ONLY at analysis time; once a
        filing is analyzed, the cached ``analysis_{FORM}_{date}.md`` is the read
        target (``_check_symbol`` globs the LATEST analysis per symbol). So this
        prunes only old raw HTML and leaves ALL analysis markdown untouched
        (small, and the actual money-relevant read target for evening's
        thesis_health). keep_days=1000 retains ~2.7 years of filings; only
        stale orphans go. Best-effort. Returns the count removed.
        """
        cutoff = et_today() - timedelta(days=keep_days)
        removed = 0
        try:
            symbol_dirs = [d for d in self.data_dir.iterdir() if d.is_dir()]
        except OSError as exc:
            logger.warning("earnings prune: cannot list %s: %s", self.data_dir, exc)
            return 0
        for sdir in symbol_dirs:
            for f in sdir.glob("*.html"):
                # filename: '{FORM}_{YYYY-MM-DD}.html' e.g. '10-Q_2026-03-15.html'
                datestr = f.stem.rsplit("_", 1)[-1]
                try:
                    d = date.fromisoformat(datestr)
                except (ValueError, TypeError):
                    continue  # unrecognized name — leave it alone
                if d < cutoff:
                    try:
                        f.unlink()
                        removed += 1
                    except OSError as exc:
                        logger.warning("earnings prune: failed to rm %s: %s", f, exc)
        if removed:
            logger.info(
                "earnings prune: removed %d raw filing HTML older than %s",
                removed, cutoff,
            )
        return removed

    def confirm_filing(self, report: "EarningsReport"):
        """Mark a filing as processed in the manifest. Call after analysis file is written."""
        with self._manifest_lock:
            manifest_key = f"{report.symbol}_{report.form_type}"
            self.manifest[manifest_key] = {
                "filing_date": report.filing_date,
                "form_type": report.form_type,
                "local_path": report.filing_path,
                "analysis_path": report.analysis_path,
                "failed_attempts": 0,
            }
        self.save_manifest()

    def record_failure(self, report: "EarningsReport", max_attempts: int = 3) -> bool:
        """Track a failed LLM analysis attempt. Abandon after `max_attempts`.

        Without bounded retries, a filing whose analysis consistently fails
        (parse error, rate limit, model overloaded) would be re-queued every
        session forever — wasting tokens indefinitely. After max_attempts we
        mark the filing abandoned so _check_symbol skips it and falls back to
        any prior analysis.

        Filing-date scoping: the manifest is keyed by symbol+form_type, but
        a single key spans multiple quarters of 10-Qs. When the entry's
        stored filing_date differs from the incoming report, this is a NEW
        filing — reset failed_attempts and abandoned flag so a one-off
        parse failure on Q1 doesn't pre-abandon Q2 on its first attempt.
        Codex r11 P2: previously the prior quarter's abandoned/attempts
        carried forward, so Q2's first transient failure landed at
        attempts=4 (abandon immediately).

        Returns True when the filing has just been abandoned (caller should
        stop queueing it).
        """
        abandoned = False
        with self._manifest_lock:
            key = f"{report.symbol}_{report.form_type}"
            entry = dict(self.manifest.get(key, {}))
            prior_filing_date = entry.get("filing_date")
            if prior_filing_date and prior_filing_date != report.filing_date:
                # Different filing_date → this is a new quarter. Reset the
                # retry budget; previous failure history doesn't apply.
                entry["failed_attempts"] = 0
                entry.pop("abandoned", None)
                entry.pop("abandoned_at", None)
                logger.info(
                    "Earnings retry budget reset for %s %s: prior filing %s "
                    "→ new filing %s",
                    report.symbol, report.form_type,
                    prior_filing_date, report.filing_date,
                )
            attempts = int(entry.get("failed_attempts", 0)) + 1
            entry["filing_date"] = report.filing_date
            entry["form_type"] = report.form_type
            entry["local_path"] = report.filing_path
            entry["failed_attempts"] = attempts
            if attempts >= max_attempts:
                entry["abandoned"] = True
                entry["abandoned_at"] = et_now().isoformat()
                abandoned = True
                logger.error(
                    "Abandoning earnings analysis for %s %s (%s) after %d attempts",
                    report.symbol, report.form_type, report.filing_date, attempts,
                )
            else:
                logger.warning(
                    "Earnings analysis for %s %s failed (attempt %d/%d); will retry next session",
                    report.symbol, report.form_type, attempts, max_attempts,
                )
            self.manifest[key] = entry
        self.save_manifest()
        return abandoned

    def _sec_get(
        self,
        url: str,
        max_retries: int = 3,
        total_timeout_s: float = 45.0,
    ) -> bytes:
        """GET with SEC-required headers, rate limiting, and retry on
        transient SEC errors.

        SEC enforces 10 req/sec via 429 (rate-limited) and returns 503
        when the service is overloaded. Before this retry loop, both
        errors raised HTTPError uncaught — caller's broad `except
        Exception` turned them into a silent empty filing list, which
        propagated to evening's `thesis_health_review` as missing 10-Q
        context (the core input for value-investing thesis decisions).

        Retries 429 / 503 / transient URLError with exponential backoff
        (1s, 2s, 4s). 404 / 400 / other 4xx-5xx propagate immediately —
        those mean the URL itself is wrong (bad CIK, missing filing),
        not a transient rate-limit, and retrying wastes the budget.

        `total_timeout_s` caps the worst-case time the loop can spend.
        Without it, 3 retries on a sustained SEC outage could burn
        REQUEST_DELAY(0.12s) + urlopen(15s) + backoff(1+2+4s) = ~21s × 3
        = ~63s per URL. With 77 stocks × 2 calls (submissions + filing
        body) that's hours of session time on a bad SEC day. 45s default
        keeps any single URL's worst-case bounded and lets the outer
        per-symbol `try: except Exception` move on.
        """
        start = time.time()
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            elapsed = time.time() - start
            if elapsed > total_timeout_s:
                logger.warning(
                    "SEC fetch exceeded total_timeout_s=%.0fs for %s "
                    "after %d attempts (elapsed=%.1fs)",
                    total_timeout_s, url, attempt, elapsed,
                )
                if last_exc is not None:
                    raise last_exc
                raise TimeoutError(
                    f"SEC fetch exceeded {total_timeout_s}s for {url}"
                )
            time.sleep(REQUEST_DELAY)
            try:
                with urlopen(req, timeout=15) as resp:
                    return resp.read()
            except HTTPError as e:
                last_exc = e
                if e.code in (429, 503):
                    backoff = 1.0 * (2 ** attempt)  # 1s → 2s → 4s
                    logger.warning(
                        "SEC %d on attempt %d/%d for %s — backing off %.1fs",
                        e.code, attempt + 1, max_retries, url, backoff,
                    )
                    time.sleep(backoff)
                    continue
                # Non-transient HTTP error: don't retry, surface immediately.
                raise
            except URLError as e:
                # Network blip (DNS / connection reset / timeout). Retry
                # since these are typically transient.
                last_exc = e
                backoff = 1.0 * (2 ** attempt)
                logger.warning(
                    "SEC URLError on attempt %d/%d for %s: %s — backing off %.1fs",
                    attempt + 1, max_retries, url, e, backoff,
                )
                time.sleep(backoff)
                continue
        # All retries exhausted; surface the last exception so caller
        # (currently inside a broad except Exception) can log it.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"SEC fetch failed for {url} without exception")

    def _get_cik(self, ticker: str) -> str | None:
        """Look up CIK number for a ticker symbol."""
        if self._ticker_to_cik is None:
            try:
                data = json.loads(self._sec_get(SEC_TICKERS_URL))
                self._ticker_to_cik = {}
                for entry in data.values():
                    t = entry.get("ticker", "").upper()
                    cik = str(entry.get("cik_str", ""))
                    if t and cik:
                        self._ticker_to_cik[t] = cik
            except Exception as e:
                logger.warning("Failed to fetch SEC ticker map: %s", e)
                self._ticker_to_cik = {}
        return self._ticker_to_cik.get(ticker.upper())

    def _get_recent_filings(self, cik: str, ticker: str) -> list[FilingInfo]:
        """Get recent 10-Q/10-K filings from SEC EDGAR.

        Note on MLPs (master limited partnerships, e.g. EPD): they are SEC
        registrants and DO file 10-Q/10-K via the partnership entity —
        no special handling required. The Schedule K-1 some operators
        associate with MLPs is a tax document mailed to unit holders, not
        a substitute for the corporate filing. EPD shows up on EDGAR with
        regular quarterly 10-Qs that this method will pick up.
        """
        padded_cik = cik.zfill(10)
        url = f"{SEC_BASE}/submissions/CIK{padded_cik}.json"
        try:
            data = json.loads(self._sec_get(url))
        except Exception as e:
            logger.warning("Failed to fetch submissions for %s (CIK %s): %s", ticker, cik, e)
            return []

        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])

        # SEC's submissions JSON returns parallel arrays; in practice they
        # always align, but an upstream truncation or partial response
        # would silently desync them. Index-based access on the previous
        # version checked only forms vs dates length and could IndexError
        # on accessions / primary_docs if those came up short. zip()
        # tolerates whichever array is shortest and exits cleanly — at
        # worst we miss a trailing filing rather than crash mid-scan.
        if not (len(forms) == len(dates) == len(accessions) == len(primary_docs)):
            logger.warning(
                "SEC submissions arrays misaligned for %s (CIK %s): "
                "forms=%d dates=%d accessions=%d primary_docs=%d — "
                "iterating over the shortest",
                ticker, cik, len(forms), len(dates),
                len(accessions), len(primary_docs),
            )

        cutoff = (et_now() - timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")
        filings = []
        for form, filing_date, accession, primary_doc in zip(
            forms, dates, accessions, primary_docs,
        ):
            if form not in ("10-Q", "10-K"):
                continue
            if filing_date < cutoff:
                continue
            filings.append(FilingInfo(
                symbol=ticker,
                form_type=form,
                filing_date=filing_date,
                accession_number=accession,
                primary_doc=primary_doc or "",
            ))
        return filings

    def _fetch_xbrl_raw(self, cik: str, ticker: str, filing_date: str) -> dict[str, tuple[float, str]]:
        """Pull hard financial-statement figures from SEC's structured XBRL
        data instead of hoping a text-regex found the right heading in the
        filing's rendered HTML.

        ROOT CAUSE this replaces: `_extract_key_sections` below regexes for
        headings like "consolidated statements of operations" in flattened
        plain text, and filing layout / heading phrasing varies enough by
        filer that no amount of regex tuning holds up filing-to-filing.
        Measured on production: ~20 of 67 filings recovered under 1,600
        characters from a 184,000-character document this way, and 12 —
        including MSFT, AAPL, GOOGL, BAC, CVX, NFLX — extracted exactly ZERO
        financial figures. The earnings analyst had never seen a single
        number for those names.

        XBRL is the SEC-mandated structured version of these exact numbers,
        served for free with no auth beyond the same User-Agent every other
        SEC call here already sends. This is fetched INDEPENDENTLY of
        whatever `_extract_key_sections` finds below, so it grounds the
        numeric side of the analysis even when the text matcher fails
        completely — it doesn't make the matcher smarter, it makes the
        matcher's failure mode harmless for the numbers that matter most.
        It does NOT cover MD&A / risk-factors prose: XBRL doesn't tag prose,
        so that stays on the text-matching path exactly as before.

        Fails open: any error (network, missing CIK in XBRL, no matching
        concept) returns {} and callers proceed exactly as before this
        existed — a SEC API hiccup can only leave the analysis as good as
        before, never worse. Two callers share this one fetch so a filing
        is only hit once per `_check_symbol` run, not twice:
        `_get_xbrl_financial_facts` (below) turns this into the prompt's
        text block, and `_xbrl_comparable_values` turns it into the
        parsed-number ground truth `_classify_earnings_status`
        (src/pipeline_stages.py) cross-checks the analyst's own figures
        against.

        Returns a plain `{concept_key: (value, period_end_iso)}` dict —
        `concept_key` is the internal name used below ("revenue",
        "net_income", "gross_profit", "operating_income", "assets", "cash",
        "long_term_debt", "eps"), not the raw XBRL tag name.
        """
        padded_cik = cik.zfill(10)
        url = f"{SEC_BASE}/api/xbrl/companyfacts/CIK{padded_cik}.json"
        try:
            data = json.loads(self._sec_get(url))
        except Exception as e:  # noqa: BLE001 — fail open, see docstring
            logger.warning(
                "XBRL companyfacts fetch failed for %s (CIK %s): %s", ticker, cik, e,
            )
            return {}

        facts = data.get("facts", {}).get("us-gaap", {})
        if not facts:
            return {}

        try:
            target = date.fromisoformat(filing_date)
        except (TypeError, ValueError):
            target = None

        def _best_value(concept_names: list[str], unit_key: str):
            # Gather candidates across ALL given concept names, not just the
            # first one that has any data at all. Filers change which XBRL
            # tag they report under over time — e.g. many large companies
            # stopped using `Revenues` around ASC 606 adoption (~2018) in
            # favor of `RevenueFromContractWithCustomerExcludingAssessedTax`.
            # `Revenues` still HAS entries for those filers, just a decade
            # stale — stopping at "first concept with any data" silently
            # picked a ~10-year-old number for MSFT/AAPL revenue in testing.
            # Comparing recency across every concept and picking the single
            # freshest match closes that.
            dated: list[tuple[date, dict]] = []
            stale_fallbacks: list[tuple[date, dict]] = []
            for concept in concept_names:
                entries = facts.get(concept, {}).get("units", {}).get(unit_key, [])
                if not entries:
                    continue
                candidates = [e for e in entries if e.get("form") in ("10-Q", "10-K")]
                if not candidates:
                    candidates = entries
                for e in candidates:
                    end = e.get("end")
                    if not end:
                        continue
                    try:
                        end_d = date.fromisoformat(end)
                    except ValueError:
                        continue
                    if target is None or end_d <= target:
                        dated.append((end_d, e))
                    else:
                        stale_fallbacks.append((end_d, e))
            if dated:
                dated.sort(key=lambda pair: pair[0])
                chosen_end, chosen = dated[-1]
            elif stale_fallbacks:
                stale_fallbacks.sort(key=lambda pair: pair[0])
                chosen_end, chosen = stale_fallbacks[-1]
            else:
                return None
            # Some filers stop reporting a given XBRL tag (switch to a
            # differently-named one, or fold it into a different line item)
            # without ever filing a final value under the old tag — that
            # stale entry still LOOKS like real data and would otherwise be
            # presented as current. Measured while building this: BAC's
            # cash tag was 5+ years stale, CVX's long-term-debt tag ~8
            # years, NFLX's gross-profit tag ~5 years, despite each having
            # CURRENT data available under the concept the analysis prompt
            # actually needs. A number this old is worse than no number —
            # it's the exact "PM sizes off an ungrounded field" failure
            # mode this whole fix exists to close, just moved from text
            # extraction into XBRL. One fiscal year plus one quarter of
            # slack (~455 days) comfortably covers a filer that's merely
            # running one quarter behind without accepting a genuinely
            # abandoned tag.
            MAX_STALENESS_DAYS = 455
            if target is not None and (target - chosen_end).days > MAX_STALENESS_DAYS:
                return None
            val = chosen.get("val")
            end = chosen.get("end", "?")
            if val is None:
                return None
            return val, end

        raw: dict[str, tuple[float, str]] = {}
        revenue = _best_value(
            ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"], "USD",
        )
        if revenue is not None:
            raw["revenue"] = revenue
        for concept, key in (
            ("NetIncomeLoss", "net_income"),
            ("GrossProfit", "gross_profit"),
            ("OperatingIncomeLoss", "operating_income"),
            ("Assets", "assets"),
            ("CashAndCashEquivalentsAtCarryingValue", "cash"),
            ("LongTermDebtNoncurrent", "long_term_debt"),
        ):
            v = _best_value([concept], "USD")
            if v is not None:
                raw[key] = v
        eps = _best_value(["EarningsPerShareDiluted"], "USD/shares")
        if eps is not None:
            raw["eps"] = eps
        return raw

    # Human-readable labels for the text block the LLM prompt reads, in the
    # same fixed order the block has always rendered in — kept in one place
    # so `_get_xbrl_financial_facts` and any future consumer of
    # `_fetch_xbrl_raw` render the same concepts under the same names.
    _XBRL_TEXT_LABELS = (
        ("revenue", "Total Revenue"),
        ("net_income", "Net Income"),
        ("gross_profit", "Gross Profit"),
        ("operating_income", "Operating Income"),
        ("assets", "Total Assets"),
        ("cash", "Cash & Equivalents"),
        ("long_term_debt", "Long-Term Debt"),
        ("eps", "Diluted EPS"),
    )

    def _get_xbrl_financial_facts(self, cik: str, ticker: str, filing_date: str) -> str:
        """Fetch + format `_fetch_xbrl_raw`'s values as the text block
        prepended to the filing text the earnings analyst LLM reads —
        unchanged output from before this was split out of one combined
        fetch+format method, see `_fetch_xbrl_raw`'s docstring for the real
        motivation and the fail-open contract this inherits unchanged
        ("" on no data/error). `_check_symbol` calls `_format_xbrl_text`
        directly instead of this, so a filing already fetched once via
        `_fetch_xbrl_raw` isn't fetched again just to build this block."""
        return self._format_xbrl_text(self._fetch_xbrl_raw(cik, ticker, filing_date))

    def _format_xbrl_text(self, raw: dict[str, tuple[float, str]]) -> str:
        """Pure formatter half of `_get_xbrl_financial_facts` — no network."""
        if not raw:
            return ""
        lines: list[str] = []
        for key, label in self._XBRL_TEXT_LABELS:
            entry = raw.get(key)
            if entry is None:
                continue
            value, end = entry
            if key == "eps":
                lines.append(f"{label}: ${value:.2f} (period ending {end})")
            else:
                lines.append(f"{label}: ${value:,.0f} (period ending {end})")
        if not lines:
            return ""
        return (
            "=== STRUCTURED FINANCIAL FACTS (SEC XBRL, not text-extracted) ===\n"
            + "\n".join(lines)
            + "\n"
        )

    # Concept keys (from `_fetch_xbrl_raw`) that map ONE-TO-ONE onto an
    # `EarningsAnalysis` field — the same real-world figure, not a derived
    # or differently-scoped one. Deliberately excludes `gross_profit` /
    # `operating_income` (the analyst reports MARGINS — ratios it computes
    # itself — not the raw dollar figures XBRL tags, so there's no
    # apples-to-apples number to compare) and `long_term_debt` (XBRL here
    # is non-current debt only, while `EarningsBalanceSheet.total_debt`
    # conventionally includes the current portion too — comparing them
    # would flag a definitional gap as if it were a factual error). Cash
    # flow statement fields aren't fetched by `_fetch_xbrl_raw` at all yet,
    # so there is nothing to compare them against.
    _XBRL_COMPARABLE_KEYS = ("revenue", "net_income", "cash", "eps")

    def _xbrl_comparable_values(self, raw: dict[str, tuple[float, str]]) -> dict[str, float]:
        """The subset of `_fetch_xbrl_raw`'s output usable as real,
        directly-comparable ground truth — see `_XBRL_COMPARABLE_KEYS` for
        which fields and why only those. Pure/no network: callers already
        have `raw` from one `_fetch_xbrl_raw` call and derive both the
        prompt text block and this from it, rather than fetching twice.

        Returns {} (not None) when nothing comparable was available — the
        SEC XBRL fetch failed open, or none of the comparable concepts had
        current data — which the mismatch check downstream already treats
        the same as "nothing to compare," never as a mismatch.
        """
        return {
            key: raw[key][0] for key in self._XBRL_COMPARABLE_KEYS if key in raw
        }

    def _download_filing(self, cik: str, filing: FilingInfo) -> str | None:
        """Download filing HTML and save to local file. Returns local path."""
        symbol_dir = self.data_dir / filing.symbol
        symbol_dir.mkdir(parents=True, exist_ok=True)

        accession_clean = filing.accession_number.replace("-", "")
        url = f"{SEC_ARCHIVES}/{cik}/{accession_clean}/{filing.primary_doc}"

        local_path = symbol_dir / f"{filing.form_type}_{filing.filing_date}.html"
        if local_path.exists():
            return str(local_path)

        try:
            content = self._sec_get(url)
            local_path.write_bytes(content)
            logger.info("Downloaded %s %s (%s) → %s", filing.symbol, filing.form_type,
                        filing.filing_date, local_path)
            return str(local_path)
        except Exception as e:
            logger.warning("Failed to download %s %s: %s", filing.symbol, filing.form_type, e)
            return None

    def _extract_text(self, html_path: str, max_chars: int = 30000) -> str:
        """Extract high-signal sections from a SEC 10-Q / 10-K filing.

        A raw 10-K can be 200K+ chars; 70-80% is boilerplate the LLM doesn't
        need (properties listings, mine safety disclosures, legal notes,
        signatures, exhibit indices, XBRL footers). Dumping that to the
        earnings_analyst wastes ~30% of our total token budget and dilutes
        its attention away from what drives the investment call.

        This returns a compressed document with just:
        - Financial statements  (revenue / margins / EPS numbers)
        - MD&A                  (narrative on growth, segments, outlook)
        - Risk factors          (top risks management flagged)

        Falls back to truncated full-text when structured extraction
        can't locate any sections (non-standard filing layout).
        """
        raw = Path(html_path).read_bytes()
        soup = BeautifulSoup(raw, "html.parser")

        for tag in soup(["script", "style", "meta", "link"]):
            tag.decompose()

        text = soup.get_text(separator="\n")
        lines = [line.strip() for line in text.splitlines()]
        text = "\n".join(line for line in lines if line)
        text = re.sub(r"\n{3,}", "\n\n", text)

        # Structured path
        sections = self._extract_key_sections(text)
        structured_output = ""
        if sections:
            parts: list[str] = []
            total = 0
            # Order: financials (hard numbers) → MD&A (narrative) → risks (tail)
            order = ("financial_statements", "mdna", "risk_factors")
            for label in order:
                body = sections.get(label)
                if not body:
                    continue
                # Per-section cap — MD&A on a 10-K can run 40K+ on its own.
                if len(body) > 12000:
                    body = body[:12000] + "\n[... section truncated ...]"
                header = label.replace("_", " ").upper()
                section_text = f"=== {header} ===\n{body}"
                if total + len(section_text) + 2 > max_chars:
                    remaining = max_chars - total - 30  # 30 chars for tail marker
                    if remaining > 2000:
                        parts.append(section_text[:remaining] + "\n[... truncated ...]")
                    break
                parts.append(section_text)
                total += len(section_text) + 2
            if parts:
                structured_output = "\n\n".join(parts)

        # Accept the structured extraction only if it is BOTH long enough and
        # actually contains financial figures.
        #
        # Length alone was the test until 2026-08-28, and it silently gutted
        # the entire earnings evidence source. `_extract_key_sections` matches
        # the phrase "financial statements", which also appears in the
        # auditor's opinion letter — "...the related notes (collectively
        # referred to as the financial statements)". That letter is prose, it
        # is several thousand characters long, and it therefore cleared a
        # 3,000-character bar comfortably. Clearing the bar SUPPRESSED the
        # density-seeking fallback below, which is the code that would have
        # found the real tables.
        #
        # Measured over the 68 filings cached on the production box: 56 of
        # them extracted fewer than 20 dollar amounts, and 12 — including
        # MSFT, AAPL, GOOGL, BAC, CVX and NFLX — extracted exactly ZERO. The
        # earnings analyst had never seen a single number for those names.
        #
        # A financial statement is defined by its figures, so that is what is
        # tested. Same pattern `_find_financial_dense_region` scores with, so
        # "dense enough to keep" and "dense enough to seek" mean one thing.
        MIN_STRUCTURED_SIZE = 3000
        MIN_STRUCTURED_FIGURES = 40
        figure_count = len(_FINANCIAL_FIGURE_RE.findall(structured_output))
        if (
            structured_output
            and len(structured_output) >= MIN_STRUCTURED_SIZE
            and figure_count >= MIN_STRUCTURED_FIGURES
        ):
            logger.info(
                "Extracted %d section(s) from filing → %d chars, %d figures "
                "(down from %d)",
                len(sections), len(structured_output), figure_count, len(text),
            )
            return structured_output

        if structured_output and len(structured_output) >= MIN_STRUCTURED_SIZE:
            logger.warning(
                "Structured extraction produced %d chars but only %d financial "
                "figures (need %d) — this is narrative, not statements. "
                "Falling back to the density-seeking slice.",
                len(structured_output), figure_count, MIN_STRUCTURED_FIGURES,
            )

        # Fallback: truncated full text. The naive "first max_chars" slice
        # is wrong for iXBRL 10-Q filings — the front of the cleaned text is
        # typically cover page + TOC + XBRL boilerplate, and the actual
        # financial tables live 30-50% into the document. R6 log audit
        # found 58 cases where this fallback fed the earnings LLM nothing
        # but XBRL labels and got "data quality: CRITICAL" back. Instead,
        # find the densest $-amount / numeric-table region and slice
        # around it.
        if len(text) > max_chars:
            slice_start = self._find_financial_dense_region(text, max_chars)
            logger.info(
                "Structured extraction too sparse (%d chars); falling back to truncated full text "
                "(%d → %d chars, slice @ %d)",
                len(structured_output), len(text), max_chars, slice_start,
            )
            text = text[slice_start:slice_start + max_chars] + "\n\n[... truncated ...]"
        return text

    @staticmethod
    def _find_financial_dense_region(text: str, window: int) -> int:
        """Return the start index of the `window`-char slice with the
        highest density of financial-table content.

        Heuristic: count dollar-prefixed amounts (`$1,234`), bare
        comma-separated thousands (`1,234,567`), and parenthesized
        negatives (`(123)`). These patterns are dense in income
        statements, balance sheets, and cash flow statements; sparse
        in cover pages, TOCs, and XBRL taxonomy boilerplate.

        Returns 0 when text is shorter than window, or when no
        candidate slice is meaningfully denser than the head — in
        which case the original behavior (head slice) is preserved.
        """
        if len(text) <= window:
            return 0
        # Slide in 10 steps across the document; cheap, ~10 regex passes.
        pattern = _FINANCIAL_FIGURE_RE
        step = max(window // 10, 1000)
        scores: list[tuple[int, int]] = []  # (count, start)
        for start in range(0, len(text) - window + 1, step):
            chunk = text[start:start + window]
            scores.append((len(pattern.findall(chunk)), start))
        if not scores:
            return 0
        best_count, best_start = max(scores, key=lambda x: x[0])
        head_count = scores[0][0]
        # Only relocate if the densest region is meaningfully richer than
        # the head — guards against the "all chunks are equally barren"
        # case where moving the slice doesn't help. 2× threshold tuned
        # against the audit's 58 affected filings: their head chunks had
        # ~5-15 matches while mid-document chunks had 50-300.
        if best_count >= 2 * max(head_count, 5):
            return best_start
        return 0

    def _extract_key_sections(self, text: str) -> dict[str, str]:
        """Locate financial / MD&A / risk-factor section bodies via regex.

        Filings typically carry a table of contents listing 'Item 1. ...',
        'Item 2. ...' near the top — those are pointers, not the section
        bodies themselves. We prefer matches beyond the first ~15K chars
        (past the TOC) when multiple matches exist. Body extends from the
        header to the next detected section/stop marker.
        """
        # Each entry: (label, pattern, strategy)
        # - "first":    the pattern matches a distinctive heading and the
        #               first occurrence is the real one.
        # - "skip_toc": the pattern matches a section title that DOES appear
        #               in a TOC — prefer the first occurrence past ~15K
        #               chars. Originally only used for mdna / risk_factors,
        #               but R6 log audit (May 2026) found 58 filings where
        #               financial_statements matched a TOC-style entry and
        #               produced a tiny (200-700 char) body — affected
        #               PG / SBUX / V / ABT / AMZN / CAT / COP / LLY 10-Qs
        #               whose internal "Index to Financial Statements"
        #               navigation listed "Consolidated Statements of
        #               Operations" before the real section started. Switched
        #               to skip_toc so we land on the actual table.
        patterns = [
            ("financial_statements", re.compile(
                r"(?im)(?:condensed\s+)?consolidated\s+statements?\s+of\s+(?:operations?|income|earnings)\b"
            ), "skip_toc"),
            ("mdna", re.compile(
                # [\u2019'] accepts both ASCII apostrophe and the curly
                # quote U+2019 that SEC HTML filings commonly use.
                r"(?im)^\s*(?:item\s*[27]\.?)\s*management[\u2019']?s?\s+discussion"
            ), "skip_toc"),
            ("risk_factors", re.compile(
                r"(?im)^\s*(?:item\s*1a\.?)\s*risk\s+factors"
            ), "skip_toc"),
        ]
        stop_pattern = re.compile(
            r"(?im)^\s*(?:item\s*\d+[a-z]?\.?\s|"
            r"signatures?\s*$|"
            r"exhibit\s+index|"
            r"part\s+(?:i|ii|iii|iv)\b)"
        )
        all_stops = sorted(m.start() for m in stop_pattern.finditer(text))

        found: dict[str, str] = {}
        for label, pat, strategy in patterns:
            matches = list(pat.finditer(text))
            if not matches:
                continue
            if strategy == "first":
                chosen = matches[0]
            else:  # skip_toc
                chosen = next(
                    (m for m in matches if m.start() >= 15000),
                    matches[-1],
                )
            body_start = chosen.end()
            # Next stop after (body_start + 200) — don't let the header's
            # own "Item X" mention terminate its own body.
            next_stop = None
            for stop in all_stops:
                if stop > body_start + 200:
                    next_stop = stop
                    break
            body = (
                text[body_start:next_stop].strip()
                if next_stop else text[body_start:].strip()
            )
            # Low threshold — 10-Q Risk Factors sections often read "No
            # material changes from 10-K" in ~200-400 chars, which is still
            # useful information (confirms no new risks flagged). Below 150
            # is almost certainly a false-positive match.
            if len(body) >= 150:
                found[label] = body
        return found

    def _get_analysis_path(self, symbol: str, form_type: str, filing_date: str) -> str:
        """Return path for the analysis markdown file."""
        symbol_dir = self.data_dir / symbol
        symbol_dir.mkdir(parents=True, exist_ok=True)
        return str(symbol_dir / f"analysis_{form_type}_{filing_date}.md")

    def check_and_fetch(self, symbols: list[str]) -> list[EarningsReport]:
        """Check for new filings for all symbols. Download new ones, return reports.

        Returns EarningsReport for each symbol that has:
        - A newly downloaded filing (is_new=True), or
        - An existing analysis from a previous run (is_new=False)
        """
        reports: list[EarningsReport] = []
        stocks = [s for s in symbols if s not in ETFS]

        for symbol in stocks:
            try:
                report = self._check_symbol(symbol)
                if report:
                    reports.append(report)
            except Exception as e:
                logger.warning("Error checking earnings for %s: %s", symbol, e)

        logger.info("Earnings check: %d reports (%d new) from %d stocks",
                     len(reports), sum(1 for r in reports if r.is_new), len(stocks))
        return reports

    def _check_symbol(self, symbol: str) -> EarningsReport | None:
        """Check a single symbol for new or existing filings."""
        cik = self._get_cik(symbol)
        if not cik:
            return None

        filings = self._get_recent_filings(cik, symbol)
        if not filings:
            # No recent filings — check for existing analysis (any form)
            return self._get_existing_analysis(symbol)

        # Take the most recent filing
        latest = filings[0]
        manifest_key = f"{symbol}_{latest.form_type}"
        entry = self.manifest.get(manifest_key, {})
        last_known = entry.get("filing_date")

        # Honor the abandoned flag: after N failed analysis attempts we stop
        # re-queueing this specific filing. Fall back to prior analysis if any.
        if entry.get("abandoned") and last_known == latest.filing_date:
            logger.info(
                "Skipping %s %s (%s) — previously abandoned after repeated LLM failures",
                symbol, latest.form_type, latest.filing_date,
            )
            return self._get_existing_analysis(symbol, form_type=latest.form_type)

        # "Already processed" must mean SUCCEEDED, not merely attempted.
        #
        # 2026-07-16 audit: record_failure() writes the new filing_date into
        # the manifest alongside failed_attempts=1 and logs "will retry next
        # session" — but this gate then saw last_known == latest.filing_date,
        # found the PRIOR quarter's analysis on disk, and returned it with
        # is_new=False. The pipeline only re-queues is_new reports, so the
        # filing was never re-analyzed, record_failure never ticked again, the
        # 3-strike budget never reached `abandoned`, and PM was served last
        # quarter's numbers labelled "[from cache]" as if they were current.
        # One transient LLM failure permanently dropped that quarter's filing —
        # for every symbol that already had a same-form analysis on disk, i.e.
        # the whole universe in steady state.
        # confirm_filing() writes failed_attempts=0 on success, so a genuinely
        # processed filing still short-circuits here. Attempts 1-2 now fall
        # through to re-download → is_new=True → re-analysis; on the 3rd
        # failure the `abandoned` branch above takes over as designed.
        try:
            prior_failures = int(entry.get("failed_attempts", 0) or 0)
        except (TypeError, ValueError):
            prior_failures = 0
        if last_known == latest.filing_date and not prior_failures:
            # Already processed this filing — return existing analysis matching this form_type
            existing = self._get_existing_analysis(symbol, form_type=latest.form_type)
            if existing:
                return existing
            # Analysis file missing (e.g. killed mid-analysis) — re-download
        elif last_known == latest.filing_date and prior_failures:
            logger.info(
                "%s %s (%s): retrying after %d failed analysis attempt(s)",
                symbol, latest.form_type, latest.filing_date, prior_failures,
            )

        # New filing — download it
        local_path = self._download_filing(cik, latest)
        if not local_path:
            return self._get_existing_analysis(symbol, form_type=latest.form_type)

        text = self._extract_text(local_path)
        xbrl_raw = self._fetch_xbrl_raw(cik, symbol, latest.filing_date)
        xbrl_block = self._format_xbrl_text(xbrl_raw)
        if xbrl_block:
            text = xbrl_block + "\n" + text
        analysis_path = self._get_analysis_path(symbol, latest.form_type, latest.filing_date)

        return EarningsReport(
            symbol=symbol,
            form_type=latest.form_type,
            filing_date=latest.filing_date,
            filing_path=local_path,
            analysis_path=analysis_path,
            text_excerpt=text,
            is_new=True,
            xbrl_facts=self._xbrl_comparable_values(xbrl_raw),
        )

    def _get_existing_analysis(
        self, symbol: str, form_type: str | None = None
    ) -> EarningsReport | None:
        """Find the latest existing analysis for a symbol, bounded by age.

        When form_type is given, only analyses of that form are considered; otherwise
        any form's most-recent analysis is returned. Ordering is by filing_date from
        the filename, not by lexicographic sort (so 10-K 2026-03-01 beats 10-Q 2026-02-15).

        This is the fallback `_check_symbol` reaches whenever the SEC scan window
        (45 days) has nothing for the symbol — a quiet quarter, not an outage, is
        the common case. Before 2026-09-02 that fallback had no age bound at all:
        whatever was newest on disk was re-served as the CURRENT earnings view no
        matter how old, because `prune()` only removes raw filing HTML, and only
        past 1000 days. A symbol with no new filing for a year would still hand
        back that year-old analysis looking exactly like a fresh one.
        `PortfolioManagerAgent.stale_evidence_sources` already stops a stance past
        this age from EARNING SIZE — but that gate is downstream, in the sizing
        path, and it deliberately leaves a served stance in place (see its
        docstring) rather than remove it, because pulling a stance the PM has
        already cited out of the evidence registry fails `validate_grounding` for
        the WHOLE session, not just that one target. Bounding it HERE instead
        means an over-age analysis is never handed to a session as "current" in
        the first place: the symbol looks exactly like one with no earnings
        coverage yet at all — a CIK miss, an unlisted name, a first-run symbol —
        which every consumer already treats as ordinary, not an error. Nothing
        about `stale_evidence_sources` changes: it still recomputes staleness
        generically from `filing_date` for anything that IS served, so a report
        reaching the PM through some other path is still caught there too.

        Reuses `EARNINGS_STANCE_MAX_AGE_DAYS` (`src/risk/rules.py`) rather than a
        new threshold — this desk already wrote 90 days down twice before this
        fix existed (the earnings seat's own prompt caps conviction past it and
        calls anything past 180d one that "should not have reached you";
        `TradingPipeline._missed_ops_earnings_signal` already refuses anything
        older as "recent" evidence). A fourth, independent number here would just
        be one more way for the three to quietly disagree.

        An unparseable or missing filing_date is treated as too old to serve —
        an unknowable age is not evidence of freshness, the same call
        `stale_evidence_sources` and `_missed_ops_earnings_signal` already make.
        """
        symbol_dir = self.data_dir / symbol
        if not symbol_dir.exists():
            return None

        pattern = f"analysis_{form_type}_*.md" if form_type else "analysis_*.md"

        def _filing_date(path: Path) -> str:
            # filename format: analysis_<form_type>_<YYYY-MM-DD>.md
            parts = path.stem.split("_", 2)
            return parts[2] if len(parts) > 2 else ""

        analyses = sorted(symbol_dir.glob(pattern), key=_filing_date, reverse=True)
        if not analyses:
            return None

        analysis_path = str(analyses[0])
        # Parse form type and date from filename: analysis_10-Q_2026-03-15.md
        parts = analyses[0].stem.split("_", 2)
        form_type = parts[1] if len(parts) > 1 else "unknown"
        filing_date = parts[2] if len(parts) > 2 else "unknown"

        try:
            age_days = (et_today() - date.fromisoformat(filing_date)).days
        except (TypeError, ValueError):
            age_days = None
        if age_days is None or age_days > EARNINGS_STANCE_MAX_AGE_DAYS:
            logger.info(
                "Existing earnings analysis for %s (%s, filed %s) is %s — "
                "the fallback will not re-serve it (bound: %dd)",
                symbol, form_type, filing_date,
                "unparseable" if age_days is None else f"{age_days}d old",
                EARNINGS_STANCE_MAX_AGE_DAYS,
            )
            return None

        return EarningsReport(
            symbol=symbol,
            form_type=form_type,
            filing_date=filing_date,
            filing_path="",
            analysis_path=analysis_path,
            text_excerpt="",  # No text needed — analysis already exists
            is_new=False,
        )
