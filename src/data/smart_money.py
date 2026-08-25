"""Replaceable, fail-soft smart-money data provider (Bargo Congress REST)."""
from __future__ import annotations

import logging
from datetime import date
from typing import Protocol

import requests

from src.models import SmartMoneyObservation

logger = logging.getLogger(__name__)


class SmartMoneySource(Protocol):
    def fetch(self, symbols: list[str]) -> tuple[list[SmartMoneyObservation], str | None]: ...


class BargoCongressProvider:
    def __init__(self, *, base_url: str, api_key: str = "", timeout_s: float = 10, max_rows_per_symbol: int = 20):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_rows_per_symbol = max_rows_per_symbol

    @staticmethod
    def _direction(value: object) -> str:
        text = str(value or "").lower()
        if "purchase" in text or text == "buy": return "buy"
        if "sale" in text or text == "sell": return "sell"
        if "exchange" in text: return "exchange"
        return "unknown"

    @staticmethod
    def _parse_date(value: object) -> date:
        return date.fromisoformat(str(value)[:10])

    def fetch(self, symbols: list[str]) -> tuple[list[SmartMoneyObservation], str | None]:
        rows: list[SmartMoneyObservation] = []
        errors: list[str] = []
        headers = {"Accept": "application/json", "User-Agent": "QAMC/1.0 research-intelligence"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        for symbol in dict.fromkeys(s.strip().upper() for s in symbols if s.strip()):
            try:
                response = requests.get(
                    f"{self.base_url}/trades/{symbol}",
                    params={"limit": self.max_rows_per_symbol}, headers=headers,
                    timeout=self.timeout_s,
                )
                response.raise_for_status()
                payload = response.json()
                for raw in (payload.get("trades", []) if isinstance(payload, dict) else []):
                    try:
                        tx = self._parse_date(raw.get("transaction_date"))
                        disclosed = self._parse_date(raw.get("disclosure_date"))
                        lag = max(0, (disclosed - tx).days)
                        age = max(0, (date.today() - disclosed).days)
                        freshness = "fresh" if age <= 7 and lag <= 30 else "delayed" if age <= 30 else "stale"
                        source = (raw.get("filing_url") or raw.get("official_url")
                                  or raw.get("source_url") or raw.get("source"))
                        if not source:
                            raise ValueError("missing_source")
                        rows.append(SmartMoneyObservation(
                            symbol=symbol, actor=str(raw.get("member") or raw.get("politician") or "Unknown filer"),
                            direction=self._direction(raw.get("type") or raw.get("transaction_type")),
                            amount_range=str(raw.get("amount_range") or raw.get("amount") or ""),
                            transaction_date=tx, disclosure_date=disclosed, source_url=str(source),
                            lag_days=lag, disclosure_age_days=age, freshness=freshness,
                            economic_role="historical",
                        ))
                    except Exception as exc:
                        errors.append(f"{symbol}:row:{type(exc).__name__}")
            except Exception as exc:  # one symbol must not erase prior successes
                logger.warning("Smart-money provider failed for %s: %s", symbol, exc)
                errors.append(f"{symbol}:{type(exc).__name__}")
        error = f"provider_partial_error:{','.join(errors)}" if errors and rows else (
            f"provider_error:{','.join(errors)}" if errors else None
        )
        return rows, error
