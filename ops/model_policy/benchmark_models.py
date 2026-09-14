#!/usr/bin/env python3
"""Benchmark OpenRouter models on QAMC's own agent tasks.

    python ops/model_policy/benchmark_models.py --from-onecli --repeats 2 --budget-usd <USD>
    python ops/model_policy/benchmark_models.py --models qwen/qwen3.7-flash --scenario tech_batch --budget-usd <USD>
    python ops/model_policy/benchmark_models.py --report ops/model_policy/results/latest.json

Every candidate is driven through the REAL agent classes with the REAL
prompts (see `scenarios.py` for why), and graded by deterministic Python
assertions. The output is decision-quality-per-dollar, not token price:

    quality      = weighted mean of the scenario's graded checks, 0..1
    cost         = measured tokens x OpenRouter's own published rate
    quality/$    = quality / cost, the number the policy is chosen on

Cost uses OpenRouter's `/api/v1/models` pricing rather than LiteLLM: we pay
OpenRouter's rate for OpenRouter-routed traffic, and their catalog is the
only source that is definitionally correct about that.

Credentials: this never holds a real key. It sends the same
`placeholder-managed-by-onecli` stand-in the commissioning preflight uses
and lets the OneCLI gateway substitute the real value server-side, so a
benchmark log can never contain a credential. `--from-onecli` resolves the
gateway wiring the same way `ops/commissioning/verify_commissioning.py`
does; without it, the process environment must already carry it (the
runtime account's normal state).

Runs cost real money — a full sweep is roughly 5 scenarios x N models x
repeats LLM calls. Nothing here places an order or touches the broker.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import copy
import hashlib
import importlib
import json
import os
import signal
import statistics
import sys
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ops.model_policy.scenarios import (  # noqa: E402
    Check, DEFAULT_SCENARIOS, Scenario, SCENARIOS, SCENARIOS_BY_KEY,
)

ONECLI_DASHBOARD = "http://127.0.0.1:10254/api/container-config"
OPENROUTER_CATALOG = "https://openrouter.ai/api/v1/models"
PLACEHOLDER_KEY = "placeholder-managed-by-onecli"

# A model id prefixed with this routes the trial over Google AI Studio
# direct (src/agents/base.py's `provider="google"` OpenAI-wire path)
# instead of OpenRouter — e.g. "google-direct:gemini-3.5-flash-lite". This
# is the owner requirement (2026-09-14): the benchmark must be able to test
# a model on the EXACT route the live desk uses for it, not always assume
# OpenRouter. The bare id after the prefix is passed to the agent
# unmodified; credentials still come from the same OneCLI/.env placeholder
# wiring (GOOGLE_API_KEY), never printed.
GOOGLE_DIRECT_PREFIX = "google-direct:"


def parse_benchmark_model(model: str) -> tuple[str, str]:
    """(effective_model_id, provider) for a `--models` entry.

    Strips `GOOGLE_DIRECT_PREFIX` when present and routes to "google";
    otherwise unchanged and routed to "openrouter", exactly as before this
    prefix existed.
    """
    if model.startswith(GOOGLE_DIRECT_PREFIX):
        return model[len(GOOGLE_DIRECT_PREFIX):], "google"
    return model, "openrouter"

BASELINE_MODEL = "openai/gpt-5.5"

# The candidate slate. Chosen from a full sweep of OpenRouter's catalog
# (410 models on 2026-08-12) by: text-capable, >= 128K context (QAMC's
# tech_analyst batches are large), priced, non-`:free` (free tiers are
# rate-limited and can be withdrawn without notice, which is not an
# acceptable dependency for a trading session), and either a current
# flagship or a current cost-efficient workhorse. The Qwen and DeepSeek
# entries the work contract names explicitly are all here.
DEFAULT_CANDIDATES = [
    BASELINE_MODEL,
    # --- frontier / near-frontier, for the decision seats ---
    "deepseek/deepseek-v4-pro-0813",
    "qwen/qwen3.7-max",
    "qwen/qwen3.7-plus",
    "z-ai/glm-5.2",
    "openai/gpt-5.6-luna",
    "minimax/minimax-m3",
    # --- cost-efficient workhorses, for the specialist seats ---
    "deepseek/deepseek-v4-flash-0731",
    "qwen/qwen3.7-flash",
    "qwen/qwen3-235b-a22b-2507",
    "openai/gpt-5-nano",
    "google/gemini-2.5-flash-lite",
]


# --------------------------------------------------------------------------
# Gateway wiring + pricing
# --------------------------------------------------------------------------


def wire_from_onecli() -> str:
    """Point this process at the OneCLI credential gateway.

    Same shape as `ops/commissioning/verify_commissioning.py:resolve_wiring`
    and `ops/onecli/README.md` step 4b: read the container-config, rewrite
    `host.docker.internal` to loopback (bare processes are not in a
    container), write the CA to a 0600 temp file removed on exit.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(ONECLI_DASHBOARD, timeout=15) as resp:
        cfg = json.loads(resp.read().decode())
    proxy = (cfg.get("env", {}).get("HTTPS_PROXY") or "").replace(
        "host.docker.internal", "127.0.0.1"
    )
    cert = cfg.get("caCertificate") or ""
    if not proxy or not cert:
        raise SystemExit("onecli container-config lacked a proxy URL or CA certificate")
    fd, path = tempfile.mkstemp(prefix="qamc-bench-ca-", suffix=".pem")
    with os.fdopen(fd, "w") as fh:
        fh.write(cert)
    os.chmod(path, 0o600)
    atexit.register(lambda: os.path.exists(path) and os.unlink(path))
    os.environ["HTTPS_PROXY"] = proxy
    os.environ["SSL_CERT_FILE"] = path
    os.environ["REQUESTS_CA_BUNDLE"] = path
    return "onecli container-config"


