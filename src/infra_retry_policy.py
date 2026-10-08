"""The desk's transient-fault retry policy, in a leaf module anything may import.

Single home of the three numbers: `LLMCostCircuitConfig` takes its defaults from
here and the owner-alert sender reads them from here, so neither pulls the
config package (and the agents package behind it) into the notifier. Plain
constants, not a pydantic model: the bounds already live on the consuming
`Field(...)`s, and this module must stay import-free.

They are not invented: each equals the macro data provider's transient-fault
default (`MacroConfig.max_retries`, `MacroConfig.retry_backoff_base_s`,
`MacroConfig.retry_backoff_max_s`, used by `MacroDataProvider._next_backoff`).
Each has its own row in config/number_ledger.yaml. Since 2026-10-08 they also pace
the live-price read and the today-print re-ask, so they decide when an entry is
skipped: trade-governing, and ledgered as such with the owner's pacing ruling.
"""

MAX_RETRIES = 2
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 8.0

# Default HTTP timeout for ALL Alpaca SDK calls (connect, read).
# Without this, a stalled TCP connection to the broker can hang the process
# for hours under launchd — observed 2026-04-17 when the evening job sat for
# 13+ hours at the very first broker call.
_BROKER_HTTP_TIMEOUT = 30.0
# Lives here, not in src.execution, since 2026-10-08: the price-feed preflight
# needs the worst case of one read, and the broker seam forbids it importing
# src.execution. src/execution/broker_parts/http_timeout.py re-exports it.


def _backoff_s(attempt: int) -> float:
    return min(BACKOFF_BASE_S * (2 ** (attempt - 1)), BACKOFF_MAX_S)


def read_worst_case_s() -> float:
    """Longest one `read_price_with_retry` call can take: every attempt runs
    to the broker's ledgered HTTP timeout, plus the policy's backoff between
    attempts. Built only from ledgered numbers; it adds none of its own."""
    attempts = 1 + MAX_RETRIES
    return attempts * _BROKER_HTTP_TIMEOUT + sum(_backoff_s(a) for a in range(1, attempts))
