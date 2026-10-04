"""Third-route selection.

Moved VERBATIM out of src/agents/base.py; base.py re-exports every name.
"""



# === The third route =======================================================
#
# REQUIREMENT: a genuinely DIFFERENT MODEL, not the same model on another
# road. The existing secondary (OpenRouter serving the SAME
# gemini-3.5-flash-lite the primary serves) is route diversity only — when
# the MODEL is saturated rather than the road, both fail together, which is
# the 2026-09-22 shape: Google direct returned "This model is currently
# experiencing high demand" 17 times and the same-model failover went down
# with it.
#
# CHOICE: `anthropic/claude-haiku-4.5`, served over OPENROUTER.
#
# WHY NOT ANTHROPIC OR OPENAI DIRECT — this is the load-bearing finding and
# it overrode the first version of this design. QAMC does not hold its own
# LLM credentials: `.env` carries placeholders and OneCLI's gateway injects
# the real value, matched BY DESTINATION HOST, only for hosts that have an
# explicit grant. docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md
# enumerates the grants that exist and were verified end-to-end:
# `openrouter.ai`, `api.stlouisfed.org`, `*.alpaca.markets`, plus
# `generativelanguage.googleapis.com` (recorded in .env.example). There is
# NO grant for `api.openai.com` and NO grant for `api.anthropic.com`, and
# neither appears in that document's verification list. A route 3 on either
# direct endpoint would send `Authorization: Bearer placeholder-...` and
# collect a 401 every single time — a guaranteed-failing extra attempt
# dressed up as a rescue. "The key is named in settings.yaml" and "the model
# is in cost_table" prove the wiring and the price; neither proves the road
# exists. Nothing in this repository has ever made a successful call on
# `provider: openai` or `provider: anthropic`.
#
# So route 3 goes over OpenRouter — the one LLM road besides Google with a
# verified grant — carrying a DIFFERENT MODEL. That satisfies the actual
# requirement (model diversity) using the only road that is known to work.
#
# WHY HAIKU AND NOT `openai/o4-mini`. Both are on OpenRouter's catalog and
# the prices cross over, so price does not decide it: haiku is $1.00 in /
# $5.00 out and o4-mini is $1.10 in / $4.40 out per million tokens
# [measured 2026-09-23, OpenRouter's live /api/v1/models]. What decides it is
# family independence. The desk already depends on OpenAI for its Portfolio
# Manager seat (`openai/gpt-5.5`) and on Google for routes 1 and 2. Anthropic
# is the only major family the desk has no other dependency on, so an
# OpenAI-side incident cannot take out the PM seat and the specialists'
# last-resort route in the same stroke.
#
# WITHDRAWN ARGUMENT, recorded because it was wrong and the reasoning is
# reusable. An earlier draft chose o4-mini on the grounds that Anthropic
# documents a 529 `overloaded_error` (platform.claude.com/docs/en/api/errors)
# that src/cost_circuit.py's zero-cost allow-list does not carry, so an
# Anthropic tertiary's likeliest failure would be booked ambiguous. That is
# true of Anthropic DIRECT and irrelevant here: OpenRouter "normalizes every
# upstream provider error" into its own status codes and surfaces the
# upstream code only in `error.metadata.provider_code`
# (openrouter.ai/docs/api-reference/errors, verified 2026-09-23). A 529
# never reaches this process as a 529. Choosing the desk's analyst by its
# provider's HTTP error taxonomy was the wrong axis anyway.
#
# BONUS THE OPENROUTER ROAD BUYS, and the reason this is not a compromise:
# `_openai_wire_call` builds `extra_body` for `openrouter` and `google` ONLY.
# The `openai` and `anthropic` branches get NO `reasoning.effort` and NO
# `response_format`. A tertiary on OpenAI direct would therefore have
# violated the 2026-09-14 uniform-testing requirement recorded in that same
# function, run a reasoning model at an undeclared effort against a 16k
# output ceiling, and risk returning an empty body with
# finish_reason="length" — which `_TRUNCATION_FINISH_REASONS` treats as a
# SUCCESS that never fails over. Over OpenRouter route 3 gets the identical
# declared effort, the identical structured-output constraint, and
# `usage: {include: true}` — the last of which matters because a successful
# call with no usage telemetry trips `unknown_actual_cost`, the one hard
# latch that does NOT self-clear.
#
# HONEST RESIDUAL: routes 2 and 3 share the OpenRouter account, so an
# account-level OpenRouter outage takes both. That is a real reduction in
# road diversity relative to the first draft, accepted because the first
# draft's extra road did not exist. Closing it means getting a grant for a
# third host, which is an owner decision (a new paid dependency), not one to
# make inside this change.
#
# RELATION TO THE 2026-08-31 "change the ROAD, not the REASONING" ruling
# recorded at `_DEFAULT_FALLBACK_PROVIDER` above: that ruling is not
# overturned, it is respected and then exhausted. Routes 1 and 2 are still
# the same model on two roads, and route 3 is only ever reached when BOTH
# have failed — i.e. when the alternative to a different model answering is
# no analysis at all. A surprising answer is worse than an expected one; it
# is better than the desk sitting out the open.
_DEFAULT_TERTIARY_PROVIDER = "openrouter"
_DEFAULT_TERTIARY_MODEL = "anthropic/claude-haiku-4.5"

