"""src.research_continuity.change_detectors -- is a remembered research answer still true?

Bodies moved verbatim from src/pipeline_research_continuity.py (originally
src/pipeline.py), item 210 step 9, first research-continuity instalment: the
macro regime / FRED-print change detectors and the news-wire peek that decide
whether a stored seat answer may be reused. Every collaborator is an explicit
keyword-only constructor argument; nothing here imports src.pipeline. Read-only:
places no orders, cancels nothing, amends no stop, writes no journal row.

The one piece of state the bodies share -- the wire items the expiry peek just
fetched, which the heal path later re-asks with -- lives behind the
`last_news_peek_items` property so the host can keep it where it always kept it
(`_last_news_peek_items` on the pipeline); standalone it is an in-memory slot.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from src.sentinel.counted import record_swallowed, record_swallowed_here

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class ResearchChangeDetectors:
    """Macro regime/print change detection and the news-wire peek for research reuse."""

    def __init__(
        self,
        *,
        macro_store,
        macro,
        news_provider,
        news_store,
        config,
        peek_items_get: Callable[[], object] | None = None,
        peek_items_set: Callable[[object], None] | None = None,
        macro_history_regime_changed=None,
        macro_series_prints_changed=None,
        live_macro_series_prints=None,
        watched_research_symbols=None,
        peek_news_headlines=None,
    ) -> None:
        self.macro_store = macro_store
        self.macro = macro
        self.news_provider = news_provider
        self.news_store = news_store
        self.config = config
        self._peek_items: object = None
        self._peek_items_get = peek_items_get
        self._peek_items_set = peek_items_set
        # A host that replaced one of these (a test double, an instance-level
        # override) is honoured; the host's own thin shim is never passed back
        # in, so the part keeps its own body (see cost_circuit/parts/shim_guard).
        if macro_history_regime_changed is not None:
            self._macro_history_regime_changed = macro_history_regime_changed
        if macro_series_prints_changed is not None:
            self._macro_series_prints_changed = macro_series_prints_changed
        if live_macro_series_prints is not None:
            self._live_macro_series_prints = live_macro_series_prints
        if watched_research_symbols is not None:
            self._watched_research_symbols = watched_research_symbols
        if peek_news_headlines is not None:
            self._peek_news_headlines = peek_news_headlines

    @property
    def last_news_peek_items(self):
        if self._peek_items_get is not None:
            return self._peek_items_get()
        return self._peek_items

    @last_news_peek_items.setter
    def last_news_peek_items(self, items) -> None:
        if self._peek_items_set is not None:
            self._peek_items_set(items)
        else:
            self._peek_items = items

    def _macro_regime_or_print_changed(self, state: dict) -> bool:
        """True when a later snapshot actually changed the regime or a FRED print.

        Calendar age is not expiry — macro is reusable across days until a
        real regime/print change. A failed detector is not a change.
        Print change is a change in the actual series values/observation
        dates, not a change in the regime label string.
        """
        if not isinstance(state, dict):
            return False
        stored_regime = str(state.get("regime") or "").strip()
        if not stored_regime:
            return False
        try:
            if self._macro_history_regime_changed(state, stored_regime):
                return True
        except Exception:  # noqa: BLE001 — failed detector is not a change
            pass
        try:
            if self._macro_series_prints_changed(state):
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _macro_history_regime_changed(self, state: dict, stored_regime: str) -> bool:
        """True when a newer dated history snapshot carries a different regime."""
        store = getattr(self, "macro_store", None)
        load = getattr(store, "load_history", None)
        if not callable(load):
            return False
        history = load(days=2) or []
        if not isinstance(history, list) or not history:
            return False
        latest = history[-1]
        if not isinstance(latest, dict):
            return False
        latest_regime = str(latest.get("regime") or "").strip()
        latest_date = str(latest.get("date") or "").strip()[:10]
        stored_date = str(state.get("date") or state.get("as_of") or "").strip()[:10]
        if (
            latest_date
            and stored_date
            and latest_date > stored_date
            and latest_regime
            and latest_regime != stored_regime
        ):
            return True
        return False

    def _live_macro_series_prints(self) -> dict | None:
        """Fetch current FRED prints. None on a failed or missing provider.

        Restores ``last_coverage`` / ``_run_freshness`` afterwards —
        ``get_macro_summary`` mutates both, and this peek must not overwrite
        the morning side-channel a later reader still needs.
        """
        from src.data.macro_store import series_prints_from_summary

        provider = getattr(self, "macro", None)
        getter = getattr(provider, "get_macro_summary", None)
        if not callable(getter):
            return None
        had_coverage = hasattr(provider, "last_coverage")
        had_freshness = hasattr(provider, "_run_freshness")
        previous_coverage = getattr(provider, "last_coverage", None)
        previous_freshness = getattr(provider, "_run_freshness", None)
        summary = None
        freshness = None
        try:
            try:
                summary = getter()
                freshness = getattr(provider, "_run_freshness", None)
            except Exception:  # noqa: BLE001 — failed fetch ≠ print change
                record_swallowed_here("research_continuity.change_detectors._live_macro_series_prints", log=logger)
                return None
            if not isinstance(summary, dict) or not summary:
                return None
            return series_prints_from_summary(summary, freshness=freshness)
        finally:
            if had_coverage:
                provider.last_coverage = previous_coverage
            if had_freshness:
                provider._run_freshness = previous_freshness

    def _macro_series_prints_changed(self, state: dict) -> bool:
        """True when live FRED prints differ from the stored fingerprint.

        A snapshot that never recorded prints cannot claim a change —
        that would expire every pre-fingerprint last_state and invent
        churn. Failed live fetch is not a change.
        """
        from src.data.macro_store import series_prints_changed

        stored = state.get("series_prints")
        if not isinstance(stored, dict) or not (stored.get("values") or stored.get("observations")):
            return False
        live = self._live_macro_series_prints()
        if not live:
            return False
        return series_prints_changed(stored, live)

    def _watched_research_symbols(self, ctx=None, report=None) -> list[str]:
        """Tickers this desk is actually watching. Empty if none are known.

        Form 4 / news peeks must not scan the whole listed market or treat
        an unrelated wire as a change to remembered research.
        """
        out: set[str] = set()
        trading = getattr(getattr(self, "config", None), "trading", None)
        for raw in getattr(trading, "universe", None) or []:
            text = str(raw or "").strip().upper()
            if text:
                out.add(text)
        stock_news = getattr(report, "stock_news", None) if report is not None else None
        if isinstance(report, dict):
            stock_news = report.get("stock_news")
        if isinstance(stock_news, dict):
            for raw in stock_news:
                text = str(raw or "").strip().upper()
                if text:
                    out.add(text)
        if ctx is not None:
            for finding in getattr(ctx, "smart_money_findings", None) or []:
                symbol = getattr(finding, "symbol", None)
                if symbol is None and isinstance(finding, dict):
                    symbol = finding.get("symbol")
                text = str(symbol or "").strip().upper()
                if text:
                    out.add(text)
        return sorted(out)

    def _peek_news_headlines(self, report) -> list[str]:
        """Live RSS titles for mechanical wire-expiry. Failed fetch → [].

        General wires only — do not pass the universe as a per-symbol
        fetch. That cap preserves caller order and morning's order is
        positions-first; an alphabetical universe peek would query a
        different 15 names and invent new titles.
        """
        from src.evidence_kind import headline_mentions_symbols

        self.last_news_peek_items = []
        provider = getattr(self, "news_provider", None)
        fetch = getattr(provider, "fetch_news", None)
        if not callable(fetch):
            return []
        watched = self._watched_research_symbols(report=report)
        if not watched:
            return []
        try:
            try:
                items, _coverage = fetch(symbols=None)
            except TypeError:
                items, _coverage = fetch()
        except Exception:  # noqa: BLE001 — failed fetch ≠ supersede
            record_swallowed_here("research_continuity.change_detectors._peek_news_headlines", log=logger)
            return []
        # Keep what we just paid for. The expiry compare only needs titles,
        # but discarding the wire body meant the tick proved its remembered
        # news was superseded and then had nothing to re-ask with.
        self.last_news_peek_items = list(items or [])
        titles: list[str] = []
        for item in items or []:
            title = getattr(item, "title", None)
            summary = getattr(item, "summary", None)
            if isinstance(item, dict):
                if title is None:
                    title = item.get("title") or item.get("headline")
                if summary is None:
                    summary = item.get("summary")
            text = str(title or "").strip()
            if not text:
                continue
            search = f"{text} {summary or ''}"
            if not headline_mentions_symbols(search, watched):
                continue
            titles.append(text)
        return titles

    def _peeked_news_wire_text(self) -> str:
        """The wire text the expiry peek already fetched, formatted for the
        news analyst. Empty when the peek returned nothing.

        No second fetch and no new window: this is the same fetch that
        proved the remembered report superseded. A failed or empty peek
        stays empty so the seat expires and is lost — a fetch that got
        nothing must never be dressed up as fresh news.
        """
        items = list(self.last_news_peek_items or [])
        if not items:
            return ""
        provider = getattr(self, "news_provider", None)
        fmt = getattr(provider, "format_for_prompt", None)
        if not callable(fmt):
            return ""
        try:
            text = fmt(items, max_items=self.config.news.max_prompt_items)
        except Exception as e:  # noqa: BLE001
            record_swallowed("research_continuity.change_detectors._peeked_news_wire_text", e, log=logger)
            return ""
        return text if isinstance(text, str) and text.strip() else ""

    def _news_has_newer_material_wire(self, report) -> bool:
        """Best-effort mechanical headline compare. Failed fetch ≠ supersede.

        Only a new title that names a watched ticker is a wire change
        (the peek already drops unnamed general-wire titles). A sliding
        24h RSS window always grows unrelated headlines; treating those
        as expiry would freeze the midday tick, which cannot re-pay the
        news seat.
        """
        from src.evidence_kind import covered_news_headlines, newer_material_wire

        covered = set(covered_news_headlines(report))
        load_raw = getattr(getattr(self, "news_store", None), "load_raw_headlines", None)
        if callable(load_raw):
            try:
                for item in load_raw() or []:
                    if not isinstance(item, dict):
                        continue
                    text = str(item.get("title") or item.get("headline") or "").strip()
                    if text:
                        covered.add(text)
            except Exception:  # noqa: BLE001
                pass
        try:
            fetched = self._peek_news_headlines(report) or []
        except Exception:  # noqa: BLE001
            record_swallowed_here("research_continuity.change_detectors._news_has_newer_material_wire", log=logger)
            return False
        return newer_material_wire(frozenset(covered), fetched)