def openrouter_pricing() -> dict[str, dict[str, float]]:
    """{model_id: {'input': $/M, 'output': $/M}} straight from the catalog."""
    opener = urllib.request.build_opener()
    with opener.open(OPENROUTER_CATALOG, timeout=30) as resp:
        data = json.loads(resp.read().decode())["data"]
    out: dict[str, dict[str, float]] = {}
    for m in data:
        p = m.get("pricing") or {}
        try:
            inp = float(p.get("prompt") or 0) * 1e6
            outp = float(p.get("completion") or 0) * 1e6
        except (TypeError, ValueError):
            continue
        if inp > 0 and outp > 0:
            out[m["id"]] = {"input": inp, "output": outp}
    return out


# --------------------------------------------------------------------------
# Running one (model, scenario) pair
# --------------------------------------------------------------------------


@dataclass
class Trial:
    model: str
    scenario: str
    role: str
    ok: bool
    quality: float
    checks: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_s: float = 0.0
    error: str = ""
    # First LLM response, truncated. A score is only auditable if a reviewer
    # can see what the model actually said — "qwen scored 0.4 on risk" is an
    # assertion; the verdict text next to it is evidence. Bounded so the
    # results file stays reviewable, and it holds market opinions about
    # synthetic tickers, never a credential.
    sample_output: str = ""
    # What ACTUALLY answered, per AgentResult. If this ever differs from
    # `model`, the trial silently benchmarked something else and the row
    # must not be read as evidence about `model`.
    actual_model: str = ""
    used_fallback: bool = False
    # WHICH symbols this run proposed, as "ACTION:SYMBOL", sorted.
    # quality says whether a run was VALID; this says whether two runs
    # AGREED. A seat that scores 1.00 twice while naming disjoint books is
    # not a stable seat, and the score alone cannot show that.
    picks: list[str] = field(default_factory=list)
    # "run" = the model was called and graded. STATUS_SKIPPED_BUDGET = the
    # benchmark's own budget was exhausted before this trial started, so the
    # model was NEVER CALLED. A skipped trial is not a 0.00 score and is
    # excluded from every quality/cost aggregate. Defaulted so results files
    # written before this field existed still load.
    status: str = "run"
    # The uniform-testing settings actually sent with this trial's request
    # (src/agents/base.py's BaseAgent.result_model / _openai_wire_call) — so
    # a results file is self-describing evidence of what was measured,
    # rather than requiring a reader to cross-check the benchmark's source
    # against whatever config.llm defaults were live at run time.
    reasoning_effort: str = "medium"
    structured_output: bool = True
    # Which route this trial actually used ("openrouter" or "google") — the
    # owner requirement (2026-09-14) is that a model is tested under the
    # SAME route the live desk uses for it, so the results file must say
    # which route produced each row rather than assuming openrouter.
    provider: str = "openrouter"


STATUS_RUN = "run"
STATUS_SKIPPED_BUDGET = "skipped_budget"


def skipped_trial(scenario: Scenario, model: str) -> Trial:
    return Trial(
        model=model, scenario=scenario.key, role=scenario.role, ok=False,
        quality=0.0, status=STATUS_SKIPPED_BUDGET,
        error="NOT RUN: benchmark budget exhausted before this trial",
    )


# --------------------------------------------------------------------------
# The benchmark's own budget (2026-09-14)
# --------------------------------------------------------------------------
#
# Until this fix the benchmark built its breaker from the LIVE desk's
# `llm_cost_circuit` block, so it inherited a trading session's
# `session_cost_limit_usd` and `max_calls_per_session`. A six-model run spent
# past that session cap, tripped `PaidAnalysisSuspended`, and every later
# trial errored and was scored 0.00. The live desk's caps are correct for a
# trading session and are NOT changed here: the benchmark now builds a
# modified COPY of that config for its own breaker only.

# Worst-case LOGICAL calls one trial can make, each counted by
# `LLMCostCircuitBreaker.begin_call` against `max_calls_per_session`:
#   1 primary call (every scenario is one chunk: tech_batch is 3 symbols,
#     tech_batch_full is 25 = one `_CHUNK_SIZE` chunk)
#   + 1 `schema_repair` (`BaseAgent`'s single repair reprompt)
#   + `_MAX_MISSING_RETRIES` per-chunk missing-symbol recoveries (tech)
#   + 1 consolidated cross-chunk recovery (tech `analyze_batch`)
# Provider-level retries/failover inside one logical call are bounded by
# `max_provider_attempts_per_call`, which is left exactly as configured.
def logical_calls_per_trial_worst() -> int:
    from src.agents.tech_analyst import _MAX_MISSING_RETRIES
    return 1 + 1 + int(_MAX_MISSING_RETRIES) + 1


def positive_budget(value: str) -> float:
    try:
        budget = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--budget-usd must be a number, got {value!r}")
    if not (budget > 0 and budget != float("inf")):  # also rejects nan
        raise argparse.ArgumentTypeError(
            f"--budget-usd must be a positive, finite dollar amount, got {value!r}"
        )
    return budget


