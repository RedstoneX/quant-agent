"""Research continuity: may yesterday's paid research be reused, and can a lost seat be healed?

Step 7 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210), clusters S + T + U + V.
Moved VERBATIM out of `src/pipeline.py` as a mixin, so `TradingPipeline` keeps
every one of these as its own attribute and every test that calls, patches or
reads them through the class is untouched.

One question is answered here: whether the desk may stand on evidence it has
already paid for. The change detectors (macro regime, FRED prints, a newer
material news wire) decide whether a stored answer is still true; the
carry-forward readers hand the morning's macro, news, earnings and insider
payloads to an intraday tick with an honest `data_status` rather than a
silent reuse; the Form-4 backlog alert and the congressional refresh say when
an evidence lane is behind; and the seat healing path spends at most one paid
retry to recover a seat that was lost, recording what it did either way.

`CarryForward` travels with it: the dataclass is read and constructed only by
the moved bodies, and this module may not import `src.pipeline`. It is
re-exported from `src.pipeline`, so `from src.pipeline import CarryForward`
keeps working.

Nothing here may import `src.pipeline`: this module is one of its bases.
"""

import json as _json
import logging
from dataclasses import dataclass

from src.agents.base import agent_log_kwargs
from src.cost_circuit import PaidAnalysisSuspended
from src.models import NewsIntelligenceReport
from src.pipeline_context import RunContext
from src.pipeline_stages import _persist_evidence
from src.trading_calendar import et_today

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


@dataclass(frozen=True)
class CarryForward:
    """Morning evidence reused on an intraday tick, with an honest status.

    `payload` is the stored object when a reusable answer exists; otherwise
    None. `status` is the data_status word the caller must write — never
    inferred from payload truthiness. `same_session` is True only when the
    stored answer is from today's session and carries a trustworthy date —
    an undated snapshot is not same-session. Holding-discipline uses that
    to refuse treating a cross-day remembered regime as proof about today.
    """

    payload: object | None
    status: str
    same_session: bool = True

    def __bool__(self) -> bool:
        """True only when a payload is present.

        Status must still be read from `.status` — truthiness is only a
        safety net so a leftover `if carried` cannot treat an empty or
        failed lookup as a successful carry.
        """
        return self.payload is not None


