"""Run one complete trading session offline, against real components.

    python ops/rehearsal/run.py --session morning \\
        --source-db /home/qamc/quant-agent/data/quant_agent.db \\
        --source-data /home/qamc/quant-agent/data \\
        --sudo-user qamc --replay-run run-be9f8f06

What runs for real: the pipeline, the cost circuit, prompt assembly, the
portfolio constructor, the risk engine, the execution stage, the database
layer, and every line of `AlpacaBroker`.

What does not: the model providers (replayed from `agent_logs`), the two
alpaca-py SDK clients inside the broker (stubbed), and yfinance (no offline
source exists). Each is documented where it is defined, and each appears in
the report.

WHAT A REHEARSAL IS REWOUND TO
------------------------------
A rehearsal is a fresh session against a snapshot of production's state, not a
re-enactment of a past one. It gets its own run id, so its spending accrues to
its own session ledger rather than adding a second helping to the historical
row it is replaying. The day's already-recorded spending is inherited as-is,
which makes the rehearsal's budget position equal to or tighter than the real
one — the conservative direction, and the only one that cannot turn a real
block into a rehearsed pass.

PRICING-CACHE AGE IS AN INPUT, NOT AN INHERITANCE
-------------------------------------------------
Everything else in the sandbox is production's state as it stands. The
OpenRouter pricing cache is the one deliberate exception: its *age* is set by
`pricing_cache_age_hours` (default `DEFAULT_PRICING_CACHE_AGE_HOURS`, i.e.
fresh), not inherited from whenever production last happened to refresh it.

Until this was fixed the sandbox copy carried production's real mtime, so a
rehearsal run on a weekend — when nothing refreshes that file — could report
`paid_analysis_suspended` for a reason that had nothing whatever to do with
what the rehearsal was testing. `tests/test_rehearsal_reproduces_cost_ceiling.py`
exists to prove a specific spending-limit failure still reproduces on demand;
it must fail when that failure regresses, and for no other reason.

This is not a safety shortcut. Nothing about the circuit's fail-closed
behaviour changes — a rehearsal that ASKS for a stale cache (pass an age past
24h + the configured grace) still gets the suspension, and that is how the
staleness path itself is tested. What changed is that staleness became
something a rehearsal declares rather than something it catches from the
calendar.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger(__name__)

#: Age, in hours, the OpenRouter pricing cache is given inside a rehearsal
#: sandbox unless the caller asks for something else. Comfortably inside the
#: 24h freshness window with room to spare, so a rehearsal exercises the
#: normal priced path and never the grace band by accident. Not zero: a
#: zero-age file is not a state production can ever actually be in, and a
#: rehearsal should run against a plausible one.
DEFAULT_PRICING_CACHE_AGE_HOURS = 1.0

#: Name of the cache file inside the sandbox's data directory. Must match
#: `_OPENROUTER_CACHE_PATH` in src/cost_table.py.
PRICING_CACHE_FILENAME = "openrouter_pricing_cache.json"

SESSIONS = {
    "morning": "run_morning",
    "midday": "run_midday",
    "close": "run_close",
    "evening": "run_evening",
    "intra_check": "run_intra_check",
}


def _ensure_import_path() -> None:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))


class RehearsalNotifier:
    """Captures the alerts a session would have sent, instead of sending them.

    NOT a way to switch alerting off. `llm_cost_circuit.require_telegram_alerts`
    is a real safety requirement — a circuit that can trip silently is worse
    than no circuit — and disabling it to make a rehearsal run would be exactly
    the kind of shortcut this harness exists to catch. So the requirement is
    satisfied honestly: alerting is enabled, every message is captured, and the
    captured messages are printed with the report. More visible than Telegram,
    not less.
    """

    def __init__(self, *args, **kwargs):
        self.enabled = True
        self.sent: list[str] = []

    def send(self, message, *args, **kwargs) -> bool:
        self.sent.append(str(message))
        return True

    def __getattr__(self, name):
        # Any other notifier method a session reaches for records and succeeds.
        def _capture(*args, **kwargs):
            self.sent.append(f"{name}: {args and args[0] or ''}")
            return True
        return _capture


@contextmanager
def _rehearsal_notifier():
    import src.notifier as notifier_module

    captured = RehearsalNotifier()
    original = notifier_module.TelegramNotifier
    notifier_module.TelegramNotifier = lambda *a, **k: captured
    try:
        yield captured
    finally:
        notifier_module.TelegramNotifier = original


@contextmanager
def _metered_agents():
    """Force the real cost circuit on, even under the test conftest.

    `tests/conftest.py` sets `BaseAgent._allow_unmetered_for_tests = True` for
    the whole suite, and `TradingPipeline.__init__` reads that flag and skips
    building a cost circuit entirely. A rehearsal that inherited it would be
    running without the single component the 2026-08-28 failure lived in.
    """
    from src.agents.base import BaseAgent

    previous = BaseAgent._allow_unmetered_for_tests
    BaseAgent._allow_unmetered_for_tests = False
    try:
        yield
    finally:
        BaseAgent._allow_unmetered_for_tests = previous


@contextmanager
def _sentinel_credentials():
    """Put non-functional API keys in the environment for config loading."""
    from ops.rehearsal.isolation import SENTINEL_KEY

    names = (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
        "FRED_API_KEY",
    )
    previous = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ[name] = SENTINEL_KEY
    # Suppress operator alerts for the duration. A rehearsal replays a real
    # session and therefore raises real alerts; delivered unmarked to the
    # operator's normal chat they are indistinguishable from production. An
    # env var rather than config because it has to hold for any code path
    # that builds a notifier, including ones reading `.env` directly.
    previous_rehearsal = os.environ.get("QAMC_REHEARSAL")
    os.environ["QAMC_REHEARSAL"] = "1"
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if previous_rehearsal is None:
            os.environ.pop("QAMC_REHEARSAL", None)
        else:
            os.environ["QAMC_REHEARSAL"] = previous_rehearsal


# Placeholder for the pricing-cache note's reserved slot in `run_rehearsal`'s
# notes list. It is overwritten before the list is read; if this string ever
# reaches a report, the fill-in below `build_rehearsal_config` was skipped.
_PENDING_NOTE = "pricing-cache note not built"


def build_rehearsal_config(sandbox, *, base_settings: Path | None = None,
                           overrides: dict | None = None):
    """Write a sandbox settings.yaml and load it through the real loader.

    Isolation is achieved by configuration, not by forking code: the same
    `load_config` and the same `AppConfig` validators the production process
    uses. Only the paths and the credentials differ.
    """
    import yaml

    from src.config import load_config

    base_settings = base_settings or (PROJECT_ROOT / "config" / "settings.yaml")
    raw = yaml.safe_load(base_settings.read_text())

    raw.setdefault("storage", {})["db_path"] = str(sandbox.db_path)
    if "smart_money" in raw:
        raw["smart_money"]["data_dir"] = str(sandbox.data_dir / "smart_money")

    for dotted, value in (overrides or {}).items():
        node = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    config_dir = sandbox.root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "settings.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with _sentinel_credentials():
        return load_config(config_path)


def run_rehearsal(
    sandbox,
    *,
    session: str = "morning",
    now_et: datetime | None = None,
    replay_run: str | None = None,
    config_overrides: dict | None = None,
    fill_model: str = "immediate",
    production_db: str | Path | None = None,
    sudo_user: str | None = None,
    base_settings: Path | None = None,
    pricing_cache_age_hours: float | None = DEFAULT_PRICING_CACHE_AGE_HOURS,
    provider_faults=None,
):
    """Rehearse one session in `sandbox` and return a `RehearsalReport`.

    `pricing_cache_age_hours` is the age the sandbox's OpenRouter pricing
    cache is stamped with — see `apply_pricing_cache_age` and the module
    docstring. Pass a value past 24h + the configured grace to rehearse the
    fail-closed staleness path deliberately; pass `None` to inherit
    production's real mtime.

    `provider_faults` is an optional list of `agent:kind[:count]` specs (see
    `ops/rehearsal/faults.py`) that force provider attempts to fail. Without
    it a rehearsal can only replay responses that succeeded, which leaves the
    retry and cross-provider failover branches — and the circuit guards they
    cross — untestable outside a live session.
    """
    _ensure_import_path()

    from ops.rehearsal.broker import (
        BrokerSnapshot, blocked_market_data, install_rehearsal_broker,
    )
    from ops.rehearsal.clock import frozen_clock
    from ops.rehearsal.isolation import (
        ProductionWitness, assert_broker_is_stubbed, assert_isolated, no_network,
    )
    from ops.rehearsal.faults import ProviderFaultInjector
    from ops.rehearsal.replay import ResponseLibrary, replay_provider_calls
    from ops.rehearsal.report import collect

    if session not in SESSIONS:
        raise ValueError(
            f"unknown session '{session}'; expected one of {sorted(SESSIONS)}"
        )

    # Parsed before any sandbox work: a typo in a fault spec should cost a
    # message, not a database snapshot.
    injector = ProviderFaultInjector.from_specs(provider_faults)

    from src.trading_calendar import ET

    now_et = now_et or datetime.now(ET)
    if now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=ET)
    run_id = f"rehearsal-{session}-{now_et.strftime('%Y%m%d')}"

    notes = list(sandbox.notes)
    age_note = apply_pricing_cache_age(sandbox, pricing_cache_age_hours)
    if age_note:
        notes.append(age_note)
    # The pricing-cache note quotes the grace window this rehearsal actually
    # ran under, which only exists once `build_rehearsal_config` below has
    # written the sandbox's own config/settings.yaml. Built here, it read a
    # file that did not exist yet, fell through `_pricing_grace_hours`'s
    # except clause, and reported "0h grace" on every rehearsal regardless of
    # the real setting -- misdescribing any rehearsal running inside the
    # grace band. Its place in the ordering is reserved now and filled in
    # below, so the note keeps its position without being built too early.
    pricing_note_index = len(notes)
    notes.append(_PENDING_NOTE)

    library = ResponseLibrary.from_database(str(sandbox.db_path), run_id=replay_run)
    if not library.available():
        notes.append(
            "no recorded model responses matched the requested run — every "
            "agent call in this rehearsal will be reported as unanswerable"
        )
    else:
        counts = ", ".join(f"{k} x{v}" for k, v in sorted(library.available().items()))
        notes.append(f"recorded responses loaded: {counts}")

    # Stated in the report's own notes, not only in the log: a run whose
    # providers were forced to fail describes a hypothetical, and must never
    # read as an account of what production would have done unprompted.
    for line in injector.summary():
        notes.append(f"PROVIDER FAULT INJECTED — {line}")

    snapshot = BrokerSnapshot.from_database(str(sandbox.db_path), now_et.date())
    notes.extend(snapshot.notes)

    witness = ProductionWitness.watch(production_db, sudo_user=sudo_user)
    unavailable: list[str] = []
    network_attempts: list[str] = []
    result: dict = {}
    error: str | None = None
    started = time.monotonic()

    with ExitStack() as stack:
        stack.enter_context(sandbox.activate())
        config = build_rehearsal_config(
            sandbox, base_settings=base_settings, overrides=config_overrides,
        )
        # Now that the sandbox carries its own settings.yaml, the note can
        # state the real grace window instead of guessing at it.
        notes[pricing_note_index] = _pricing_cache_note(sandbox)
        checks = assert_isolated(sandbox, config, production_db=production_db)

        stack.enter_context(_metered_agents())
        captured_alerts = stack.enter_context(_rehearsal_notifier())
        stack.enter_context(frozen_clock(now_et, run_id=run_id))
        stack.enter_context(no_network(network_attempts))

        from src.pipeline import TradingPipeline

        pipeline = TradingPipeline(config)
        trading_stub = install_rehearsal_broker(
            pipeline.broker, snapshot, now=now_et, fill_model=fill_model,
        )
        pipeline.market = blocked_market_data(unavailable)
        checks.append(assert_broker_is_stubbed(pipeline.broker))
        checks.append(
            "no outbound network connection is possible for the duration of "
            "the session"
        )

        stack.enter_context(replay_provider_calls(library, faults=injector))
        try:
            result = getattr(pipeline, SESSIONS[session])() or {}
        except Exception as exc:  # a crash IS the finding; report it
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("Rehearsed session raised")
        finally:
            try:
                pipeline.db.close()
            except Exception:
                pass

    duration = time.monotonic() - started
    checks.append(witness.assert_untouched(sudo_user=sudo_user))

    for symbol in getattr(pipeline.broker._data_client, "missing_price_symbols", []):
        unavailable.append(f"a current price for {symbol}")
    report = collect(
        session=session,
        rehearsed_date=now_et.date().isoformat(),
        run_id=run_id,
        source_run_id=replay_run,
        result=result,
        db_path=str(sandbox.db_path),
        library=library,
        trading_stub=trading_stub,
        isolation_checks=checks,
        unavailable=unavailable,
        network_attempts=network_attempts,
        notes=notes,
        fill_model=fill_model,
        duration_s=duration,
        error=error,
    )
    if captured_alerts.sent:
        report.notes.append(
            f"{len(captured_alerts.sent)} operator alert(s) were raised and "
            f"captured instead of sent: "
            + " | ".join(a.splitlines()[0][:120] for a in captured_alerts.sent[:4])
        )
    return report


def apply_pricing_cache_age(sandbox, age_hours: float | None) -> str | None:
    """Give the sandbox's OpenRouter pricing cache a chosen age. Returns a note.

    `age_hours=None` means "leave production's real mtime alone" — the old
    behaviour, kept because "what would the desk do with the cache exactly as
    it stands right now" is a legitimate diagnostic question. It is not the
    default, because as a default it made every rehearsal's verdict depend on
    when a paid session last happened to run (see the module docstring).

    Refuses to touch anything that is not inside the sandbox. The production
    cache's mtime is load-bearing for the live cost circuit; a harness that
    could reach it would be able to make production's pricing look current
    when it is not, which is the one thing this file must never do.
    """
    cache = sandbox.data_dir / PRICING_CACHE_FILENAME
    if age_hours is None:
        return (
            "pricing-cache age: inherited from the production copy (explicitly "
            "requested) — this rehearsal's pricing verdict depends on when a "
            "paid session last refreshed that file"
        )
    if not cache.exists():
        return None  # `_pricing_cache_note` already reports the absence
    if not sandbox.contains(cache):
        raise ValueError(
            f"refusing to set the mtime of {cache}, which is outside the "
            f"rehearsal sandbox {sandbox.root}"
        )
    if age_hours < 0:
        raise ValueError(f"pricing cache age must not be negative, got {age_hours}")
    stamp = time.time() - age_hours * 3600
    os.utime(cache, (stamp, stamp))
    return (
        f"pricing-cache age: set to {age_hours:g}h by the harness, not "
        f"inherited from production's copy"
    )


def _pricing_cache_note(sandbox) -> str:
    """State the pricing-provenance position rather than assuming it."""
    cache = sandbox.data_dir / PRICING_CACHE_FILENAME
    if not cache.exists():
        return (
            "no OpenRouter pricing cache in the sandbox — the cost circuit "
            "cannot confirm current rates offline and will suspend paid "
            "analysis, which is the correct fail-closed behaviour but means "
            "this rehearsal stops before any model call"
        )
    age_h = (time.time() - cache.stat().st_mtime) / 3600
    # The boundaries below are read from config rather than hardcoded at 24h.
    # Until 2026-08-28 a cache over 24h old suspended paid analysis outright,
    # and this note said so. That policy has since been graduated: a stale
    # cache within `openrouter_pricing_grace_period_hours` is now USED, with
    # the reservation multiplier widened in proportion to its age, and only a
    # cache past the grace window (or absent, or missing a rate for a model in
    # use) still fails closed. A note that kept quoting a flat 24h would have
    # been wrong for the whole grace band — the exact kind of stale-by-hand
    # claim this project has been bitten by repeatedly.
    #
    # The age itself is whatever `apply_pricing_cache_age` stamped on the
    # sandbox copy (a rehearsal input, reported in its own note), or
    # production's real mtime when the caller explicitly asked to inherit it.
    # Either way this reads the file as it stands and states the consequence;
    # it never adjusts anything to reach a nicer verdict.
    grace_h = _pricing_grace_hours(sandbox)
    if age_h >= 24 + grace_h:
        return (
            f"the OpenRouter pricing cache is {age_h:.0f}h old — past the "
            f"{24 + grace_h:.0f}h limit (24h fresh + {grace_h:.0f}h grace). "
            f"The cost circuit will treat pricing as unconfirmed and suspend "
            f"paid analysis, which is the correct fail-closed behaviour"
        )
    if age_h >= 24:
        return (
            f"the OpenRouter pricing cache is {age_h:.0f}h old — stale but "
            f"inside the {grace_h:.0f}h grace window, so paid analysis "
            f"proceeds with a widened reservation multiplier"
        )
    return f"OpenRouter pricing cache is {age_h:.0f}h old (fresh, under 24h)"


def _pricing_grace_hours(sandbox) -> float:
    """The configured grace window, read from the sandbox's own settings so
    the note describes the policy this rehearsal actually ran under rather
    than a number written here by hand.

    Raises when the sandbox has no settings.yaml at all. That is not a
    policy question with a safe default — it is the caller running before
    `build_rehearsal_config` wrote the file, which is exactly the bug this
    guard exists to stop coming back: the old code caught it in the `except`
    below and reported "0h grace" on every rehearsal, indistinguishable from
    a genuinely configured zero. A rehearsal harness that quietly misstates
    the policy it ran under is worse than one that stops.
    """
    settings = sandbox.root / "config" / "settings.yaml"
    if not settings.is_file():
        raise RuntimeError(
            f"{settings} does not exist yet — the pricing-cache note quotes "
            "the configured grace window and must be built after "
            "build_rehearsal_config has written the sandbox's settings. "
            "Reporting a fallback here would silently misdescribe the policy "
            "this rehearsal ran under."
        )
    try:
        import yaml
        cfg = yaml.safe_load(settings.read_text())
        return float(
            (cfg.get("llm_cost_circuit") or {}).get(
                "openrouter_pricing_grace_period_hours", 0.0
            )
        )
    except Exception:
        # The file exists but carries no readable value: describe the strict
        # policy — never claim more tolerance than we can prove is
        # configured.
        return 0.0