def benchmark_circuit_config(live_cfg, *, budget_usd: float, planned_trials: int):
    """A COPY of the live `llm_cost_circuit` config with the benchmark's caps.

    Only `session_cost_limit_usd` and `max_calls_per_session` differ. The
    daily cap, Telegram requirement, attempt ceilings and pricing grace are
    inherited unchanged. `live_cfg` is never mutated.
    """
    daily_limit = float(live_cfg.daily_cost_limit_usd)
    if float(budget_usd) > daily_limit:
        # `LLMCostCircuitConfig` itself rejects session > daily, and the
        # breaker's settled daily check would stop the run there anyway.
        # Fail before any paid call; the daily cap is never raised here.
        raise SystemExit(
            f"REFUSING TO START: --budget-usd ${float(budget_usd):.2f} is above the "
            f"live daily LLM cap ${daily_limit:.2f}, which the benchmark shares and "
            f"does not raise. Use a budget at or below the daily cap."
        )
    update = {
        "session_cost_limit_usd": float(budget_usd),
        "max_calls_per_session": max(1, int(planned_trials)) * logical_calls_per_trial_worst(),
    }
    if hasattr(live_cfg, "model_dump"):
        # Re-validate so the copy obeys the same field constraints.
        return type(live_cfg).model_validate({**live_cfg.model_dump(), **update})
    clone = copy.copy(live_cfg)
    for key, value in update.items():
        setattr(clone, key, value)
    return clone


def with_circuit_config(app_config, circuit_cfg):
    """A shallow copy of `app_config` carrying `circuit_cfg`; original untouched."""
    if hasattr(app_config, "model_copy"):
        return app_config.model_copy(update={"llm_cost_circuit": circuit_cfg})
    clone = copy.copy(app_config)
    clone.llm_cost_circuit = circuit_cfg
    return clone


def build_benchmark_circuit(app_config, *, budget_usd: float, planned_trials: int,
                            run_id: str, notifier=None, db_path=None):
    from src.cost_circuit import activate_paid_call_session
    cfg = benchmark_circuit_config(
        app_config.llm_cost_circuit, budget_usd=budget_usd,
        planned_trials=planned_trials,
    )
    return activate_paid_call_session(
        with_circuit_config(app_config, cfg),
        run_id=run_id, mode="benchmark", notifier=notifier, db_path=db_path,
    )


# Used only when no committed result has measured a scenario's tokens.
FALLBACK_CHARS_PER_TOKEN = 4.0


def token_assumptions(scenarios, results_dir: Path | None = None) -> dict[str, dict]:
    """Per-scenario {input, output, source} token assumption for the estimate.

    Taken as the LARGEST input and output token counts any committed trial
    of that scenario has measured (any model), so the estimate is read from
    this harness's own data rather than invented. A scenario with no
    measured trial falls back to prompt bytes / FALLBACK_CHARS_PER_TOKEN for
    input and the scenario's output cap for output, labelled UNMEASURED.
    """
    results_dir = results_dir or (PROJECT_ROOT / "ops" / "model_policy" / "results")
    seen: dict[str, list[int]] = {}
    for path in sorted(results_dir.glob("*.json")):
        try:
            trials = json.loads(path.read_text()).get("trials") or []
        except (OSError, ValueError):
            continue
        for t in trials:
            if t.get("status", STATUS_RUN) != STATUS_RUN:
                continue
            m = seen.setdefault(t.get("scenario", ""), [0, 0])
            m[0] = max(m[0], int(t.get("input_tokens") or 0))
            m[1] = max(m[1], int(t.get("output_tokens") or 0))
    out = {}
    for s in scenarios:
        inp, outp = seen.get(s.key, [0, 0])
        if inp or outp:
            out[s.key] = {"input": inp, "output": outp,
                          "source": "max measured in committed results"}
        else:
            try:
                prompt_bytes = (PROJECT_ROOT / f"config/prompts/{s.role}.md").stat().st_size
            except OSError:
                prompt_bytes = 0
            out[s.key] = {"input": int(prompt_bytes / FALLBACK_CHARS_PER_TOKEN),
                          "output": int(s.max_tokens),
                          "source": "UNMEASURED: prompt bytes/4 + output cap"}
    return out


def estimate_costs(models, scenarios, repeats: int, pricing: dict,
                   tokens: dict[str, dict]) -> dict[str, float | None]:
    """{model: estimated USD for its trials}, None when the model is unpriced."""
    out: dict[str, float | None] = {}
    for model in models:
        rates = pricing.get(model)
        if not rates:
            out[model] = None
            continue
        per = sum(
            (tokens[s.key]["input"] * rates["input"]
             + tokens[s.key]["output"] * rates["output"]) / 1e6
            for s in scenarios
        )
        out[model] = per * repeats
    return out


def daily_cap_problem(status: dict, daily_limit_usd: float,
                      planned_spend_usd: float) -> str | None:
    """Why the live daily cap would stop this run, or None.

    The breaker checks `daily >= daily_cost_limit_usd` against SETTLED spend
    before every call, and the benchmark's spend is written to the same
    day row as the desk's. A run that needs more than the remaining daily
    headroom would therefore trip mid-run after spending. Refuse up front.
    """
    daily = float(status.get("current_daily_cost_usd") or 0.0)
    headroom = float(daily_limit_usd) - daily
    if planned_spend_usd > headroom:
        return (
            f"daily LLM cap ${float(daily_limit_usd):.2f} has ${max(headroom, 0):.4f} "
            f"left today (${daily:.4f} already spent) but this run plans up to "
            f"${planned_spend_usd:.4f}. Not starting. Lower --budget-usd or run "
            f"another day; the daily cap is not raised by the benchmark."
        )
    return None


