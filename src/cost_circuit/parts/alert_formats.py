"""src.cost_circuit.parts.alert_formats -- Owner-facing alert texts for the cost circuit.

Bodies moved verbatim from the former src/cost_circuit/breaker_formats.py shim (originally src/cost_circuit.py); held by LLMCostCircuitBreaker.
Every collaborator is an explicit keyword-only constructor argument.
"""

from __future__ import annotations
from typing import Any, Callable, TypeVar
from src.cost_circuit.refusal import _fmt_settled
from src.cost_circuit.parts.episode_wording import EpisodeWording


class AlertFormats:
    def __init__(self) -> None:
        # Four pure formatters: no state, no collaborators.
        pass

    @staticmethod
    def format_auto_reset_alert(event: dict[str, Any]) -> str:
        # A manual reset also clears an inexact day or a durable marker, which
        # carry no trigger code at all; say so rather than borrowing the
        # auto path's wording for a latch that may not have existed.
        code = str(event.get("trigger_code") or "").strip() or (
            "not recorded" if str(event.get("event_type") or "") == "reset" else "transient provider latch"
        )
        session_cost = float(event.get("session_cost_usd") or 0.0)
        daily_cost = float(event.get("daily_cost_usd") or 0.0)
        # "When" is read from the event row's own `created_at` (UTC, written by
        # SQLite at the instant of the auto-clear), never from the clock at
        # send time -- a retried alert can go out minutes or a boundary later
        # than the resume it describes.
        resumed_at = str(event.get("created_at") or "").strip()
        when = f"resumed at: {resumed_at} UTC\n" if resumed_at else ""
        # The two paths share one notification, but the closing line must say
        # which one actually happened: "no operator reset was needed" is
        # false of a manual reset, and an owner-facing statement the row
        # cannot support is a lie, not a rounding.
        manual = str(event.get("event_type") or "") == "reset"
        if manual:
            default_reason = "operator reset"
            how = "status: paid analysis is live again after an operator reset. "
        else:
            default_reason = "transient provider latch auto-expired"
            how = "status: paid analysis is live again; no operator reset was needed. "
        return (
            "🟢 QAMC PAID ANALYSIS RESUMED\n"
            f"previous suspension: {code}\n"
            f"{when}"
            f"{EpisodeWording._format_episode_summary(event)}"
            f"reason: {event.get('detail') or default_reason}\n"
            f"settled spend at resume: {_fmt_settled(session_cost)} this run · "
            f"{_fmt_settled(daily_cost)} today\n"
            f"{how}"
            "Session, call-count, attempt, and daily limits remain enforced and "
            "re-latch instantly if real settled spend is over a cap."
        )

    @staticmethod
    def format_quota_alert(hold: dict[str, Any]) -> str:
        scope = str(hold.get("scope") or "session")
        if scope == "day":
            recovery = "recovery: automatic at the next ET budget day after exact accounting checks pass"
            affected = "all paid analysis for this ET budget day"
        elif scope == "mode_day":
            recovery = "recovery: this mode is eligible again next ET budget day after exact accounting checks pass"
            affected = f"{hold.get('mode') or 'this mode'} paid sessions today"
        else:
            recovery = "recovery: later independent sessions remain eligible"
            affected = f"run {hold.get('run_id') or 'unknown'} only"
        attempts = int(hold.get("attempts") or 0)
        session_cost = float(hold.get("session_cost_usd") or 0.0)
        daily_cost = float(hold.get("daily_cost_usd") or 0.0)
        qualifier = "" if bool(hold.get("costs_exact", 1)) else " (conservative exposure)"
        return (
            "🟠 QAMC PAID ANALYSIS QUOTA HOLD\n"
            f"trigger: {hold.get('trigger_detail') or hold.get('trigger_code')}\n"
            f"scope: {affected}\n"
            f"affected run: {hold.get('run_id') or 'unknown'} "
            f"({hold.get('mode') or 'unknown'} / {hold.get('agent_name') or 'unknown'})\n"
            f"attempts: {attempts} provider attempt{'s' if attempts != 1 else ''}\n"
            f"cost: {_fmt_settled(session_cost)} this run · "
            f"{_fmt_settled(daily_cost)} on ET day {hold.get('day')}{qualifier}\n"
            f"{recovery}\n"
            "preserved: broker-resident stops, reconciliation, deterministic loss "
            "protection, close/P&L jobs, and the read-only API; no operator reset is required."
        )

    @staticmethod
    def format_recovery_alert(hold: dict[str, Any]) -> str:
        scope = str(hold.get("scope") or "day")
        released = "all paid modes" if scope == "day" else str(hold.get("mode") or "unknown mode")
        return (
            "🟢 QAMC PAID ANALYSIS REARMED\n"
            f"previous hold: {hold.get('trigger_code')} on ET day {hold.get('day')}\n"
            f"scope released: {scope} / {released}\n"
            f"checks passed: {hold.get('release_reason') or 'new ET budget window is exact'}\n"
            "status: paid analysis is eligible again; session, call-count, "
            "attempt, and daily limits remain enforced."
        )

    @staticmethod
    def format_alert(state: dict[str, Any]) -> str:
        attempts = int(state.get("session_attempts") or 0)
        attempts_exact = bool(state.get("attempts_exact", 1))
        session_cost = float(state.get("session_cost_usd") or 0.0)
        daily_cost = float(state.get("daily_cost_usd") or 0.0)
        trigger_code = str(state.get("trigger_code") or "")
        costs_exact = bool(state.get("costs_exact", 1))
        if trigger_code == "legacy_unknown_cost":
            cost_note = " (known minimum; legacy rows have unknown cost)"
        else:
            cost_note = " (includes conservative or unresolved-request accounting)" if not costs_exact else ""
        attempts_line = (
            f"attempts: {attempts} provider attempt{'s' if attempts != 1 else ''}"
            if attempts_exact
            else f"attempts: {attempts} logged agent record{'s' if attempts != 1 else ''} "
            "(legacy; exact provider-request count unavailable)"
        )
        return (
            "🔴 QAMC PAID ANALYSIS SUSPENDED\n"
            f"trigger: {state.get('trigger_detail') or state.get('trigger_code') or 'safety limit'}\n"
            f"{EpisodeWording._format_episode_line(state)}"
            f"affected run: {state.get('run_id') or 'unknown'} "
            f"({state.get('mode') or 'unknown'} / {state.get('agent_name') or 'unknown'})\n"
            f"{attempts_line}\n"
            f"cost: {_fmt_settled(session_cost)} this run · "
            f"{_fmt_settled(daily_cost)} today{cost_note}\n"
            "suspended: all paid LLM analysis, repairs, retries, and provider failover\n"
            "preserved: broker-resident stops, order/fill reconciliation, deterministic "
            "loss protection, close/P&L jobs, and the read-only API\n"
            "operator reset with a recorded reason is required before paid analysis resumes."
        )
