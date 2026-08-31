"""Force provider failures during a rehearsal, offline and on demand.

WHY THIS EXISTS
---------------
The rehearsal harness replays recorded model responses (see `replay.py`), and
every recorded response is, by construction, a response that SUCCEEDED. So
the branch that runs when a provider does not answer — the retry loop, the
cross-provider failover, and every cost-circuit guard those cross — could not
be exercised offline at all. It was reachable only by waiting for a real
provider to fail during a real trading session, which is to say: by finding
out in front of the market.

That gap had a cost. On 2026-08-31 the circuit's per-call attempt ceiling (2)
sat below what the retry loop could spend (3: two primary attempts plus one
failover). Every failover attempt was therefore guaranteed to trip the
circuit rather than rescue the session. The contradiction had existed for six
days and survived a weekend of testing and auditing, because nothing that
could be run on a weekend could make a provider fail. An upstream rate-limit
at 09:32 ET on the Monday found it instead, and the desk went dark for the
day over $0.05 of spend.

This module closes that gap. A fault spec makes any provider attempt fail,
in any of the ways a real provider fails, at any hour, for free.

WHAT A FAULT DOES NOT DO
------------------------
It does not stub, skip or soften anything above the transport. The failure is
raised at exactly the point a real transport failure surfaces — after the
cost circuit has authorized the attempt, so the attempt is counted and priced
exactly as a real one would be. Everything above it is the production code
path: `_is_retryable` classification, backoff, the retry deadline, the
failover gate, reservation accounting, and every circuit guard at every
network boundary.

RETRYABILITY IS NOT DECLARED HERE
---------------------------------
Each fault kind carries the status code a real provider would return, and
`src.agents.base._is_retryable` — the production classifier, unmodified —
decides what happens next. `tests/test_rehearsal_fault_injection.py` pins the
classification of every kind, so if that classifier is ever tightened, the
test fails loudly rather than these faults quietly ceasing to represent the
real failures they are named after.

SPEC FORMAT
-----------
    agent:kind[:count]

`agent` is an agent name (`tech_analyst`) or `*` for every agent. `kind` is
one of `KINDS` below. `count` is how many provider attempts to fail; omit it
to fail every attempt for the whole run.

    tech_analyst:rate_limit:2   the 2026-08-31 incident: the primary is
                                rate-limited out of both its attempts, so
                                the failover must carry the session
    tech_analyst:rate_limit     the primary never recovers AND the failover
                                also fails -- the session must degrade, not
                                latch
    *:server_error:1            one transient blip per agent, everywhere

The count is kept per agent across the whole run, not per logical call. An
agent that chunks its work (tech_analyst) therefore spends the budget on its
first chunks; that is what makes `:2` reproduce a first-call failover.
"""

from __future__ import annotations

import fnmatch
import threading
from dataclasses import dataclass, field


class InjectedProviderFault(Exception):
    """A deliberately induced transport failure.

    Deliberately NOT a subclass of any provider SDK's exception type: it must
    travel through the same generic handling a real one does without being
    special-cased anywhere, and it must be obvious in a traceback that the
    failure was induced rather than encountered.
    """

    # Read by `src.agents.base._is_retryable`, the same way a provider SDK
    # exception's own status is. None means "no status" -- that classifier's
    # documented catch-all treats it as a transient local fault and retries.
    status_code: int | None = None


def _fault(name: str, status: int | None, description: str) -> type:
    return type(name, (InjectedProviderFault,), {
        "status_code": status,
        "__doc__": description,
    })


RateLimited = _fault(
    "RateLimited", 429,
    "HTTP 429. Retryable. The 2026-08-31 failure: zero tokens billed, because "
    "the provider refused before generating anything.",
)
ServerError = _fault(
    "ServerError", 503,
    "HTTP 503. Retryable. An upstream outage rather than a per-seat limit.",
)
Timeout = _fault(
    "Timeout", None,
    "No status code. Retryable via _is_retryable's catch-all for unclassified "
    "local/network faults.",
)
AuthFailure = _fault(
    "AuthFailure", 401,
    "HTTP 401. NOT retryable -- a dead key cannot be slept off, so the loop "
    "must abandon the primary immediately and let failover take over.",
)
InsufficientBalance = _fault(
    "InsufficientBalance", 402,
    "HTTP 402. NOT retryable. The DeepSeek out-of-money case that "
    "cross-provider failover was originally built for.",
)