def run_sweep(models, scenarios, repeats: int, *, budget_usd: float,
              run_one, budget_state, log=None) -> tuple[list[Trial], bool]:
    """Run every planned trial until the budget is reached.

    `run_one(scenario, model) -> Trial`. `budget_state() -> (session_spent_usd,
    circuit_suspended)`. Once spend reaches the budget, or the breaker
    suspends paid calls, every remaining trial is recorded as
    STATUS_SKIPPED_BUDGET without calling the model. A trial that the breaker
    refused before any tokens were metered is also NOT RUN, not a failure.
    """
    log = log or (lambda msg: print(msg, file=sys.stderr))
    trials: list[Trial] = []
    total = len(models) * len(scenarios) * repeats
    stopped = False
    n = 0
    for model in models:
        for scenario in scenarios:
            for _ in range(repeats):
                n += 1
                if stopped:
                    trials.append(skipped_trial(scenario, model))
                    continue
                trial = run_one(scenario, model)
                spent, suspended = budget_state()
                if suspended and not (trial.input_tokens or trial.output_tokens):
                    trial = skipped_trial(scenario, model)
                trials.append(trial)
                log(f"[{n}/{total}] {model} :: {scenario.key} "
                    + ("NOT RUN (budget)" if trial.status != STATUS_RUN else
                       f"q={trial.quality:.2f} "
                       + (f"${trial.cost_usd:.4f} " if trial.cost_usd is not None else "$? ")
                       + f"{trial.latency_s:.0f}s"
                       + (f" ERR {trial.error[:90]}" if trial.error else "")))
                if spent >= budget_usd or suspended:
                    stopped = True
                    log(f"BUDGET STOP: session spend ${spent:.4f} of ${budget_usd:.2f}"
                        + (" (cost circuit suspended)" if suspended else "")
                        + f"; remaining {total - n} trial(s) marked NOT RUN")
    return trials, stopped


_REQUIRED_ENV_KEYS = (
    "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "FRED_API_KEY",
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
    # Needed only for a "google-direct:" model entry, but listed here (like
    # the other provider keys) so a missing value forces the same .env
    # placeholder load rather than failing deep inside agent construction.
    "GOOGLE_API_KEY",
)


def load_env_if_keys_missing(env_path: Path | None = None) -> bool:
    """Load `.env` when a required key variable is unset or empty.

    Same line-based loader as `scripts/export_alpaca_trades.py` (no
    python-dotenv). Existing environment wins. Values are OneCLI
    placeholders; they are never printed. Returns True if the file was read.
    """
    if all(os.environ.get(k) for k in _REQUIRED_ENV_KEYS):
        return False
    env_path = env_path or (PROJECT_ROOT / ".env")
    if not env_path.exists():
        return False
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and not os.environ.get(k):
            os.environ[k] = v
    return True


def _load_agent_cls(path: str):
    module_name, cls_name = path.split(":")
    return getattr(importlib.import_module(module_name), cls_name)


class TrialTimeout(BaseException):
    """Raised when a trial exceeds the session budget.

    Deliberately a BaseException, not an Exception: `BaseAgent._execute`
    catches `Exception` broadly and would classify this as a transient
    failure and retry it, which is precisely the runaway the deadline
    exists to stop.
    """


# A trading session is not open-ended: `scripts/run_if_et_window.sh` wraps
# each run in a hard kill, and an agent that has not answered inside that
# window is unusable at that seat no matter how well it scores.
#
# This deadline matters more than it looks. `_call_openai` STREAMS (see the
# docstring there — it is how QAMC survives a Cloudflare 524 relay), which
# makes `_LLM_HTTP_TIMEOUT` a per-CHUNK read timeout rather than a total
# one. A slow reasoning model that keeps trickling tokens therefore runs
# unbounded inside a single attempt, and `_DEFAULT_RETRY_DEADLINE_S` never
# sees it because that only applies BETWEEN attempts. Without this, one
# pathological candidate stalls the whole sweep — observed on the first
# run, where a single tech_batch call passed 15 minutes with no timeout.
TRIAL_DEADLINE_S = 420.0


@contextlib.contextmanager
def trial_deadline(seconds: float):
    """SIGALRM-based wall-clock cap around one trial.

    A watchdog thread cannot help here: the call is blocked in a socket read
    inside C, and Python threads have no way to interrupt that. A signal
    delivered to the main thread does.
    """
    def _fire(_signum, _frame):
        raise TrialTimeout(f"exceeded {seconds:.0f}s trial deadline")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class _Meter:
    """Captures token usage from the agent's own AgentResult.

    BaseAgent already records exact input/output tokens and hands them back
    on every AgentResult, so the benchmark reads the same numbers the
    production telemetry would log — no parallel accounting, no estimate.
    """

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.sample_output = ""
        self.actual_model = ""
        self.used_fallback = False

    def observe(self, result) -> None:
        if result is None:
            return
        self.input_tokens += getattr(result, "input_tokens", 0) or 0
        self.output_tokens += getattr(result, "output_tokens", 0) or 0
        if not self.sample_output:
            self.sample_output = (getattr(result, "raw_text", "") or "")[:1500]
        self.actual_model = getattr(result, "model", "") or self.actual_model
        self.used_fallback = self.used_fallback or bool(
            getattr(result, "used_fallback", False)
        )


