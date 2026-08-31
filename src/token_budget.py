"""Know how big a request is BEFORE sending it, and build it to fit.

THE PRINCIPLE (owner instruction, 2026-08-31)
---------------------------------------------
Never discover a limit by hitting it. Every request costs money, so the work
of sizing one belongs on this side of the wire, done once and properly — not
guessed at, sent, refused, and retried.

That rules out the two obvious designs. Picking a tokens-per-minute ceiling
and backing off when it trips means paying for the refusal that taught you
the number. Letting a ceiling learn from refusals means paying for every
lesson. Both make the provider the measuring instrument, and it charges for
readings.

THE MODEL
---------
A request's token count is fixed overhead plus a rate per byte of content:

    tokens  =  fixed_tokens  +  tokens_per_byte x content_bytes

Both are fitted by least squares from the agent's own rows in `agent_logs` —
calls already made and paid for, so measuring costs nothing and improves as
the desk runs. This is the "learns as it goes" part, and it never learns from
a refusal.

The two parameters are not curve-fitting artefacts; they are the two real
things in a prompt. Fitted on production data 2026-08-31:

    tech_analyst        4,127 tok + 0.939 tok/byte   (1.07 B/tok content)
    news_analyst        4,200 tok + 0.227 tok/byte   (4.40 B/tok content)
    portfolio_manager  12,483 tok + 0.214 tok/byte   (4.68 B/tok content)

The intercepts recover each agent's system prompt (tech_analyst's is 19,792
bytes of prose, and 19,792/4,127 = 4.8 bytes per token — exactly what English
tokenizes at). The slopes recover the content: ~1.07 bytes/token for
tech_analyst's dense OHLCV digits, ~4.4 for news prose. Median prediction
error 0.9-4.3% on the agents that matter.

A single bytes-per-token ratio cannot express this. It folds a per-request
constant into a per-byte rate, so it is only correct at one message size —
and tech_analyst's messages span 6KB to 379KB.

WHY A FIXED ITEM COUNT WAS NEVER THE RIGHT UNIT
-----------------------------------------------
`_CHUNK_SIZE = 25` was calibrated against "~300 input tokens per symbol". The
bar window grew from 20 to 40, per-symbol context was added, and the real
figure became ~3,600 tokens per symbol. Nothing failed loudly, because
context length was never the binding constraint — the model accepts a million
tokens. The binding constraint was the provider's rate limit, and a fixed
count cannot bound tokens. A budget can, and it stays correct when the
content changes underneath it, which a hand-set count demonstrably does not.

FAILURE POSTURE
---------------
Nothing here may take a trading session down. Every entry point catches
broadly and degrades to the caller's own fixed-size behaviour. A sizing
helper that raised would convert a cost optimisation into an outage, which
is precisely the failure mode this whole day was spent removing.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Used when history cannot answer. Deliberately pessimistic: 1.0 bytes/token
# is denser than any real content, so an unmeasured agent gets SMALLER
# requests. Erring this way costs a little efficiency; erring the other way
# is the flood this module exists to prevent.
DEFAULT_TOKENS_PER_BYTE = 1.0
DEFAULT_FIXED_TOKENS = 6_000

# Below this, one outlier moves the fit more than the signal does.
MIN_SAMPLES = 8

# The fit changes when prompts are restructured — across releases, not across
# minutes. Re-reading `agent_logs` on every call would be pure overhead.
_CACHE_TTL_S = 900.0

_cache: dict[tuple[str, str], tuple[float, "SizeModel"]] = {}
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class SizeModel:
    """How this agent's prompt size converts to provider tokens."""

    fixed_tokens: float
    tokens_per_byte: float
    samples: int
    measured: bool
    observed_max_bytes: float = 0.0

    def predict(self, content_bytes: int) -> int:
        """Tokens a request carrying `content_bytes` of content will cost.

        Extrapolating far past anything ever observed is where a fitted model
        is least trustworthy, so beyond twice the largest message this agent
        has actually sent, the estimate is inflated by 20%. That is the safe
        direction: it makes requests smaller, never larger, exactly where the
        fit is weakest.
        """
        content_bytes = max(0, int(content_bytes))
        tokens = self.fixed_tokens + self.tokens_per_byte * content_bytes
        if self.measured and self.observed_max_bytes > 0:
            if content_bytes > 2 * self.observed_max_bytes:
                tokens *= 1.20
        return max(1, int(tokens))