# === ROUTE 3, SECOND-ROAD SUBSTITUTE (2026-09-30) =========================
# Closes the "HONEST RESIDUAL" written into the block above, for the seats
# where it is not a residual at all but the whole ladder.
#
# THE MEASUREMENT. Production log /home/qamc/quant-agent/quant_agent.log,
# 2026-09-29 19:46:45-19:47:46: portfolio_manager's route 1
# (openrouter, openai/gpt-5.5) returned HTTP 402 Payment Required, route 2
# (openrouter, google/gemini-3.5-flash-lite) was skipped on a demoted
# provider, route 3 (openrouter, anthropic/claude-haiku-4.5) returned HTTP
# 402 again and logged "Every route is down." At 19:46:08 the SAME PROCESS
# had completed tech_analyst on `generativelanguage.googleapis.com` at
# HTTP 200 for $0.00 using gemini-3.5-flash-lite. A healthy, credentialed,
# free road sat unused while every rung of the decision seat's ladder
# queued behind one exhausted OpenRouter balance.
#
# WHY THIS IS NOT THE SAME CHOICE THE BLOCK ABOVE ALREADY MADE. That block
# reasoned about the eight specialist seats, whose route 1 IS Google
# direct: for them a Google route 3 would be a third attempt at the road
# that already failed twice, and OpenRouter/haiku is the right answer. It
# then recorded the OpenRouter-account collision as an accepted residual
# and said closing it "means getting a grant for a third host". For the
# decision seats (portfolio_manager, risk_manager, position_reviewer —
# every one of them `provider: openrouter`) that is simply not true: the
# SECOND credentialed host is already there, already primary for eight
# other seats, already exercised every session. No new host, no new
# grant, no new paid dependency.
#
# THE RULE, and it is deliberately narrow: when routes 1 and 2 resolve to
# the SAME provider and the configured route 3 would land on that provider
# too, route 3 is swapped for this pair instead — but ONLY if this pair is
# on a provider not already in the ladder. A seat whose ladder already
# spans two roads is untouched, so the eight Google-primary seats keep
# OpenRouter/haiku exactly as reasoned above. This adds no rung: the
# attempt budget is unchanged (see `provider_attempt_budget`).
#
# MODEL DIVERSITY IS NOT SACRIFICED, it is traded where it is worthless.
# Route 3's purpose per the block above is to change the MODEL once the
# same model on two roads has failed. When both of those roads are ONE
# account, changing the model changes nothing — a 402 is an account-level
# refusal, not a model-level one, and the 2026-09-29 log is that sentence
# measured. Against a dead account, a different ROAD is the only variable
# that can still move.
#
# WHY THIS MODEL. `gemini-3.5-flash-lite` on Google AI Studio direct is
# the only (provider, model) pair on a non-OpenRouter host that this
# deployment has ever completed a call on — it is the configured PRIMARY
# for eight seats and it answered at 19:46:08 on the very day of the
# outage. It is not picked for quality at the decision seats and does not
# claim to be their equal; it is the last rung, reached only when the desk's
# alternative is producing nothing at all, which is the same standard the
# block above applied to haiku. It also inherits the uniform reasoning
# effort, the structured-output constraint and usage telemetry, because
# `_openai_wire_call` builds `extra_body` for `google` as well as
# `openrouter` — the exact property that ruled out the OpenAI/Anthropic
# direct endpoints there.
#
# RESIDUAL THAT REMAINS. A decision seat's route 3 is now a small free
# model rather than haiku, so a total-OpenRouter outage degrades decision
# QUALITY at those seats. That is accepted for the same reason the block
# above accepted a model change at all: the alternative on 2026-09-29 was
# no decision run whatsoever. Set `tertiary_alt_model` to "" to switch the
# substitution off and restore the previous behaviour exactly.
_DEFAULT_TERTIARY_ALT_PROVIDER = "google"
_DEFAULT_TERTIARY_ALT_MODEL = "gemini-3.5-flash-lite"