def _extract_picks(output) -> list[str]:
    """The book a trial proposed, flattened to sorted "ACTION:SYMBOL" strings.

    Deliberately duck-typed over the parsed agent output rather than keyed to
    one agent class: `decisions` (constructed orders) is preferred over
    `targets` (pre-construction intent) because it is what would reach the
    broker. Any shape it does not recognise yields [], never an exception —
    a benchmark must not fail because it could not summarise a result.
    """
    if output is None:
        return []
    try:
        decisions = getattr(output, "decisions", None)
        if decisions:
            return sorted(
                f"{getattr(d, 'action', '?')}:{str(getattr(d, 'symbol', '?')).upper()}"
                for d in decisions
            )
        targets = getattr(output, "targets", None)
        if targets:
            return sorted(
                f"TARGET:{str(getattr(x, 'symbol', '?')).upper()}" for x in targets
            )
    except Exception:
        return []
    return []


def run_trial(scenario: Scenario, model: str, pricing: dict, cost_circuit=None) -> Trial:
    agent_cls = _load_agent_cls(scenario.agent_path)
    effective_model, provider = parse_benchmark_model(model)
    agent = agent_cls(
        api_key=PLACEHOLDER_KEY,
        model=effective_model,
        max_tokens=scenario.max_tokens,
        provider=provider,
    )
    if cost_circuit is not None:
        agent.set_cost_circuit(cost_circuit)

    meter = _Meter()
    # Wrap _execute so every underlying call is metered even for agents that
    # make several (tech_analyst chunks). Wrapping the lowest common call
    # rather than each entry point keeps this honest as agents change.
    original_execute = agent._execute

    def metered(user_message: str, **kwargs):
        result = original_execute(user_message, **kwargs)
        meter.observe(result)
        return result

    agent._execute = metered  # type: ignore[method-assign]

    started = time.monotonic()
    error = ""
    output = None
    try:
        with trial_deadline(TRIAL_DEADLINE_S):
            output = scenario.invoke(agent)
    except TrialTimeout as exc:
        # Scores 0, and that is the right answer rather than a gap in the
        # table: "could not answer inside the session budget" is a
        # disqualifying result for a seat, not missing data.
        error = f"TrialTimeout: {exc}"
    except Exception as exc:  # a model that cannot complete the call scores 0
        error = f"{type(exc).__name__}: {str(exc)[:240]}"
    latency = time.monotonic() - started

    try:
        checks = scenario.grade(output)
    except Exception as exc:
        checks = [Check("grading_crashed", 1.0, False, f"{type(exc).__name__}: {exc}")]

    total_weight = sum(c.weight for c in checks) or 1.0
    quality = sum(c.weight for c in checks if c.passed) / total_weight

    # Google direct is a free tier, not in the OpenRouter catalog `pricing`
    # is keyed from — looked up by the bare id so a future priced entry
    # would still be picked up, but today this is always a miss and cost
    # stays None ("$?"), same as any other unpriced model.
    rates = pricing.get(effective_model)
    cost = None
    if rates and (meter.input_tokens or meter.output_tokens):
        cost = (
            meter.input_tokens * rates["input"] + meter.output_tokens * rates["output"]
        ) / 1e6
    # A trial that timed out or raised never returned an AgentResult, so no
    # usage was metered — but tokens WERE spent. Reporting $0.0000 there
    # would understate the true cost of a model that fails slowly, which is
    # exactly the model this table needs to warn about. `None` renders "$?"
    # and keeps it out of the averages.

    return Trial(
        model=model,
        scenario=scenario.key,
        role=scenario.role,
        ok=not error,
        quality=round(quality, 4),
        checks=[asdict(c) for c in checks],
        input_tokens=meter.input_tokens,
        output_tokens=meter.output_tokens,
        cost_usd=cost,
        latency_s=round(latency, 2),
        error=error,
        sample_output=meter.sample_output,
        actual_model=meter.actual_model,
        used_fallback=meter.used_fallback,
        picks=_extract_picks(output),
        reasoning_effort=agent._reasoning_effort,
        structured_output=agent._structured_output and agent.result_model is not None,
        provider=provider,
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _prompt_conflicts(sources: list[dict]) -> list[str]:
    """Roles whose prompt sha differs between the files being merged."""
    by_role: dict[str, dict[str, list[str]]] = {}
    for src in sources:
        for role, fp in (src.get("prompts") or {}).items():
            sha = fp.get("sha256")
            if sha:
                by_role.setdefault(role, {}).setdefault(sha, []).append(src["path"])
    out = []
    for role, shas in sorted(by_role.items()):
        if len(shas) > 1:
            detail = "; ".join(
                f"{sha[:12]} <- {', '.join(paths)}" for sha, paths in shas.items()
            )
            out.append(f"{role}: {detail}")
    return out


def prompt_fingerprints(scenarios) -> dict:
    """Record WHICH PROMPT produced these scores.

    The harness drives the real agent classes, which load
    `config/prompts/<role>.md` from disk. The prompt is therefore an INPUT
    to every grade, not a constant — and until 2026-09-01 no result file
    recorded which version it was. That is how a full set of PM
    model-selection numbers stayed on disk for a week after the prompt they
    were measured against had been rewritten, with nothing to reveal it.
    Nobody was careless; the file simply could not be asked the question.

    A missing or unreadable prompt is recorded as an explicit error rather
    than omitted, because a silently absent fingerprint reproduces the exact
    failure this exists to prevent.
    """
    out: dict[str, dict] = {}
    for role in sorted({s.role for s in scenarios}):
        rel = f"config/prompts/{role}.md"
        path = PROJECT_ROOT / rel
        try:
            raw = path.read_bytes()
        except OSError as exc:
            out[role] = {"path": rel, "error": str(exc)}
            continue
        out[role] = {
            "path": rel,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "lines": raw.count(b"\n") + (0 if raw.endswith(b"\n") else 1),
        }
    return out


def aggregate(trials: list[Trial]) -> dict:
    """Per (model, scenario) means, then per-model rollups.

    NOT-RUN trials (budget skips) never enter a mean. A pair or model with
    no run trial is published with `not_run: True` and null quality, and
    renders as NOT RUN — a model that was never called has no score.
    """
    all_trials = trials
    trials = [t for t in all_trials if t.status == STATUS_RUN]
    by_pair: dict[tuple[str, str], list[Trial]] = {}
    for t in trials:
        by_pair.setdefault((t.model, t.scenario), []).append(t)

    pairs = {}
    for t in all_trials:
        key = (t.model, t.scenario)
        if key in by_pair:
            continue
        pairs[f"{t.model}|{t.scenario}"] = {
            "model": t.model, "scenario": t.scenario, "role": t.role,
            "runs": 0, "not_run": True, "quality_mean": None,
            "quality_min": None, "cost_mean": None, "latency_mean": None,
            "errors": [], "misattributed": [],
        }
    for (model, scenario), group in by_pair.items():
        costs = [t.cost_usd for t in group if t.cost_usd is not None]
        pairs[f"{model}|{scenario}"] = {
            "model": model,
            "scenario": scenario,
            "role": group[0].role,
            "runs": len(group),
            "quality_mean": round(statistics.fmean(t.quality for t in group), 4),
            "quality_min": round(min(t.quality for t in group), 4),
            "cost_mean": round(statistics.fmean(costs), 6) if costs else None,
            "latency_mean": round(statistics.fmean(t.latency_s for t in group), 2),
            "errors": [t.error for t in group if t.error],
            # A row where something else answered is not evidence about
            # `model`. Surfaced rather than silently averaged in.
            "misattributed": [
                t.actual_model for t in group
                if t.actual_model and t.actual_model != t.model
            ],
        }

    models = {}
    for entry in pairs.values():
        m = models.setdefault(entry["model"], {"scenarios": {}, "quality": [], "cost": []})
        m["scenarios"][entry["scenario"]] = entry
        if entry.get("not_run"):
            continue
        m["quality"].append(entry["quality_mean"])
        if entry["cost_mean"] is not None:
            m["cost"].append(entry["cost_mean"])
    # Worst SINGLE run, not the worst scenario average. Averaging hides the
    # failure mode that actually matters for a trading session: a model that
    # alternates between excellent and unparseable looks fine on the mean and
    # silences a session every other day.
    worst_trial: dict[str, float] = {}
    for t in trials:
        worst_trial[t.model] = min(worst_trial.get(t.model, 1.0), t.quality)
    for model, m in models.items():
        if not m["quality"]:
            m.update(not_run=True, quality_mean=None, quality_worst=None,
                     quality_worst_run=None, cost_per_run_mean=None,
                     cost_total_usd=None, quality_per_dollar=None)
            del m["quality"], m["cost"]
            continue
        m["not_run"] = False
        m["quality_mean"] = round(statistics.fmean(m["quality"]), 4)
        m["quality_worst"] = round(min(m["quality"]), 4)
        m["quality_worst_run"] = round(worst_trial.get(model, 0.0), 4)
        # `m["cost"]` holds one PER-RUN MEAN per scenario, not per trial, so
        # summing it never produced a total. It was published as
        # `cost_total` anyway, and `quality_per_dollar` was derived from it
        # — which read correctly at one scenario x one repeat and was wrong
        # by a factor of `repeats` everywhere else. Found 2026-09-01 when a
        # 10-repeat run reported a $0.06 "total" for $0.69 of real spend.
        #
        # Both numbers are now published under names that say what they are.
        # quality_per_dollar stays keyed to the PER-RUN cost: the decision
        # it informs is "what does one session cost at this seat", which a
        # sweep total does not answer.
        m["cost_per_run_mean"] = (
            round(statistics.fmean(m["cost"]), 6) if m["cost"] else None
        )
        m["cost_total_usd"] = round(
            sum(t.cost_usd for t in trials
                if t.model == model and t.cost_usd is not None), 6
        ) or None
        if m["cost_per_run_mean"]:
            m["quality_per_dollar"] = round(
                m["quality_mean"] / m["cost_per_run_mean"], 2
            )
        else:
            m["quality_per_dollar"] = None
        del m["quality"], m["cost"]
    return {"pairs": pairs, "models": models}


def render_markdown(report: dict) -> str:
    models = report["models"]
    scen_keys = [s.key for s in SCENARIOS]
    lines = [
        "| model | " + " | ".join(scen_keys)
        + " | mean | worst run | $/sweep | quality/$ |",
        "|" + "---|" * (len(scen_keys) + 5),
    ]
    # Ordered by QUALITY first, cost second — deliberately not by
    # quality/$. Cost here spans three orders of magnitude while quality
    # spans one, so ranking on the ratio puts the cheapest model on top
    # almost regardless of how it scored, which is the exact reasoning error
    # this table exists to prevent. The policy is chosen by clearing a
    # quality bar and THEN taking the cheapest that clears it; quality/$ is
    # reported as a tiebreaker, not as the ranking.
    ordered = sorted(
        models.items(),
        key=lambda kv: (bool(kv[1].get("not_run")),
                        -(kv[1]["quality_mean"] or 0.0),
                        -(kv[1]["quality_worst"] or 0.0),
                        kv[1]["cost_per_run_mean"]
                        if kv[1]["cost_per_run_mean"] is not None else 1e9),
    )
    for model, m in ordered:
        cells = []
        for key in scen_keys:
            e = m["scenarios"].get(key)
            if e is None:
                cells.append("—")
            elif e.get("not_run"):
                cells.append("NOT RUN")
            else:
                cells.append(f"{e['quality_mean']:.2f}")
        if m.get("not_run"):
            lines.append(f"| `{model}` | " + " | ".join(cells)
                         + " | NOT RUN | NOT RUN | — | — |")
            continue
        cost = m["cost_per_run_mean"]
        lines.append(
            f"| `{model}` | " + " | ".join(cells)
            + f" | {m['quality_mean']:.2f} | {m['quality_worst_run']:.2f} | "
            + (f"${cost:.4f}" if cost is not None else "?")
            + " | "
            + (f"{m['quality_per_dollar']:,.0f}" if m["quality_per_dollar"] else "?")
            + " |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--models", nargs="*", default=None,
                    help="model ids to test (default: the curated candidate slate)")
    ap.add_argument("--scenario", action="append", choices=sorted(SCENARIOS_BY_KEY),
                    help="restrict to this scenario (repeatable)")
    ap.add_argument("--repeats", type=int, default=1,
                    help="runs per (model, scenario) — 2+ exposes variance")
    ap.add_argument("--from-onecli", action="store_true",
                    help="resolve gateway wiring from the live OneCLI instance")
    ap.add_argument("--out", default="ops/model_policy/results/latest.json")
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument(
        "--report", nargs="+",
        help="render markdown from existing results file(s) and exit; several "
             "files are merged at the trial level, so a scenario added after "
             "a sweep can be run on its own and folded in rather than paying "
             "for the whole sweep again",
    )
    ap.add_argument("--merge-out",
                    help="with --report: also write the merged report here")
    ap.add_argument(
        "--budget-usd", type=positive_budget, default=None,
        help="REQUIRED for any run that calls a model: this benchmark's own "
             "session spend cap in USD. No default -- the person running it "
             "sets it. Independent of the live desk's caps, which are unchanged.",
    )
    ap.add_argument(
        "--allow-partial", action="store_true",
        help="start even when the pre-run estimate exceeds --budget-usd (or "
             "cannot be computed); trials past the budget are recorded NOT RUN",
    )
    args = ap.parse_args(argv)

    if args.report:
        # Later files SUPERSEDE earlier ones for any (model, scenario) pair
        # they cover. That is what makes a targeted re-run reproducible: when
        # a harness bug is found that distorted only some pairs — as happened
        # with the benchmark-local `max_tokens` cap that scored two deepseek
        # rows 0.00 for being cut off mid-JSON — the fix is to re-run those
        # pairs and list the correction file last, not to silently average a
        # known-bad trial in with its replacement.
        by_pair: dict[tuple[str, str], list[Trial]] = {}
        order: list[tuple[str, str]] = []
        sources = []
        superseded: list[str] = []
        for path in args.report:
            doc = json.loads(Path(path).read_text())
            sources.append({"path": path,
                            "generated_at": doc.get("generated_at"),
                            "prompts": doc.get("prompts")})
            seen_here: set[tuple[str, str]] = set()
            for raw in doc["trials"]:
                trial = Trial(**raw)
                key = (trial.model, trial.scenario)
                if key not in seen_here and key in by_pair:
                    superseded.append(f"{trial.model}|{trial.scenario} <- {path}")
                    by_pair[key] = []
                seen_here.add(key)
                if key not in by_pair:
                    by_pair[key] = []
                    order.append(key)
                by_pair[key].append(trial)
        merged: list[Trial] = [t for key in order for t in by_pair[key]]
        if superseded:
            print(f"superseded {len(superseded)} pair(s) by a later file:",
                  file=sys.stderr)
            for s in superseded:
                print(f"  {s}", file=sys.stderr)
        # Merging trials graded against DIFFERENT prompts is the silent
        # staleness this fingerprint exists to catch. Say so loudly rather
        # than averaging across a prompt rewrite.
        prompt_conflicts = _prompt_conflicts(sources)
        if prompt_conflicts:
            print("PROMPT MISMATCH — these files were graded against "
                  "different prompts. Their scores are NOT comparable:",
                  file=sys.stderr)
            for c in prompt_conflicts:
                print(f"  {c}", file=sys.stderr)
        missing = [s_["path"] for s_ in sources if not s_.get("prompts")]
        if missing:
            print("NO PROMPT FINGERPRINT recorded in "
                  f"{len(missing)} file(s) — predates this field, so prompt "
                  "drift CANNOT be ruled out:", file=sys.stderr)
            for m_ in missing:
                print(f"  {m_}", file=sys.stderr)

        report = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "merged_from": sources,
            "superseded_pairs": superseded,
            "prompt_conflicts": prompt_conflicts,
            "sources_without_prompt_fingerprint": missing,
            "baseline": BASELINE_MODEL,
            "scenarios": {s.key: {"role": s.role, "description": s.description}
                          for s in SCENARIOS},
            "trials": [asdict(t) for t in merged],
            "aggregate": aggregate(merged),
        }
        if args.merge_out:
            out = Path(args.merge_out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2))
            print(f"wrote {out}\n", file=sys.stderr)
        print(render_markdown(report["aggregate"]))
        return 0

    if args.budget_usd is None:
        ap.error("--budget-usd is required for any run that calls a model "
                 "(not needed with --report)")

    if load_env_if_keys_missing():
        print("loaded .env (placeholder keys; values not shown)", file=sys.stderr)

    if args.from_onecli:
        source = wire_from_onecli()
    elif os.environ.get("HTTPS_PROXY"):
        source = "environment"
    else:
        print("No gateway wiring: pass --from-onecli or run with HTTPS_PROXY set.",
              file=sys.stderr)
        return 2

    models = args.models or DEFAULT_CANDIDATES
    # DEFAULT_SCENARIOS, not SCENARIOS: the production-scale tech batch is
    # opt-in, because running it against every candidate costs far more
    # than it informs. Name it explicitly for the finalists.
    scenarios = (
        [SCENARIOS_BY_KEY[k] for k in args.scenario]
        if args.scenario else DEFAULT_SCENARIOS
    )
    total = len(models) * len(scenarios) * args.repeats

    pricing = openrouter_pricing()
    unpriced = [m for m in models if m not in pricing]
    if unpriced:
        print(f"WARNING: no catalog price for {unpriced} — their cost will be null.",
              file=sys.stderr)

    # Pre-run plan, before ANY paid call.
    tokens = token_assumptions(scenarios)
    estimates = estimate_costs(models, scenarios, args.repeats, pricing, tokens)
    print(f"PLAN: {len(models)} model(s) x {len(scenarios)} scenario(s) x "
          f"{args.repeats} repeat(s) = {total} trial(s); budget ${args.budget_usd:.2f}",
          file=sys.stderr)
    for key, tok in tokens.items():
        print(f"  token assumption {key}: in={tok['input']} out={tok['output']} "
              f"per trial ({tok['source']})", file=sys.stderr)
    for model, est in estimates.items():
        print(f"  est {model}: " + (f"${est:.4f}" if est is not None else "? (unpriced)"),
              file=sys.stderr)
    known = [e for e in estimates.values() if e is not None]
    estimate_total = sum(known)
    estimate_complete = len(known) == len(estimates)
    print(f"  est total: ${estimate_total:.4f}"
          + ("" if estimate_complete else " + unpriced model(s)"), file=sys.stderr)
    if (estimate_total > args.budget_usd or not estimate_complete) and not args.allow_partial:
        print("REFUSING TO START: the estimate "
              + ("exceeds" if estimate_complete else "cannot be completed within")
              + f" --budget-usd ${args.budget_usd:.2f}. Raise the budget, narrow "
              "the run, or pass --allow-partial to run until the budget is spent.",
              file=sys.stderr)
        return 2

    # The breaker uses the same in-process pinned table. OpenRouter's live
    # catalog is authoritative for benchmark-only candidates and was fetched
    # before any completion request.
    from src.cost_table import PRICING
    PRICING.update(pricing)
    from src.config import load_config
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    app_config = load_config(config_path)
    cost_circuit = build_benchmark_circuit(
        app_config, budget_usd=args.budget_usd, planned_trials=total,
        run_id=f"benchmark-{int(time.time())}",
    )
    cost_circuit.require_paid_analysis("benchmark_start")

    planned_spend = (
        min(args.budget_usd, estimate_total) if estimate_complete else args.budget_usd
    )
    problem = daily_cap_problem(
        cost_circuit.status(), app_config.llm_cost_circuit.daily_cost_limit_usd,
        planned_spend,
    )
    if problem:
        print(f"REFUSING TO START: {problem}", file=sys.stderr)
        return 2

    def _budget_state() -> tuple[float, bool]:
        st = cost_circuit.status()
        return float(st.get("current_session_cost_usd") or 0.0), bool(st.get("suspended"))

    trials, stopped_on_budget = run_sweep(
        models, scenarios, args.repeats, budget_usd=args.budget_usd,
        run_one=lambda s, m: run_trial(s, m, pricing, cost_circuit=cost_circuit),
        budget_state=_budget_state,
    )

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wiring_source": source,
        "baseline": BASELINE_MODEL,
        "repeats": args.repeats,
        "budget_usd": args.budget_usd,
        "benchmark_circuit": {
            "session_cost_limit_usd": cost_circuit.config.session_cost_limit_usd,
            "max_calls_per_session": cost_circuit.config.max_calls_per_session,
        },
        "estimate_usd": {"per_model": estimates, "tokens": tokens},
        "stopped_on_budget": stopped_on_budget,
        "scenarios": {s.key: {"role": s.role, "description": s.description}
                      for s in scenarios},
        "prompts": prompt_fingerprints(scenarios),
        "pricing_used": {m: pricing.get(m) for m in models},
        "trials": [asdict(t) for t in trials],
        "aggregate": aggregate(trials),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"\nwrote {out}\n", file=sys.stderr)
    print(render_markdown(report["aggregate"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
