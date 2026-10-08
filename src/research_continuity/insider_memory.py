"""src.research_continuity.insider_memory -- is the remembered Form 4 answer still current?

Bodies moved verbatim from src/pipeline_research_continuity.py (originally
src/pipeline.py), item 210 step 9, second research-continuity instalment.
The insider seat's carry-forward reader and the reads it is built
from: the Form 4 freshness probe, the known accessions, the remembered
findings in specialist_evidence and their session date. Read-only.

Every collaborator is an explicit keyword-only constructor argument; nothing
here imports src.pipeline. Storage and the evidence journal (anything with
`EventJournal.persist_evidence`, src/ports/event_journal.py) are handed in,
never reached for through a host.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from src.research_continuity.carry_forward import CarryForward
from src.sentinel.counted import record_swallowed_here

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class InsiderMemory:
    """Remembered Form 4 findings and the freshness probe that decides whether to reuse them."""

    def __init__(
        self, *,
        db,
        smart_money_provider,
        watched_research_symbols: Callable | None = None,
        form4_freshness=None,
        form4_known_accessions=None,
        findings_from_specialist_evidence=None,
        load_remembered_insider_findings=None,
        specialist_insider_as_of=None,
        insider_same_session=None,
        carry_forward_insider=None,
    ) -> None:
        self.db = db
        self.smart_money_provider = smart_money_provider
        self._watched_research_symbols = watched_research_symbols
        # A host that replaced one of these (a test double, an instance-level
        # override) is honoured; the host's own thin shim is never passed back
        # in, so the part keeps its own body (see cost_circuit/parts/shim_guard).
        if form4_freshness is not None:
            self._form4_freshness = form4_freshness
        if form4_known_accessions is not None:
            self._form4_known_accessions = form4_known_accessions
        if findings_from_specialist_evidence is not None:
            self._findings_from_specialist_evidence = findings_from_specialist_evidence
        if load_remembered_insider_findings is not None:
            self._load_remembered_insider_findings = load_remembered_insider_findings
        if specialist_insider_as_of is not None:
            self._specialist_insider_as_of = specialist_insider_as_of
        if insider_same_session is not None:
            self._insider_same_session = insider_same_session
        if carry_forward_insider is not None:
            self._carry_forward_insider = carry_forward_insider

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
            record_swallowed_here(
                "research_continuity.insider_memory._findings_from_specialist_evidence", log=logger
            )
            return []
        if not row:
            return []
        try:
            run_id = row["run_id"] if hasattr(row, "keys") else row[0]
        except Exception:  # noqa: BLE001
            record_swallowed_here(
                "research_continuity.insider_memory._findings_from_specialist_evidence", log=logger
            )
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
            record_swallowed_here(
                "research_continuity.insider_memory._findings_from_specialist_evidence", log=logger
            )
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
            record_swallowed_here("research_continuity.insider_memory._specialist_insider_as_of", log=logger)
            return ""
        if not row:
            return ""
        try:
            raw = row["timestamp"] if hasattr(row, "keys") else row[0]
        except Exception:  # noqa: BLE001
            record_swallowed_here("research_continuity.insider_memory._specialist_insider_as_of", log=logger)
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

    def _carry_forward_insider(self, ctx) -> CarryForward:
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
