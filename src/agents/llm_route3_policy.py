"""Route 3 policy: which (provider, model, key) the last rung uses, and
whether a DECISION seat may answer on it at all.

Moved VERBATIM out of src/agents/base.py (`BaseAgent.__init__`'s route-3
resolution and the route-3 stage's refusal branch, board item 188). The
statements are unchanged; `self.<field>` became a parameter so each piece can
be constructed and exercised from plain values, without building a
`BaseAgent`. base.py calls both; it owns none of this.

Boundary test this module passes: `resolve_tertiary_route` is a pure function
of config strings, and `refuse_unmeasured_route` needs a seat name, two
(provider, model) pairs, a run id and an error — stubs, not the agent.
"""

import logging
from dataclasses import dataclass

from src import llm_route_journal
from src.agents.llm_providers import resolve_provider
from src.agents.llm_tertiary_route import (
    select_tertiary_route,
    seat_must_refuse_unmeasured_route,
    _route_price,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TertiaryRoute:
    """What `BaseAgent.__init__` used to compute inline as five attributes."""
    provider: str
    model: str
    api_key: str
    reachable: bool
    on_alt_road: bool


def resolve_tertiary_route(
    *,
    primary: tuple[str, str],
    fallback: tuple[str, str],
    failover_reachable: bool,
    tertiary_provider: str | None,
    tertiary_model: str,
    tertiary_api_key: str | None,
    tertiary_alt_provider: str | None,
    tertiary_alt_model: str | None,
    tertiary_alt_api_key: str | None,
) -> TertiaryRoute:
    # === Route 3: a genuinely DIFFERENT model ===========================
    # See _DEFAULT_TERTIARY_PROVIDER above for why haiku-over-OpenRouter
    # and not either direct endpoint (neither is credentialed on this
    # deployment). Reachability is gated exactly like the failover's:
    # a key must be configured AND the pair must differ from BOTH routes
    # already in the ladder, or route 3 is just a third attempt at
    # something that has already failed twice.
    #
    # Before any of that is fixed, route 3 may be SWAPPED for the
    # second-road substitute — see `_DEFAULT_TERTIARY_ALT_PROVIDER` and
    # `select_tertiary_route` above for the rule and the 2026-09-29
    # measurement behind it. The swap fires only for a seat whose routes
    # 1 and 2 have collapsed onto one provider; it never adds a rung.
    _configured_tertiary = (
        resolve_provider(tertiary_model, tertiary_provider), tertiary_model,
    )
    _alt_key = (tertiary_alt_api_key or "").strip()
    _alt = (
        (resolve_provider(tertiary_alt_model, tertiary_alt_provider),
         tertiary_alt_model)
        if _alt_key and (tertiary_alt_model or "").strip()
        and (tertiary_model or "").strip()
        else None
    )
    _selected = select_tertiary_route(
        primary=primary,
        fallback=(fallback if failover_reachable else None),
        tertiary=_configured_tertiary,
        alt=_alt,
    )
    on_alt_road = _selected is _alt and _alt is not None
    provider, model = _selected
    api_key = (
        _alt_key if on_alt_road
        else (tertiary_api_key or "").strip()
    )
    reachable = bool(api_key) and (
        (provider, model)
        not in {primary, fallback}
    )
    return TertiaryRoute(provider, model, api_key, reachable, on_alt_road)


def refuse_unmeasured_route(
    *,
    seat_name: str,
    on_alt_road: bool,
    tertiary: tuple[str, str],
    primary: tuple[str, str],
    run_id,
    primary_error,
) -> RuntimeError | None:
    """The refusal branch of the route-3 stage.

    Returns the error the seat raises in place of a verdict when it refuses,
    having logged the refusal and written its `seat_refused` row; returns
    None when the seat may try route 3. The caller skips route 3 and reports
    the returned error whenever this is not None.
    """
    _tertiary_provider, _tertiary_model = tertiary
    _provider, _model = primary
    # BOARD ITEM 188. A decision seat does not answer on a model
    # nobody has measured at that seat. Routes 1 and 2 have been
    # attempted in full above; only this last rung is withheld,
    # and the refusal is recorded as a durable counted row rather
    # than a log line. `record` never raises, so a journal
    # failure cannot abort the decision stage.
    seat_refuses = seat_must_refuse_unmeasured_route(
        seat_name, on_alt_road,
    )
    if not seat_refuses:
        return None
    logger.error(
        "Agent %s: REFUSING route 3 — the only route left is "
        "%s/%s, which has never been measured at this "
        "decision seat. The seat produces no verdict; a "
        "defaulted or fabricated one would be a lie.",
        seat_name, _tertiary_provider,
        _tertiary_model,
    )
    _in_p, _out_p = _route_price(_tertiary_model)
    llm_route_journal.record(
        "seat_refused", agent_name=seat_name,
        run_id=run_id,
        route=f"{_tertiary_provider}/{_tertiary_model}",
        from_route=f"{_provider}/{_model}", tier=3,
        input_usd_per_mtok=_in_p, output_usd_per_mtok=_out_p,
        error=primary_error,
        detail=(
            "decision seat refused the last rung: route 3 is "
            "a model unmeasured at this seat, and the owner's "
            "model choice for the trade seat is closed"
        ),
    )
    # The refusal, not the provider error, is the proximate
    # reason there is no answer — so it is what the caller
    # and the owner-facing failure summary must say. This is
    # the ONE case where the primary's error is not the most
    # truthful thing to report.
    return RuntimeError(
        f"{seat_name}: refused to answer. Every measured "
        f"route failed and the only route left "
        f"({_tertiary_provider}/{_tertiary_model}) "
        f"has never been measured at this decision seat, so "
        f"the seat declines rather than produce a verdict of "
        f"unknown quality."
    )