_FALLBACK = SizeModel(
    fixed_tokens=DEFAULT_FIXED_TOKENS,
    tokens_per_byte=DEFAULT_TOKENS_PER_BYTE,
    samples=0,
    measured=False,
)


def _fit(rows) -> SizeModel | None:
    """Least-squares fit of tokens against content bytes."""
    xs = [float(b) for b, t in rows]
    ys = [float(t) for b, t in rows]
    n = len(xs)
    if n < MIN_SAMPLES:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom <= 0:
        # Every sample is the same size: a slope is unidentifiable. Refusing
        # to invent one is the honest move.
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    intercept = mean_y - slope * mean_x
    # A negative slope or intercept means the fit is describing noise, not a
    # prompt. Prompts cannot cost less for being longer.
    if not (slope > 0) or intercept < 0:
        return None
    return SizeModel(
        fixed_tokens=intercept,
        tokens_per_byte=slope,
        samples=n,
        measured=True,
        observed_max_bytes=max(xs),
    )


def size_model(conn, agent_name: str, model: str, *, now=time.monotonic) -> SizeModel:
    """This agent's fitted size model, or a conservative fallback."""
    key = (agent_name, model)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now() - hit[0] < _CACHE_TTL_S:
            return hit[1]
    fitted = None
    try:
        rows = conn.execute(
            "SELECT LENGTH(CAST(COALESCE(input_message, '') AS BLOB)) AS b, "
            "input_tokens AS t FROM agent_logs "
            "WHERE agent_name=? AND model=? AND input_tokens > 0 "
            "AND LENGTH(COALESCE(input_message, '')) > 0",
            (agent_name, model),
        ).fetchall()
        fitted = _fit([(r[0], r[1]) for r in rows])
    except Exception:
        logger.debug(
            "token budget: could not fit a size model for %s/%s; using the "
            "conservative fallback", agent_name, model, exc_info=True,
        )
    result = fitted or _FALLBACK
    with _cache_lock:
        _cache[key] = (now(), result)
    if fitted is not None:
        logger.info(
            "token budget: %s/%s sized from %d measured calls — %.0f tokens "
            "fixed + %.3f tokens/byte",
            agent_name, model, fitted.samples, fitted.fixed_tokens,
            fitted.tokens_per_byte,
        )
    return result


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()


def pack_to_budget(
    items: list,
    size_of,
    *,
    budget_tokens: int,
    model: SizeModel,
    max_items: int | None = None,
) -> list[list]:
    """Split `items` into batches none of which is predicted to exceed budget.

    Fills a batch until the next item would push it over, then starts a new
    one — so an oversized request is not merely unlikely, it is unreachable:
    nothing is ever added to a full batch.

    A single item too large for the whole budget still gets its own batch
    rather than an empty one or an endless loop. Splitting further is the
    caller's business, and one oversized item is a content bug worth seeing
    rather than something to silently drop.
    """
    if not items:
        return []
    if budget_tokens <= 0:
        return [list(items)]

    batches: list[list] = []
    current: list = []
    used_bytes = 0
    for item in items:
        try:
            item_bytes = max(0, int(size_of(item)))
        except Exception:
            # Unmeasurable: assume it fills a request rather than assume it
            # is free. The safe direction is fewer items per request.
            item_bytes = budget_tokens
        over_budget = bool(current) and (
            model.predict(used_bytes + item_bytes) > budget_tokens
        )
        at_cap = max_items is not None and len(current) >= max_items
        if over_budget or at_cap:
            batches.append(current)
            current, used_bytes = [], 0
        current.append(item)
        used_bytes += item_bytes
    if current:
        batches.append(current)
    return batches


def size_model_for_agent(agent, *, default_model: str | None = None) -> SizeModel:
    """Fit `agent`'s size model from whatever database its cost circuit uses.

    Never raises and never blocks a session: any problem reaching the history
    — no circuit attached, a locked file, a missing table — yields the
    conservative fallback, which only makes requests smaller.
    """
    import sqlite3

    circuit = getattr(agent, "_cost_circuit", None)
    db_path = getattr(circuit, "db_path", None)
    model = default_model or getattr(agent, "model", "") or ""
    if not db_path:
        return _FALLBACK
    conn = None
    try:
        uri = str(db_path)
        if uri.startswith("file:"):
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        else:
            conn = sqlite3.connect(f"file:{uri}?mode=ro", uri=True, timeout=2.0)
        return size_model(conn, getattr(agent, "name", "?"), model)
    except Exception:
        logger.debug("token budget: history unavailable; using fallback", exc_info=True)
        return _FALLBACK
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
