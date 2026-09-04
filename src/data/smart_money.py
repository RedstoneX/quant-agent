"""SEC-native, fail-soft Form 4 smart-money provider.

``refresh`` is the only network path. It discovers Form 4/4-A filings through
the SEC full-text-search index, caches complete submissions by accession, and
parses exact non-derivative P/S rows. ``fetch`` is cache-only and applies the
materiality/cluster reduction before any LLM can see the evidence.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests

from src.data.insider_signal import (
    InsiderHistory,
    InsiderPriorTrade,
    InsiderSignalThresholds,
    classify_transaction,
)
from src.data.smart_money_cluster import cluster_survivors, observation_key
from src.models import SmartMoneyObservation
from src.util.time import et_today

logger = logging.getLogger(__name__)

# Fallback if the provider is constructed without an explicit
# ``insider_history_retention_days`` (every production call site passes one
# from ``config.smart_money.insider_history_retention_days`` — see
# ``src/pipeline.py``). Five years covers the three preceding years the
# default calendar-month routine test needs, with slack for late and
# amended filings; kept in sync with ``SmartMoneyConfig``'s own default in
# ``src/config.py``.
_DEFAULT_HISTORY_RETENTION_DAYS = 5 * 366


def _history_entry_date(entry: str) -> date | None:
    try:
        return date.fromisoformat(str(entry).partition("|")[0])
    except ValueError:
        return None

EFTS_SEARCH = "https://efts.sec.gov/LATEST/search-index"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
SEC_TICKERS_EXCHANGE = "https://www.sec.gov/files/company_tickers_exchange.json"
DEFAULT_USER_AGENT = (
    "QAMC/1.0 research-intelligence "
    "https://github.com/yebof/quant-agent"
)
_LISTED_EXCHANGES = {"Nasdaq", "NYSE", "CBOE"}
_ET = ZoneInfo("America/New_York")
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_ACCEPTED_RE = re.compile(r"<ACCEPTANCE-DATETIME>(\d{14})", re.I)
_XML_RE = re.compile(
    r"<XML>\s*((?:<\?xml[^>]*>\s*)?<ownershipDocument>.*?</ownershipDocument>)\s*</XML>",
    re.I | re.S,
)
_NON_EQUITY_SUFFIXES = (".WS", ".WSA", ".WSB", ".U", ".UN", ".RT")

# One process-global limiter covers EFTS, ticker metadata and Archives calls.
# 0.125 seconds is exactly 8 requests/sec, below the SEC's 10 req/s cap.
_RATE_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0
_MIN_REQUEST_INTERVAL_S = 0.125


class SmartMoneySource(Protocol):
    def refresh(self) -> dict: ...
    def fetch(self, symbols: list[str]) -> tuple[list[SmartMoneyObservation], str | None]: ...


class _RefreshDeadline(TimeoutError):
    pass


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _text(node: ET.Element, path: str) -> str:
    found = node.find(path)
    return (found.text or "").strip() if found is not None else ""


def _number(value: str) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _bool(value: str) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"1", "true"}:
        return True
    if text in {"0", "false"}:
        return False
    return None


def _symbol(value: str) -> str:
    return str(value or "").strip().upper().replace(".", "-")


class SECForm4Provider:
    """Credentialless SEC Form 4 discovery with bounded, resumable caching."""

    def __init__(
        self,
        *,
        search_url: str = EFTS_SEARCH,
        archives_url: str = SEC_ARCHIVES,
        data_dir: str = "data/smart_money",
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_s: float | None = None,
        request_timeout_s: float = 15.0,
        requests_per_second: float = 8.0,
        lookback_days: int = 14,
        min_transaction_value_usd: float = 100_000,
        external_min_transaction_value_usd: float = 250_000,
        # Alldredge & Blank (J. Financial Research, 2019): purchases within
        # ~2 days of a colleague's trade define a "cluster" (see
        # docs/RESEARCH_FINDINGS.md:19). Was 14 days with no documented
        # rationale until the 2026-09-04 audit fix.
        cluster_window_days: int = 2,
        min_cluster_owners: int = 2,
        max_observations: int = 40,
        refresh_deadline_s: float = 180,
        max_filings_per_refresh: int = 1000,
        session: requests.Session | None = None,
        # Routine-versus-opportunistic classification thresholds
        # (`src/data/insider_signal.py`). Defaults mirror
        # `InsiderSignalThresholds`'s own defaults; production wiring
        # (`src/pipeline.py`) passes every one of these explicitly from
        # `config.smart_money.insider_*`, so a settings.yaml edit reaches
        # the classifier without a code change.
        insider_calendar_routine_years: int = 3,
        insider_min_cadence_trades: int = 3,
        insider_cadence_min_mean_gap_days: float = 20.0,
        insider_cadence_max_mean_gap_days: float = 120.0,
        insider_cadence_max_gap_dispersion: float = 0.25,
        insider_min_material_sell_fraction: float = 0.05,
        insider_history_retention_days: int = _DEFAULT_HISTORY_RETENTION_DAYS,
    ):
        self.data_dir = Path(data_dir)
        self.raw_dir = self.data_dir / "filings"
        self.observations_path = self.data_dir / "observations.json"
        self.manifest_path = self.data_dir / "manifest.json"
        # Long-horizon (insider, issuer) trade dates. ``observations.json`` is
        # pruned to the lookback window, which is far too short for the
        # three-year calendar-month routine test; this file is the only place
        # that history survives. It stores dates and direction only.
        self.history_path = self.data_dir / "insider_history.json"
        self.tickers_path = self.data_dir / "company_tickers_exchange.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.search_url = search_url.rstrip("/")
        self.archives_url = archives_url.rstrip("/")
        self.user_agent = user_agent.strip() or DEFAULT_USER_AGENT
        effective_timeout = request_timeout_s if timeout_s is None else timeout_s
        self.timeout_s = max(1.0, float(effective_timeout))
        self.request_interval_s = 1.0 / min(8.0, max(0.5, float(requests_per_second)))
        self.lookback_days = max(1, int(lookback_days))
        self.min_transaction_value_usd = max(0.0, float(min_transaction_value_usd))
        self.external_min_transaction_value_usd = max(
            self.min_transaction_value_usd,
            float(external_min_transaction_value_usd),
        )
        self.cluster_window_days = max(1, int(cluster_window_days))
        self.min_cluster_owners = max(2, int(min_cluster_owners))
        self.max_observations = max(1, int(max_observations))
        self.refresh_deadline_s = max(1.0, float(refresh_deadline_s))
        self.max_filings_per_refresh = max(1, int(max_filings_per_refresh))
        self.session = session or requests.Session()
        self._cache_lock = threading.Lock()
        self.history_retention_days = max(1, int(insider_history_retention_days))
        self._signal_thresholds = InsiderSignalThresholds(
            calendar_routine_years=max(1, int(insider_calendar_routine_years)),
            min_cadence_trades=max(2, int(insider_min_cadence_trades)),
            cadence_min_mean_gap_days=max(0.0, float(insider_cadence_min_mean_gap_days)),
            cadence_max_mean_gap_days=max(0.0, float(insider_cadence_max_mean_gap_days)),
            cadence_max_gap_dispersion=max(0.0, float(insider_cadence_max_gap_dispersion)),
            min_material_sell_fraction=max(0.0, float(insider_min_material_sell_fraction)),
        )

    def _load_json(self, path: Path, fallback):
        try:
            return json.loads(path.read_text()) if path.exists() else fallback
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("Smart-money cache unreadable at %s: %s", path, exc)
            return fallback

    def _load_history(self) -> InsiderHistory:
        """Read the accumulated (insider, issuer) trade index, fail-soft."""
        raw = self._load_json(self.history_path, {})
        trades: dict[tuple[str, str], list[InsiderPriorTrade]] = {}
        for key, entries in (raw if isinstance(raw, dict) else {}).items():
            actor_cik, _, symbol = str(key).partition("|")
            if not actor_cik or not symbol or not isinstance(entries, list):
                continue
            parsed: list[InsiderPriorTrade] = []
            for entry in entries:
                day, _, direction = str(entry).partition("|")
                try:
                    parsed.append(InsiderPriorTrade(
                        transaction_date=date.fromisoformat(day),
                        direction=direction,
                    ))
                except ValueError:
                    continue
            if parsed:
                trades[(actor_cik, symbol)] = parsed
        return InsiderHistory(trades)

    def _record_history(self, rows: list[dict]) -> None:
        """Merge this refresh's trades into the long-horizon index.

        Append-only apart from a hard age prune. Entries are ``date|direction``
        strings so the file stays a fraction of the observation cache.
        """
        raw = self._load_json(self.history_path, {})
        merged: dict[str, set[str]] = {
            str(key): {str(value) for value in values}
            for key, values in (raw if isinstance(raw, dict) else {}).items()
            if isinstance(values, list)
        }
        for row in rows:
            actor_cik = str(row.get("actor_cik") or "").strip()
            symbol = str(row.get("symbol") or "").strip().upper()
            direction = str(row.get("direction") or "").strip()
            day = str(row.get("transaction_date") or "")[:10]
            if not actor_cik or not symbol or direction not in {"buy", "sell"}:
                continue
            try:
                date.fromisoformat(day)
            except ValueError:
                continue
            merged.setdefault(f"{actor_cik}|{symbol}", set()).add(f"{day}|{direction}")
        cutoff = et_today() - timedelta(days=self.history_retention_days)
        pruned: dict[str, list[str]] = {}
        for key, values in merged.items():
            kept = sorted(
                value for value in values
                if _history_entry_date(value) is not None
                and _history_entry_date(value) >= cutoff
            )
            if kept:
                pruned[key] = kept
        _atomic_json(self.history_path, pruned)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _RefreshDeadline("refresh_deadline_exceeded")
        return remaining

    def _get(self, url: str, *, params: dict | None, deadline: float) -> requests.Response:
        global _LAST_REQUEST_AT
        last_exc: Exception | None = None
        for attempt in range(3):
            remaining = self._remaining(deadline)
            with _RATE_LOCK:
                delay = self.request_interval_s - (time.monotonic() - _LAST_REQUEST_AT)
                if delay > 0:
                    if delay >= remaining:
                        raise _RefreshDeadline("refresh_deadline_exceeded")
                    time.sleep(delay)
                _LAST_REQUEST_AT = time.monotonic()
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "application/json, application/xml, text/plain, */*",
                        "Accept-Encoding": "identity",
                    },
                    timeout=min(self.timeout_s, self._remaining(deadline)),
                )
                if response.status_code not in {429, 500, 502, 503, 504}:
                    response.raise_for_status()
                    return response
                response.raise_for_status()
            except (requests.RequestException, TimeoutError) as exc:
                last_exc = exc
                if isinstance(exc, requests.HTTPError):
                    code = exc.response.status_code if exc.response is not None else 0
                    if code not in {429, 500, 502, 503, 504}:
                        raise
                if attempt == 2:
                    break
                backoff = min(2 ** attempt, self._remaining(deadline))
                time.sleep(backoff)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"SEC GET failed without exception: {url}")

    def _listed_map(self, deadline: float) -> dict[str, dict[str, str]]:
        stale = True
        try:
            stale = (
                not self.tickers_path.exists()
                or time.time() - self.tickers_path.stat().st_mtime > 24 * 3600
            )
        except OSError:
            pass
        if stale:
            try:
                payload = self._get(
                    SEC_TICKERS_EXCHANGE, params=None, deadline=deadline,
                ).json()
                _atomic_json(self.tickers_path, payload)
            except Exception as exc:
                if not self.tickers_path.exists():
                    raise
                logger.warning("Using stale SEC ticker/exchange cache: %s", exc)
        payload = self._load_json(self.tickers_path, {})
        fields = payload.get("fields", []) if isinstance(payload, dict) else []
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        try:
            indexes = {name: fields.index(name) for name in ("cik", "ticker", "exchange")}
        except ValueError:
            return {}
        out: dict[str, dict[str, str]] = defaultdict(dict)
        for row in rows:
            try:
                exchange = row[indexes["exchange"]]
                if exchange not in _LISTED_EXCHANGES:
                    continue
                cik = str(int(row[indexes["cik"]]))
                ticker = _symbol(row[indexes["ticker"]])
                if ticker and not ticker.endswith(_NON_EQUITY_SUFFIXES):
                    out[cik][ticker] = str(exchange)
            except (IndexError, TypeError, ValueError):
                continue
        return dict(out)

    def _discover(
        self,
        listed: dict[str, dict[str, str]],
        deadline: float,
        processed: set[str] | None = None,
    ) -> list[dict]:
        processed = processed or set()
        found: dict[str, dict] = {}
        # Query one day at a time. EFTS caps deep pagination, while 14 days of
        # ownership filings can exceed that cap. Day slices also let repeated
        # refreshes skip the processed head and make progress into a backlog.
        for days_ago in range(self.lookback_days + 1):
            filing_date = et_today() - timedelta(days=days_ago)
            params = {
                "forms": "4",
                "startdt": filing_date.isoformat(),
                "enddt": filing_date.isoformat(),
                "from": 0,
                "size": 100,
            }
            while len(found) < self.max_filings_per_refresh:
                payload = self._get(self.search_url, params=params, deadline=deadline).json()
                hits_block = payload.get("hits", {}) if isinstance(payload, dict) else {}
                hits = hits_block.get("hits", []) if isinstance(hits_block, dict) else []
                if not hits:
                    break
                for hit in hits:
                    source = hit.get("_source", {}) if isinstance(hit, dict) else {}
                    accession = str(source.get("adsh") or "")
                    form = str(source.get("form") or "")
                    if (
                        accession in processed
                        or not _ACCESSION_RE.fullmatch(accession)
                        or form not in {"4", "4/A"}
                    ):
                        continue
                    ciks: list[str] = []
                    for raw_cik in source.get("ciks", []) or []:
                        try:
                            ciks.append(str(int(raw_cik)))
                        except (TypeError, ValueError):
                            continue
                    listed_ciks = [cik for cik in ciks if cik in listed]
                    if not listed_ciks:
                        continue
                    found[accession] = {
                        "accession": accession,
                        "form": form,
                        "cik": listed_ciks[-1],
                    }
                    if len(found) >= self.max_filings_per_refresh:
                        break
                total = hits_block.get("total", {})
                total_value = total.get("value", 0) if isinstance(total, dict) else int(total or 0)
                params["from"] = int(params["from"]) + len(hits)
                if len(hits) < int(params["size"]) or int(params["from"]) >= total_value:
                    break
            if len(found) >= self.max_filings_per_refresh:
                break
        return list(found.values())

    def _archive_url(self, cik: str, accession: str) -> str:
        return (
            f"{self.archives_url}/{int(cik)}/{accession.replace('-', '')}/"
            f"{accession}.txt"
        )

    def _submission(self, filing: dict, deadline: float) -> tuple[str, str]:
        accession = filing["accession"]
        path = self.raw_dir / f"{accession}.txt"
        url = self._archive_url(filing["cik"], accession)
        if path.exists():
            return path.read_text(errors="replace"), url
        response = self._get(url, params=None, deadline=deadline)
        tmp = path.with_suffix(".txt.tmp")
        tmp.write_bytes(response.content)
        os.replace(tmp, path)
        return response.content.decode("utf-8", "replace"), url

    @staticmethod
    def _roles(owner: ET.Element) -> list[str]:
        rel = owner.find("reportingOwnerRelationship")
        if rel is None:
            return []
        roles: list[str] = []
        for element, label in (
            ("isDirector", "director"),
            ("isOfficer", "officer"),
            ("isTenPercentOwner", "ten_percent_owner"),
            ("isOther", "other"),
        ):
            if _bool(_text(rel, element)):
                roles.append(label)
        for value in (_text(rel, "officerTitle"), _text(rel, "otherText")):
            if value:
                roles.append(value)
        return roles

    def _parse_submission(
        self,
        text: str,
        *,
        source_url: str,
        listed: dict[str, dict[str, str]],
    ) -> list[SmartMoneyObservation]:
        accepted_match = _ACCEPTED_RE.search(text)
        xml_match = _XML_RE.search(text)
        if not accepted_match or not xml_match:
            raise ValueError("missing_acceptance_or_ownership_xml")
        accepted_at = datetime.strptime(
            accepted_match.group(1), "%Y%m%d%H%M%S",
        ).replace(tzinfo=_ET)
        root = ET.fromstring(xml_match.group(1))
        form = _text(root, "documentType")
        if form not in {"4", "4/A"}:
            raise ValueError("not_form_4")
        try:
            issuer_cik = str(int(_text(root, "issuer/issuerCik")))
        except (TypeError, ValueError):
            raise ValueError("missing_issuer_cik")
        symbol = _symbol(_text(root, "issuer/issuerTradingSymbol"))
        exchange = listed.get(issuer_cik, {}).get(symbol, "")
        if not symbol or not exchange or symbol.endswith(_NON_EQUITY_SUFFIXES):
            return []

        owner_ciks: list[str] = []
        owner_names: list[str] = []
        owner_roles: list[str] = []
        for owner in root.findall("reportingOwner"):
            try:
                owner_cik = str(int(_text(owner, "reportingOwnerId/rptOwnerCik")))
            except (TypeError, ValueError):
                owner_cik = ""
            name = _text(owner, "reportingOwnerId/rptOwnerName")
            if owner_cik:
                owner_ciks.append(owner_cik)
            if name:
                owner_names.append(name)
            for role in self._roles(owner):
                if role not in owner_roles:
                    owner_roles.append(role)
        actor = " / ".join(owner_names) or "Unknown reporting owner"
        accession = source_url.rsplit("/", 1)[-1].removesuffix(".txt")
        is_10b5_1 = _bool(_text(root, "aff10b5One"))

        rows: list[SmartMoneyObservation] = []
        for index, transaction in enumerate(
            root.findall("nonDerivativeTable/nonDerivativeTransaction")
        ):
            code = _text(transaction, "transactionCoding/transactionCode").upper()
            if code not in {"P", "S"}:
                continue
            acquired_disposed = _text(
                transaction,
                "transactionAmounts/transactionAcquiredDisposedCode/value",
            ).upper()
            if (code == "P" and acquired_disposed != "A") or (
                code == "S" and acquired_disposed != "D"
            ):
                logger.warning(
                    "Dropping direction-inconsistent SEC row %s#%d: code=%s A/D=%s",
                    accession, index, code, acquired_disposed,
                )
                continue
            try:
                transaction_date = date.fromisoformat(
                    _text(transaction, "transactionDate/value")
                )
            except ValueError:
                continue
            shares = _number(_text(
                transaction, "transactionAmounts/transactionShares/value",
            ))
            price = _number(_text(
                transaction, "transactionAmounts/transactionPricePerShare/value",
            ))
            value = shares * price if shares is not None and price is not None else None
            post_shares = _number(_text(
                transaction,
                "postTransactionAmounts/sharesOwnedFollowingTransaction/value",
            ))
            directness = {
                "D": "direct", "I": "indirect",
            }.get(_text(
                transaction, "ownershipNature/directOrIndirectOwnership/value",
            ).upper(), "unknown")
            lag_days = max(0, (accepted_at.date() - transaction_date).days)
            age_days = max(0, (et_today() - accepted_at.date()).days)
            freshness = "fresh" if age_days <= 7 else (
                "delayed" if age_days <= self.lookback_days else "stale"
            )
            rows.append(SmartMoneyObservation(
                symbol=symbol,
                stream="insider",
                actor=actor,
                actor_cik=owner_ciks[0] if owner_ciks else "",
                actor_roles=owner_roles,
                joint_owner_ciks=owner_ciks[1:],
                direction="buy" if code == "P" else "sell",
                transaction_date=transaction_date,
                disclosure_date=accepted_at.date(),
                accepted_at=accepted_at,
                known_at=accepted_at,
                source_url=source_url,
                accession_number=accession,
                filing_form=form,
                transaction_code=code,
                transaction_row=index,
                security_title=_text(transaction, "securityTitle/value"),
                shares=shares,
                price_per_share=price,
                transaction_value_usd=value,
                post_transaction_shares=post_shares,
                ownership_nature=directness,
                amendment=(form == "4/A"),
                # Calendar lag spans weekends; only the filing's explicit
                # timeliness code can truthfully label the transaction late.
                late_filing=(
                    _text(transaction, "transactionTimeliness/value").upper() == "L"
                ),
                is_10b5_1=is_10b5_1,
                listed_exchange=exchange,
                lag_days=lag_days,
                disclosure_age_days=age_days,
                freshness=freshness,
                economic_role="confirmatory",
            ))
        return rows

    def refresh(self) -> dict:
        """Network refresh with a JSON-safe status/result summary."""
        deadline = time.monotonic() + self.refresh_deadline_s
        manifest = self._load_json(self.manifest_path, {})
        processed = set(manifest.get("processed_accessions", []))
        cached_rows = self._load_json(self.observations_path, [])
        observations: dict[str, dict] = {}
        for raw in cached_rows if isinstance(cached_rows, list) else []:
            key = f"{raw.get('accession_number', '')}:{raw.get('transaction_row', '')}"
            observations[key] = raw
        new_count = 0
        processed_count = 0
        errors: list[str] = []
        try:
            listed = self._listed_map(deadline)
            for filing in self._discover(listed, deadline, processed):
                accession = filing["accession"]
                if accession in processed:
                    continue
                try:
                    body, source_url = self._submission(filing, deadline)
                    for row in self._parse_submission(
                        body, source_url=source_url, listed=listed,
                    ):
                        key = f"{row.accession_number}:{row.transaction_row}"
                        if key not in observations:
                            new_count += 1
                        observations[key] = row.model_dump(mode="json")
                    processed.add(accession)
                    processed_count += 1
                except _RefreshDeadline:
                    raise
                except Exception as exc:
                    logger.warning("SEC Form 4 failed for %s: %s", accession, exc)
                    errors.append(f"{accession}:{type(exc).__name__}")
        except _RefreshDeadline:
            errors.append("refresh_deadline_exceeded")
        except Exception as exc:
            logger.warning("SEC Form 4 refresh failed: %s", exc)
            errors.append(f"refresh:{type(exc).__name__}")

        cutoff = et_today() - timedelta(
            days=self.lookback_days + self.cluster_window_days,
        )
        kept: list[dict] = []
        for raw in observations.values():
            try:
                if date.fromisoformat(str(raw.get("disclosure_date"))[:10]) >= cutoff:
                    kept.append(raw)
            except (TypeError, ValueError):
                continue
        with self._cache_lock:
            # History is recorded from every row seen this refresh, including
            # those the lookback prune is about to drop — that prune is what
            # makes a separate long-horizon index necessary.
            try:
                self._record_history(list(observations.values()))
            except OSError as exc:
                logger.warning("Insider history index write failed: %s", exc)
            _atomic_json(self.observations_path, kept)
            _atomic_json(self.manifest_path, {
                "processed_accessions": sorted(processed),
                "last_refresh_at": datetime.now(tz=_ET).isoformat(),
            })
        error = None
        if errors:
            error = (
                "provider_partial_error" if kept else "provider_error"
            ) + ":" + ",".join(errors[:20])
        return {
            "status": (
                "provider_error" if error and not kept else
                "partial" if error else "ok"
            ),
            "new_observations": new_count,
            "processed_filings": processed_count,
            "cached_observations": len(kept),
            "error": error,
        }

    def _merged_history(self, raw_rows) -> InsiderHistory:
        """Long-horizon index plus the trades in the current cache window."""
        merged = self._load_history().as_mapping()
        for raw in raw_rows if isinstance(raw_rows, list) else []:
            if not isinstance(raw, dict):
                continue
            actor_cik = str(raw.get("actor_cik") or "").strip()
            symbol = str(raw.get("symbol") or "").strip().upper()
            direction = str(raw.get("direction") or "").strip()
            if not actor_cik or not symbol or direction not in {"buy", "sell"}:
                continue
            try:
                day = date.fromisoformat(str(raw.get("transaction_date"))[:10])
            except (TypeError, ValueError):
                continue
            entry = InsiderPriorTrade(transaction_date=day, direction=direction)
            bucket = merged.setdefault((actor_cik, symbol), [])
            if entry not in bucket:
                bucket.append(entry)
        return InsiderHistory(merged)

    # Kept as a thin alias: several tests and call sites still spell this
    # `provider._observation_key(...)`. The real implementation now lives in
    # `src.data.smart_money_cluster` so `CongressionalTradingProvider` can
    # share it exactly rather than growing its own copy.
    _observation_key = staticmethod(observation_key)

    def fetch(self, symbols: list[str]) -> tuple[list[SmartMoneyObservation], str | None]:
        """Cache-only broad fetch; ``symbols`` marks core but does not filter."""
        core = {_symbol(s) for s in symbols if str(s).strip()}
        raw_rows = self._load_json(self.observations_path, [])
        # The long-horizon index carries the three-year calendar history; the
        # current cache window contributes this refresh's own trades so a
        # cadence inside the window is still visible on the first ever run.
        history = self._merged_history(raw_rows)
        parsed: list[SmartMoneyObservation] = []
        invalid = 0
        for raw in raw_rows if isinstance(raw_rows, list) else []:
            try:
                item = SmartMoneyObservation(**raw)
            except Exception:
                invalid += 1
                continue
            if item.stream != "insider" or item.disclosure_age_days > self.lookback_days:
                continue
            verdict = classify_transaction(item, history, self._signal_thresholds)
            item = item.model_copy(update={
                "signal_class": verdict.label,
                "signal_class_reason": verdict.reason,
                "signal_class_detail": verdict.detail,
                "signal_weight": verdict.weight,
            })
            age_days = max(0, (et_today() - item.disclosure_date).days)
            freshness = "fresh" if age_days <= 7 else (
                "delayed" if age_days <= self.lookback_days else "stale"
            )
            if age_days > self.lookback_days:
                continue
            threshold = (
                self.min_transaction_value_usd
                if item.symbol in core else self.external_min_transaction_value_usd
            )
            admission = (
                item.symbol not in core
                and not item.amendment
                and item.direction == "buy"
                and item.transaction_code == "P"
                # A routine purchase has no predictive power (Cohen/Malloy/
                # Pomorski); it must never be the reason a symbol enters the
                # trading surface. Strictly narrows the existing gate.
                and verdict.label != "routine"
                and item.transaction_value_usd is not None
                and item.transaction_value_usd >= threshold
                and item.freshness != "stale"
            )
            parsed.append(item.model_copy(update={
                "in_core_universe": item.symbol in core,
                "in_trading_universe": item.symbol in core,
                "admission_eligible": admission,
                "transient_admission_eligible": admission,
                "economic_role": "actionable" if admission else "confirmatory",
                "disclosure_age_days": age_days,
                "freshness": freshness,
            }))

        survivors = cluster_survivors(
            parsed,
            threshold_fn=lambda symbol: (
                self.min_transaction_value_usd
                if symbol in core else self.external_min_transaction_value_usd
            ),
            cluster_window_days=self.cluster_window_days,
            min_cluster_owners=self.min_cluster_owners,
        )

        ordered = sorted(
            survivors.values(),
            key=lambda item: (
                not item.transient_admission_eligible,
                not item.in_core_universe,
                # Routine rows stay visible (the operator still sees them and
                # their reason) but sort last, so they are the first cut when
                # ``max_observations`` binds and a real signal is competing.
                -item.signal_weight,
                -(item.transaction_value_usd or 0),
                -(item.accepted_at.timestamp() if item.accepted_at else 0),
            ),
        )[:self.max_observations]
        error = f"cache_partial_error:{invalid}_invalid_rows" if invalid else None
        return ordered, error
