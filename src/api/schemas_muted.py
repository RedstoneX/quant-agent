"""Muted-backlog response models (moved out of schemas.py; re-exported there)."""

from __future__ import annotations

from pydantic import BaseModel


class MutedKindCount(BaseModel):
    """Muted messages of one kind, with its live-risk share kept visible."""

    kind: str
    count: int = 0
    live_risk_count: int = 0
    muted_count: int = 0
    """Of `count`, how many the global mute swallowed."""
    filtered_count: int = 0
    """Of `count`, how many TELEGRAM_RISK_ONLY dropped as operational."""


class MutedDayCount(BaseModel):
    """Muted messages on one ET day, with its live-risk share kept visible."""

    day: str
    count: int = 0
    live_risk_count: int = 0
    muted_count: int = 0
    """Of `count`, how many the global mute swallowed."""
    filtered_count: int = 0
    """Of `count`, how many TELEGRAM_RISK_ONLY dropped as operational."""


class MutedLiveRiskMessage(BaseModel):
    """One undelivered message about a position whose protection was gone."""

    reason: str = "muted"
    """"muted" (the global mute) or "filtered" (dropped as operational)."""

    timestamp: str
    day: str
    kind: str
    symbols: list[str] = []
    headline: str = ""


class MutedBacklogResponse(BaseModel):
    """Item 211 — what the global mute has been swallowing.

    `coverage_complete` is False while the record begins after the mute did;
    `coverage_gap` says so in the owner's words, so the surface can never
    present a partial list as the whole period.
    """

    record_available: bool = False
    record_begins_at: str = ""
    mute_began_on: str = ""
    coverage_complete: bool = False
    coverage_gap: str = ""
    total: int = 0
    muted_total: int = 0
    """Of `total`, how many the global mute swallowed."""
    filtered_total: int = 0
    """Of `total`, how many the per-category mute dropped as operational."""
    live_risk_total: int = 0
    by_kind: list[MutedKindCount] = []
    by_day: list[MutedDayCount] = []
    live_risk: list[MutedLiveRiskMessage] = []
    oldest: str | None = None
    newest: str | None = None