KINDS: dict[str, type] = {
    "rate_limit": RateLimited,
    "server_error": ServerError,
    "timeout": Timeout,
    "auth": AuthFailure,
    "insufficient_balance": InsufficientBalance,
}


@dataclass(frozen=True)
class FaultSpec:
    agent: str
    kind: str
    count: int | None = None

    @property
    def exception_type(self) -> type:
        return KINDS[self.kind]

    def matches(self, agent_name: str) -> bool:
        return fnmatch.fnmatch(agent_name, self.agent)

    def describe(self) -> str:
        who = "every agent" if self.agent == "*" else self.agent
        how_many = (
            "every provider attempt fails" if self.count is None
            else (f"the first {self.count} provider attempts fail"
                  if self.count != 1 else "the first provider attempt fails")
        )
        return f"{who}: {how_many} with {self.kind}"


def parse_spec(raw: str) -> FaultSpec:
    """Parse one `agent:kind[:count]` spec, or raise ValueError explaining it."""
    parts = [p.strip() for p in str(raw).split(":")]
    if len(parts) not in (2, 3) or not all(parts[:2]):
        raise ValueError(
            f"fault spec {raw!r} is not 'agent:kind' or 'agent:kind:count'"
        )
    agent, kind = parts[0], parts[1]
    if kind not in KINDS:
        raise ValueError(
            f"unknown fault kind {kind!r} in {raw!r}; "
            f"choose from {', '.join(sorted(KINDS))}"
        )
    count: int | None = None
    if len(parts) == 3:
        try:
            count = int(parts[2])
        except ValueError:
            raise ValueError(
                f"fault count {parts[2]!r} in {raw!r} is not a whole number"
            ) from None
        if count < 1:
            raise ValueError(f"fault count in {raw!r} must be 1 or more")
    return FaultSpec(agent=agent, kind=kind, count=count)


@dataclass
class ProviderFaultInjector:
    """Decides whether a given provider attempt should fail, and how.

    Counters are held per agent and guarded by a lock: specialists run
    concurrently, and a fault budget that could be double-spent by two
    threads would make a rehearsal non-deterministic -- the one property it
    exists to have.
    """

    specs: list[FaultSpec] = field(default_factory=list)
    injected: list[dict] = field(default_factory=list)
    _attempts: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_specs(cls, raw_specs) -> "ProviderFaultInjector":
        return cls(specs=[parse_spec(r) for r in (raw_specs or [])])

    @property
    def active(self) -> bool:
        return bool(self.specs)

    def check(self, agent_name: str, model: str) -> None:
        """Raise the configured fault if this attempt is due to fail.

        Called after the cost circuit has authorized the attempt, so a failure
        here is counted and priced exactly as a real provider's would be.
        """
        if not self.specs:
            return
        with self._lock:
            seen = self._attempts.get(agent_name, 0) + 1
            self._attempts[agent_name] = seen
            for spec in self.specs:
                if not spec.matches(agent_name):
                    continue
                if spec.count is not None and seen > spec.count:
                    continue
                record = {
                    "kind": "injected_provider_fault",
                    "agent": agent_name,
                    "detail": (
                        f"provider attempt {seen} for {agent_name} was FORCED "
                        f"to fail with {spec.kind} against {model} "
                        f"(rehearsal fault injection, not a real provider "
                        f"failure)"
                    ),
                    "fault_kind": spec.kind,
                    "attempt": seen,
                    "model": model,
                }
                self.injected.append(record)
                fault = spec.exception_type(
                    f"injected {spec.kind} on attempt {seen} for {agent_name}"
                )
                # Carried on the exception, not looked up from `injected[-1]`
                # afterwards: specialists run concurrently, and another
                # thread's fault can land in that list between the raise and
                # the handler.
                fault.record = record
                raise fault

    def summary(self) -> list[str]:
        return [spec.describe() for spec in self.specs]