def select_tertiary_route(
    *,
    primary: tuple[str, str],
    fallback: tuple[str, str] | None,
    tertiary: tuple[str, str],
    alt: tuple[str, str] | None,
) -> tuple[str, str]:
    """Which (provider, model) route 3 actually uses.

    Returns `alt` when the ladder has collapsed onto a single provider and
    `alt` is on a different one; otherwise returns `tertiary` unchanged.
    `fallback` is None when route 2 is unreachable for this seat (no key, or
    the same pair as the primary), and `alt` is None when the substitute is
    unconfigured or uncredentialed.

    A free function, not an inline branch in `__init__`, so the rule can be
    asserted directly per seat in tests rather than only through a
    constructed agent.
    """
    if alt is None:
        return tertiary
    ladder_roads = {primary[0]}
    if fallback is not None:
        ladder_roads.add(fallback[0])
    if len(ladder_roads) != 1:
        return tertiary          # the ladder already spans two roads
    if tertiary[0] not in ladder_roads:
        return tertiary          # route 3 already leaves that road
    if alt[0] in ladder_roads:
        return tertiary          # the substitute would not change the road
    return alt


def _route_price(model: str) -> tuple[float | None, float | None]:
    """Published list price of `model` in USD per MILLION tokens, for the
    route journal. `(None, None)` when the model is not in the pricing table
    — the journal records the gap rather than a confident zero."""
    try:
        from src.cost_table import PRICING
        row = PRICING.get(model)
        if not row:
            return None, None
        return row.get("input"), row.get("output")
    except Exception:  # noqa: BLE001 — pricing must never break a call
        return None, None


# === DECISION SEATS REFUSE THE UNMEASURED SUBSTITUTE (board item 188) ======
#
# The substitution reasoned about above ends the decision seats' ladder on
# `gemini-3.5-flash-lite`, a model NOBODY HAS EVER MEASURED AT THOSE SEATS.
# The owner's model choice for the trade seat is a CLOSED question decided on
# 148 trials, so a route that quietly swaps in an unmeasured model does not
# merely degrade quality: it overrides a ruling that has already been made,
# and its output is indistinguishable from a measured one.
#
# The answer is NOT to benchmark the substitute (re-opening the closed
# question) and NOT to pick a different model. It is for the seat to REFUSE:
# produce no verdict at all, say so honestly, and leave a durable counted row.
# Refusal is the LAST step, never the first — routes 1 and 2 are attempted in
# full exactly as before, and only the final rung is withheld.
#
# NARROW BY CONSTRUCTION. This fires only when `select_tertiary_route` has
# actually substituted the alt road (`on_alt_road`), which by that function's
# own rule can only happen for a seat whose whole ladder sits on one provider
# — in this deployment the three decision seats. The eight specialist seats
# keep OpenRouter/haiku, a model route 3 has always carried, and are not
# decision seats anyway: they describe, they do not decide.
DECISION_SEATS = ("portfolio_manager", "risk_manager", "position_reviewer")


def seat_must_refuse_unmeasured_route(seat_name: str,
                                      on_alt_road: bool) -> bool:
    """True when this seat must decline rather than answer on route 3.

    `on_alt_road` is `BaseAgent._tertiary_on_alt_road`: route 3 was swapped
    for the second-road substitute, which is unmeasured at every decision
    seat. A free function so the rule is assertable per seat name without
    constructing an agent.
    """
    return bool(on_alt_road) and (seat_name or "") in DECISION_SEATS