class ResearchContinuityMixin:
    """Change detection, carry-forward and seat healing for TradingPipeline."""

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
        if latest_date and stored_date and latest_date > stored_date and latest_regime and latest_regime != stored_regime:
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
        if not isinstance(stored, dict) or not (
            stored.get("values") or stored.get("observations")
        ):
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
        self._last_news_peek_items = []
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
            return []
        # Keep what we just paid for. The expiry compare only needs titles,
        # but discarding the wire body meant the tick proved its remembered
        # news was superseded and then had nothing to re-ask with.
        self._last_news_peek_items = list(items or [])
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
        items = list(getattr(self, "_last_news_peek_items", None) or [])
        if not items:
            return ""
        provider = getattr(self, "news_provider", None)
        fmt = getattr(provider, "format_for_prompt", None)
        if not callable(fmt):
            return ""
        try:
            text = fmt(items, max_items=self.config.news.max_prompt_items)
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: wire text for news heal failed: %s", e)
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
            return False
        return newer_material_wire(frozenset(covered), fetched)

    def _record_form4_backlog(self, run_id: str, refresh: dict) -> None:
        """Persist the pre-market Form 4 backlog and coverage. Never raises."""
        if not isinstance(refresh, dict):
            return
        import json as _json
        keys = (
            "status", "pending_filings", "watched_pending_filings",
            "discovery_cap_reached", "watched_read_through",
            "watched_names", "watched_names_read_through",
            "watched_names_unread", "watched_unchecked_names",
            "watched_drain_ran", "watched_drain_read",
            "watched_drain_deadline_hit", "edgar_coverage", "error",
        )
        _persist_evidence(
            getattr(self, "db", None), run_id=run_id,
            agent_name="smart_money_refresh", kind="form4_backlog", scope="run",
            evidence_json=_json.dumps(
                {k: refresh.get(k) for k in keys}, sort_keys=True, default=str,
            ),
        )

    def _record_congressional_refresh(self, run_id: str, refresh: dict) -> None:
        """Persist the congressional refresh's counts. Never raises.

        Same record as the Form 4 backlog above: per source fetched, already
        seen, processed, new, dropped by reason, watermark before/after,
        duration, and how old the newest disclosure and each source's copy
        are. Nothing is written when the congressional feed is switched off.
        """
        summary = refresh.get("congressional") if isinstance(refresh, dict) else None
        if not isinstance(summary, dict):
            return
        import json as _json
        _persist_evidence(
            getattr(self, "db", None), run_id=run_id,
            agent_name="smart_money_refresh", kind="congressional_refresh",
            scope="run",
            evidence_json=_json.dumps(summary, sort_keys=True, default=str),
        )

    def _alert_form4_backlog_before_open(self, refresh: dict) -> None:
        """Say BEFORE the open that today's insider evidence is incomplete.

        `refresh` has always computed the backlog numbers and the pipeline
        had only ever logged them. A returned value nobody catches is a
        check that does not exist — on 2026-09-18 the cap bound at the
        pre-market refresh, and the first anyone knew of it was six lost
        decision windows later.

        The condition is coverage: every watched name read through today,
        nothing unread, nothing unchecked. Anything else means the insider
        seat cannot be current on every tick today. Since PR #535 that seat
        is advisory — it no longer stops the desk — so the alert says the
        desk decides WITHOUT complete insider evidence, not that it refuses.
        """
        if not isinstance(refresh, dict):
            return
        from src.util.time import et_today
        read_through = str(refresh.get("watched_read_through") or "").strip()[:10]
        today = et_today().isoformat()
        watched_pending = int(refresh.get("watched_pending_filings") or 0)
        unchecked = list(refresh.get("watched_unchecked_names") or [])
        cap_reached = bool(refresh.get("discovery_cap_reached"))
        names = int(refresh.get("watched_names") or 0)
        names_read = int(refresh.get("watched_names_read_through") or 0)
        # Board item 126. EDGAR publishes its own count of the Form 4s filed
        # on a day. When the morning read could not obtain that count, it
        # cannot tell "nobody filed anything" from "our read of the filings
        # service came back broken" — and the second case used to reach this
        # desk looking exactly like the first.
        #
        # Fail CLOSED on a missing record, matching the morning seat in
        # src/pipeline_stages.py: a refresh that ran a Form 4 pass and
        # recorded no coverage answered the question not at all, which is
        # not the same as answering it well. A refresh that carries the
        # Form 4 drain keys is held to this, and so is one that reports an
        # error — a sub-provider that raised outright produces neither the
        # drain keys nor a coverage record, and that is the LOUDEST case,
        # not an exemption. A wrapper with neither is not asked to answer
        # for coverage it never had; it is NOT thereby let off the alert,
        # because an empty `watched_read_through` still trips the ordinary
        # did-not-finish clause below.
        edgar = refresh.get("edgar_coverage")
        form4_answered = "watched_drain_ran" in refresh or bool(refresh.get("error"))
        edgar_unverified = (
            form4_answered
            and not (isinstance(edgar, dict) and edgar.get("verified"))
        )
        record = edgar if isinstance(edgar, dict) else {}
        # Reported whether or not anything is wrong. The market-wide scan is
        # bounded by its own deadline and in production reaches a minority
        # of the lookback window, so "how much of the window did we check"
        # is a fact the owner needs on an ORDINARY morning — rendering it
        # only on the failure branch would have shown him the honest number
        # exactly when it was least representative.
        #
        # Gated on whether coverage was RECORDED, not merely present. A
        # blank record is all zeros, and "read 0 of 0 filings across 0 of 0
        # days" reads to a human as nothing to worry about when it means
        # the opposite — the same trap `ratio` already avoids by answering
        # None to nought-of-nought rather than 1.0.
        if record.get("known"):
            coverage_line = (
                "Insider-filing coverage this morning: read "
                f"{record.get('enumerated', 0)} of {record.get('edgar_total', 0)} "
                "filings the service reported, across "
                f"{record.get('days_queried', 0)} of "
                f"{record.get('days_in_window', 0)} days looked at."
            )
        elif record:
            coverage_line = (
                "Insider-filing coverage this morning: NOT KNOWN — the "
                "morning read did not record how much of the filing service "
                "it covered."
            )
        else:
            coverage_line = ""
        if coverage_line:
            logger.info("PRE-OPEN: %s", coverage_line)
        if (
            read_through == today and not watched_pending and not unchecked
            and not edgar_unverified
        ):
            return
        why: list[str] = []
        if edgar_unverified:
            reasons = ", ".join(str(r) for r in (record.get("reasons") or [])) \
                or "no coverage was recorded at all"
            why.append(
                "the filing service did not account for how many filings "
                f"existed, so a quiet day and a failed read cannot be told "
                f"apart ({reasons})",
            )
        if names:
            why.append(
                f"{names_read} of our {names} companies have every insider "
                "filing read",
            )
        if watched_pending:
            why.append(
                f"{watched_pending} company filing(s) on names we hold are "
                "still unread",
            )
        if unchecked:
            why.append(
                f"{len(unchecked)} of our own companies could not be checked "
                "at all",
            )
        if bool(refresh.get("watched_drain_deadline_hit")):
            why.append(
                "the morning read of our own companies ran out of time; it "
                "resumes where it stopped tomorrow morning",
            )
        if cap_reached:
            why.append(
                "the morning read stopped at its own limit before finishing",
            )
        if not why:
            why.append(
                "the morning read did not confirm it finished"
                + (f" (last confirmed {read_through})" if read_through else ""),
            )
        text = (
            "Insider-filing check did not finish this morning: "
            + "; ".join(why)
            + ". Until it does, the desk still makes its trading decisions "
            "but without complete insider evidence, and each decision "
            "records that. Existing positions and their stops are unaffected."
            # Carried whatever the reason for the alert, not only when
            # coverage itself is the complaint — the counts are the context
            # for every other line above them.
            + (f" {coverage_line}" if coverage_line else "")
        )
        logger.error("PRE-OPEN: %s", text)
        try:
            from src.notifier import send_owner_alert
            send_owner_alert(text)
        except Exception as exc:  # noqa: BLE001
            logger.error("Form 4 backlog pre-open alert failed to send: %s", exc)

    def _form4_freshness(self, ctx=None, symbols=None) -> dict:
        """"Has anything been FILED on a watched name since our last read?"

        The ONLY freshness question the decision tick asks. It is answered
        from each watched issuer's own SEC filing history — O(watched names)
        plain GETs — not from a full-text crawl of the whole filing stream.
        The crawl answers a different question ("is there a filing I have
        not read?"), belongs to the pre-market producing step, and ran
        inside every decision tick until 2026-09-18, where it cost six
        consecutive decision windows.

        Returns the provider verdict unchanged. A provider that cannot
        answer returns ``ok=False``, and the caller MUST treat that as
        unknown freshness rather than as "nothing new".
        """
        provider = getattr(self, "smart_money_provider", None)
        probe = getattr(provider, "form4_freshness", None)
        if not callable(probe):
            # No probe at all is not a silent pass. The seat's freshness is
            # unknown, and unknown loses the seat at the evidence gate.
            return {
                "ok": False, "new_filings": [], "read_through": "",
                "checked": 0, "unchecked": [],
                "reason": "provider cannot answer Form 4 freshness",
            }
        if symbols is None:
            symbols = self._watched_research_symbols(ctx=ctx)
        try:
            try:
                result = probe(symbols)
            except TypeError:
                result = probe()
        except Exception as exc:  # noqa: BLE001
            # Logged here, not only returned: the caller logs only when it
            # holds findings, so an empty-seat tick used to lose this.
            logger.warning(
                "Form 4 freshness probe raised %s: %s", type(exc).__name__, exc,
            )
            return {
                "ok": False, "new_filings": [], "read_through": "",
                "checked": 0, "unchecked": [],
                "reason": f"freshness probe raised {type(exc).__name__}: {exc}",
            }
        return result if isinstance(result, dict) else {
            "ok": False, "new_filings": [], "read_through": "",
            "checked": 0, "unchecked": [],
            "reason": "freshness probe returned no verdict",
        }

    def _form4_known_accessions(self) -> set[str]:
        """Accessions already processed or cached. No network."""
        out: set[str] = set()
        provider = getattr(self, "smart_money_provider", None)
        providers = getattr(provider, "providers", None)
        if not isinstance(providers, (list, tuple)):
            providers = [provider] if provider is not None else []
        for item in providers:
            known = getattr(item, "known_accessions", None)
            if not callable(known):
                continue
            try:
                out.update(str(a).strip() for a in (known() or []) if str(a).strip())
            except Exception:  # noqa: BLE001
                continue
        return out

    def _findings_from_specialist_evidence(self) -> list:
        """Most recent smart-money findings from specialist_evidence. [] if none."""
        from src.models import SmartMoneyFinding
        db = getattr(self, "db", None)
        execute = getattr(db, "execute", None)
        if not callable(execute):
            return []
        try:
            row = execute(
                "SELECT run_id FROM specialist_evidence "
                "WHERE agent_name = ? AND kind IN ('finding', 'scan_summary') "
                "ORDER BY id DESC LIMIT 1",
                ("smart_money_analyst",),
            ).fetchone()
        except Exception:  # noqa: BLE001
            return []
        if not row:
            return []
        try:
            run_id = row["run_id"] if hasattr(row, "keys") else row[0]
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(run_id, str) or not run_id.strip():
            return []
        try:
            rows = execute(
                "SELECT evidence_json FROM specialist_evidence "
                "WHERE run_id = ? AND agent_name = ? AND kind = ? "
                "ORDER BY id",
                (run_id, "smart_money_analyst", "finding"),
            ).fetchall()
        except Exception:  # noqa: BLE001
            return []
        findings: list = []
        for item in rows or []:
            try:
                raw = item["evidence_json"] if hasattr(item, "keys") else item[0]
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                findings.append(SmartMoneyFinding.model_validate_json(raw))
            except Exception:  # noqa: BLE001 — skip unreadable rows
                continue
        return findings

    def _load_remembered_insider_findings(self, ctx) -> tuple[list, set[str]]:
        """Remembered Form 4 findings plus the accessions already seen.

        Findings come from this tick if already populated, else from
        specialist_evidence. Accessions are the cached/processed set so a
        non-material filing already seen cannot look 'new'.
        """
        findings: list = list(getattr(ctx, "smart_money_findings", None) or [])
        if not findings:
            findings = self._findings_from_specialist_evidence()
        accessions = set(self._form4_known_accessions())
        for finding in findings:
            observations = getattr(finding, "observations", None)
            if observations is None and isinstance(finding, dict):
                observations = finding.get("observations")
            for obs in observations or []:
                acc = getattr(obs, "accession_number", None)
                if acc is None and isinstance(obs, dict):
                    acc = obs.get("accession_number")
                text = str(acc or "").strip()
                if text:
                    accessions.add(text)
        return findings, accessions

    def _carry_forward_macro(self) -> CarryForward:
        """Remembered macro regime, keyed by kind+expiry event.

        GOOD same-session reuse stays `carried_from_morning` (PR #430).
        A GOOD prior-day regime is `remembered` until a real regime/print
        change — not `carry_forward_empty`. A blank snapshot with no
        regime is lost. A same-day `{date, regime}` trim is a regime
        snapshot for holding-discipline; it is not a full MacroAnalysis.
        PM still refuses a chain-less dict via `_macro_analysis_as_dict`.
        Holding-discipline must read `.same_session`, not payload
        truthiness, so a cross-day remember cannot falsify today's exit
        claim. An undated snapshot is not same-session — that claim
        needs a trustworthy date.
        """
        from src.evidence_kind import macro_reuse, same_session_from_date
        from src.seat_heal import coerce_macro_shape
        try:
            state = self.macro_store.load_last_state() or None
        except Exception as e:  # noqa: BLE001 — never fail a tick on carry-forward
            logger.warning("Intraday scan: macro carry-forward failed: %s", e)
            return CarryForward(None, "carry_forward_failed", same_session=False)
        if not state:
            return CarryForward(None, "carry_forward_empty", same_session=False)
        stored_date = str(state.get("date") or state.get("as_of") or "").strip()[:10]
        same_session = same_session_from_date(stored_date)
        # A regime snapshot is reusable research for holding-discipline and
        # kind-reuse. Full MacroAnalysis validation is the PM path
        # (`_macro_analysis_as_dict`) — requiring a chain here turned a
        # same-day {date, regime} read into carry_forward_failed and made
        # a provably-false exit claim look unverifiable.
        if not isinstance(state, dict) or not str(state.get("regime") or "").strip():
            return CarryForward(None, "carry_forward_failed", same_session=same_session)
        payload, _fixes = coerce_macro_shape(dict(state))
        verdict = macro_reuse(
            payload,
            same_session=same_session,
            regime_or_print_changed=self._macro_regime_or_print_changed(payload),
        )
        if not verdict.usable:
            return CarryForward(None, verdict.status, same_session=same_session)
        return CarryForward(payload, verdict.status, same_session=same_session)

    def _latest_news_read_today(self) -> dict | None:
        """Today's news report, INCLUDING an answer a paid heal bought.

        The scheduled sessions write `data/news/<ET day>/full_report.json`.
        A paid heal does not, and must not: four readers walk that file
        ACROSS days — `NewsStore.recent_state_changes` and the three
        missed-ops/thesis scans in this module — so overwriting it would
        push a heal's baseline-less `state_changes` into a multi-week
        catalyst memory and delete the morning's from it. A file written for
        a cross-day window is the wrong place to put a within-day refresh.

        So the file stays the base, and the freshest PAID read of the day is
        layered over it from `specialist_evidence`, which is already written
        for every news answer (heal and scheduled alike) and is already
        ET-day scoped. Nothing is written here.

        LIFETIME, because this is the whole question: unchanged. Both
        sources are bounded by the same ET trading day, and what expires the
        result is still `evidence_kind.news_reuse` — the next material wire,
        or the session ending. No clock, no N-minute refresh, no new number.
        What changes is only WHICH of today's paid reads the desk finds.

        Per-symbol coverage the newer read was never asked about is kept
        (`seat_heal.merge_carried_stock_news`). Returns the file alone when
        there is no newer row, when it will not parse, or when the store is
        unreachable — a sick forensic table must never cost the desk the
        news it already has on disk.
        """
        report = self.news_store.load_daily_report()
        fetch = getattr(getattr(self, "db", None), "latest_news_analysis_today", None)
        if not callable(fetch):
            return report
        try:
            raw = fetch()
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: latest news answer read failed: %s", e)
            return report
        if not raw:
            return report
        try:
            import json as _json
            from src.models import NewsIntelligenceReport
            from src.seat_heal import merge_carried_stock_news
            fresher = _json.loads(raw)
            if not isinstance(fresher, dict):
                return report
            # Must still be a real report. A row that cannot parse is not
            # allowed to demote a file that can — that would turn an
            # `expired` seat into a LOST one, which is strictly worse.
            NewsIntelligenceReport(**fresher)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Intraday scan: stored news answer would not parse; using "
                "the day's report file: %s", e,
            )
            return report
        if not report:
            return fresher
        return merge_carried_stock_news(fresher, report)

    def _carry_forward_news(self, ctx: RunContext | None = None) -> CarryForward:
        """This session's news intelligence, re-validated from its stored dump.

        Same-session GOOD reuse is #430. A newer material wire expires it.
        Parse failure is lost, never reused as research.

        When the wire DID move, the peek that proved it holds current wire
        text. Hand that to the heal path (`ctx.heal_news_text`) so the
        expired seat is re-asked with the data this tick already paid for,
        instead of being lost while fresh headlines are thrown away.

        The evidence gate is untouched by this, but NOT because expired is
        lost — it stopped being lost when #535 split `CATEGORY_EXPIRED` out
        on 2026-09-18, which is what orphaned this hand-off for five days.
        The gate is untouched because the re-ask changes only whether a
        fresher answer exists, never what the gate does with the answer the
        desk already holds. See `evidence_gate.HEALABLE_CATEGORIES`.
        """
        from src.evidence_kind import news_reuse
        try:
            report = self._latest_news_read_today()
            if not report:
                return CarryForward(None, "carry_forward_empty", same_session=False)
            from src.models import NewsIntelligenceReport
            payload = NewsIntelligenceReport(**report)
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: news carry-forward failed: %s", e)
            return CarryForward(None, "carry_forward_failed", same_session=False)
        # Both sources `_latest_news_read_today` can return are bounded by
        # the SAME ET trading day — the dated report directory and the
        # ET-day evidence-row filter — so a successful load is same-session
        # either way; there is no undated news snapshot on this path.
        # Empty/failed above cannot claim it.
        wire_moved = self._news_has_newer_material_wire(payload)
        verdict = news_reuse(
            payload,
            same_session=True,
            newer_material_wire=wire_moved,
        )
        if not verdict.usable:
            if wire_moved and ctx is not None:
                wire_text = self._peeked_news_wire_text()
                if wire_text:
                    ctx.heal_news_text = wire_text
            return CarryForward(None, verdict.status, same_session=True)
        return CarryForward(payload, verdict.status, same_session=True)

    def _carry_forward_earnings(self, ctx: RunContext) -> CarryForward:
        """Remembered earnings write-ups until the next report / 8-K.

        Loads the cached analyses (no LLM). A `queued=True` placeholder is
        the expiry event — a new filing the preprocess has not written up.
        """
        from src.evidence_kind import earnings_reuse
        provider = getattr(self, "earnings_provider", None)
        load = getattr(self, "_load_earnings_analyses", None)
        if provider is None or not callable(load):
            return CarryForward([], "not_run_intraday", same_session=True)
        try:
            _, results = self._load_earnings_analyses(
                ctx.run_id, session=ctx.session, ctx=ctx,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: earnings remember failed: %s", e)
            return CarryForward(None, "carry_forward_failed", same_session=True)
        results = list(results or [])
        new_report = any(
            isinstance(item, dict) and item.get("queued")
            for item in results
        )
        analyzed = [
            item for item in results
            if isinstance(item, dict) and isinstance(item.get("analysis"), dict)
        ]
        payload = analyzed if analyzed else results
        verdict = earnings_reuse(
            payload if payload else [],
            same_session=True,
            new_report_or_8k=new_report,
        )
        # Placeholders for new filings are still handed to PM (it sizes
        # down); the status is `expired` only when we have nothing usable
        # AND a new filing. When we have cached write-ups plus a queued
        # name, keep the write-ups and label the seat partial via the
        # existing earnings classifier — do not drop remembered work.
        if analyzed and new_report:
            return CarryForward(results, "chose_not_to_refetch", same_session=True)
        if not verdict.usable and not results:
            return CarryForward(None, verdict.status, same_session=True)
        status = verdict.status
        if verdict.decision == "refetch" and not analyzed:
            status = "expired"
            return CarryForward(results, status, same_session=True)
        return CarryForward(results, status, same_session=True)

    def _specialist_insider_as_of(self) -> str:
        """Timestamp of the latest smart-money specialist_evidence row, or ''."""
        db = getattr(self, "db", None)
        execute = getattr(db, "execute", None)
        if not callable(execute):
            return ""
        try:
            row = execute(
                "SELECT timestamp FROM specialist_evidence "
                "WHERE agent_name = ? AND kind IN ('finding', 'scan_summary') "
                "ORDER BY id DESC LIMIT 1",
                ("smart_money_analyst",),
            ).fetchone()
        except Exception:  # noqa: BLE001
            return ""
        if not row:
            return ""
        try:
            raw = row["timestamp"] if hasattr(row, "keys") else row[0]
        except Exception:  # noqa: BLE001
            return ""
        return str(raw or "").strip()

    def _insider_same_session(self, findings) -> bool:
        """True only with a trustworthy date equal to today.

        Production ``SmartMoneyFinding`` has no as_of field. The producing
        step's date is the specialist_evidence timestamp. An undated
        finding cannot claim same-session; Form 4 is still remembered
        until a new accession.
        """
        from src.evidence_kind import same_session_from_date
        for finding in findings or []:
            raw = finding if isinstance(finding, dict) else None
            for key in ("as_of", "date", "session_date", "analyzed_on"):
                value = getattr(finding, key, None)
                if value is None and raw is not None:
                    value = raw.get(key)
                if same_session_from_date(value):
                    return True
        return same_session_from_date(self._specialist_insider_as_of())

    def _carry_forward_insider(self, ctx: RunContext) -> CarryForward:
        """Remembered Form 4 findings; refresh only when a NEW filing appears."""
        from src.evidence_kind import insider_reuse
        findings: list = []
        accessions: set[str] = set()
        try:
            findings, accessions = self._load_remembered_insider_findings(ctx)
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: insider remember failed: %s", e)
            return CarryForward(None, "carry_forward_failed", same_session=False)
        same_session = self._insider_same_session(findings)
        # The freshness ladder, written down deliberately because the old
        # code fell the wrong way at every rung. Previously a failed peek
        # was swallowed and became `new_form4=False`, i.e. "nothing new",
        # i.e. REUSE — so a broken network let the desk decide on research
        # it never checked was current, while a WORKING network that found
        # the desk's own unread backlog refused the decision. Backwards in
        # both directions. Now:
        #
        #   every watched name read through, nothing unread  -> reuse
        #   a read-through name has an unread filing          -> expired (real)
        #   probe failed or partial, or any watched name not
        #   yet fully read (per-issuer coverage)              -> expired
        #
        # Expiry is per tick and the probe is cheap, so an unknown costs one
        # window and the next tick re-asks. Coverage only grows: the
        # pre-market drain records each issuer as it finishes it. Reuse on an unknown would put a
        # decision on evidence nobody checked, which the evidence gate
        # exists to prevent and which no later tick can undo.
        freshness = self._form4_freshness(ctx=ctx)
        probe_ok = bool(freshness.get("ok"))
        incoming = {
            str(a).strip() for a in (freshness.get("new_filings") or [])
            if str(a).strip()
        }
        new_form4 = bool(incoming - set(accessions))
        # Fail closed whenever the probe cannot call the seat current —
        # including when the remembered answer is EMPTY. CORRECTED
        # 2026-09-19: this used to expire only a seat holding findings, on
        # the stated ground that `insider_reuse` "already classifies an
        # empty payload as lost". It does not: an empty list is BLANK, and
        # BLANK reuses as `chose_not_to_refetch` — "Form 4 filings
        # remembered; no new filing", an integrity-clean status. An empty
        # answer is still a claim ("no material insider activity on any
        # watched name"), and it is exactly as uncheckable as a full one
        # when the probe failed or some watched names were never fully
        # read. `not ok` now covers both: a failed or partial probe, and
        # partial COVERAGE (`unread_names`), whose reason says how many
        # names are not yet read. A desk with NO insider provider at all has
        # no seat to be stale about, so an empty answer there is left alone.
        has_provider = getattr(self, "smart_money_provider", None) is not None
        if not probe_ok and (findings or has_provider):
            logger.warning(
                "Intraday scan: insider seat cannot be called current, "
                "expires this tick — %s", freshness.get("reason") or "no reason",
            )
            return CarryForward(findings, "expired", same_session=same_session)
        verdict = insider_reuse(
            findings if findings else [],
            same_session=same_session,
            new_form4=new_form4,
        )
        if verdict.decision == "refetch":
            return CarryForward(findings, "expired", same_session=same_session)
        return CarryForward(findings, verdict.status, same_session=same_session)

    def _record_heal(self, ctx: RunContext, result, *, alert: bool) -> None:
        """Durable heal log. Pages only on attempted-and-failed / cap-block."""
        import json as _json
        try:
            _persist_evidence(
                self.db, run_id=ctx.run_id, agent_name="seat_heal",
                kind="seat_heal", scope="run",
                evidence_json=_json.dumps(result.to_evidence(), sort_keys=True),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("seat heal: evidence write failed: %s", e)
        if not alert:
            return
        try:
            from src.notifier import send_owner_alert
            from src.seat_heal import HEAL_CAP_BLOCKED, heal_failure_alert_text
            send_owner_alert(
                heal_failure_alert_text(
                    result, cap_blocked=result.outcome == HEAL_CAP_BLOCKED,
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("seat heal: owner alert failed: %s", e)

    def _persist_heal_call(self, ctx: RunContext, seat: str, agent_name: str,
                            analysis, call_result) -> None:
        """Record a PAID heal exactly the way an ordinary paid call is recorded.

        Two rows, both of them the EXISTING path, neither of them new:

          * `agent_logs` — the model, the tokens, the cost, the raw answer and
            the prompt that produced it. Written under the seat's ORDINARY
            agent name and marked as a heal in `input_summary`, which is the
            convention the desk's two other paid re-asks already follow (the
            exit-trigger re-ask logs `position_reviewer`, the candidate-
            accounting re-ask logs `portfolio_manager`). A separate agent name
            would hide the spend from every per-seat query that exists today,
            which is a different corruption, not less of one. News keeps its
            `_{session}` suffix because that IS its ordinary name.
          * `specialist_evidence(kind="analysis")` — the model's answer as
            structured evidence, the same row `RiskStage` writes for an
            ordinary news or macro read.

        Never raises: a forensic-write failure must not undo a heal that
        succeeded, the same rule `_persist_evidence` and the two re-ask log
        writes above already follow.
        """
        from src.pipeline_stages import _persist_evidence
        session = getattr(ctx, "session", None) or "intra_check"
        # `news_analyst_{session}` is the ordinary name for the news seat
        # (`_run_news_analysis`); macro logs flat. Match each, don't invent.
        log_name = f"{agent_name}_{session}" if seat == "news" else agent_name
        if call_result is None:
            # An analyst that returned an answer but no call record. Nothing
            # to bill and nothing to quote — say so rather than writing a row
            # of zeroes that would read as a free call.
            logger.warning(
                "seat heal: %s returned no call result; cost and raw answer "
                "for this paid retry cannot be recorded", seat,
            )
        else:
            try:
                self.db.insert_agent_log(
                    agent_name=log_name, run_id=ctx.run_id,
                    input_summary=f"seat heal re-ask | {seat} | session={session}",
                    input_message=getattr(call_result, "user_message", "") or "",
                    output_summary=f"seat heal refreshed {seat}",
                    full_response=getattr(call_result, "raw_text", "") or "",
                    model=getattr(call_result, "model", "") or "",
                    tokens_used=getattr(call_result, "tokens_used", 0) or 0,
                    input_tokens=getattr(call_result, "input_tokens", None),
                    output_tokens=getattr(call_result, "output_tokens", None),
                    cost_usd=getattr(call_result, "cost_usd", None),
                    **agent_log_kwargs(call_result),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("seat heal: paid-call log write failed: %s", e)
        try:
            dump = getattr(analysis, "model_dump_json", None)
            if callable(dump):
                evidence_json = dump()
            else:
                import json as _json
                evidence_json = _json.dumps(analysis, sort_keys=True, default=str)
        except Exception as e:  # noqa: BLE001
            logger.warning("seat heal: could not serialise %s answer: %s", seat, e)
            return
        _persist_evidence(
            self.db, run_id=ctx.run_id, agent_name=agent_name,
            kind="analysis", scope="run", evidence_json=evidence_json,
        )

    def _persist_healed_macro_store(self, ctx: RunContext, payload: dict) -> None:
        """Write a paid macro heal's answer back to the macro store.

        THE MACRO HALF OF THE SAME DEFECT the news heal already fixed.
        `_persist_heal_call` keeps the forensic rows (`agent_logs`,
        `specialist_evidence`); this keeps the WORKING macro state. Without it
        the paid read only ever reached `ctx.macro_analysis` for this one
        tick's PM, then vanished: `_carry_forward_macro` re-reads
        `macro_store.load_last_state()` every tick, and the evening
        thesis-health read and the 7-day regime history read the same store,
        so the stale morning snapshot — not the fresher regime the desk PAID
        for — was what every later reader saw. That is the KEEP WHAT COSTS
        MONEY class of defect, on the macro seat instead of the news seat.

        Persisted the SAME way the scheduled morning read persists
        (`MorningResearchStage`): `save_last_state(payload, series_prints)`,
        with the FRED fingerprint the heal call actually saw so a later tick's
        expiry compares against real prints rather than re-expiring blind. A
        summary with no prints simply stores none — `_macro_series_prints_
        changed` treats an absent fingerprint as "no change", never as churn.

        Never raises — a store-write failure must not undo a paid heal that
        succeeded, the same rule `_persist_heal_call` and
        `_cover_healed_news_wire` already follow.
        """
        store = getattr(self, "macro_store", None)
        save = getattr(store, "save_last_state", None)
        if not callable(save) or not isinstance(payload, dict):
            return
        try:
            from src.data.macro_store import series_prints_from_summary
            prints = series_prints_from_summary(
                getattr(ctx, "macro_summary", None) or {},
                freshness=getattr(getattr(self, "macro", None), "_run_freshness", None),
            )
            save(payload, series_prints=prints)
            logger.info(
                "seat heal: persisted the paid macro read to the macro store "
                "(regime=%s)", payload.get("regime"),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("seat heal: macro store-write failed: %s", e)

    def _cover_healed_news_wire(self, ctx: RunContext) -> None:
        """Record the wire a paid news heal just read, so it stops expiring.

        THE SECOND HALF OF THE SAME DEFECT. `_persist_heal_call` keeps the
        ANSWER; this keeps the QUESTION. Without it the answer alone changes
        nothing, because `_news_has_newer_material_wire` compares live RSS
        titles against `covered_news_headlines(report)` — the analyst's own
        REWRITTEN headlines — union today's `raw_headlines.json`. Those two
        strings are not the same ID (`NewsStore.load_raw_headlines` says so
        outright), so a healed report is compared against titles it never
        claimed to contain, the same wire reads as newly moved on the next
        tick, and the seat expires again 30 minutes after the desk bought it.

        Only headlines the model was ACTUALLY SHOWN are recorded — measured
        off the prompt text, not the fetch (`seat_heal.wire_titles_shown_to_
        model`). A title the peek fetched but the prompt truncated away is
        left uncovered on purpose: it must still be able to expire the seat.
        That is the difference between recording research and buying silence.

        Appends, never replaces: overwriting would drop the morning's
        per-symbol titles and re-arm the very compare this is quieting.
        Never raises — a coverage write must not undo a paid heal.
        """
        from src.seat_heal import wire_titles_shown_to_model
        try:
            items = list(getattr(self, "_last_news_peek_items", None) or [])
            titles: list[str] = []
            for item in items:
                title = getattr(item, "title", None)
                if title is None and isinstance(item, dict):
                    title = item.get("title") or item.get("headline")
                text = str(title or "").strip()
                if text:
                    titles.append(text)
            shown = wire_titles_shown_to_model(
                titles, getattr(ctx, "heal_news_text", "") or "",
            )
            if not shown:
                return
            append = getattr(
                getattr(self, "news_store", None), "append_raw_headlines", None,
            )
            if not callable(append):
                return
            added = append([{"title": t, "source": "seat_heal", "summary": ""}
                            for t in shown])
            logger.info(
                "seat heal: recorded %d of %d peeked wire titles as read by "
                "the paid news re-ask", added, len(titles),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("seat heal: wire-coverage write failed: %s", e)

    def _try_one_paid_research_retry(self, ctx: RunContext, seat: str) -> bool:
        """One paid retry for a LOST or EXPIRED seat, once per ET day.

        False if we cannot honestly retry. An EXPIRED seat is a REFRESH, not
        a recovery: the desk holds the earlier answer and will decide on it
        either way, so every owner-facing sentence out of here must say that
        rather than claim the seat was lost.

        Intra often has no FRED/news stack. A retry without inputs would
        invent the seat — refuse that, and do not burn the retry slot.
        Cost-cap blocks alert the owner. Success is a durable log, not a page.
        """
        from src.cost_circuit import PaidAnalysisSuspended
        from src.seat_heal import (
            HealResult, HEAL_CAP_BLOCKED, HEAL_DAY_CAP, HEAL_FAILED,
            HEAL_PAID_RETRY, can_paid_retry, record_paid_retry,
        )
        from src import evidence_gate as _gate
        # An EXPIRED seat is being REFRESHED, not recovered: the desk holds
        # the earlier answer and will decide on it whatever happens here. Any
        # owner page from this function must say so, because the default
        # sentence ("the desk will not decide on this seat as if it had
        # answered") is true of a lost seat and false of this one — the
        # owner-facing-lie class of defect item 133 closed.
        _incoming = (getattr(ctx, "data_status", None) or {}).get(seat)
        _expired_seat = (
            _gate.STATUS_CATEGORY.get(_incoming) == _gate.CATEGORY_EXPIRED
        )
        _consequence = (
            "The desk still holds this seat's earlier answer and will decide "
            "on it, labelled as carried rather than read this tick. No trade "
            "was withheld for this."
        ) if _expired_seat else ""
        retries = dict(getattr(ctx, "heal_paid_retries", None) or {})
        if not can_paid_retry(retries, seat):
            return False
        require = getattr(self, "_require_paid_analysis", None)
        agent_name = {
            "macro": "macro_analyst",
            "news": "news_analyst",
            "tech": "tech_analyst",
        }.get(seat)
        if agent_name is None or not callable(require):
            return False
        agent = getattr(self, agent_name, None)
        analyze = (
            getattr(agent, "analyze", None) if seat != "tech"
            else getattr(agent, "analyze_batch", None)
        )
        if not callable(analyze):
            return False
        # The analyst already spent the one paid retry on its own parse.
        if getattr(agent, "_heal_retry_used", False) is True:
            return False
        # No inputs → would invent the seat. Do not consume the retry.
        if seat == "macro" and not (ctx.macro_summary or {}):
            return False
        if seat == "news":
            # Fresh wire text is required. Morning parse-site retry lives
            # on the analyst; intra has no honest news_text unless a hook
            # supplied one.
            news_text = getattr(ctx, "heal_news_text", None)
            if not (isinstance(news_text, str) and news_text.strip()):
                return False
        if seat == "tech":
            return False
        # Cross-tick cap, checked HERE — after the honest-inputs checks
        # above, never before them. A tick that has no wire text would have
        # refused anyway, and a day-cap row on that tick would record the cap
        # as the binding constraint when it was not. That row's whole purpose
        # is to be the evidence that later settles whether one refresh a day
        # is the right number, so it must only be written when the cap is
        # what actually stopped the spend.
        #
        # `retries` above is per-RunContext and a RunContext is one tick;
        # intra_check runs every 30 minutes and an expired seat is still
        # expired on the next tick, so without this the "one paid retry" is
        # one per tick. It was: production recorded EIGHT paid news heals on
        # 2026-09-18. See Database.count_paid_seat_heals_today.
        db = getattr(self, "db", None)
        counter = getattr(db, "count_paid_seat_heals_today", None)
        if callable(counter):
            try:
                spent_today = counter(seat)
            except Exception as e:  # noqa: BLE001
                logger.warning("seat heal: day-cap read failed for %s: %s", seat, e)
                spent_today = None
            if spent_today is None:
                # Could not find out, which is NOT the same as nothing spent.
                # Allowed through on purpose: the cost circuit below is the
                # fail-closed authority for spend and still runs, so a sick
                # forensic store cannot silently stop the desk buying fresher
                # news. Logged at WARNING so the degradation is visible
                # rather than assumed.
                logger.warning(
                    "seat heal: could not read %s's day allowance; allowing "
                    "the retry and leaving the spend to the cost circuit", seat,
                )
            elif not can_paid_retry({seat: int(spent_today)}, seat):
                logger.info(
                    "seat heal: %s already had its one paid retry today "
                    "(%d spent); not re-asking", seat, spent_today,
                )
                # Durable, not just a log line. "The desk declined to pay for
                # fresher research on this tick" is a decision about money,
                # and it is the only record that could ever show whether one
                # refresh a day is the right number. Not an owner page: the
                # cap doing its job is not an incident.
                self._record_heal(
                    ctx,
                    HealResult(
                        seat=seat, outcome=HEAL_DAY_CAP,
                        reason=(
                            f"seat already had its one paid heal this ET day "
                            f"({spent_today} recorded); not re-asking"
                        ),
                        paid_retry=False,
                        details={
                            "spent_today": int(spent_today),
                            "was_expired": _expired_seat,
                        },
                        owner_consequence=_consequence,
                    ),
                    alert=False,
                )
                return False
        try:
            require(agent_name)
        except PaidAnalysisSuspended as exc:
            blocked = HealResult(
                seat=seat, outcome=HEAL_CAP_BLOCKED,
                reason=f"spend cap blocked the one paid retry: {exc}",
                paid_retry=False, owner_consequence=_consequence,
                details={"was_expired": _expired_seat},
            )
            self._record_heal(ctx, blocked, alert=True)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("seat heal: cost-circuit preflight failed for %s: %s", seat, exc)
            return False
        ctx.heal_paid_retries = record_paid_retry(retries, seat)
        try:
            if seat == "macro":
                analysis, call_result = analyze(ctx.macro_summary)
            else:
                # Pass this run's session. The analyst's session guidance
                # defaults to MORNING ("treat today as a fresh book... this
                # report sets the tone for the day's trading"), which is
                # false on a 14:00 intra_check — the same mislabelling audit
                # round 2 #24 already fixed for the close session. The other
                # arguments stay at their defaults: a heal re-ask genuinely
                # has no universe or prior-session baseline to offer, and
                # inventing one would be worse than admitting it.
                analysis, call_result = analyze(
                    getattr(ctx, "heal_news_text", ""),
                    session=getattr(ctx, "session", None) or "intra_check",
                )
        except Exception as exc:  # noqa: BLE001
            failed = HealResult(
                seat=seat, outcome=HEAL_FAILED,
                reason=f"paid heal retry raised: {exc}",
                paid_retry=True, owner_consequence=_consequence,
                details={"was_expired": _expired_seat},
            )
            self._record_heal(ctx, failed, alert=True)
            return False
        if analysis is None:
            failed = HealResult(
                seat=seat, outcome=HEAL_FAILED,
                reason="paid heal retry returned no usable output",
                paid_retry=True, owner_consequence=_consequence,
                details={"was_expired": _expired_seat},
            )
            self._record_heal(ctx, failed, alert=True)
            return False
        payload = (
            analysis.model_dump() if hasattr(analysis, "model_dump") else analysis
        )
        if seat == "macro":
            ctx.macro_analysis = payload
        elif seat == "news":
            ctx.news_intel = analysis
        # KEEP WHAT COSTS MONEY. Until 2026-09-23 this function spent real
        # dollars on a research call and then kept neither the answer nor the
        # price: `call_result` was discarded as `_raw`, no `agent_logs` row
        # was written, and `HealResult.to_evidence()` omits `payload`. All 8
        # paid heals in production (2026-09-18, news seat) left the desk with
        # a row saying "paid_retry / usable" and nothing else — no model, no
        # tokens, no cost, not one word the model actually said. The owner's
        # own per-session cost line sums `agent_logs.cost_usd` by `run_id`
        # (`src/notifier.py`), so those eight calls read as free.
        self._persist_heal_call(ctx, seat, agent_name, analysis, call_result)
        if seat == "news":
            self._cover_healed_news_wire(ctx)
        elif seat == "macro":
            self._persist_healed_macro_store(ctx, payload)
        status = dict(ctx.data_status or {})
        status[seat] = "ok"
        ctx.data_status = status
        self._record_heal(
            ctx,
            HealResult(
                seat=seat, outcome=HEAL_PAID_RETRY,
                reason=(
                    "one paid retry refreshed a superseded seat"
                    if _expired_seat else
                    "one paid retry replaced a lost seat"
                ),
                payload=payload, paid_retry=True, usable=True,
                details={"was_expired": _expired_seat},
            ),
            alert=False,
        )
        return True

    def _heal_lost_research_seats(self, ctx: RunContext) -> None:
        """After carry-forward: log unhealed seats. Don't page empty-store gaps
        (the evidence gate already pages those). Attempt a paid retry only
        when the seat's inputs actually exist on this run.

        Selects work by `evidence_gate.HEALABLE_CATEGORIES`, which covers a
        LOST seat (no answer) and an EXPIRED one (an answer the desk knows is
        superseded). Those two are deliberately different categories to the
        evidence gate and stay different: this loop reads the set only to
        decide whether to go and LOOK again, and changes no verdict, no skip,
        no degraded count and no freshness label. Testing for CATEGORY_LOST
        here is what orphaned the expired-news refresh on 2026-09-18.
        """
        from src import evidence_gate
        from src.seat_heal import HealResult, HEAL_FAILED
        data_status = ctx.data_status or {}
        for seat, status in list(data_status.items()):
            category = evidence_gate.STATUS_CATEGORY.get(status)
            if category not in evidence_gate.HEALABLE_CATEGORIES:
                continue
            # Empty store: nothing to heal. Gate skip is the owner page.
            if status == "carry_forward_empty":
                continue
            was_expired = category == evidence_gate.CATEGORY_EXPIRED
            attempted = self._try_one_paid_research_retry(ctx, seat)
            still = (ctx.data_status or {}).get(seat)
            if evidence_gate.STATUS_CATEGORY.get(still) not in evidence_gate.HEALABLE_CATEGORIES:
                continue
            # A LOST seat is recorded whether or not a retry was possible —
            # that row is the forensic trail for an absent answer. An EXPIRED
            # seat is not absent, and most expired seats (insider, earnings)
            # have no heal wired at all by design (#535 dissolved that
            # asymmetry rather than repairing it), so recording every one of
            # them would bury the real rows in noise. Record an expired seat
            # only when the desk actually tried and failed.
            if was_expired and not attempted:
                continue
            # An expired seat that could not be refreshed is NOT a seat the
            # desk will refuse to decide on — it still holds the earlier
            # answer. Saying otherwise in the alert would repeat the
            # owner-facing lie item 133 fixed, so the consequence sentence is
            # overridden rather than defaulted.
            consequence = (
                "The desk still holds this seat's earlier answer and will "
                "decide on it, labelled as carried rather than read this "
                "tick. No trade was withheld for this."
            ) if was_expired else ""
            result = HealResult(
                seat=seat, outcome=HEAL_FAILED,
                reason=f"seat still {still} after mechanical heal",
                details={
                    "status": still,
                    "paid_retry_attempted": attempted,
                    "was_expired": was_expired,
                },
                owner_consequence=consequence,
            )
            # Page only when we actually paid a retry and it failed.
            # Kind-expiry / empty-store without inputs is the evidence
            # gate's skip, not a second owner page.
            self._record_heal(ctx, result, alert=bool(attempted))
